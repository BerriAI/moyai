"""Bot-only message operations. Personal/shared user read tokens never enter here."""
from .connector_errors import ConnectorError
from .connector_schemas import SlackEdit, SlackMessage, SlackReaction, SlackReply
from .slack_origin import metadata

TOOLS = {
    'slack_message': ('slack', False, SlackMessage, "Read one exact bot-accessible message and reactions for reconciliation. Requires bot reactions:read. Uses only the installed bot, never a personal/shared user token. A not-found response is not proof of deletion."),
    'slack_reply': ('slack', True, SlackReply, "Reply as Moyai to an explicitly identified root message in its channel. Verifies the root using the bot first; requires reactions:read and chat:write. Adds requester attribution. Long replies remain in the original thread. Reconcile uncertain parts with slack_thread(as_bot=true)."),
    'slack_edit_message': ('slack', True, SlackEdit, "Edit one message authored by the installed Moyai bot, verified by exact channel, timestamp and author. Requires reactions:read and chat:write. Replaces text with requester attribution; removes existing blocks/attachments. Read slack_message to reconcile before retrying."),
    'slack_delete_message': ('slack', True, SlackMessage, "Delete one exact message authored by the installed Moyai bot after checking channel, timestamp and author. Requires reactions:read and chat:write. No user-token fallback. Reconcile uncertain deletion with slack_message; inaccessible is not proof of deleted."),
    'slack_reaction': ('slack', True, SlackReaction, "Add or remove the installed bot's reaction on an exact bot-accessible message. Requires reactions:read and reactions:write. Never removes another user's reaction. Read slack_message to reconcile; already present/absent is reported explicitly."),
}
READS = frozenset({'slack_message'})


async def call(connectors, name, args, run):
    write = TOOLS[name][1]
    if write:
        connectors.slack_sender_name(run)
    token = await connectors.slack_bot_token()
    revision = connectors.connection_revision('slack')
    installation = connectors.slack_installation()
    scopes = set(installation.get('scopes', []))
    required = {'reactions:read'}
    if write:
        required.add('reactions:write' if name == 'slack_reaction' else 'chat:write')
    if not required <= scopes:
        raise ConnectorError(f"Reconnect the Moyai Slack bot with {', '.join(sorted(required - scopes))}.",
                             code='missing_scope', dispatched=False)
    if not installation.get('user_id') or not installation.get('team_id'):
        raise ConnectorError('Reconnect Slack to identify the installed bot and workspace.', dispatched=False)
    headers = {'Authorization': f'Bearer {token}'}
    ts = args.get('ts') or args['thread_ts']
    connectors.guard(name, run, revision)
    try:
        current = await connectors.request('GET', 'https://slack.com/api/reactions.get', headers=headers,
            params={'channel': args['channel'], 'timestamp': ts, 'full': True})
    except ConnectorError as exc:
        if not write:
            raise
        raise ConnectorError('Could not verify the target message. No message mutation was sent.',
                             code=exc.code, dispatched=False, retryable=exc.retryable,
                             status_code=exc.status_code, retry_after=exc.retry_after) from exc
    connectors.guard(name, run, revision)
    message = current.get('message') or {}
    if (not isinstance(message, dict) or current.get('type') != 'message'
            or current.get('channel') != args['channel'] or message.get('ts') != ts):
        raise ConnectorError('Slack did not verify the exact channel and message timestamp.',
                             code='message_mismatch', dispatched=False)
    if not write:
        return current
    if name in {'slack_edit_message', 'slack_delete_message'} and message.get('user') != installation['user_id']:
        raise ConnectorError('Only messages authored by this installed Moyai bot can be edited or deleted.',
                             code='not_bot_message', dispatched=False)
    if name == 'slack_reply':
        if message.get('thread_ts', ts) != ts:
            raise ConnectorError('Use the thread root timestamp, not a reply timestamp.', dispatched=False)
        return await connectors.slack_send(args, run, thread_ts=ts, operation=name, revision=revision)
    payload = {'channel': args['channel'], 'ts': ts}
    allowed_errors = ()
    if name == 'slack_reaction':
        endpoint = 'reactions.' + args['action']
        payload = {'channel': args['channel'], 'timestamp': ts, 'name': args['name']}
        allowed_errors = ('already_reacted',) if args['action'] == 'add' else ('no_reaction',)
    elif name == 'slack_edit_message':
        endpoint = 'chat.update'
        text = f"{connectors.slack_sender_name(run)}: {args['text']}"
        if len(text) > 40000:
            raise ConnectorError('The attributed message exceeds Slack’s 40,000 character limit.', dispatched=False)
        payload.update(text=text, blocks=[], attachments=[], metadata=metadata(connectors, args['channel'], text))
    else:
        endpoint = 'chat.delete'
    connectors.guard(name, run, revision)
    result = await connectors.request('POST', f'https://slack.com/api/{endpoint}',
                                      headers=headers, json=payload, allowed_errors=allowed_errors)
    if name != 'slack_reaction' and (result.get('ok') is not True
                                   or result.get('channel') != args['channel'] or result.get('ts') != ts):
        raise ConnectorError('Slack did not confirm the expected message mutation. Read the destination before retrying.',
                             code='missing_receipt', dispatched=True)
    if name == 'slack_reaction':
        if result.get('ok') is not True and result.get('error') not in allowed_errors:
            raise ConnectorError('Slack did not confirm the reaction. Read the message before retrying.',
                                 code='missing_receipt', dispatched=True)
        return {'ok': True, 'channel': args['channel'], 'ts': ts, 'name': args['name'],
                'action': args['action'], 'already_in_state': result.get('error') in allowed_errors}
    return result
