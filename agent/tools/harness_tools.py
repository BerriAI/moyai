"""Tool bindings for the in-process harnesses, still inside the Modal sandbox."""
import json


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

    async def computer(name, arguments):
        # Same schemas and implementation as the MCP bridge. In an independent
        # runtime, workspace_call forwards the action to its leased computer.
        return await invoke(name, arguments)

    async def read_file(path: str, offset: int = 0, length: int = 65536) -> str:
        """Read a complete workspace file page; follow next_offset, with exact base64 bytes."""
        return await computer('computer_read_file', {'path': path, 'offset': offset, 'length': length})

    async def write_file(path: str, content: str) -> str:
        """Write complete UTF-8 contents to a workspace file."""
        return await computer('computer_write_file', {'path': path, 'text': content, 'overwrite': True})

    async def terminal(command: str, yield_ms: int = 1000, request_key: str = '') -> str:
        """Start a command and return its process handle. Long commands continue; use terminal_read."""
        return await computer('computer_terminal', {'command': command, 'yield_ms': yield_ms,
                                                   **({'request_key': request_key} if request_key else {})})

    async def terminal_read(process_id: str, offset: int = 0, length: int = 65536, yield_ms: int = 1000) -> str:
        """Read command progress and complete output pages without terminating the command."""
        return await computer('computer_terminal_read', {'process_id': process_id, 'offset': offset, 'length': length, 'yield_ms': yield_ms})

    async def terminal_input(process_id: str, text: str = '', eof: bool = False, stop: bool = False, data_base64: str = '') -> str:
        """Send input/EOF or explicitly stop a command. Never retry unconfirmed input automatically."""
        if text and data_base64:
            return json.dumps({'error': 'Choose text or data_base64 for command input.'})
        return await computer('computer_terminal_input', {'process_id': process_id, 'eof': eof, 'stop': stop,
            **({'data_base64': data_base64} if data_base64 else {'text': text})})

    return [workspace_tools, workspace_call, read_file, write_file, terminal, terminal_read, terminal_input]
