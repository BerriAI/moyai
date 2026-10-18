"""Rotate Slack credentials once and persist them before honoring cancellation."""

import asyncio
import json
import time

from .connection_authority import authority, refresh_connection
from .connector_errors import ConnectorError


def refresh_slack_token(connectors, original, *, bot=False):
    """Called through drained database work, with no database handle on the event loop.

    Acquire write ownership before consuming a rotating provider credential.
    The provider-scoped writer shares canonical admission's read lock, so a
    browser awaiting this refresh cannot block its own credential persistence.
    It also serializes user/bot refreshes across processes. Revocation retains
    the exclusive canonical writer lock and the saved installation epoch.
    """
    previous = json.loads(connectors.security.decrypt(original['encrypted']))
    with connectors.store.connect(write_scope='connection-refresh:slack') as connection:
        connection.begin_write()
        row = connection.execute("SELECT * FROM connections WHERE provider='slack'").fetchone()
        if row is None:
            raise ConnectorError('The Slack connection changed.', code='access_changed', dispatched=False)
        row = dict(row)
        credentials = json.loads(connectors.security.decrypt(row['encrypted']))
        if authority(credentials, row['encrypted']) != authority(previous, original['encrypted']):
            raise ConnectorError('The Slack connection changed.', code='access_changed', dispatched=False)
        selected = credentials.get('bot', {}) if bot else credentials
        if selected.get('expires_at', 0) and selected['expires_at'] < time.time() + 90:
            if not selected.get('refresh_token'):
                raise ConnectorError('The Slack connection expired. Reconnect Slack.', dispatched=False)
            # The caller drains this entire operation. A browser timeout must
            # not discard the replacement after Slack consumes the old token.
            fresh = asyncio.run(connectors.exchange('slack', refresh_token=selected['refresh_token']))
            if bot:
                credentials['bot'] = {**selected, **fresh}
            else:
                if credentials.get('bot') and not fresh.get('bot'):
                    fresh['bot'] = credentials['bot']
                credentials = fresh
            credentials = refresh_connection(
                connectors.store, connectors.security, 'slack', row, credentials, connection=connection,
            )
        return credentials
