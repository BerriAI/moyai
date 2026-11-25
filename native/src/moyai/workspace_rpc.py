"""One bounded sandboxd process request in the isolated transport environment."""
from __future__ import annotations

import json
import os
import ssl
import sys
from datetime import timedelta
from importlib import import_module
from pathlib import Path


def execute(request: dict) -> dict:
    # Resolve these exclusively inside the isolated environment.
    grpc = import_module("grpc")
    stubs = import_module("k8s_agent_sandbox.commands._process_stubs")
    pb = stubs.process_pb2

    limit = request["limit"]
    config = pb.ProcessConfig(command=request["argv"], env_vars=request["env"])
    operation = pb.ExecuteWithLimitsRequest(
        config=config, stdout_limit_bytes=limit, stderr_limit_bytes=limit,
    )
    timeout = request["timeout"]
    if timeout is not None:
        operation.time_limit.FromTimedelta(timedelta(seconds=timeout))
    metadata = (
        ("authorization", "Bearer " + request["router_token"]),
        ("x-sandboxd-token", request["runtime_token"]),
        ("x-sandbox-id", request["name"]), ("x-sandbox-uid", request["uid"]),
        ("x-sandbox-namespace", request["namespace"]), ("x-sandbox-port", "9090"),
    )
    options = (
        ("grpc.enable_retries", 0), ("grpc.enable_http_proxy", 0),
        ("grpc.max_receive_message_length", 2 * limit + 65536),
        ("grpc.primary_user_agent", "workspace-commands"),
    )
    trust = os.environ.get("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH") or ssl.get_default_verify_paths().cafile
    credentials = grpc.ssl_channel_credentials(Path(trust).read_bytes() if trust else None)
    with grpc.secure_channel(request["target"], credentials, options=options) as channel:
        stub = stubs.process_pb2_grpc.ProcessServiceStub(channel)
        if os.getppid() != request["worker_pid"]:
            return {"outcome": "unknown"}
        try:
            pending = stub.ExecuteWithLimits.future(
                operation, metadata=metadata, timeout=(timeout + 15 if timeout is not None else None),
            )
            # A worker can be killed without unwinding its coroutine. Cancel its
            # outstanding request if this helper has been orphaned.
            while True:
                try:
                    response = pending.result(timeout=1)
                    break
                except grpc.FutureTimeoutError:
                    if os.getppid() != request["worker_pid"]:
                        pending.cancel()
                        return {"outcome": "unknown"}
        except grpc.RpcError as error:
            if error.code() in {grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED}:
                return {"outcome": "refused"}
            if error.code() in {grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.UNIMPLEMENTED}:
                return {"outcome": "invalid"}
            if error.code() == grpc.StatusCode.NOT_FOUND and (error.details() or "").startswith(
                "failed to execute command: command or path not found:"
            ):
                return {"outcome": "exited", "exit_code": 127, "stdout": "", "stderr": "Command not found"}
            return {"outcome": "unknown"}

    if response.termination_reason == pb.EXECUTE_TERMINATION_REASON_TIME_LIMIT_EXCEEDED:
        outcome = "timeout"
    elif response.termination_reason != pb.EXECUTE_TERMINATION_REASON_EXITED:
        return {"outcome": "unknown"}
    elif response.stdout_truncated or response.stderr_truncated:
        outcome = "truncated"
    else:
        outcome = "exited"
    return {
        "outcome": outcome, "exit_code": response.exit_code,
        "stdout": response.stdout.decode("utf-8", "replace"), "stderr": response.stderr.decode("utf-8", "replace"),
    }


if __name__ == "__main__":
    json.dump(execute(json.load(sys.stdin)), sys.stdout)
