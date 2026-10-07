"""Claude Agent SDK lifecycle inside the existing isolated workspace."""
import asyncio
import json
import os
import subprocess
import sys
import threading

try:
    from .harness_agent import HarnessAgent, HarnessContext, TurnJournal
except ImportError:
    from harness_agent import HarnessAgent, HarnessContext, TurnJournal


NATIVE_TOOLS = ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'ToolSearch']


class ClaudeAgent(HarnessAgent):
    def __init__(self, *, spec, relay, config, activity, step, cwd, definition, context_store=None):
        self.context = HarnessContext(spec, relay, config, activity, step, cwd)
        self.context_store = context_store
        self.stopped = threading.Event()
        self.journal = None
        self.pending_text = []
        relay.before_model = self.before_model

    def validate(self):
        from importlib.metadata import PackageNotFoundError, version
        # Current images already have this pin. Project snapshots can predate
        # the SDK; upgrade just this runtime inside the isolated workspace.
        try:
            installed = version('claude-agent-sdk')
        except PackageNotFoundError:
            installed = ''
        if installed != '0.2.163':
            subprocess.run([sys.executable, '-m', 'pip', 'install',
                            'claude-agent-sdk==0.2.163', 'mcp<2'], check=True, timeout=300)
        from claude_agent_sdk import ClaudeSDKClient  # noqa: F401

    def interrupt(self):
        self.stopped.set()

    def before_model(self):
        if self.journal and not self.journal.pending:
            self.context.step()
        return not self.stopped.is_set()

    async def tool_hook(self, event, call_id, context):
        call_id = call_id or event['tool_use_id']
        name, args = event['tool_name'], event['tool_input']
        if event['hook_event_name'] == 'PreToolUse':
            if self.pending_text:
                self.context.activity.commentary(''.join(self.pending_text))
                self.journal.finish(''.join(self.pending_text))
                self.pending_text.clear()
            self.journal.tool_started(call_id, name, args)
            self.context.activity.start(call_id, name, args)
        else:
            failed = event['hook_event_name'] == 'PostToolUseFailure'
            output = event.get('error', '') if failed else event.get('tool_response', '')
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            self.journal.tool_finished(call_id, text)
            self.context.activity.complete(call_id, name, args, {
                'content': [{'type': 'text', 'text': text}], 'isError': failed})
        return {}

    def options(self, system_message):
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        ctx = self.context
        # The only inference credential in the child is this run's capability.
        # SDK prompt caching is on by default; override inherited disable flags.
        # The loopback gateway needs an explicit opt-in to deferred tool loading.
        # ToolSearch must also be in tools/allowed_tools for discovery to work.
        env = {'ANTHROPIC_BASE_URL': ctx.relay.url,
               'ANTHROPIC_API_KEY': os.environ['WORKSPACE_RUN_TOKEN'],
               'ANTHROPIC_AUTH_TOKEN': '', 'CLAUDE_CODE_OAUTH_TOKEN': '',
               'CLAUDE_CODE_USE_BEDROCK': '0', 'CLAUDE_CODE_USE_VERTEX': '0',
               'CLAUDE_CODE_USE_FOUNDRY': '0', 'CLAUDE_CODE_MAX_RETRIES': '0',
               'ENABLE_TOOL_SEARCH': 'true', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
               'DISABLE_PROMPT_CACHING': '0', 'DISABLE_PROMPT_CACHING_HAIKU': '0',
               'DISABLE_PROMPT_CACHING_SONNET': '0', 'DISABLE_PROMPT_CACHING_OPUS': '0'}
        return ClaudeAgentOptions(
            model=ctx.spec['model'], cwd=ctx.cwd,
            system_prompt=system_message + '\nUse ToolSearch to discover Moyai MCP tools before calling them. Do not start detached work.',
            tools=NATIVE_TOOLS, allowed_tools=[*NATIVE_TOOLS, 'mcp__moyai__*'],
            permission_mode='dontAsk', setting_sources=[], strict_mcp_config=True,
            mcp_servers={'moyai': ctx.config['mcp_servers']['workspace']}, env=env,
            max_turns=ctx.spec.get('max_iterations') or None,
            hooks={name: [HookMatcher(hooks=[self.tool_hook])] for name in
                   ('PreToolUse', 'PostToolUse', 'PostToolUseFailure')})

    def run_conversation(self, prompt, *, conversation_history, system_message):
        self.validate()
        self.stopped.clear()
        self.pending_text.clear()
        if self.context_store is not None:
            self.context_store.compact(self.context.relay.compact)
            conversation_history = self.context_store.history()
        self.journal = TurnJournal(conversation_history, prompt, self.context_store)
        reference_dir = self.context.spec.get('history_reference_dir', self.context.cwd)
        return asyncio.run(self._run(self.journal.prompt(prompt, conversation_history, cwd=reference_dir), system_message))

    async def _run(self, prompt, system_message):
        from claude_agent_sdk import ClaudeSDKClient, AssistantMessage, TextBlock, ResultMessage
        result = None
        try:
            async with asyncio.timeout(self.context.spec.get('timeout') or None):
                async with ClaudeSDKClient(options=self.options(system_message)) as client:
                    await client.query(prompt)
                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage):
                            self.pending_text.extend(block.text for block in message.content if isinstance(block, TextBlock))
                        elif isinstance(message, ResultMessage):
                            result = message
        except Exception:
            # Never expose SDK stderr or provider payloads in public activity.
            # Preserve receipts after failure; no automatic replay or fallback.
            pass
        interrupted = self.stopped.is_set()
        completed = bool(result and result.subtype == 'success' and not result.is_error and not interrupted)
        answer = result.result if result else ''
        if completed:
            answer = answer or ''.join(self.pending_text)
            self.journal.finish(answer)
        return {'completed': completed, 'interrupted': interrupted,
                'failed': not completed and not interrupted, 'messages': self.journal.messages,
                'final_response': answer or 'Claude Agent SDK stopped before completing the response.'}

    def close(self):
        self.context.relay.before_model = None
