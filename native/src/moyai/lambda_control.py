"""Trusted AWS control plane and authenticated guest transport.

Only scoped presigned object URLs cross into the guest. Every checkpoint is a
verified, independent delta from its pinned image, never a chain of VM snapshots.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from contextlib import closing
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4
from weakref import WeakValueDictionary

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic_ai.workspaces import WorkspaceUnavailableError

from .settings import Settings

logger = logging.getLogger(__name__)
MAX_ARCHIVE = 4 * 1024**3
LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


class ObservationError(RuntimeError):
    """A transport failure is not proof that a submitted mutation failed."""


class LambdaControl:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Credential resolution is deferred until the first operation.
        self._session: Any = None

    async def aws(self, service: str, method: str, **kwargs: Any) -> Any:
        def call():
            if self._session is None:
                self._session = boto3.Session(
                    profile_name=self.settings.lambda_profile or None, region_name=self.settings.lambda_region,
                )
            config = Config(
                connect_timeout=10, read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 3},
                **({"signature_version": "s3v4"} if service == "s3" else {}),
            )
            with closing(self._session.client(service, config=config)) as client:
                result = getattr(client, method)(**kwargs)
                if service == "s3" and method == "get_object":
                    with closing(result["Body"]) as body:
                        raw = body.read(1024 * 1024 + 1)
                        if len(raw) > 1024 * 1024:
                            raise ValueError("Lambda manifest exceeds limit")
                        return json.loads(raw)
                return result
        try:
            return await asyncio.to_thread(call)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code in {"NoSuchKey", "404", "ResourceNotFoundException"}:
                raise WorkspaceUnavailableError("Lambda resource is missing") from None
            # Never persist AWS response bodies or capabilities in Temporal history.
            raise RuntimeError(f"AWS {method} failed: {code}") from None

    def key(self, suffix: str) -> str:
        return f"{self.settings.lambda_checkpoint_prefix}/{suffix}"

    async def read(self, key: str) -> dict[str, Any]:
        return await self.aws("s3", "get_object", Bucket=self.settings.lambda_checkpoint_bucket, Key=key)

    async def write(self, key: str, value: dict[str, Any], **kwargs: Any) -> Any:
        return await self.aws(
            "s3", "put_object", Bucket=self.settings.lambda_checkpoint_bucket, Key=key,
            Body=json.dumps(value).encode(), ContentType="application/json", ServerSideEncryption="AES256", **kwargs,
        )

    async def url(self, method: str, key: str, **kwargs: Any) -> str:
        return await self.aws(
            "s3", "generate_presigned_url", ClientMethod=method, ExpiresIn=900,
            Params={"Bucket": self.settings.lambda_checkpoint_bucket, "Key": key, **kwargs},
        )

    async def manifest(self, identity: str) -> dict[str, Any]:
        kind, region, bucket, key = identity.split(":", 3)
        if (
            kind != "lambda" or region != self.settings.lambda_region
            or bucket != self.settings.lambda_checkpoint_bucket
            or not key.startswith(self.key("checkpoints/")) or not key.endswith(".json") or ".." in key
        ):
            raise ValueError("Checkpoint belongs to another installation")
        value = await self.read(key)
        if (
            value.get("version") != 1 or not value.get("image") or not value.get("image_version")
            or not 0 < value.get("size", 0) <= MAX_ARCHIVE
            or len(value.get("sha256", "")) != 64 or value.get("key") != key[:-5] + ".tar.gz"
        ):
            raise ValueError("Invalid Lambda checkpoint")
        return value

    async def get(self, identity: str) -> LambdaVM:
        kind, region, vm_id = identity.split(":")
        if kind != "lambda" or region != self.settings.lambda_region:
            raise ValueError("Workspace belongs to another AWS region")
        vm = await self.aws("lambda-microvms", "get_microvm", microvmIdentifier=vm_id)
        return LambdaVM(self, vm)

    async def create(
        self, name: str, *, checkpoint: str | None = None, created: Callable[[LambdaVM], None] | None = None,
    ) -> LambdaVM:
        """S3 plus AWS idempotency resolve lost launch replies across workers."""
        started = time.perf_counter()
        manifest = await self.manifest(checkpoint) if checkpoint else None
        region = self.settings.lambda_region
        registry = self.key("machines/" + hashlib.sha256(name.encode()).hexdigest() + ".json")
        request: dict[str, Any] = {
            "clientToken": uuid4().hex,
            "imageIdentifier": manifest["image"] if manifest else self.settings.lambda_image,
            "imageVersion": manifest["image_version"] if manifest else self.settings.lambda_image_version,
            "maximumDurationInSeconds": min(self.settings.computer_lifetime, 28800),
            "idlePolicy": {
                "autoResumeEnabled": False, "maxIdleDurationSeconds": 28800, "suspendedDurationSeconds": 28800,
            },
            "ingressNetworkConnectors": [
                f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:ALL_INGRESS",
            ],
            "egressNetworkConnectors": [self.settings.lambda_egress_connector or
                f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"],
        }
        if self.settings.lambda_execution_role_arn:
            request["executionRoleArn"] = self.settings.lambda_execution_role_arn
        record = {"created_at": time.time(), "checkpoint": checkpoint, "request": request}
        try:
            await self.write(registry, record, IfNoneMatch="*")
        except RuntimeError as error:
            if not str(error).endswith(("PreconditionFailed", "ConditionalRequestConflict")):
                raise
            record = await self.read(registry)
            if record["checkpoint"] != checkpoint:
                raise ValueError("Launch name has conflicting checkpoint")
        if record.get("vm_id"):
            vm = await self.get(f"lambda:{region}:{record['vm_id']}")
        else:
            if time.time() - record["created_at"] >= 3600:
                raise RuntimeError("Unconfirmed launch exceeded one hour; inspect its registry before retrying")
            result = await self.aws("lambda-microvms", "run_microvm", **record["request"])
            vm = LambdaVM(self, result)
            if created:
                created(vm)
            await self.write(registry, {**record, "vm_id": result["microvmId"]})
        if created:
            created(vm)
        # A stored terminated launch never silently creates a replacement.
        await vm.running()
        try:
            await vm.request("/bootstrap", {})
        except (ObservationError, httpx.TransportError):
            pass
        async with asyncio.timeout(180):
            while True:
                try:
                    if (await vm.request("/health", {})).get("bootstrapped"):
                        break
                except (ObservationError, httpx.TransportError):
                    pass
                await asyncio.sleep(.5)
        body: dict[str, Any] = {"id": record["request"]["clientToken"]}
        if manifest:
            body["restore"] = {**manifest, "url": await self.url("get_object", manifest["key"])}
        await vm.job("/initialize", body, timeout=self.settings.lambda_checkpoint_timeout)
        logger.info("Lambda VM setup completed: %s", json.dumps({
            "setup_seconds": time.perf_counter() - started, "restored": bool(checkpoint),
        }))
        return vm


class LambdaVM:
    def __init__(self, control: LambdaControl, vm: dict[str, Any]):
        self.control, self.vm = control, vm
        self.id = f"lambda:{control.settings.lambda_region}:{vm['microvmId']}"
        self.lock = LOCKS.setdefault(self.id, asyncio.Lock())
        self.auth_lock = asyncio.Lock()
        self.auth, self.auth_until = "", 0.0

    async def alive(self) -> bool:
        try:
            self.vm = await self.control.aws(
                "lambda-microvms", "get_microvm", microvmIdentifier=self.vm["microvmId"],
            )
        except WorkspaceUnavailableError:
            return False
        return self.vm["state"] not in {"TERMINATED", "TERMINATING"}

    async def running(self) -> None:
        async with asyncio.timeout(180):
            while True:
                if not await self.alive():
                    raise WorkspaceUnavailableError("Lambda workspace terminated")
                if self.vm["state"] == "RUNNING":
                    return
                if self.vm["state"] != "PENDING":
                    raise WorkspaceUnavailableError("Lambda workspace is not running")
                await asyncio.sleep(1)

    async def terminate(self) -> None:
        try:
            await self.control.aws("lambda-microvms", "terminate_microvm", microvmIdentifier=self.vm["microvmId"])
            async with asyncio.timeout(120):
                while True:
                    vm = await self.control.aws(
                        "lambda-microvms", "get_microvm", microvmIdentifier=self.vm["microvmId"],
                    )
                    if vm["state"] == "TERMINATED":
                        return
                    await asyncio.sleep(1)
        except WorkspaceUnavailableError:
            return

    async def request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with self.auth_lock:
            if time.time() >= self.auth_until:
                result = await self.control.aws(
                    "lambda-microvms", "create_microvm_auth_token", microvmIdentifier=self.vm["microvmId"],
                    expirationInMinutes=15, allowedPorts=[{"port": 80}],
                )
                self.auth = result["authToken"]["X-aws-proxy-auth"]
                self.auth_until = time.time() + 720
        endpoint = self.vm["endpoint"]
        url = urlsplit(endpoint if "://" in endpoint else "https://" + endpoint)
        if (
            url.scheme != "https" or url.username or url.password or url.port
            or not (url.hostname or "").endswith(
                f".lambda-microvm.{self.control.settings.lambda_region}.on.aws"
            ) or url.path not in {"", "/"} or url.query or url.fragment
        ):
            raise ValueError("Unexpected Lambda endpoint")
        observations = {"/health", "/job", "/command/read", "/checkpoint/finish"}
        attempts = 3 if path in observations else 1
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            for attempt in range(attempts):
                try:
                    async with client.stream(
                        "POST", "https://" + url.netloc + path, json=body,
                        headers={"X-aws-proxy-auth": self.auth, "X-aws-proxy-port": "80"},
                    ) as response:
                        if response.status_code in {429, 500, 502, 503, 504}:
                            raise ObservationError(f"Lambda endpoint HTTP {response.status_code}")
                        if response.status_code == 404:
                            raise FileNotFoundError("Lambda operation not found")
                        if response.status_code != 200:
                            raise RuntimeError(f"Lambda endpoint HTTP {response.status_code}")
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 2 * 1024 * 1024:
                                raise ValueError("Lambda response exceeds limit")
                        return json.loads(raw)
                except (ObservationError, httpx.TransportError):
                    if attempt + 1 == attempts:
                        raise
                    await asyncio.sleep(.5 * (attempt + 1))
        raise AssertionError("Unreachable")

    async def job(self, path: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        last_phase, last_report = None, 0.0
        async with asyncio.timeout(timeout):
            try:
                await self.request(path, body)
            except (ObservationError, httpx.TransportError):
                pass  # Poll the submitted identity; do not repeat a mutation.
            while True:
                try:
                    value = await self.request("/job", {"id": body["id"]})
                except (ObservationError, httpx.TransportError):
                    await asyncio.sleep(1)
                    continue
                progress = value.get("progress", {})
                phase = progress.get("phase") if isinstance(progress, dict) else None
                if (
                    path == "/checkpoint" and phase in {"inventory", "signatures", "archive", "checksum", "packed"}
                    and (phase != last_phase or time.monotonic() - last_report >= 10)
                ):
                    counts = {key: progress[key] for key in (
                        "elapsed_seconds", "processed", "entries", "files_hashed", "bytes_hashed",
                        "cache_hits", "changed_entries", "deleted_entries", "archive_bytes",
                    ) if isinstance(progress.get(key), (int, float))}
                    logger.info("Lambda checkpoint progress: %s", json.dumps({"phase": phase, **counts}))
                    last_phase, last_report = phase, time.monotonic()
                if value["state"] == "failed":
                    raise RuntimeError("Lambda guest operation failed: " + value.get("error", "unknown"))
                if value["state"] == "done":
                    return value["result"]
                await asyncio.sleep(1)

    async def checkpoint(self) -> tuple[str, dict[str, Any]]:
        async with self.lock:
            started = time.perf_counter()
            timings: dict[str, Any] = {}
            identity = uuid4().hex
            key = self.control.key("checkpoints/" + identity)
            timeout = self.control.settings.lambda_checkpoint_timeout
            try:
                async with asyncio.timeout(timeout):
                    meta = await self.job("/checkpoint", {"id": identity, "timeout": timeout}, timeout=timeout)
                    timings["prepare_seconds"] = time.perf_counter() - started
                    phase = time.perf_counter()
                    url = await self.control.url(
                        "put_object", key + ".tar.gz", ChecksumSHA256=meta["checksum"], ServerSideEncryption="AES256",
                    )
                    await self.job("/upload", {
                        "id": uuid4().hex, "checkpoint": identity, "url": url, "checksum": meta["checksum"],
                    }, timeout=timeout)
                    timings["upload_seconds"] = time.perf_counter() - phase
                    phase = time.perf_counter()
                    obj = await self.control.aws(
                        "s3", "head_object", Bucket=self.control.settings.lambda_checkpoint_bucket,
                        Key=key + ".tar.gz", ChecksumMode="ENABLED",
                    )
                    if obj["ContentLength"] != meta["size"] or obj.get("ChecksumSHA256") != meta["checksum"]:
                        raise RuntimeError("Lambda checkpoint verification failed")
                    timings["verification_seconds"] = time.perf_counter() - phase
                    phase = time.perf_counter()
                    await self.control.write(key + ".json", {
                        "version": 1, "key": key + ".tar.gz", "sha256": meta["sha256"], "size": meta["size"],
                        "image": self.vm["imageArn"], "image_version": self.vm["imageVersion"],
                        "guest_diagnostics": meta["diagnostics"],
                    }, IfNoneMatch="*")
                    timings["publication_seconds"] = time.perf_counter() - phase
            finally:
                await asyncio.shield(self.request("/checkpoint/finish", {"id": identity}))
            timings.update(total_seconds=time.perf_counter() - started, guest=meta["diagnostics"],
                           archive_bytes=meta["size"])
            logger.info("Lambda checkpoint completed: %s", json.dumps(timings))
            return ":".join((
                "lambda", self.control.settings.lambda_region, self.control.settings.lambda_checkpoint_bucket,
                key + ".json",
            )), timings
