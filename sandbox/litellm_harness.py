"""One LiteLLM session runner for every supported LiteLLM harness."""
import asyncio
import os
import threading

try:
    from .harness_agent import HarnessAgent, HarnessContext, TurnJournal
    from .harness_bindings import RUNTIME_BINDINGS
    from .harness_dependencies import prepare_runtime, prepare_binary
except ImportError:
    from harness_agent import HarnessAgent, HarnessContext, TurnJournal
    from harness_bindings import RUNTIME_BINDINGS
    from harness_dependencies import prepare_runtime, prepare_binary


class LiteLLMAgent(HarnessAgent):
    def __init__(self, *, spec, relay, config, activity, step, cwd, definition):
        self.context = HarnessContext(spec, relay, config, activity, step, cwd)
        self.definition = definition
        self.stopped = threading.Event()
        self.journal = None
        self.calls = {}
        relay.before_model = self.before_model

    def validate(self):
        if self.definition.runtime_binding not in RUNTIME_BINDINGS:
            raise ValueError('No verified sandbox/tool binding for the selected LiteLLM harness')
        prepare_runtime()
        prepare_binary(self.definition.runtime_binding)

    def interrupt(self):
        self.stopped.set()

    def before_model(self):
        # Public runtime events, rather than model-wire parsing, own the journal.
        # If tool result delivery is still catching up, defer the checkpoint.
        if self.journal and not self.journal.pending:
            self.context.step()
        return not self.stopped.is_set()

    def run_conversation(self, prompt, *, conversation_history, system_message):
        self.validate()
        self.stopped.clear()
        self.journal = TurnJournal(conversation_history, prompt)
        reference_dir = self.context.spec.get('history_reference_dir', self.context.cwd)
        return asyncio.run(self._run(self.journal.prompt(prompt, conversation_history, cwd=reference_dir), system_message))

    async def _run(self, prompt, system_message):
        import litellm
        from litellm.harness import Text, Reasoning, ToolCall, ToolResult, Approval
        ctx = self.context
        binding = RUNTIME_BINDINGS[self.definition.runtime_binding]
        harness = getattr(litellm.Harness, self.definition.litellm_harness)
        pending_text = []
        async with binding.sandbox_factory(ctx.cwd, ctx.config) as sandbox:
            async with litellm.aagent_session(
                harness, sandbox=sandbox, model='litellm_proxy/' + ctx.spec['model'],
                api_base=ctx.relay.url + ('/v1' if binding.in_process else ''),
                api_key=os.environ['WORKSPACE_RUN_TOKEN'],
                instructions=system_message + '\n' + binding.instructions
                    + ' Hermes discovery wrappers are unavailable. Do not start detached work.',
                max_turns=ctx.spec.get('max_iterations') or None, timeout=ctx.spec.get('timeout'),
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
        interrupted = self.stopped.is_set()
        completed = bool(result and result.stop_reason == 'done' and not interrupted)
        answer = result.text if result else ''
        if completed:
            self.journal.finish(answer)
        return {'completed': completed, 'interrupted': interrupted,
                'failed': not completed and not interrupted, 'messages': self.journal.messages,
                'final_response': answer or 'The selected harness stopped before completing the response.'}

    def close(self):
        self.context.relay.before_model = None
