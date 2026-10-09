"""Claude Agent SDK lifecycle inside the existing isolated workspace."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from uuid import UUID, uuid4

try:
    from .harness_agent import HarnessAgent, HarnessContext, HarnessInputs, TurnJournal
    from .context_recovery import run_with_context_recovery, prepare_context, maintain_context
    from .native_session import NativeSession, MAX_BYTES
    from .sdk_failure import claude_details, exception_details, failure_diagnostic, failure_summary
    from .transport_recovery import MAX_TRANSPORT_ATTEMPTS, retryable_failure
    from .broker_relay import InputPending
except ImportError:
    from harness_agent import HarnessAgent, HarnessContext, HarnessInputs, TurnJournal
    from context_recovery import run_with_context_recovery, prepare_context, maintain_context
    from native_session import NativeSession, MAX_BYTES
    from sdk_failure import claude_details, exception_details, failure_diagnostic, failure_summary
    from transport_recovery import MAX_TRANSPORT_ATTEMPTS, retryable_failure
    from broker_relay import InputPending


NATIVE_TOOLS = ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'ToolSearch']
# Native image results, hook callbacks and transcript mirrors can exceed the
# SDK's 1 MiB JSON-line default. Leave room for their encoding/envelopes while
# keeping a finite per-message bound; this does not change model context limits.
SDK_MAX_BUFFER_SIZE = 16 * 1024 * 1024


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
        self.inputs = None
        self.model_calls = 0
        self.model_lock = threading.RLock()
        self.boundary_failed = False
        self.boundary_reason = ''
        self.transport_attempt = spec.get('transport_attempt', 0)
        self.recovery_input = None
        self.recovery_rejections = set()
        relay.before_model = self.before_model
        relay.on_model_blocked = self.model_blocked
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

    def model_blocked(self):
        with self.model_lock:
            receipt = uuid4().hex
            self.recovery_rejections.add(receipt)
            return InputPending(receipt)

    def before_model(self, request=None):
        with self.model_lock:
            # A finished background command can wake Claude before its queued
            # continuation is ingested. Admit only a request carrying that input.
            if self.recovery_input:
                if self.recovery_input.encode() not in (request or b''):
                    raise self.model_blocked()
                self.recovery_input = None
            if self.journal and not self.journal.pending:
                self.context.step()
                if not self.stopped.is_set():
                    maintain_context(self)
            if self.stopped.is_set() or self.boundary_failed:
                return False
            # Native max_turns restarts for another streamed user query. The
            # enclosing Moyai invocation keeps one shared inference ceiling.
            limit = self.context.spec.get('max_iterations')
            if limit and self.model_calls >= limit:
                self.boundary_failed = True
                self.boundary_reason = 'model call limit reached'
                return False
            self.model_calls += 1
            return True

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
            background = getattr(ctx.relay, 'live_compaction', False)
            env.update(DISABLE_COMPACT='1' if background else '0', DISABLE_AUTO_COMPACT='1' if background else '0',
                CLAUDE_CODE_AUTO_COMPACT_WINDOW=str(window),
                CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=str(max(1, min(80, self.compaction_window * 80 // window))))
        return ClaudeAgentOptions(
            model=ctx.spec['model'], cwd=ctx.cwd,
            max_buffer_size=SDK_MAX_BUFFER_SIZE,
            system_prompt=system_message + '\nUse ToolSearch to discover Moyai MCP tools before calling them. Do not start detached work.',
            tools=NATIVE_TOOLS, allowed_tools=[*NATIVE_TOOLS, 'mcp__moyai__*'],
            permission_mode='dontAsk', setting_sources=[], strict_mcp_config=True,
            mcp_servers={'moyai': ctx.config['mcp_servers']['workspace']}, env=env,
            max_turns=ctx.spec.get('max_iterations') or None,
            extra_args={'replay-user-messages': None},
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

    async def recover_model(self, client, observed_failure):
        """Continue the existing SDK session; never replay a failed HTTP POST.

        The receive loop stays active while readiness is checked, so SDK hooks
        and late tool receipts cannot be blocked by its bounded event queue.
        The surrounding task timeout includes this recovery window.
        """
        relay = self.context.relay

        def can_continue():
            limit = self.context.spec.get('max_iterations')
            return (retryable_failure(observed_failure, live=True)
                    and not self.stopped.is_set() and not self.boundary_failed and not relay.uncertain_tool
                    and relay.last_failure is observed_failure and not relay.context_required
                    and not (limit and self.model_calls >= limit))

        if self.transport_attempt >= MAX_TRANSPORT_ATTEMPTS or not can_continue():
            return False
        self.transport_attempt += 1
        self.context.activity.emit('status', 'Reconnecting to continue with the existing tools and saved results.',
                                   {'activity_version': 1, 'phase': 'reconnecting', 'stage': 'model_transport'})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.context.spec.get('transport_recovery_seconds', 600)
        retry_at = loop.time() + 2 ** self.transport_attempt
        probe = None
        try:
            while True:
                if not can_continue():
                    return False
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self.boundary_failed = True
                    self.boundary_reason = 'model connection recovery timed out'
                    return False
                if probe is None and loop.time() >= retry_at:
                    probe = asyncio.create_task(asyncio.to_thread(relay.model_ready, timeout=min(1, remaining)))
                if probe is not None and probe.done():
                    try:
                        ready = probe.result()
                    except Exception:
                        self.boundary_failed = True
                        self.boundary_reason = 'broker readiness could not be verified'
                        return False
                    probe = None
                    if ready:
                        break
                    retry_at = loop.time() + 1
                await asyncio.sleep(min(.05, remaining))
        finally:
            if probe is not None:
                probe.cancel()
                await asyncio.gather(probe, return_exceptions=True)
        self.pending_text.clear()  # Native error text is not a public update.
        continuation = ('Continue the unfinished task in this same session after the connection recovered. '
                        'Inspect the existing tool results and running commands; do not repeat completed actions '
                        'or start duplicate commands. Apply any user corrections already received. '
                        'Continue working without repeating the acknowledgement or opening plan.')
        corrections = self.inputs.take() if self.inputs is not None else []
        if corrections:
            continuation += '\n\nLatest user corrections:\n' + '\n\n'.join(corrections)
        with self.model_lock:
            if not can_continue():
                return False
            self.recovery_input = str(uuid4())
            continuation += '\nRecovery input: ' + self.recovery_input
            if not relay.resume_model(observed_failure, live=True):
                self.recovery_input = None
                return False
        await client.query(continuation)
        self.context.activity.emit('status', 'Connection restored. Continuing the task.',
                                   {'activity_version': 1, 'phase': 'recovered', 'stage': 'model_transport'})
        return True

    async def _run(self, prompt, system_message):
        from claude_agent_sdk import ClaudeSDKClient, AssistantMessage, TextBlock, ResultMessage, SystemMessage, UserMessage
        result = None
        failure = {}
        finished = False
        self.model_calls = 0
        self.boundary_failed = False
        self.boundary_reason = ''
        self.recovery_input = None
        self.recovery_rejections = set()
        self.inputs = HarnessInputs(self.journal)
        submitted = set()
        query_lock = asyncio.Lock()
        recovering = False

        async def send_inputs(client):
            while True:
                async with query_lock:
                    corrections = [] if recovering else self.inputs.take()
                    if corrections:
                        identity = str(uuid4())
                        submitted.add(identity)

                        async def envelope():
                            yield {'type': 'user', 'uuid': identity, 'parent_tool_use_id': None,
                                   'origin': {'kind': 'human'},
                                   'message': {'role': 'user', 'content': '\n\n'.join(corrections)}}

                        await client.query(envelope())
                await asyncio.sleep(.02)
        # A confirmed context rejection retires the prior transcript before
        # run_with_context_recovery starts a fresh SDK with public receipts.
        if self.native is not None and not self.native.resumed:
            self.transcript = ClaudeTranscript() if self.native.enabled else None
        try:
            async with asyncio.timeout(getattr(self, 'context_timeout', self.context.spec.get('timeout')) or None):
                async with ClaudeSDKClient(options=self.options(system_message)) as client:
                    await client.query(prompt)
                    try:
                        async with asyncio.TaskGroup() as tasks:
                            sender = tasks.create_task(send_inputs(client))
                            stream = client.receive_messages().__aiter__()
                            incoming = asyncio.create_task(anext(stream))
                            recovery = None
                            recovery_failure = None
                            deferred = None
                            recovery_failed = False

                            async def resume(observed_failure):
                                async with query_lock:
                                    return await self.recover_model(client, observed_failure)

                            try:
                                while not recovery_failed:
                                    ready, _ = await asyncio.wait(
                                        [incoming, *([recovery] if recovery is not None else [])],
                                        return_when=asyncio.FIRST_COMPLETED)
                                    if recovery is not None and recovery in ready:
                                        recovered = await recovery
                                        recovery = None
                                        recovering = False
                                        if deferred is not None:
                                            result, recovery_failure = deferred
                                            deferred = None
                                            recovering = True
                                            recovery = asyncio.create_task(resume(recovery_failure))
                                        else:
                                            recovery_failed = not recovered
                                            if recovered:
                                                result = None
                                    if incoming not in ready:
                                        continue
                                    try:
                                        message = incoming.result()
                                    except StopAsyncIteration:
                                        break
                                    incoming = asyncio.create_task(anext(stream))
                                    if isinstance(message, UserMessage):
                                        if message.parent_tool_use_id is None:
                                            submitted.discard(message.uuid)
                                    elif isinstance(message, AssistantMessage):
                                        if not message.error and message.parent_tool_use_id is None:
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
                                        # Match an issued local rejection, not a generic HTTP
                                        # status or count. A later stop/limit remains terminal.
                                        with self.model_lock:
                                            if (not self.stopped.is_set() and not self.boundary_failed
                                                    and message.is_error and getattr(message, 'api_error_status', None) == 400):
                                                receipt = next((value for value in self.recovery_rejections
                                                                if message.result == 'API Error: 400 ' + InputPending(value).message()), None)
                                                if receipt is not None:
                                                    self.recovery_rejections.remove(receipt)
                                                    continue
                                        if message.subtype != 'success' or message.is_error or self.stopped.is_set():
                                            result = message
                                            if self.stopped.is_set() or self.boundary_failed:
                                                break
                                            observed_failure = getattr(self.context.relay, 'last_failure', None)
                                            status = getattr(result, 'api_error_status', None)
                                            # Network faults without an upstream HTTP status
                                            # are returned as 502 by the relay. Stream EOF can
                                            # instead end natively without an API status.
                                            model_error = isinstance(observed_failure, dict) and (
                                                status == (observed_failure.get('http_status') or 502)
                                                or (status is None and observed_failure.get('response_started')
                                                    and observed_failure.get('transport_interrupted')))
                                            if (result.subtype not in {'success', 'error_during_execution'}
                                                    or not model_error):
                                                # The native result owns the failure reason;
                                                # this flag only prevents transport recovery.
                                                self.boundary_failed = True
                                                break
                                            if recovery is not None:
                                                # A new forwarded request can fail before query()
                                                # returns. Retain that failure until the write ends;
                                                # unrelated native errors must terminate immediately.
                                                if (observed_failure is recovery_failure
                                                        or not retryable_failure(observed_failure, live=True)):
                                                    self.boundary_failed = True
                                                    break
                                                deferred = (result, observed_failure)
                                                continue
                                            recovery_failure = observed_failure
                                            recovering = True
                                            recovery_failed = False
                                            recovery = asyncio.create_task(resume(recovery_failure))
                                            continue
                                        if (recovery_failed or deferred is not None or self.recovery_input
                                                or (recovering and self.context.relay.model_failed)):
                                            continue
                                        result = message
                                        # During tools, Claude can merge a correction into
                                        # this turn; during a final response it starts a new
                                        # one. UUID echoes, not result counts, prove delivery.
                                        # An echo can race the coroutine writing its
                                        # envelope. Join that write before closing input.
                                        async with query_lock:
                                            if not submitted and self.inputs.close_if_empty():
                                                finished = True
                                                break
                                        text = result.result or ''.join(self.pending_text)
                                        if text:
                                            self.journal.finish(text)
                                            self.context.activity.commentary(text)
                                        self.pending_text.clear()
                            finally:
                                sender.cancel()
                                pending = [incoming, *([recovery] if recovery is not None else [])]
                                for task in pending:
                                    task.cancel()
                                await asyncio.gather(*pending, return_exceptions=True)
                    except ExceptionGroup as exc:
                        # Preserve the SDK's body-free diagnostic type when a
                        # single receive/write failure exits the task group.
                        if len(exc.exceptions) == 1:
                            raise exc.exceptions[0]
                        raise
        except Exception as exc:
            failure.update(exception_details(exc))
            # Match only the SDK's fixed limit error, never publish JSON lines
            # (which can contain private tool results) or arbitrary stderr.
            from claude_agent_sdk import CLIJSONDecodeError
            if (isinstance(exc, CLIJSONDecodeError)
                    and exc.line == f'JSON message exceeded maximum buffer size of {SDK_MAX_BUFFER_SIZE} bytes'):
                failure.update(code='sdk_message_buffer_exceeded', buffer_limit_bytes=SDK_MAX_BUFFER_SIZE)
        finally:
            self.inputs.close()
        interrupted = self.stopped.is_set()
        completed = bool(result and result.subtype == 'success' and not result.is_error
                         and finished and not interrupted and not failure and not self.journal.pending
                         and not self.boundary_failed)
        answer = result.result if completed else ''
        if completed:
            answer = answer or ''.join(self.pending_text)
            self.journal.finish(answer)
            if (self.transcript is not None and self.transcript.valid and self.transcript.appended and self.transcript.records
                    and result.session_id == self.transcript.session_id and not self.journal.pending):
                self.native.finish(self.transcript.payload())
        if result is not None and not completed:
            failure = {**claude_details(result), **failure}
        diagnostic = failure_diagnostic(self, 'claude-agent-sdk', failure) if not completed and not interrupted else None
        return {'completed': completed, 'interrupted': interrupted,
                'failed': not completed and not interrupted, 'messages': self.journal.messages,
                **({'sdk_failure': diagnostic} if diagnostic else {}),
                'final_response': (answer if completed else failure_summary(diagnostic) if diagnostic else
                    'Claude Agent SDK stopped before completing the response.')}

    def close(self):
        self.context.relay.before_model = None
        self.context.relay.on_model_blocked = None
        self.context.relay.context_recovery = False
        if self.native is not None:
            self.native.close()
