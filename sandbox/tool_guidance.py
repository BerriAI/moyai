"""Tool discovery instructions matched to the selected runtime."""


def tool_guidance(harness):
    if harness == 'hermes':
        discovery = (
            'Workspace MCP tools load on demand through tool_search, tool_describe, and tool_call. '
            'Search by service and action, describe the exact matching names, then invoke them through tool_call. '
            'If an exact name is already in the catalog, describe it directly. '
            'Use one workspace invocation per tool_call; batch tool_describe when you need several schemas. ')
    elif harness == 'codex':
        discovery = (
            'Codex exposes native tool_search for supported models. Search by service and action to load '
            'the matching Moyai MCP schemas, then invoke the exact returned tool and namespace directly. '
            'Native search returns schemas; no separate describe or generic tool_call step is needed. '
            'Programmatic calls through functions.exec remain available. If the model does not expose '
            'tool_search, use its actual catalog; in code mode, inspect ALL_TOOLS and call the listed tool through tools. '
            'This integration does not provide the Hermes search/describe/call interface. '
            'The runtime waits for the Moyai MCP server before the first model request. ')
    elif harness == 'claude-agent-sdk':
        discovery = (
            'Claude exposes workspace MCP tools through its native ToolSearch and mcp__moyai__ names. '
            'Use ToolSearch for deferred tools, then invoke the discovered tool directly with its schema. '
            'The Hermes search/describe/call interface is not used here. ')
    else:
        discovery = (
            'Use the workspace MCP tools exposed by this runtime, with their exact catalog names and schemas. '
            'Use native discovery when offered; otherwise invoke the listed tools directly. ')
    return discovery + (
        'This applies to connected apps, browser, skills, credentials, memory and agent coordination. '
        'An absent discovery interface or delayed catalog does not prove a connection is disabled. '
        'For missing tools or Moyai runtime failures, call workspace_diagnostics first. It reports this session’s '
        'broker catalog, connection scope and recent sanitized failure records. Compare its broker catalog with '
        'the MCP catalog and native tools before attributing a failure to permissions. '
        'Source access uses github_repositories and github_checkout for an authorized repository; '
        'Moyai’s upstream source is BerriAI/moyai. An empty local workspace does not mean source is inaccessible. '
        'Workspace diagnostics are not unrestricted host logs. If they do not establish the cause, state what '
        'is known and which request IDs or additional server records are needed. Do not invent a cause. ')
