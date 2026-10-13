"""Read-only explanations of recorded facts, never recovery authorization.

Older records stay readable. Missing evidence is unknown, not a negative receipt.
No error prose, provider body, tool input/output or stack is used for classification.
"""
from agent.activity import public_text
from sandbox.broker_failure import safe_error, safe_id, safe_recovery as recovery_record


CATEGORIES = {
    'execution': 'Agent execution',
    'no-answer': 'Response completion',
    'checkout': 'Repository checkout',
    'slack-delivery': 'Slack delivery',
    'artifact-path': 'Artifact transfer',
    'capture-persistence': 'Saved media',
    'attachment': 'Attachment reading',
    'private-export': 'Private-session export',
    'checkpoint': 'Conversation checkpoint',
    'invalid-request': 'Model request',
    'provider-policy': 'Provider policy',
    'github-access': 'GitHub access',
    'unknown': 'Operation',
}
TOOLS = {
    'github_checkout': 'checkout', 'github_checkout_pull_request': 'checkout',
    'slack_send': 'slack-delivery', 'slack_upload_file': 'slack-delivery',
    'artifact_publish': 'artifact-path', 'artifact_fetch': 'artifact-path',
    'artifact_read': 'artifact-path',
    'media_list': 'capture-persistence', 'media_share': 'capture-persistence',
    'slack_file': 'attachment', 'slack_file_read': 'attachment',
}
STAGES = {
    'runtime_entrypoint': 'Agent entrypoint — exact operation not recorded',
    'model_metadata': 'Reading gateway model metadata',
    'tool_catalog': 'Reading the tool catalog', 'tool_activity': 'Recording tool activity',
    'tool_dispatch': 'Calling the tool', 'computer_poll': 'Reading the computer receipt',
    'computer_step': 'Preparing or running computer work', 'tool_receipt': 'Saving the tool receipt',
    'tool_journal': 'Saving conversation history', 'supervisor_launch': 'Starting execution',
    'supervisor_events': 'Reading execution events', 'supervisor_wait': 'Waiting for execution',
    'remote_setup': 'Setting up the workspace', 'remote_prepare': 'Preparing the workspace',
    'remote_finish': 'Finishing workspace work', 'remote_activate': 'Activating the workspace',
    'remote_attachments': 'Preparing attachments', 'remote_dispatch': 'Calling the workspace tool',
    'remote_result': 'Reading the workspace result', 'remote_cleanup': 'Cleaning up the workspace',
    'validation': 'Validating the request', 'context_preparation': 'Preparing model context',
    'admission': 'Waiting for model capacity', 'upstream': 'Calling the model provider',
    'stream': 'Receiving the model response',
    'deepagents.invoke_model': 'Calling the model', 'deepagents.invoke_tool': 'Running a tool',
    'deepagents_prepare_v1': 'Preparing the response', 'deepagents_boundary_v1': 'Checking response state',
    'deepagents_publish_v1': 'Saving the response', 'deepagents_computer_v1': 'Running computer work',
}
CAUSES = {
    'cyber_policy': 'The provider reported a cybersecurity policy rejection.',
    'cyberPolicy': 'The native SDK reported a cybersecurity policy rejection.',
    'content_policy_violation': 'The provider reported a content policy rejection.',
    'invalid_request': 'The SDK classified the request as invalid; the rejected field was not recorded.',
    'invalid_request_error': 'The provider classified the request as invalid; the rejected field was not recorded.',
    'context_length_exceeded': 'The provider reported that the input exceeded its context limit.',
    'authentication_error': 'The provider reported an authentication failure.',
    'authentication_failed': 'The SDK reported an authentication failure.',
    'permission_error': 'The provider reported a permission failure.',
    'insufficient_quota': 'The provider reported insufficient quota.',
    'stream_incomplete': 'The model stream ended without a completed response.',
    'ModelResponseIncomplete': 'The model did not confirm a completed response.',
    'ContextImportIncomplete': 'The durable conversation import needs its original checkpoint.',
    'PrivateExportDenied': 'The private-session export guard rejected the operation.',
}
EXCEPTIONS = {
    'ApplicationError', 'BrokerRejected', 'CapabilityExpired', 'CancelledError', 'ConnectionError',
    'ConnectError', 'ReadTimeout', 'TimeoutError', 'HTTPError', 'HTTPException', 'HTTPStatusError',
    'ValueError', 'TypeError', 'RuntimeError', 'OSError', 'PermissionError', 'FileNotFoundError',
    'DatabaseBusyError', 'DatabaseError', 'InternalRpcError', 'RemoteProtocolError',
    'UncertainToolOutcome', 'ModelRequestFailed', 'ContextUnavailable', 'TurnDeadline',
}
OUTCOMES = {'completed', 'failed', 'rejected', 'cancelled', 'unknown'}
SLACK_STAGES = {
    'access': 'Checking attachment access', 'metadata': 'Reading attachment metadata',
    'headers': 'Validating download headers', 'download': 'Reading attachment bytes',
    'retry_wait': 'Waiting before another read', 'inspection': 'Inspecting the complete attachment',
}
SLACK_CAUSES = {
    'access_changed': 'Attachment access changed during the read.',
    'authentication': 'Slack attachment authentication failed.',
    'missing_scope': 'Slack reported a missing permission scope.',
    'unavailable': 'The attachment was unavailable.',
    'invalid_identifier': 'The attachment identifier failed validation.',
    'invalid_size': 'The declared attachment size was invalid.',
    'hard_limit': 'A recorded size exceeded the independent storage limit.',
    'unsupported_location': 'The download location was not supported.',
    'redirect': 'The download returned a redirect.',
    'encoded_response': 'The download did not confirm the requested identity encoding.',
    'invalid_headers': 'The download headers failed validation.',
    'partial_response': 'The server returned a partial response.',
    'metadata_size_mismatch': 'The advertised or received size disagreed with current metadata. The cause of the disagreement is unknown.',
    'truncated_stream': 'The HTTP client rejected incomplete or invalid body framing.',
    'rate_limited': 'Slack reported a rate limit.',
    'transient_http': 'A retry-eligible HTTP failure was observed.',
    'timeout': 'The attachment read timed out.',
    'read_budget': 'The bounded read budget was exhausted.',
    'transport': 'The attachment transport failed; the underlying cause is unknown.',
    'tls': 'The attachment TLS connection failed.',
    'storage': 'Storing the attachment failed.',
    'inspection_timeout': 'Attachment inspection timed out.',
    'credential_upstream': 'Slack credential acquisition received an upstream HTTP error; its underlying cause is unknown.',
    'credential_transport': 'Slack credential acquisition failed to connect before dispatch.',
    'credential_unconfirmed': 'The result of Slack credential acquisition could not be confirmed.',
    'credential_unavailable': 'Slack bot credentials could not be obtained; the underlying cause is unknown.',
}
SLACK_CREDENTIAL_CODES = ('credential_upstream', 'credential_transport',
                          'credential_unconfirmed', 'credential_unavailable')
SLACK_ENCODINGS = {'identity', 'gzip', 'br', 'deflate', 'zstd', 'unsupported'}


def explain(data):
    data = data if isinstance(data, dict) else {}
    diagnostic = safe_error(data.get('diagnostic'))
    facts = {**diagnostic, **safe_error(data)}
    category = 'unknown'
    tool = data.get('tool')
    if isinstance(tool, str):
        category = TOOLS.get(tool, 'unknown')
        if tool.startswith('github_') and facts.get('http_status') in {401, 403}:
            category = 'github-access'
    code = next((facts[key] for key in ('error_code', 'sdk_error', 'code')
                 if facts.get(key) and facts[key] != 'unknown'), None)
    exception = facts.get('exception_type')
    missing_final = (data.get('phase') == 'response_completion' and data.get('code') == 'missing_final_answer'
                     and data.get('completed_marker') is True and data.get('final_text_present') is False)
    if code in {'cyber_policy', 'cyberPolicy', 'content_policy_violation'}:
        category = 'provider-policy'
    elif code in {'invalid_request', 'invalid_request_error'}:
        category = 'invalid-request'
    elif exception == 'PrivateExportDenied':
        category = 'private-export'
    elif exception == 'ContextImportIncomplete':
        category = 'checkpoint'
    elif missing_final or exception == 'ModelResponseIncomplete' or code == 'stream_incomplete':
        category = 'no-answer'
    elif category == 'unknown' and data.get('phase') in ('sdk_failure', 'supervisor_failure'):
        category = 'execution'
    observed = CAUSES.get(code) or CAUSES.get(exception)
    if not observed:
        details = []
        if exception in EXCEPTIONS:
            details.append('Exception type: ' + exception + '.')
        status = facts.get('http_status') or facts.get('upstream_status')
        if status and status >= 400:
            details.append(f'HTTP {status} was recorded.')
        observed = ' '.join([*details, 'Underlying cause unknown.'])
    activity = data.get('activity_type')
    stage = (STAGES.get(facts.get('failure_stage')) or
             STAGES.get(activity if isinstance(activity, str) else '') or STAGES.get(facts.get('stage')))
    if missing_final:
        observed = 'The runtime marked completion but supplied no final answer text. The underlying cause is unknown.'
        stage = 'Receiving the final answer'
    action = data.get('action') if isinstance(data.get('action'), dict) else {}
    dispatched = action.get('dispatched') if type(action.get('dispatched')) is bool else None
    outcome = action.get('outcome')
    outcome = outcome if isinstance(outcome, str) and outcome in OUTCOMES else 'unknown'
    if data.get('outcome_type') == 'UncertainToolOutcome' and outcome != 'completed':
        outcome = 'unknown'
    decision, next_action = 'unknown', 'Inspect saved receipts and the destination before deciding whether to retry.'
    if outcome == 'completed':
        decision, next_action = 'do_not_repeat', 'Keep the confirmed result. Check the later recording failure; do not repeat the action.'
    elif outcome == 'unknown' and (dispatched is True or data.get('outcome_type') == 'UncertainToolOutcome'
            or data.get('uncertain_tool') is True
            or type(data.get('pending_tools')) is int and data['pending_tools'] > 0):
        decision, next_action = 'blocked_unknown_outcome', 'Verify the destination and saved receipts. An unknown action outcome must not be replayed automatically.'
    elif category == 'provider-policy':
        decision, next_action = 'not_retryable', 'Review the provider policy response. Repeating the same request does not resolve a policy rejection.'
    elif category == 'checkpoint':
        decision, next_action = 'not_retryable', 'Restore the original matching checkpoint to finish the import. Do not clear or mark the incomplete import as complete.'
    elif category == 'private-export':
        decision = 'not_retryable'
        next_action = ('Continue without exporting. Use the recorded stage and support IDs to inspect why the export guard was called. '
                       'If it was ordinary answer saving, a product fix is needed; private export is not a grantable permission.')
    elif category == 'github-access':
        decision, next_action = 'not_retryable', 'Resolve the recorded access restriction through the supported permissions flow before continuing.'
    elif category == 'invalid-request':
        decision, next_action = 'not_retryable', 'Use the support identifiers to inspect the rejected request before changing or retrying it.'
    elif action.get('retryable') is False or data.get('non_retryable') is True:
        decision = 'not_retryable'
    elif action.get('retryable') is True:
        decision, next_action = 'reported_retryable', 'The tool reported retry eligibility. Verify its receipt and use only its supported recovery path.'
    recovery = recovery_record(data.get('recovery')) or facts.get('recovery')
    if (decision in {'unknown', 'reported_retryable'} and recovery
            and recovery['decision'] in {'exhausted', 'deadline'}):
        decision = 'bounded_recovery_stopped'
        next_action = 'The recorded recovery limit was reached. Check service readiness and saved receipts before requesting more work.'
    measurements, representation = {}, {}
    component = None
    if data.get('phase') == 'slack_attachment_read':
        category = 'attachment'
        slack_code, slack_stage = data.get('code'), data.get('stage')
        observed = SLACK_CAUSES.get(slack_code if isinstance(slack_code, str) else '', 'Underlying cause unknown.')
        stage = SLACK_STAGES.get(slack_stage if isinstance(slack_stage, str) else '')
        next_action = 'Upload the attachment again or describe its contents; do not rely on incomplete bytes.'
        decision = 'unknown'
        if slack_code in ('authentication', 'missing_scope', 'access_changed'):
            decision = 'not_retryable'
            next_action = 'Restore the required Slack access through Connections, then retry the attachment.'
        attempts = data.get('attempts')
        if type(attempts) is int and 0 <= attempts <= 10000:
            recovery = {'method': 'attachment_read', 'decision': 'deadline' if slack_code == 'read_budget' else 'unknown',
                        'attempts': attempts}
        if slack_code in SLACK_CREDENTIAL_CODES:
            component = 'Slack credential acquisition'
            if slack_stage == 'access':
                stage = 'Obtaining Slack credentials'
            if slack_code == 'credential_upstream' and facts.get('http_status'):
                observed += f' HTTP {facts["http_status"]} was recorded.'
            if outcome == 'completed':
                decision = 'do_not_repeat'
                next_action = 'Keep the confirmed receipt and inspect the later credential failure. Do not repeat a confirmed credential operation.'
            else:
                decision = 'credential_replay_blocked'
                next_action = ('Inspect saved Slack connection state before retrying credential acquisition. '
                               'It may refresh a token; attachment recovery must not automatically replay that request.')
                if slack_code == 'credential_unconfirmed':
                    outcome = 'unknown'
                if slack_code == 'credential_transport' and dispatched is None and outcome == 'unknown':
                    dispatched, outcome = False, 'failed'
                recovery = {**(recovery or {'method': 'attachment_read'}), 'decision': 'credential_replay_blocked'}
        # Exact application-owned stop explanations from the optional producer;
        # arbitrary provider/error prose never enters the rendered explanation.
        blockers = {
            'Slack did not supply a usable Retry-After interval; no early retry was made.': 'retry_after_unavailable',
            'Slack’s retry interval exceeds the remaining read budget; no early retry was made.': 'retry_after_budget',
            'The remaining read budget is too short for another attempt.': 'deadline',
        }
        blocker = data.get('remaining_blocker')
        if isinstance(blocker, str) and blocker in blockers and slack_code not in SLACK_CREDENTIAL_CODES:
            recovery = {**(recovery or {'method': 'attachment_read'}), 'decision': blockers[blocker]}
        for field in ('declared_bytes', 'content_length_bytes', 'received_bytes'):
            number = data.get(field)
            if type(number) is int and 0 <= number <= 2**53 - 1:
                measurements[field] = number
        delay = data.get('retry_after_seconds')
        if type(delay) in (int, float) and 0 <= delay <= 2**31 - 1:
            measurements['retry_after_seconds'] = delay
        encoding = data.get('content_encoding')
        if isinstance(encoding, str) and encoding in SLACK_ENCODINGS:
            representation['content_encoding'] = encoding
        if type(data.get('content_encoding_present')) is bool:
            representation['content_encoding_present'] = data['content_encoding_present']
    if data.get('stage') == 'capture_sync' and data.get('status') == 'unconfirmed':
        category, stage = 'capture-persistence', 'Copying the completed capture to the app'
        observed = 'The capture completed, but its copy to the app could not be confirmed. Underlying copy failure cause unknown.'
        dispatched, outcome, decision = True, 'completed', 'do_not_repeat'
        next_action = 'Do not repeat the capture. Keep the original receipt; normal computer polling or workspace saving can retry the copy. Use media_list to confirm.'
    if facts.get('failure_stage') == 'model_metadata' and facts.get('upstream_status') in {401, 403}:
        category, decision = 'execution', 'not_retryable'
        upstream = facts['upstream_status']
        observed = (f'The gateway rejected model metadata access with HTTP {upstream}. '
                    f'The broker reported HTTP {facts.get("http_status", 503)}. '
                    'The underlying reason for the access rejection was not recorded.')
        next_action = 'Have an administrator verify the configured gateway credentials and access to model metadata before retrying.'
    support = {}
    for key in ('request_id', 'broker_request_id', 'model_request_id'):
        value = safe_id(facts.get(key))
        if value and '/' not in value and public_text(value) == value:
            support[key] = value
    for key, value in facts.get('request_ids', {}).items():
        if '/' not in value and public_text(value) == value:
            support[key] = value
    return {'version': 1, 'category': category, 'component': component or CATEGORIES[category],
            'observed_cause': observed, 'stage': stage or 'Unknown — stage not recorded.',
            'action': {'dispatched': dispatched, 'outcome': outcome},
            'retryability': decision, 'recovery': recovery,
            'next_action': next_action, 'support': support, 'measurements': measurements,
            **({'representation': representation} if representation else {})}


def project_event(event):
    """Attach a computed projection; never trust a stored explanation payload."""
    event = dict(event)
    event.pop('failure_explanation', None)
    data = event.get('data') or {}
    if not isinstance(data, dict):
        return event
    phase = data.get('phase')
    if (event.get('kind') == 'error' and (phase in (
            'sdk_failure', 'broker_failure', 'tool_failure', 'supervisor_failure', 'slack_attachment_read', 'response_completion')
            or data.get('stage') == 'capture_sync' and data.get('status') == 'unconfirmed')
            or event.get('kind') == 'tool' and phase == 'error'):
        event['failure_explanation'] = explain(data)
    return event
