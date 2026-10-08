"""Claude Agent SDK lifecycle inside the existing isolated workspace."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from uuid import UUID, uuid4

try:
    from .harness_agent import HarnessAgent, HarnessContext, TurnJournal
    from .context_recovery import run_with_context_recovery, prepare_context, maintain_context
    from .native_session import NativeSession, MAX_BYTES
    from .sdk_failure import claude_details, exception_details, report_failure, failure_summary
except ImportError:
    from harness_agent import HarnessAgent, HarnessContext, TurnJournal
    from context_recovery import run_with_context_recovery, prepare_context, maintain_context
    from native_session import NativeSession, MAX_BYTES
    from sdk_failure import claude_details, exception_details, report_failure, failure_summary


NATIVE_TOOLS = ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'ToolSearch']


class ClaudeTranscript:
    """SDK SessionStore protocol; transcript entries stay opaque and private."""
    def __init__(self, saved=None):
        self.session_id = str(uuid4())
        self.records = {}
        self.valid = True
        self.appended = False
        if saved is not None:
            self.session_id = str(UUID(saved['session_id']))
            if not isinstance(saved['records'], list):
                raise ValueError('Invalid native transcript')
            for record in saved['records']:
                key, entries = record['key'], record['entries']
                if (not isinstance(key, dict) or key.get('session_id') != self.session_id
                        or not isinstance(key.get('project_key'), str) or not isinstance(entries, list)
                        or not all(isinstance(entry, dict) and isinstance(entry.get('type'), str) for entry in entries)):
                    raise ValueError('Invalid native transcript')
                self.records[json.dumps(key, sort_keys=True)] = entries
            if not any(entries and 'subpath' not in json.loads(key) for key, entries in self.records.items()):
                raise ValueError('Missing native transcript')

    async def append(self, key, entries):
        if not self.valid:
            return
        self.appended = self.appended or bool(entries)
        encoded = json.dumps(key, sort_keys=True)
        previous = self.records.setdefault(encoded, [])
        positions = {entry['uuid']: index for index, entry in enumerate(previous) if entry.get('uuid')}
        for entry in entries:
            identity = entry.get('uuid')
            if identity and identity in positions:
                previous[positions[identity]] = entry
            else:
                if identity:
                    positions[identity] = len(previous)
                previous.append(entry)
        if len(json.dumps(self.payload()).encode()) > MAX_BYTES:
            self.valid = False
            self.records.clear()

    async def load(self, key):
        return self.records.get(json.dumps(key, sort_keys=True))

    async def list_subkeys(self, key):
        return [value['subpath'] for encoded in self.records if (value := json.loads(encoded)).get('subpath')
                and value['session_id'] == key['session_id'] and value['project_key'] == key['project_key']]

    def payload(self):
        return {'session_id': self.session_id,
                'records': [{'key': json.loads(key), 'entries': entries} for key, entries in self.records.items()]}


class ClaudeAgent(HarnessAgent):
    def __init__(self, *, spec, relay, config, activity, step, cwd, definition, context_store=None):
        self.context = HarnessContext(spec, relay, config, activity, step, cwd)
        self.context_store = context_store
        self.stopped = threading.Event()
        self.journal = None
        self.pending_text = []
        self.native_compactions = 0
        self.compaction_window = None
        self.native = None
        self.transcript = None
        relay.before_model = self.before_model
        relay.context_recovery = True

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

    def before_model(self, request=None):
        if self.journal and not self.journal.pending:
            self.context.step()
            if not self.stopped.is_set():
                maintain_context(self)
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
        if self.native is not None:
            env.update(self.native.env)
        if self.compaction_window:
            # The SDK accepts a minimum 100k window. A percentage also covers
            # smaller deployments. Neither setting caps generated output.
            window = max(100_000, self.compaction_window)
            env.update(DISABLE_COMPACT='0', DISABLE_AUTO_COMPACT='0',
                CLAUDE_CODE_AUTO_COMPACT_WINDOW=str(window),
                CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=str(max(1, min(80, self.compaction_window * 80 // window))))
        return ClaudeAgentOptions(
            model=ctx.spec['model'], cwd=ctx.cwd,
            system_prompt=system_message + '\nUse ToolSearch to discover Moyai MCP tools before calling them. Do not start detached work.',
            tools=NATIVE_TOOLS, allowed_tools=[*NATIVE_TOOLS, 'mcp__moyai__*'],
            permission_mode='dontAsk', setting_sources=[], strict_mcp_config=True,
            mcp_servers={'moyai': ctx.config['mcp_servers']['workspace']}, env=env,
            max_turns=ctx.spec.get('max_iterations') or None,
            **({'session_store': self.transcript,
                'resume': self.transcript.session_id if self.native.resumed else None,
                'session_id': None if self.native.resumed else self.transcript.session_id}
               if self.transcript is not None else {}),
            hooks={name: [HookMatcher(hooks=[self.tool_hook])] for name in
                   ('PreToolUse', 'PostToolUse', 'PostToolUseFailure')})

    def run_conversation(self, prompt, *, conversation_history, system_message):
        self.validate()
        self.stopped.clear()
        self.pending_text.clear()
        self.native_compactions = 0
        conversation_history = prepare_context(self, conversation_history)
        self.native = NativeSession(self.context, self.context_store, 'claude-agent-sdk',
            {'sdk': '0.2.163', 'instructions': system_message, 'tools': NATIVE_TOOLS, 'adapter': 1})
        saved = self.native.begin()
        try:
            self.transcript = ClaudeTranscript(saved) if self.native.enabled else None
        except (KeyError, TypeError, ValueError, AttributeError):
            self.native.begin(resume=False)
            self.transcript = None
        self.journal = TurnJournal(conversation_history, prompt, self.context_store)
        with self.native.temporary_files():
            return run_with_context_recovery(self, prompt, conversation_history,
                lambda current: asyncio.run(self._run(current, system_message)))

    async def _run(self, prompt, system_message):
        from claude_agent_sdk import ClaudeSDKClient, AssistantMessage, TextBlock, ResultMessage, SystemMessage
        result = None
        failure = {}
        # A confirmed context rejection retires the prior transcript before
        # run_with_context_recovery starts a fresh SDK with public receipts.
        if self.native is not None and not self.native.resumed:
            self.transcript = ClaudeTranscript() if self.native.enabled else None
        try:
            async with asyncio.timeout(getattr(self, 'context_timeout', self.context.spec.get('timeout')) or None):
                async with ClaudeSDKClient(options=self.options(system_message)) as client:
                    await client.query(prompt)
                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage):
                            self.pending_text.extend(block.text for block in message.content if isinstance(block, TextBlock))
                        elif isinstance(message, SystemMessage) and message.subtype == 'compact_boundary':
                            self.native_compactions += 1
                            self.pending_text.clear()
                            # Native summaries may contain requester-private
                            # context. Persist only public tool receipts.
                            self.context.activity.commentary('The agent compacted its context and is continuing. Completed tool receipts remain saved.')
                        elif isinstance(message, SystemMessage) and message.subtype == 'mirror_error':
                            if self.transcript is not None:
                                self.transcript.valid = False
                        elif isinstance(message, ResultMessage):
                            result = message
        except Exception as exc:
            failure.update(exception_details(exc))
        interrupted = self.stopped.is_set()
        completed = bool(result and result.subtype == 'success' and not result.is_error
                         and not interrupted and not failure and not self.journal.pending)
        answer = result.result if completed else ''
        if completed:
            answer = answer or ''.join(self.pending_text)
            self.journal.finish(answer)
            if (self.transcript is not None and self.transcript.valid and self.transcript.appended and self.transcript.records
                    and result.session_id == self.transcript.session_id and not self.journal.pending):
                self.native.finish(self.transcript.payload())
        if result is not None and not completed:
            failure = {**claude_details(result), **failure}
        diagnostic = report_failure(self, 'claude-agent-sdk', failure) if not completed and not interrupted else None
        return {'completed': completed, 'interrupted': interrupted,
                'failed': not completed and not interrupted, 'messages': self.journal.messages,
                **({'sdk_failure': diagnostic} if diagnostic else {}),
                'final_response': (answer if completed else failure_summary(diagnostic) if diagnostic else
                    'Claude Agent SDK stopped before completing the response.')}

    def close(self):
        self.context.relay.before_model = None
        self.context.relay.context_recovery = False
        if self.native is not None:
            self.native.close()
