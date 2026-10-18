"""Loopback-only interactive chat using the real Moyai evaluation runtime."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

EVALS = Path(__file__).resolve().parents[1]
REPOSITORY = EVALS.parent


@dataclass(frozen=True)
class ChatConfig:
    model: str
    data_dir: Path
    fixture_dir: Path = EVALS / "tasks/deepseek_support_live"


def redacted(value: str) -> str:
    for key in ("LITELLM_API_KEY", "LITELLM_GATEWAY_ADMIN_KEY", "MOYAI_TEST_POSTGRES"):
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


async def docker(*arguments: str, timeout: int = 120) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker", *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(redacted(stderr.decode(errors="replace")[-3000:]))
    return stdout.decode().strip()


@asynccontextmanager
async def local_runtime(config: ChatConfig) -> AsyncIterator[Any]:
    # Harbor is an eval-only overlay, never a production dependency.
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    from moyai_harbor.agent import MoyaiAgent
    from moyai_harbor.turn_runner import open_moyai_runtime
    from moyai_harbor.workspace import HarborWorkspace

    identity = uuid4().hex[:12]
    root = config.data_dir / identity
    root.mkdir(parents=True, mode=0o700)
    paths = TrialPaths(root / "computer")
    paths.mkdir()
    database_container = f"moyai-chat-db-{identity}"
    previous_database = os.environ.get("MOYAI_TEST_POSTGRES")
    environment = DockerEnvironment(
        environment_dir=config.fixture_dir / "environment",
        environment_name="moyai-local-chat",
        session_id=f"moyai-local-chat-{identity}",
        trial_paths=paths,
        task_env_config=EnvironmentConfig(),
        # Use Docker file transfer, so Colima needs no host bind mounts.
        mounts=[],
    )
    try:
        await docker("info", "--format", "{{.ServerVersion}}", timeout=20)
        password = uuid4().hex
        await docker(
            "run", "--detach", "--rm", "--name", database_container,
            "--publish", "127.0.0.1::5432", "--env", f"POSTGRES_PASSWORD={password}",
            "postgres:18-alpine",
        )
        for _ in range(60):
            try:
                await docker("exec", database_container, "pg_isready", "-U", "postgres", timeout=5)
                break
            except RuntimeError:
                await asyncio.sleep(1)
        else:
            raise RuntimeError("The local test database did not become ready.")
        port = (await docker("port", database_container, "5432/tcp")).splitlines()[0].rsplit(":", 1)[-1]
        if not port.isdigit():
            raise RuntimeError("Docker did not supply a local database port.")
        os.environ["MOYAI_TEST_POSTGRES"] = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
        await asyncio.wait_for(environment.start(force_build=False), 600)
        await MoyaiAgent(logs_dir=paths.agent_dir, model_name=config.model).setup(environment)
        credentials = json.loads((config.fixture_dir / "environment/eval/credentials.json").read_text())
        async with open_moyai_runtime(
            workspace=HarborWorkspace(environment), credentials=credentials,
            agent_model=config.model, root_dir=root / "runtime",
        ) as backend:
            yield backend
    finally:
        with contextlib.suppress(Exception):
            await environment.stop(delete=True)
        with contextlib.suppress(Exception):
            await docker("rm", "--force", database_container, timeout=30)
        if previous_database is None:
            os.environ.pop("MOYAI_TEST_POSTGRES", None)
        else:
            os.environ["MOYAI_TEST_POSTGRES"] = previous_database


@dataclass
class Chat:
    id: str
    thread_ts: str
    title: str = "New chat"
    status: str = "idle"
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    requests: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in ("id", "title", "status", "messages", "error")}


class SendMessage(BaseModel):
    message: str = Field(default="", max_length=20000)
    request_id: str = Field(min_length=1, max_length=128)
    answers: dict[str, str] | None = None


class LocalChat:
    def __init__(self, config: ChatConfig, *, backend_factory: Callable = local_runtime):
        self.config = config
        self.backend_factory = backend_factory
        self.backend: Any = None
        self.chats: dict[str, Chat] = {}
        self.phase = "starting"
        self.error: str | None = None
        self.active: asyncio.Task | None = None
        self.shutdown = asyncio.Event()
        self.counter = 0
        self.last_stamp = 0

    def stamp(self) -> str:
        self.last_stamp = max(time.time_ns() // 1000, self.last_stamp + 1)
        return f"{self.last_stamp // 1_000_000}.{self.last_stamp % 1_000_000:06d}"

    async def serve(self) -> None:
        try:
            async with self.backend_factory(self.config) as backend:
                self.backend = backend
                self.phase = "ready"
                await self.shutdown.wait()
                if self.active and not self.active.done():
                    self.active.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.active
        except Exception as error:  # noqa: BLE001 - display startup failures in the local UI
            self.phase = "error"
            self.error = redacted(str(error))[:3000]
        finally:
            self.backend = None

    def new_chat(self) -> Chat:
        chat = Chat(id=uuid4().hex, thread_ts=self.stamp())
        self.chats[chat.id] = chat
        return chat

    def get_chat(self, identity: str) -> Chat:
        if identity not in self.chats:
            raise HTTPException(404, "This chat is no longer available. Start a new chat.")
        return self.chats[identity]

    def send(self, identity: str, body: SendMessage) -> Chat:
        chat = self.get_chat(identity)
        fingerprint = body.model_dump_json(exclude={"request_id"})
        if body.request_id in chat.requests:
            if chat.requests[body.request_id] != fingerprint:
                raise HTTPException(409, "That request was already used for a different message.")
            return chat
        if self.phase != "ready" or self.backend is None:
            raise HTTPException(503, self.error or "The local agent is still starting.")
        if self.active and not self.active.done():
            raise HTTPException(409, "Moyai is working. Wait for the current reply.")
        text = body.message.strip()
        if body.answers is None and text.lower().lstrip("/") in {"stop", "sleep"}:
            raise HTTPException(
                422, "Stop and sleep are unavailable in the local playground. Wait for the current reply, then use New chat.",
            )
        prompt = text
        if body.answers is not None:
            if chat.status != "waiting":
                raise HTTPException(409, "There is no pending question in this chat.")
            if not body.answers or len(body.answers) > 10 or any(
                not key.strip() or not value.strip() or len(key) > 200 or len(value) > 10000
                for key, value in body.answers.items()
            ):
                raise HTTPException(422, "Enter an answer for each question.")
            prompt = "/answer " + json.dumps(body.answers)
            text = "\n".join(f"{key}: {value}" for key, value in body.answers.items())
        elif chat.status == "waiting" and not prompt.startswith("/"):
            prompt = "/answer " + prompt
        if not text:
            raise HTTPException(422, "Enter a message first.")
        chat.requests[body.request_id] = fingerprint
        if not chat.messages:
            chat.title = text[:60]
        prior = [
            {"role": "human" if item["role"] == "user" else "you",
             "display_name": "Local tester" if item["role"] == "user" else "Moyai",
             "text": item["text"], "files": [], "ts": item["ts"]}
            for item in chat.messages if item.get("text")
        ]
        current = chat.thread_ts if not chat.messages else self.stamp()
        chat.messages.append({"id": uuid4().hex, "role": "user", "text": text, "status": "complete", "ts": current})
        assistant = {"id": uuid4().hex, "role": "assistant", "text": "", "status": "running", "ts": self.stamp()}
        chat.messages.append(assistant)
        chat.status, chat.error = "running", None
        self.counter += 1
        self.active = asyncio.create_task(self.reply(chat, assistant, prompt, prior, self.counter, current))
        return chat

    async def execute(self, case: dict, **kwargs: Any) -> dict:
        from moyai_harbor.turn_runner import execute_turn
        return await execute_turn(case, **kwargs)

    async def reply(self, chat: Chat, message: dict, prompt: str, prior: list, trial: int, current: str) -> None:
        started = time.monotonic()
        try:
            result = await self.execute(
                {"id": chat.id, "prompt": prompt, "thread": prior}, trial=trial,
                thread=[*prior, {"role": "human", "display_name": "Local tester", "text": prompt, "files": [], "ts": current}],
                service=self.backend.service, runtime=self.backend.runtime, temporal=self.backend.temporal,
                thread_ts=chat.thread_ts, current=current, event_id=f"local-{uuid4().hex}",
            )
            questions = [question for group in result.get("questions", []) for question in group["questions"]]
            waiting = bool(questions) or result.get("status") == "waiting"
            error = result.get("error") or result.get("execution_error")
            text = re.sub(r"\s*<https://moyai\.invalid/[^>]+>", "", result.get("posted_reply", "")).strip()
            if not text and questions:
                text = "\n\n".join(question["question"] for question in questions)
            status = "error" if error else "waiting" if waiting else "complete"
            message.update({
                "text": redacted(text), "status": status,
                "tools": json.loads(redacted(json.dumps(result.get("tool_trace", [])))),
                "questions": questions, "error": redacted(error) if error else None,
                "elapsed_seconds": round(time.monotonic() - started, 1),
            })
            chat.status = "idle" if status == "complete" else status
            chat.error = message["error"]
            destination = self.config.data_dir / "transcripts"
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            (destination / f"{chat.id}.json").write_text(json.dumps(chat.public(), indent=2) + "\n")
        except asyncio.CancelledError:
            chat.status = message["status"] = "error"
            chat.error = message["error"] = "The local server stopped during this reply."
            raise
        except Exception as error:  # noqa: BLE001 - isolate failed turns and preserve the chat
            chat.status = message["status"] = "error"
            detail = (
                "Moyai took longer than five minutes to reply. Start a new chat to try again."
                if isinstance(error, TimeoutError) else str(error) or type(error).__name__
            )
            chat.error = message["error"] = redacted(detail)[:3000]
            message["elapsed_seconds"] = round(time.monotonic() - started, 1)


def create_app(controller: LocalChat) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(controller.serve())
        try:
            yield
        finally:
            controller.shutdown.set()
            try:
                await asyncio.wait_for(task, 30)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if request.headers.get("x-moyai-local") != "1" or (origin and origin != expected):
                return JSONResponse({"detail": "Use the local Moyai chat page to send messages."}, status_code=403)
            try:
                content_length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length header."}, status_code=400)
            if content_length < 0:
                return JSONResponse({"detail": "Invalid Content-Length header."}, status_code=400)
            if content_length > 100000:
                return JSONResponse({"detail": "This message is too large."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/api/status")
    async def status():
        return {"ready": controller.phase == "ready", "phase": controller.phase,
                "model": controller.config.model, "context": "DeepSeek support fixture", "error": controller.error}

    @app.get("/api/chats")
    async def chats():
        return {"chats": [chat.public() for chat in reversed(list(controller.chats.values()))]}

    @app.post("/api/chats")
    async def new_chat():
        return controller.new_chat().public()

    @app.get("/api/chats/{identity}")
    async def get_chat(identity: str):
        return controller.get_chat(identity).public()

    @app.post("/api/chats/{identity}/messages", status_code=202)
    async def send(identity: str, body: SendMessage):
        return controller.send(identity, body).public()

    dist = EVALS / "web/dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    async def index():
        if not (dist / "index.html").is_file():
            raise HTTPException(503, "Build the test UI with npm run build:eval-chat, then restart this server.")
        return FileResponse(dist / "index.html")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--env-file", type=Path, default=REPOSITORY / ".env")
    parser.add_argument("--data-dir", type=Path, default=REPOSITORY / ".data/local-agent-chat")
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)
    model = os.getenv("MOYAI_EVAL_AGENT_MODEL") or os.getenv("AGENT_MODEL", "")
    if not model or not os.getenv("LITELLM_API_BASE") or not os.getenv("LITELLM_API_KEY"):
        parser.error("Set LITELLM_API_BASE, LITELLM_API_KEY and MOYAI_EVAL_AGENT_MODEL in your environment file.")
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = ChatConfig(model=model, data_dir=args.data_dir.resolve())
    print(f"Moyai local test chat: http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(create_app(LocalChat(config)), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
