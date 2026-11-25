"""Run inside a disposable worker with its normal projected credentials.

Pass a fresh UUID to create, keep the two printed references, then call verify
after replacing the worker and the first workspace pod. The cleanup command
only releases the claims for those two sessions.
"""

import argparse
import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

from pydantic_ai.workspaces import Workspace, WorkspaceRef, WorkspaceTimeoutError

from moyai.contracts import RunScope
from moyai.kubernetes_preflight import check_workspace
from moyai.kubernetes_workspace import KubernetesSandbox
from moyai.settings import Settings


async def main(args):
    settings = Settings(_env_file=None)
    settings.validate_kubernetes_workspace()
    await check_workspace(settings)
    provider = KubernetesSandbox(settings)
    scopes = [RunScope(f"{args.session}-{suffix}", "verification", "operator", "verification") for suffix in ("a", "b")]
    marker = "/workspace/recovery-probe.bin"
    contents = args.session.bytes + bytes(range(256))

    if args.action == "create":
        backends = [provider.get_workspace(SimpleNamespace(deps=scope), ref=None) for scope in scopes]
        await asyncio.gather(*(backend.working_dir() for backend in backends))
        assert backends[0].ref != backends[1].ref
        workspaces = [Workspace(backend) for backend in backends]
        await workspaces[0].write_bytes(marker, contents)
        assert (await backends[1].run(["test", "!", "-e", marker], timeout=10)).exit_code == 0
        await workspaces[1].write_bytes(marker, contents[::-1])
        try:
            await backends[0].run(["sleep", "10"], timeout=1)
        except WorkspaceTimeoutError:
            pass
        else:
            raise RuntimeError("The command exceeded its deadline")
        print(json.dumps([backend.ref.id for backend in backends]))
        return

    if len(args.refs) != 2:
        raise ValueError("Supply the two workspace IDs returned by create")
    refs = [WorkspaceRef(provider="kubernetes", id=value) for value in args.refs]
    backends = [provider.resume(scope, ref) for scope, ref in zip(scopes, refs, strict=True)]
    try:
        provider.resume(scopes[1], refs[0])
    except PermissionError:
        pass
    else:
        raise RuntimeError("Accepted another session's workspace")

    if args.action == "verify":
        values = await asyncio.gather(*(Workspace(backend).read_bytes(marker) for backend in backends))
        assert values == [contents, contents[::-1]]
        print("Both sessions recovered their own persisted file")
    else:
        await asyncio.gather(*(provider.destroy(ref) for ref in refs))
        print("Released both verified claims")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify", "cleanup"))
    parser.add_argument("session", type=UUID)
    parser.add_argument("refs", nargs="*")
    asyncio.run(main(parser.parse_args()))
