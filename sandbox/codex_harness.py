"""Native Codex SDK execution within Moyai's existing sandbox and lifecycle."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
from uuid import uuid4

try:
    from .harness_agent import HarnessAgent, HarnessContext, HarnessInputs, TurnJournal
    from .harness_dependencies import prepare_codex
    from .context_recovery import run_with_context_recovery, prepare_context, maintain_context
    from .sdk_failure import codex_details, exception_details, failure_diagnostic, failure_summary
    from .transport_recovery import MAX_TRANSPORT_ATTEMPTS, retryable_failure
except ImportError:
    from harness_agent import HarnessAgent, HarnessContext, HarnessInputs, TurnJournal
    from harness_dependencies import prepare_codex
    from context_recovery import run_with_context_recovery, prepare_context, maintain_context
    from sdk_failure import codex_details, exception_details, failure_diagnostic, failure_summary
    from transport_recovery import MAX_TRANSPORT_ATTEMPTS, retryable_failure


RECEIPT_TIMEOUT_SECONDS = 10


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + '=' + toml_value(item)
                              for key, item in value.items() if item is not None) + '}'
    return json.dumps(value)


class CodexAgent(HarnessAgent):
    def __init__(self, *, spec, relay, config, activity, step, cwd, definition, context_store=None):
        self.context = HarnessContext(spec, relay, config, activity, step, cwd)
        self.context_store = context_store
        self.stopped = threading.Event()
        self.receipts = threading.Condition(threading.RLock())
        self.journal = None
        self.inputs = None
        self.calls = {}
        self.completed = set()
        self.observed_outputs = set()
        self.compaction_window = None
        self.boundary_failed = False
        self.boundary_reason = ''
        self.model_calls = 0
        self.transport_attempt = spec.get('transport_attempt', 0)
        relay.before_model = self.before_model
        relay.context_recovery = True

    def validate(self):
        prepare_codex()
        from openai_codex.async_client import AsyncCodexClient  # noqa: F401

    def interrupt(self):
        # The host invokes this at a settled tool boundary. User Stop separately
        # revokes the broker capability and terminates the enclosing sandbox.
        with self.receipts:
            self.stopped.set()
            self.receipts.notify_all()

    def before_model(self, request=None):
        with self.receipts:
            if self.stopped.is_set() or self.boundary_failed:
                return False
            if request is not None:
                try:
                    items = json.loads(request).get('input', [])
                    if not isinstance(items, (list, str)):
                        raise ValueError('Invalid input')
                    expected = {item['call_id'] for item in items if isinstance(item, dict)
                                and item.get('type') in {'function_call_output', 'custom_tool_call_output'}}
                    if not all(isinstance(call_id, str) and call_id for call_id in expected):
                        raise ValueError('Invalid call ID')
                except (ValueError, TypeError, KeyError, AttributeError):
                    self.boundary_failed = True
                    self.boundary_reason = 'invalid model request'
                    return False
                # HTTP and app-server notifications arrive on different threads.
                # Input IDs are only a barrier; wire text never becomes a receipt.
                # A code-mode output may yield while nested tools are still live.
                # Let the model poll them; lifecycle work below must still wait.
                if not self.receipts.wait_for(
                        lambda: expected <= self.completed | self.observed_outputs or self.stopped.is_set(),
                        timeout=RECEIPT_TIMEOUT_SECONDS):
                    self.boundary_failed = True
                    self.boundary_reason = 'native output notification timed out'
                    return False
            if self.stopped.is_set():
                return False
            if self.journal and not self.journal.pending:
                self.context.step()
                if not self.stopped.is_set() and not self.boundary_failed:
                    maintain_context(self)
            if self.stopped.is_set() or self.boundary_failed:
                return False
            # Polling is inference too. Pending work cannot bypass the turn cap.
            limit = self.context.spec.get('max_iterations')
            if limit and self.model_calls >= limit:
                self.boundary_failed = True
                self.boundary_reason = 'model call limit reached'
                return False
            self.model_calls += 1
            return True

    def sdk_config(self, home):
        from openai_codex import CodexConfig
        from codex_cli_bin import bundled_codex_path, bundled_path_dir
        ctx = self.context
        settings = {
            'model_provider': 'moyai',
            'model_providers': {'moyai': {
                'name': 'Moyai', 'base_url': ctx.relay.url + '/v1', 'wire_api': 'responses',
                'env_key': 'WORKSPACE_RUN_TOKEN', 'requires_openai_auth': False,
                'supports_websockets': False, 'request_max_retries': 0, 'stream_max_retries': 0}},
            'mcp_servers': {'moyai': ctx.config['mcp_servers']['workspace']},
            'projects': {str(Path(ctx.cwd).resolve()): {'trust_level': 'untrusted'}},
            'project_doc_max_bytes': 0, 'web_search': 'disabled',
            # Moyai owns plugins; native marketplace sync outlives SDK shutdown.
            'features': {'hooks': False, 'apps': False, 'plugins': False, 'memories': False,
                         'multi_agent': False, 'unified_exec': False, 'shell_snapshot': False},
            'skills': {'include_instructions': False, 'bundled': {'enabled': False}},
            'agents': {'enabled': False},
            'cli_auth_credentials_store': 'ephemeral', 'history': {'persistence': 'none'},
        }
        if self.compaction_window:
            settings['model_context_window'] = self.compaction_window
            settings['model_auto_compact_token_limit'] = max(1024, self.compaction_window * 4 // 5)
        # CodexConfig.env merges the parent environment. Clear inherited secrets
        # and runtime switches rather than handing the child another provider key.
        keep = {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT'}
        env = {key: value if key in keep else '' for key, value in os.environ.items()}
        env.update(CODEX_HOME=str(home), WORKSPACE_RUN_TOKEN=os.environ['WORKSPACE_RUN_TOKEN'])
        if bundled_path_dir():
            env['PATH'] = str(bundled_path_dir()) + os.pathsep + os.environ.get('PATH', '')
        settings_args = tuple(name + '=' + toml_value(value) for name, value in settings.items())
        # The SDK merges env, and empty native feature/originator variables still
        # have meaning. Remove them before exec instead of treating blank as unset.
        remove = set(os.environ) - keep - {'CODEX_HOME', 'WORKSPACE_RUN_TOKEN'}
        launch = ['/usr/bin/env', *[part for key in sorted(remove) for part in ('-u', key)],
                  str(bundled_codex_path()), *[part for value in settings_args for part in ('-c', value)],
                  'app-server', '--listen', 'stdio://']
        return CodexConfig(cwd=str(home), env=env,
                           launch_args_override=tuple(launch), config_overrides=settings_args,
                           client_name='moyai', client_title='Moyai', experimental_api=True)

    def record_item(self, item, *, completed):
        kind = item.get('type')
        if kind == 'commandExecution':
            name, args = 'terminal', {'command': item.get('command', ''), 'cwd': item.get('cwd', '')}
            output = item.get('aggregatedOutput') or ''
            failed = item.get('status') != 'completed' or item.get('exitCode') != 0
        elif kind == 'fileChange':
            name, args = 'apply_patch', {'changes': item.get('changes', [])}
            output = json.dumps(item.get('changes', []), ensure_ascii=False)
            failed = item.get('status') != 'completed'
        elif kind == 'mcpToolCall':
            name = 'mcp__' + item['server'] + '__' + item['tool']
            args = item.get('arguments') or {}
            result = item.get('result') or {}
            failed = item.get('status') != 'completed' or bool(result.get('isError'))
            # Native errors may contain raw provider credentials or private stderr.
            output = json.dumps(result, ensure_ascii=False) if result else 'Tool failed; action not confirmed.'
        elif kind == 'imageView':
            name, args = 'view_image', {'path': item.get('path', '')}
            output, failed = 'Image viewed.', False
        else:
            return
        call_id = item['id']
        with self.receipts:
            if call_id in self.completed:
                return
            if call_id not in self.calls:
                self.calls[call_id] = (name, args)
                self.journal.tool_started(call_id, name, args)
                self.context.activity.start(call_id, name, args)
            if completed:
                self.journal.tool_finished(call_id, output)
                self.context.activity.complete(call_id, name, args, {
                    'content': [{'type': 'text', 'text': output}], 'isError': failed})
                self.completed.add(call_id)
                self.receipts.notify_all()

    def run_conversation(self, prompt, *, conversation_history, system_message):
        self.validate()
        self.stopped.clear()
        self.boundary_failed = False
        self.boundary_reason = ''
        conversation_history = prepare_context(self, conversation_history)
        self.journal = TurnJournal(conversation_history, prompt, self.context_store)
        return run_with_context_recovery(self, prompt, conversation_history,
            lambda current: asyncio.run(self._run(current, system_message)))

    def record_tool_event(self, method, payload):
        item = payload.get('item', {})
        if method == 'rawResponseItem/completed':
            if item.get('type') in {'function_call_output', 'custom_tool_call_output'}:
                # Observation permits polling; only native completion is a receipt.
                with self.receipts:
                    self.observed_outputs.add(item['call_id'])
                    self.receipts.notify_all()
        elif method in {'item/started', 'item/completed'}:
            self.record_item(item, completed=method == 'item/completed')

    async def drain_tool_events(self, client, turn_ids):
        from openai_codex.errors import TransportClosedError
        for turn_id in turn_ids:
            try:
                while True:
                    event = await client.next_turn_notification(turn_id)
                    payload = (event.payload.params if hasattr(event.payload, 'params') else
                               event.payload.model_dump(mode='json', by_alias=True))
                    self.record_tool_event(event.method, payload)
            except TransportClosedError:
                pass  # Completed queues can still receive later tool results.

    async def await_with_tool_events(self, client, turn_ids, operation):
        # SDK startup can reach model admission before its RPC returns. Keep
        # receipts flowing for live commands owned by already-completed turns.
        request = asyncio.create_task(operation)
        try:
            while True:
                await self.drain_tool_events(client, turn_ids)
                done, _ = await asyncio.wait({request}, timeout=0.05)
                if done:
                    return await request
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    async def compact_context(self, client, thread_id, turn_ids):
        # This public SDK operation subscribes to thread events only; it does
        # not create a native goal. Compact has no returned turn ID to subscribe
        # to. Release the route before starting another ordinary native turn.
        route = client.register_goal_operation(thread_id)
        self.context.relay.native_compacting = True
        result = None

        async def read_events():
            nonlocal result
            compact_turn, compacted = None, False
            while True:
                event = await client.next_goal_notification(route)
                payload = (event.payload.params if hasattr(event.payload, 'params') else
                           event.payload.model_dump(mode='json', by_alias=True))
                self.record_tool_event(event.method, payload)
                if event.method == 'turn/started':
                    compact_turn = payload['turn']['id']
                elif event.method == 'item/completed' and payload['item']['type'] == 'contextCompaction':
                    compacted = True
                elif event.method == 'turn/completed' and payload['turn']['id'] == compact_turn:
                    result = compacted and payload['turn']['status'] == 'completed'

        # The goal route owns new thread events, even for old turn IDs. Keep
        # consuming it during the RPC and through the route's final queued event.
        notifications = asyncio.create_task(read_events())
        try:
            await self.await_with_tool_events(client, turn_ids, client.thread_compact(thread_id))
            while result is None and not self.stopped.is_set():
                await self.drain_tool_events(client, turn_ids)
                done, _ = await asyncio.wait({notifications}, timeout=0.05)
                if done:
                    await notifications  # Propagate a failed reader, not a false success.
            return result is True and not self.stopped.is_set()
        finally:
            # Unsubscribe before adding the SDK's end marker. Let the reader
            # save receipts queued after turn/completed instead of cancelling it.
            client.unregister_goal_operation(route)
            route.wake_notification_reader()
            await asyncio.gather(notifications, return_exceptions=True)
            self.context.relay.native_compacting = False

    async def settle_pending_receipts(self, client, turn_ids):
        """Keep the native runtime alive for late receipts before a handoff.

        A failed inference turn can finish while its yielded commands are still
        running. The SDK's completed turn queue remains readable, but reports
        TransportClosedError whenever it is temporarily empty. Closing the
        client here would discard those commands' eventual completion events.
        """
        from openai_codex.errors import TransportClosedError
        try:
            async with asyncio.timeout(RECEIPT_TIMEOUT_SECONDS):
                while self.journal.pending:
                    for turn_id in turn_ids:
                        while self.journal.pending:
                            try:
                                event = await client.next_turn_notification(turn_id)
                            except TransportClosedError:
                                break
                            if event.method == 'item/completed':
                                payload = event.payload.model_dump(mode='json', by_alias=True)
                                self.record_item(payload['item'], completed=True)
                    # Stop cancels grace waiting, but already-queued native
                    # completions still belong to the durable journal.
                    if self.stopped.is_set():
                        break
                    if self.journal.pending:
                        await asyncio.sleep(0.05)
        except TimeoutError:
            # The shared recovery gate still rejects genuinely unknown outcomes.
            # Neither elapsed time nor raw output IDs are completion receipts.
            pass

    async def wait_for_model(self, client, turn_ids, observed_failure):
        """Keep native history/tools alive while the broker process restarts."""
        relay = self.context.relay
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.context.spec.get('transport_recovery_seconds', 600)
        probe, retry_at = None, 0
        try:
            while True:
                await self.drain_tool_events(client, turn_ids)
                limit = self.context.spec.get('max_iterations')
                if (self.stopped.is_set() or self.boundary_failed or relay.uncertain_tool
                        or relay.last_failure is not observed_failure or relay.context_required
                        or (limit and self.model_calls >= limit)):
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
                        # Revoked access and invalid replies cannot authorize a
                        # live continuation or a fresh cold recovery window.
                        self.boundary_failed = True
                        self.boundary_reason = 'broker readiness could not be verified'
                        return False
                    probe = None
                    if ready:
                        return True
                    retry_at = loop.time() + 1
                await asyncio.sleep(min(0.05, remaining))
        finally:
            if probe is not None:
                probe.cancel()
                await asyncio.gather(probe, return_exceptions=True)

    async def _run(self, prompt, system_message):
        from openai_codex.async_client import AsyncCodexClient
        from openai_codex.errors import InvalidRequestError
        # Each fresh SDK invocation has its own local ceiling. The gateway and
        # shared recovery loop retain the whole task's request cap and deadline.
        self.model_calls = 0
        self.calls.clear()
        self.completed.clear()
        self.observed_outputs.clear()
        self.journal.call_namespace = uuid4().hex
        self.inputs = HarnessInputs(self.journal)
        message_items = {}
        finished, answer = False, ''
        failure = {}
        settlement_started = False
        prior_turns = set()
        compaction_progress, compaction_attempts = 0, 0
        late_inputs = []
        steered = False
        notification = None
        # Native transcripts are private, disposable state. Only Moyai's public
        # journal crosses requester/model changes and filesystem checkpoints.
        try:
            with tempfile.TemporaryDirectory(prefix='moyai-codex-') as home:
                async with asyncio.timeout(getattr(self, 'context_timeout', self.context.spec.get('timeout')) or None):
                    async with AsyncCodexClient(self.sdk_config(home)) as client:
                        await client.initialize()
                        thread = await client.thread_start({
                            'model': self.context.spec['model'].removeprefix('openai/'), 'modelProvider': 'moyai',
                            'cwd': self.context.cwd, 'ephemeral': True,
                            'developerInstructions': system_message + '\nUse Moyai MCP tools for authorized app actions. '
                                'Do not start detached work. Moyai owns credentials, memory, skills and delegation.',
                            'approvalPolicy': 'never', 'sandbox': 'danger-full-access',
                            # The pinned SDK exposes this native app-server field
                            # through dict params. Only output IDs are consumed.
                            'experimentalRawEvents': True})
                        turn = await client.turn_start(thread.thread.id, prompt)
                        while True:
                            notification = asyncio.create_task(client.next_turn_notification(turn.turn.id))
                            while True:
                                # Live commands retain their originating turn ID,
                                # including across multiple in-place compactions.
                                await self.drain_tool_events(client, prior_turns)
                                corrections = self.inputs.take()
                                if corrections:
                                    try:
                                        await client.turn_steer(thread.thread.id, turn.turn.id, '\n\n'.join(corrections))
                                        steered = True
                                    except InvalidRequestError as exc:
                                        # The native turn can finish before its notification
                                        # is read. Only a definite non-delivery permits a
                                        # new turn on this same live thread; other faults
                                        # keep the task incomplete with journaled input.
                                        if exc.message != 'no active turn to steer':
                                            raise
                                        late_inputs.extend(corrections)
                                done, _ = await asyncio.wait({notification}, timeout=0.05)
                                if done:
                                    break
                            event = await notification
                            notification = None
                            payload = (event.payload.params if hasattr(event.payload, 'params') else
                                       event.payload.model_dump(mode='json', by_alias=True))
                            if event.method == 'rawResponseItem/completed':
                                self.record_tool_event(event.method, payload)
                            elif event.method in {'item/started', 'item/completed'}:
                                item = payload['item']
                                if item['type'] == 'userMessage':
                                    # Native incorporation, rather than arrival time,
                                    # fences final candidates from before a correction.
                                    message_items.clear()
                                elif item['type'] == 'agentMessage' and event.method == 'item/completed':
                                    if item['id'] in message_items:
                                        continue
                                    message_items[item['id']] = item
                                    text = item.get('text', '')
                                    if text and item.get('phase') != 'final_answer':
                                        self.journal.finish(text)
                                        self.context.activity.commentary(text)
                                else:
                                    self.record_item(item, completed=event.method == 'item/completed')
                            elif event.method == 'error':
                                failure.update(codex_details(payload.get('error'), will_retry=payload.get('willRetry')))
                            elif event.method == 'turn/completed':
                                finished = payload['turn']['status'] == 'completed'
                                if not finished:
                                    failure['native_status'] = (payload['turn']['status']
                                        if payload['turn']['status'] in {'failed', 'interrupted'} else 'unknown')
                                    if payload['turn'].get('error'):
                                        failure.update(codex_details(payload['turn']['error']))
                                relay = self.context.relay
                                if failure.get('code') == 'responseStreamDisconnected':
                                    relay.note_stream_disconnect()
                                failed_request = getattr(relay, 'last_failure', None)
                                if (payload['turn']['status'] == 'failed' and not self.stopped.is_set()
                                        and not self.boundary_failed and not getattr(relay, 'uncertain_tool', False)
                                        and not getattr(relay, 'context_required', None)
                                        and retryable_failure(failed_request, live=True)
                                        and self.transport_attempt < MAX_TRANSPORT_ATTEMPTS):
                                    # Preserve the native thread and its running tools. A
                                    # preview server cannot settle merely by waiting before
                                    # a cold restart; the live agent must inspect/stop it.
                                    self.transport_attempt += 1
                                    self.context.activity.emit('status',
                                        'Reconnecting to continue with the existing tools and saved results.',
                                        {'activity_version': 1, 'phase': 'reconnecting', 'stage': 'model_transport'})
                                    await asyncio.sleep(2 ** self.transport_attempt)
                                    if (await self.wait_for_model(client, [turn.turn.id, *prior_turns], failed_request)
                                            and relay.resume_model(failed_request, live=True)):
                                        self.context.activity.emit('status', 'Connection restored. Continuing the task.',
                                            {'activity_version': 1, 'phase': 'recovered', 'stage': 'model_transport'})
                                        prior_turns.add(turn.turn.id)
                                        message_items.clear()
                                        failure.clear()
                                        late_inputs.extend(self.inputs.take())
                                        turn = await self.await_with_tool_events(client, prior_turns,
                                            client.turn_start(thread.thread.id,
                                            'The model connection was interrupted. Continue the same task '
                                            'using this thread and its existing tool sessions. Do not restart or '
                                            'replay actions. Inspect pending tools, wait for finite work, and stop '
                                            'preview servers you no longer need. Collect actual results before '
                                            'answering.\n\n' + '\n\n'.join(late_inputs)))
                                        late_inputs.clear()
                                        steered = False
                                        continue
                                if (getattr(self.context.relay, 'context_required', None)
                                        and not self.stopped.is_set() and not self.boundary_failed):
                                    finished = False
                                    progress = self.journal.completed_tools
                                    compaction_attempts = compaction_attempts + 1 if progress == compaction_progress else 1
                                    compaction_progress = progress
                                    prior_turns.add(turn.turn.id)
                                    if compaction_attempts <= 3 and await self.compact_context(client, thread.thread.id, prior_turns):
                                        self.context.relay.context_required = None
                                        message_items.clear()
                                        failure.clear()
                                        late_inputs.extend(self.inputs.take())
                                        turn = await self.await_with_tool_events(client, prior_turns,
                                            client.turn_start(thread.thread.id, '\n\n'.join(late_inputs) or
                                            'Continue the unfinished task after compaction. Existing commands are still '
                                            'owned by this thread. Collect their results without restarting or replaying actions.'))
                                        late_inputs.clear()
                                        steered = False
                                        continue
                                if ((not finished or getattr(relay, 'context_required', None))
                                        and self.journal.pending):
                                    finished = False
                                    await self.settle_pending_receipts(client, [turn.turn.id, *prior_turns])
                                    break
                                if (finished and self.journal.pending and not settlement_started
                                        and not self.stopped.is_set() and not self.boundary_failed):
                                    # One settlement turn shares this invocation's
                                    # deadline and model-call cap. Never publish an
                                    # answer that preceded its tool results.
                                    settlement_started = True
                                    prior_turns.add(turn.turn.id)
                                    message_items.clear()
                                    turn = await self.await_with_tool_events(client, prior_turns,
                                        client.turn_start(thread.thread.id,
                                        'Unfinished tool calls remain. Settle them before answering: '
                                        'wait for finite work, or deliberately stop preview servers you no longer need. '
                                        'Collect their actual results; do not restart or replay actions. '
                                        'Then provide the final answer based on the confirmed results.'))
                                    steered = False
                                    continue
                                if finished and not self.stopped.is_set() and not self.boundary_failed and not self.journal.pending:
                                    late_inputs.extend(self.inputs.take())
                                    # Native completion can record a late accepted
                                    # steer without sampling again. Its user item
                                    # clears old candidates; continue from that
                                    # history instead of resending accepted input.
                                    awaiting_answer = steered and not message_items
                                    # Admission and finalization share the inbox lock.
                                    # A racing accepted message stays in this SDK session.
                                    if late_inputs or awaiting_answer or not self.inputs.close_if_empty():
                                        late_inputs.extend(self.inputs.take())
                                        message_items.clear()
                                        turn = await self.await_with_tool_events(client, prior_turns,
                                            client.turn_start(thread.thread.id, '\n\n'.join(late_inputs) or
                                            'Continue from the latest user correction already in this conversation. '
                                            'Preserve completed work and answer the updated request.'))
                                        steered = False
                                        late_inputs.clear()
                                        continue
                                break
        except Exception as exc:
            finished = False
            failure.update(exception_details(exc))
        finally:
            self.inputs.close()
            if notification is not None:
                notification.cancel()
                await asyncio.gather(notification, return_exceptions=True)
        interrupted = self.stopped.is_set()
        completed = finished and not interrupted and not self.boundary_failed and not self.journal.pending
        if completed:
            final_text = [item['text'] for item in message_items.values()
                          if item.get('phase') == 'final_answer' and item.get('text')]
            for text in final_text:
                self.journal.finish(text)
            # Presentation is derived from native items, never another event.
            # Commentary already belongs to the journal; only confirmed final
            # items are committed here, once per native identity, not per text.
            answer = '\n'.join(final_text or [item['text'] for item in message_items.values() if item.get('text')])
        diagnostic = failure_diagnostic(self, 'codex', failure) if not completed and not interrupted else None
        return {'completed': bool(completed), 'interrupted': interrupted,
                'failed': not completed and not interrupted, 'messages': self.journal.messages,
                **({'sdk_failure': diagnostic} if diagnostic else {}),
                'final_response': (answer if completed else failure_summary(diagnostic) if diagnostic else
                    'Codex stopped before completing the response. Saved tool receipts are preserved.')}

    def close(self):
        if self.inputs is not None:
            self.inputs.close()
        self.context.relay.before_model = None
        self.context.relay.context_recovery = False
