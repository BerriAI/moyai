"""Tool bindings for the in-process harnesses, still inside the Modal sandbox."""
import asyncio
import json
from pathlib import Path


def tools_for(cwd, config):
    from mcp import ClientSession, StdioServerParameters
    try:
        from mcp import MCPError
    except ImportError:  # Controller tests and older snapshots can use MCP 1.x.
        from mcp import McpError as MCPError
    from mcp.client.stdio import stdio_client
    from mcp.types import INTERNAL_ERROR

    async def workspace_tools() -> str:
        """List currently authorized workspace tool names, descriptions and input schemas."""
        return await invoke('', {})

    async def workspace_call(name: str, arguments_json: str) -> str:
        """Call one authorized workspace tool. Pass arguments as a JSON object string; discover its schema with workspace_tools first."""
        try:
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise ValueError('Expected object')
        except (ValueError, TypeError):
            return json.dumps({'isError': True, 'content': [{'type': 'text',
                'text': 'arguments_json must be a valid JSON object. No tool was called.'}]})
        return await invoke(name, arguments)

    async def invoke(name, arguments):
        server = config['mcp_servers']['workspace']
        params = StdioServerParameters(command=server['command'], args=server.get('args', []), env=server.get('env'))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                # Populate the SDK's schema cache before an action: call_tool
                # otherwise discovers tools after execution, risking its receipt.
                try:
                    catalog = await client.list_tools()
                except MCPError as exc:
                    if exc.error.code != INTERNAL_ERROR:
                        raise
                    return json.dumps({'isError': True, 'content': [{'type': 'text',
                        'text': 'Workspace tool discovery failed. No tool action was attempted. '
                                'Check the workspace connection and call workspace_tools before trying again.'}]})
                if not name:
                    return catalog.model_dump_json(by_alias=True)
                if name not in {tool.name for tool in catalog.tools}:
                    return json.dumps({'isError': True, 'content': [{'type': 'text',
                        'text': 'Tool is not in the authorized catalog. No tool action was attempted. '
                                'Use workspace_tools to discover available tools.'}]})
                return (await client.call_tool(name, arguments)).model_dump_json(by_alias=True)

    def resolve(path):
        root = Path(cwd).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError('Path must stay inside the workspace')
        return target

    def read_file(path: str) -> str:
        """Read a UTF-8 file within the workspace, up to 100KB."""
        with resolve(path).open() as stream:
            return stream.read(100000)

    def write_file(path: str, content: str) -> str:
        """Write a UTF-8 file within the workspace."""
        target = resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return 'File written: ' + str(target)

    async def terminal(command: str) -> str:
        """Run a shell command in the isolated workspace, with a 120 second limit."""
        from litellm.harness.sandbox.local import filtered_environ
        proc = await asyncio.create_subprocess_exec('/bin/bash', '-lc', command, cwd=cwd, env=filtered_environ(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 'Command timed out; not replayed.'
        return json.dumps({'exit_code': proc.returncode, 'output': output.decode(errors='replace')[:100000]})

    return [workspace_tools, workspace_call, read_file, write_file, terminal]
