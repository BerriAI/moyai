from __future__ import annotations

import json
import os
import shlex
import traceback

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories.agent import Agent as TrajectoryAgent
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.trajectory import Trajectory

from moyai_harbor.turn_runner import run_moyai_turn
from moyai_harbor.workspace import HarborWorkspace


class MoyaiAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "moyai-eval"

    def version(self) -> str:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        log_dir = self.environment_logs_dir
        await environment.exec(f"mkdir -p {shlex.quote(str(log_dir))}", cwd="/workspace")
        log_file = log_dir / "stub_requests.jsonl"
        server_log = log_dir / "stub_server.log"
        hosts = await environment.exec(
            "grep -q '^127\\.0\\.0\\.1[[:space:]].*api\\.deepseek\\.com' /etc/hosts "
            "|| printf '\\n127.0.0.1 api.deepseek.com\\n' >> /etc/hosts",
            cwd="/workspace",
        )
        if hosts.return_code != 0:
            raise RuntimeError("Could not route the provider host to the local stub.")
        route = await environment.exec(
            "getent hosts api.deepseek.com | awk 'NR==1 {print $1}'",
            cwd="/workspace",
        )
        if route.return_code != 0 or route.stdout.strip() != "127.0.0.1":
            raise RuntimeError("The provider hostname does not resolve exclusively to the local stub.")
        command = (
            "nohup python3 /eval/stub.py "
            f"--log-path {shlex.quote(str(log_file))} "
            "--response-path /eval/stub_response.json "
            "--cert /usr/local/share/ca-certificates/api.deepseek.com.crt "
            "--key /etc/ssl/private/api.deepseek.com.key "
            f"> {shlex.quote(str(server_log))} 2>&1 </dev/null &"
        )
        launch = await environment.exec(command, cwd="/workspace", timeout_sec=10)
        for _ in range(20):
            health = await environment.exec(
                "curl --noproxy '*' -fsS https://api.deepseek.com/healthz",
                cwd="/workspace",
                timeout_sec=5,
            )
            if health.return_code == 0:
                return
        diagnostics = await environment.exec(
            f"cat {shlex.quote(str(server_log))}; "
            "printf '\\nresolved='; getent hosts api.deepseek.com | awk 'NR==1 {print $1}'",
            cwd="/workspace",
        )
        detail = (
            f"launch={launch.return_code} {launch.stderr or launch.stdout or ''}\n"
            f"health={health.return_code} {health.stderr or health.stdout or ''}\n"
            f"{diagnostics.stdout}"
        )[-2000:]
        raise RuntimeError(f"The Harbor provider stub did not become ready: {detail}")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        thread_path = self.logs_dir / "thread.json"
        credentials_path = self.logs_dir / "credentials.json"
        case_id_path = self.logs_dir / "case_id.json"
        try:
            await environment.download_file("/eval/thread.json", thread_path)
            await environment.download_file("/eval/credentials.json", credentials_path)
            await environment.download_file("/eval/case_id.json", case_id_path)
            thread = json.loads(thread_path.read_text())
            credentials = json.loads(credentials_path.read_text())
            case_id = json.loads(case_id_path.read_text())["case_id"]
            prior = thread[:-1] if thread and thread[-1]["text"] == instruction else thread
            case = {"id": case_id, "prompt": instruction, "thread": prior}
            result = await run_moyai_turn(
                case,
                thread=thread,
                credentials=credentials,
                workspace=HarborWorkspace(environment),
                agent_model=os.environ.get("MOYAI_EVAL_AGENT_MODEL", self.model_name or ""),
                trial=1,
            )
        except Exception as error:  # noqa: BLE001 - every Harbor trial must produce a failure record
            message = str(error)
            stack = traceback.format_exc()
            for key in ("LITELLM_API_KEY", "LITELLM_GATEWAY_ADMIN_KEY"):
                if value := os.getenv(key):
                    message = message.replace(value, "[redacted]")
                    stack = stack.replace(value, "[redacted]")
            result = {
                "case": locals().get("case_id", "unknown"),
                "slack_thread_input": locals().get("thread", []),
                "prompt_received": "",
                "posted_reply": "",
                "tool_trace": [],
                "tokens_cost": {},
                "execution_error": f"{type(error).__name__}: {message}"[:4000],
                "execution_traceback": stack[-12000:],
            }
        requests_file = self.environment_logs_dir / "stub_requests.jsonl"
        requests_result = await environment.exec(
            f"cat {shlex.quote(str(requests_file))} 2>/dev/null || true",
            cwd="/workspace",
        )
        request_lines = (requests_result.stdout or "").splitlines()
        try:
            result["stub_requests"] = [json.loads(line) for line in request_lines if line]
        except json.JSONDecodeError as error:
            result["stub_requests"] = []
            result["execution_error"] = f"invalid stub request log: {error}"
        result["agent_model"] = os.environ.get("MOYAI_EVAL_AGENT_MODEL", self.model_name or "")
        result["harbor_session_id"] = self.session_id
        result["harbor_context_id"] = str(self.context_id or "")
        thread_log = self.logs_dir / "slack_thread_input.json"
        prompt_log = self.logs_dir / "prompt_received.txt"
        reply_log = self.logs_dir / "posted_reply.txt"
        trace_log = self.logs_dir / "tool_trace.json"
        thread_log.write_text(json.dumps(
            result.get("slack_thread_input", []), ensure_ascii=False, indent=2,
        ) + "\n")
        prompt_log.write_text(result.get("prompt_received", ""))
        reply_log.write_text(result.get("posted_reply", ""))
        trace_log.write_text(json.dumps(
            result.get("tool_trace", []), ensure_ascii=False, indent=2,
        ) + "\n")
        trajectory = Trajectory(
            schema_version="ATIF-v1.8",
            session_id=self.session_id,
            agent=TrajectoryAgent(
                name=self.name(),
                version=self.version(),
                model_name=result["agent_model"],
            ),
            steps=[
                Step(step_id=1, source="user", message=instruction),
                Step(
                    step_id=2,
                    source="agent",
                    model_name=result["agent_model"],
                    message=result.get("posted_reply", ""),
                    extra={
                        "slack_thread_input": result.get("slack_thread_input", []),
                        "prompt_received": result.get("prompt_received", ""),
                        "tool_trace": result.get("tool_trace", []),
                    },
                ),
            ],
        )
        (self.logs_dir / "trajectory.json").write_text(
            json.dumps(trajectory.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        result_path = self.logs_dir / "moyai_result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        await environment.upload_file(result_path, "/eval/moyai_result.json")
        await environment.upload_file(
            result_path,
            str(self.environment_logs_dir / "moyai_result.json"),
        )
        usage = result.get("tokens_cost", {})
        context.n_input_tokens = usage.get("input_tokens")
        context.n_output_tokens = usage.get("output_tokens")
        # Harbor's scalar total cannot represent incomplete receipt coverage.
        context.cost_usd = usage.get("cost_usd") if usage.get("cost_status") == "complete" else None
        context.metadata = {
            "agent_model": result["agent_model"],
            "execution_error": result.get("execution_error", ""),
            "cost_status": usage.get("cost_status", "unknown"),
        }
