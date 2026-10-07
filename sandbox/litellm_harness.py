"""One LiteLLM session runner for every supported LiteLLM harness."""
import asyncio
import os
from pathlib import Path
import re
import threading

try:
    from .harness_agent import HarnessAgent, HarnessContext, TurnJournal
    from .harness_bindings import RUNTIME_BINDINGS
    from .harness_dependencies import prepare_runtime, prepare_binary, runtime_version, LITELLM_REVISION
    from .context_recovery import run_with_context_recovery, prepare_context, maintain_context
except ImportError:
    from harness_agent import HarnessAgent, HarnessContext, TurnJournal
    from harness_bindings import RUNTIME_BINDINGS
    from harness_dependencies import prepare_runtime, prepare_binary, runtime_version, LITELLM_REVISION
    from context_recovery import run_with_context_recovery, prepare_context, maintain_context


class LiteLLMAgent(HarnessAgent):
    def __init__(self, *, spec, relay, config, activity, step, cwd, definition, context_store=None):
        self.context = HarnessContext(spec, relay, config, activity, step, cwd)
        self.context_store = context_store
        self.definition = definition
        self.stopped = threading.Event()
        self.journal = None
        self.calls = {}
        self.pending_text = []
        self.native = None
        self.resume_state = None
        self.runtime_version = ''
        relay.before_model = self.before_model
        relay.context_recovery = True

    def validate(self):
        if self.definition.runtime_binding not in RUNTIME_BINDINGS:
            raise ValueError('No verified sandbox/tool binding for the selected LiteLLM harness')
        prepare_runtime()
        prepare_binary(self.definition.runtime_binding)
        if not self.runtime_version:
            self.runtime_version = runtime_version(self.definition.runtime_binding)

    def prepare_native(self, system_message):
        if self.definition.runtime_binding != 'opencode':
            return
        try:
            from .native_session import NativeSession
        except ImportError:
            from native_session import NativeSession
        # Goal continuations are new invocations. Drop an intermediate stage
        # and obtain a new lease/fingerprint before appending their user input.
        self.native = NativeSession(self.context, self.context_store, self.definition.id,
            {'litellm': LITELLM_REVISION, 'runtime': self.runtime_version,
             'adapter': 1, 'instructions': system_message})
        self.resume_state = None
        if not self.runtime_version:
            self.native.invalidate()
            return
        from litellm import Harness
        from litellm.harness import State, StateIncompatible
        try:
            saved = self.native.begin()
            if saved is None:
                return
            state = State.loads(saved['state'].encode('utf-8'))
            if (state.harness != getattr(Harness, self.definition.litellm_harness)
                    or state.workdir != str(Path(self.context.cwd).resolve())
                    or state.model != self.context.spec['model']
                    or not isinstance(state.native_session_id, str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}', state.native_session_id)
                    or not saved['files']):
                raise ValueError('Incompatible native session state')
            self.native.restore_files(self.native.cache, saved['files'])
            self.resume_state = state
        except (ValueError, TypeError, KeyError, AttributeError, OSError, StateIncompatible):
            # Before any inference, discard an unusable native state and use
            # the independently preserved public journal for a fresh session.
            self.native.begin(resume=False)

    def interrupt(self):
        self.stopped.set()

    def before_model(self, request=None):
        # Public runtime events, rather than model-wire parsing, own the journal.
        # If tool result delivery is still catching up, defer the checkpoint.
        if self.journal and not self.journal.pending:
            self.context.step()
            if not self.stopped.is_set():
                maintain_context(self)
        return not self.stopped.is_set()

    def run_conversation(self, prompt, *, conversation_history, system_message):
        self.validate()
        self.stopped.clear()
        conversation_history = prepare_context(self, conversation_history)
        self.prepare_native(system_message)
        self.journal = TurnJournal(conversation_history, prompt, self.context_store)
        self.pending_text.clear()
        return run_with_context_recovery(self, prompt, conversation_history,
            lambda current: asyncio.run(self._run(current, system_message)))

    async def _run(self, prompt, system_message):
        import litellm
        from litellm.harness import Text, Reasoning, ToolCall, ToolResult, Approval
        ctx = self.context
        binding = RUNTIME_BINDINGS[self.definition.runtime_binding]
        harness = getattr(litellm.Harness, self.definition.litellm_harness)
        pending_text = self.pending_text
        native_payload = None
        sandbox_options = {'native': self.native} if self.native is not None else {}
        async with binding.sandbox_factory(ctx.cwd, ctx.config, **sandbox_options) as sandbox:
            resume = self.native is not None and self.native.resumed and self.resume_state is not None
            if not resume:
                self.resume_state = None
            factory = litellm.aagent_resume if resume else litellm.aagent_session
            async with factory(
                self.resume_state if resume else harness, sandbox=sandbox, model='litellm_proxy/' + ctx.spec['model'],
                api_base=ctx.relay.url + ('/v1' if binding.in_process else ''),
                api_key=os.environ['WORKSPACE_RUN_TOKEN'],
                instructions=system_message + '\n' + binding.instructions
                    + ' Hermes discovery wrappers are unavailable. Do not start detached work.',
                max_turns=ctx.spec.get('max_iterations') or None, timeout=getattr(self, 'context_timeout', ctx.spec.get('timeout')),
                permissions='full', options=binding.options_factory(ctx.config),
                tools=binding.tools(ctx.cwd, ctx.config),
            ) as session:
                stream = session.astream(prompt)
                async for event in stream:
                    if isinstance(event, Reasoning):
                        continue
                    if isinstance(event, Text):
                        pending_text.append(event.delta)
                    elif isinstance(event, ToolCall):
                        if pending_text:
                            ctx.activity.commentary(''.join(pending_text))
                            self.journal.finish(''.join(pending_text))
                            pending_text.clear()
                        name, args = event.native_name, dict(event.input)
                        self.calls[event.id] = (name, args)
                        self.journal.tool_started(event.id, name, args)
                        ctx.activity.start(event.id, name, args)
                    elif isinstance(event, ToolResult):
                        name, args = self.calls.pop(event.id, ('tool', {}))
                        self.journal.tool_finished(event.id, event.output)
                        ctx.activity.complete(event.id, name, args, {
                            'content': [{'type': 'text', 'text': event.output}], 'isError': event.is_error})
                    elif isinstance(event, Approval):
                        event.deny('Use the workspace authorization flow.')
                result = stream.result
                if (self.native is not None and self.runtime_version and result
                        and result.stop_reason == 'done' and not self.stopped.is_set() and not self.journal.pending):
                    try:
                        state, files = session.state(), self.native.files(self.native.cache)
                        if state.native_session_id and files:
                            native_payload = {'state': state.dumps().decode('utf-8'), 'files': files}
                    except (ValueError, OSError):
                        self.native.invalidate()
        interrupted = self.stopped.is_set()
        completed = bool(result and result.stop_reason == 'done' and not interrupted)
        answer = result.text if result else ''
        if completed:
            self.journal.finish(answer)
        if self.native is not None:
            if completed and not self.journal.pending and native_payload is not None:
                self.native.finish(native_payload)
            else:
                self.native.invalidate()
        return {'completed': completed, 'interrupted': interrupted,
                'failed': not completed and not interrupted, 'messages': self.journal.messages,
                'final_response': answer or 'The selected harness stopped before completing the response.'}

    def close(self):
        self.context.relay.before_model = None
        self.context.relay.context_recovery = False
        if self.native is not None:
            self.native.close()
