"""Bounded public conversation with a durable, commit-safe delivery sequence.

Source IDs may be allocated before another transaction commits, so they cannot
be stream watermarks. A room-scoped projector assigns delivery IDs only after
public source rows are visible. Current subtree permissions still apply to every
read; the projection stores identifiers, never another copy of message content.
"""
import base64
import binascii
import json
import re
from collections.abc import Callable

from fastapi import HTTPException


MAX_PAGE_SIZE = 100
MAX_CONTENT_CHARACTERS = 16000
MAX_LIVE_ITEMS = 100
PROJECTION_BATCH_SIZE = 500
MAX_PROJECTION_BATCHES = 4
PREPARING_HISTORY = 'Preparing saved team messages. Please try again shortly.'
_CURSOR_VERSION = 2

_TREE = '''WITH RECURSIVE members(id) AS (
    SELECT id FROM runs WHERE id=? AND deleted_at='' AND deletion_requested_at=''
    UNION SELECT r.id FROM runs r JOIN members m ON r.parent_run_id=m.id
        WHERE r.deleted_at='' AND r.deletion_requested_at=''
)'''
_MESSAGES = '''SELECT 'message' AS source,m.id AS source_id,m.created_at,m.run_id
    FROM messages m JOIN members tree ON tree.id=m.run_id
    WHERE m.status NOT IN ('deleted','steered') AND trim(m.content)!=''
      AND (m.role='assistant' OR (m.role='user' AND m.run_id=?
        AND m.input_kind='user' AND COALESCE(m.client_id,'') NOT LIKE 'swarm:%'))'''
_POSTS = '''SELECT 'board' AS source,p.id AS source_id,p.created_at,p.member_id AS run_id
    FROM swarm_posts p JOIN agent_groups g ON g.id=p.group_id
    JOIN members parent ON parent.id=g.parent_id
    JOIN runs author ON author.id=p.member_id AND author.parent_run_id=g.parent_id
        AND author.agent_group_id=g.id
    JOIN members member ON member.id=author.id
    WHERE p.kind IN ('message','leadership','assignment')
        AND (p.target_id='' OR EXISTS(SELECT 1 FROM members target WHERE target.id=p.target_id))'''
_TIMELINE = _TREE + ', timeline AS (' + _MESSAGES + ' UNION ALL ' + _POSTS + ')'

_DELIVERY_TIMELINE = _TIMELINE + ''', delivered AS (
    SELECT timeline.source,timeline.source_id,entry.created_at,timeline.run_id,entry.id AS delivery_id FROM timeline
    JOIN swarm_chat_entries entry ON entry.source=timeline.source AND entry.source_id=timeline.source_id
    WHERE entry.root_id=?
)'''


def initialize_schema(store):
    store.execute('''CREATE TABLE IF NOT EXISTS swarm_chat_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        root_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        source TEXT NOT NULL CHECK(source IN ('message','board')),
        source_id INTEGER NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(root_id,source,source_id))''')
    store.execute('CREATE INDEX IF NOT EXISTS idx_swarm_chat_delivery ON swarm_chat_entries(root_id,id)')
    store.execute('CREATE INDEX IF NOT EXISTS idx_swarm_chat_history ON swarm_chat_entries(root_id,created_at,source,source_id)')


def _require_root(conn, root: str):
    run = conn.execute('SELECT parent_run_id,deleted_at,deletion_requested_at FROM runs WHERE id=?', (root,)).fetchone()
    if (not run or run['parent_run_id'] or run['deleted_at'] or run['deletion_requested_at']
            or not conn.execute('SELECT 1 FROM swarm_missions WHERE run_id=?', (root,)).fetchone()):
        raise HTTPException(404, 'Swarm conversation not found.')


_UNSEEN = ''' FROM timeline WHERE NOT EXISTS(SELECT 1 FROM swarm_chat_entries entry WHERE entry.root_id=?
    AND entry.source=timeline.source AND entry.source_id=timeline.source_id)'''


def _has_unprojected(conn, root: str) -> bool:
    return conn.execute(_TIMELINE + ' SELECT 1' + _UNSEEN + ' LIMIT 1', (root, root, root)).fetchone() is not None


def _project(store, root: str):
    # Each batch holds only this room's projection lock, never a member's write
    # lock. Writers in this and other rooms can commit while we discover keys.
    # Do not replace NOT EXISTS with a source-ID watermark: a lower allocated
    # ID can become visible after a higher ID on PostgreSQL.
    for _ in range(MAX_PROJECTION_BATCHES):
        with store.connect(write_scope='swarm-chat:' + root) as conn:
            conn.begin_write()
            _require_root(conn, root)
            # Avoid marking unchanged polls as writes. Insert the bounded batch
            # in one statement, not one network round trip per source row.
            if not _has_unprojected(conn, root):
                return
            inserted = conn.execute(_TIMELINE + ''' INSERT INTO swarm_chat_entries
                (root_id,run_id,source,source_id,created_at)
                SELECT ?,run_id,source,source_id,created_at''' + _UNSEEN + '''
                ORDER BY created_at,source,source_id LIMIT ?
                ON CONFLICT(root_id,source,source_id) DO NOTHING RETURNING id''',
                (root, root, root, root, PROJECTION_BATCH_SIZE)).fetchall()
        # A cursor is issued only after the existing backlog has been indexed.
        # Large histories make resumable progress without monopolizing a request.
        if len(inserted) < PROJECTION_BATCH_SIZE:
            return
    with store.connect() as conn:
        conn.begin_read()
        _require_root(conn, root)
        if not _has_unprojected(conn, root):
            return
    raise HTTPException(503, PREPARING_HISTORY, headers={'Retry-After': '1'})


def _cursor(root: str, **values) -> str:
    raw = json.dumps({'v': _CURSOR_VERSION, 'root': root, **values}, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _read_cursor(value: str, root: str, mode: str) -> dict:
    try:
        if len(value) > 1024:
            raise ValueError
        payload = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
        fields = {'v', 'root', 'mode', 'delivery'} | ({'key'} if mode == 'before' else set())
        if (not isinstance(payload, dict) or set(payload) != fields or payload['v'] != _CURSOR_VERSION
                or payload['root'] != root or payload['mode'] != mode
                or any(type(payload[name]) is not int or not 0 <= payload[name] < 2**63
                       for name in ('delivery',))):
            raise ValueError
        if mode == 'before':
            key = payload['key']
            if (not isinstance(key, list) or len(key) != 3 or not isinstance(key[0], str)
                    or len(key[0]) > 64 or key[1] not in {'message', 'board'}
                    or type(key[2]) is not int or not 0 < key[2] < 2**63):
                raise ValueError
        return payload
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(422, 'Invalid swarm chat cursor. Reload this conversation.') from None


def _key(row) -> tuple:
    return row['created_at'], row['source'], row['source_id']


def _tail(conn, root: str) -> int:
    row = conn.execute('SELECT COALESCE(MAX(id),0) AS delivery FROM swarm_chat_entries WHERE root_id=?',
                       (root,)).fetchone()
    return row['delivery']


def _history(conn, root: str, mark: int, before: dict | None, limit: int) -> tuple[list, bool]:
    params = [root, root, root, mark]
    query = _DELIVERY_TIMELINE + ' SELECT * FROM delivered WHERE delivery_id<=?'
    if before:
        stamp, source, identity = before['key']
        query += ''' AND (created_at<? OR (created_at=? AND source<?)
            OR (created_at=? AND source=? AND source_id<?))'''
        params.extend([stamp, stamp, source, stamp, source, identity])
    rows = [dict(row) for row in conn.execute(query + ' ORDER BY created_at DESC,source DESC,source_id DESC LIMIT ?',
                                             (*params, limit + 1))]
    return sorted(rows[:limit], key=_key), len(rows) > limit


def _newer(conn, root: str, mark: int, limit: int) -> tuple[list, int, bool]:
    rows = [dict(row) for row in conn.execute(_DELIVERY_TIMELINE + ''' SELECT * FROM delivered
        WHERE delivery_id>? ORDER BY delivery_id LIMIT ?''', (root, root, root, mark, limit + 1))]
    selected = rows[:limit]
    updated = selected[-1]['delivery_id'] if selected else mark
    return sorted(selected, key=_key), updated, len(rows) > limit


def _author(row, root: str, *, human=False) -> dict:
    if human:
        return {'id': row['user_id'], 'name': row['user_name'] or row['user_email'] or 'Teammate',
                'role': 'human', 'harness': None, 'model': None, 'is_leader': False}
    return {'id': row['run_id'], 'name': ('Coordinator' if row['run_id'] == root else
                                        row['agent_label'] or row['display_title'] or 'Agent'),
            'role': 'coordinator' if row['run_id'] == root else 'agent',
            'harness': row['harness'], 'model': row['model'],
            'is_leader': bool(row['leader_id'] and row['leader_id'] == row['run_id'])}


def _message_item(row, root: str) -> dict:
    human = row['role'] == 'user'
    linked = row.get('response_to_id')
    identity = f"response:{row['run_id']}:{linked}" if not human and linked else f"message:{row['id']}"
    content = row['content']
    return {'id': identity, 'run_id': row['run_id'], 'kind': 'human' if human else 'assistant',
            'created_at': row['created_at'], 'author': _author(row, root, human=human),
            'target': None, 'content': content[:MAX_CONTENT_CHARACTERS],
            'truncated': len(content) > MAX_CONTENT_CHARACTERS, 'status': row['status']}


def _items(conn, root: str, keys: list[dict]) -> list[dict]:
    payloads = {}
    message_ids = [row['source_id'] for row in keys if row['source'] == 'message']
    if message_ids:
        rows = conn.execute('''SELECT m.id,m.run_id,m.role,m.created_at,m.status,m.response_to_id,
            substr(m.content,1,?) AS content,m.model,m.user_id,COALESCE(u.name,'') AS user_name,
            COALESCE(u.email,'') AS user_email,r.agent_label,r.display_title,r.harness,
            COALESCE(t.leader_id,'') AS leader_id
            FROM messages m JOIN runs r ON r.id=m.run_id LEFT JOIN users u ON u.id=m.user_id
            LEFT JOIN swarm_teams t ON t.group_id=r.agent_group_id
            WHERE m.id IN (''' + ','.join('?' for _ in message_ids) + ')',
            (MAX_CONTENT_CHARACTERS + 1, *message_ids))
        for row in rows:
            payloads['message', row['id']] = _message_item(dict(row), root)
    # Board posts predate historical model attribution. Do not mislabel an old
    # post with a member's current model preference. Canonical replies have m.model.
    post_ids = [row['source_id'] for row in keys if row['source'] == 'board']
    if post_ids:
        rows = conn.execute('''SELECT p.id,p.member_id AS run_id,p.kind,p.created_at,p.target_id,
            substr(p.content,1,?) AS content,r.agent_label,r.display_title,r.harness,NULL AS model,
            COALESCE(t.leader_id,'') AS leader_id,COALESCE(NULLIF(target.agent_label,''),target.display_title,'') AS target_name
            FROM swarm_posts p JOIN runs r ON r.id=p.member_id
            LEFT JOIN swarm_teams t ON t.group_id=p.group_id LEFT JOIN runs target ON target.id=p.target_id
            WHERE p.id IN (''' + ','.join('?' for _ in post_ids) + ')',
            (MAX_CONTENT_CHARACTERS + 1, *post_ids))
        for raw in rows:
            row = dict(raw)
            payloads['board', row['id']] = {
                'id': f"board:{row['id']}", 'run_id': row['run_id'], 'kind': row['kind'],
                'created_at': row['created_at'], 'author': _author(row, root),
                'target': {'id': row['target_id'], 'name': row['target_name'] or 'Agent'} if row['target_id'] else None,
                'content': row['content'][:MAX_CONTENT_CHARACTERS],
                'truncated': len(row['content']) > MAX_CONTENT_CHARACTERS, 'status': 'published',
            }
    return [payloads[row['source'], row['source_id']] for row in keys]


def _live_items(conn, root: str, public_messages: Callable) -> list[dict]:
    rows = conn.execute(_TREE + ''' SELECT r.id AS run_id,r.status,r.harness,r.model,r.active_model,
        r.active_message_id,r.updated_at,r.agent_label,r.display_title,
        substr(r.summary,1,?) AS summary,substr(r.pending_result,1,?) AS pending_result,
        COALESCE(t.leader_id,'') AS leader_id,m.status AS input_status,m.model AS input_model,
        a.id AS answer_id,a.status AS answer_status,a.created_at AS answer_created_at,
        a.model AS answer_model,substr(a.content,1,?) AS answer_content
        FROM runs r JOIN members tree ON tree.id=r.id
        JOIN messages m ON m.id=r.active_message_id AND m.run_id=r.id AND m.role='user'
        LEFT JOIN messages a ON a.run_id=r.id AND a.response_to_id=r.active_message_id AND a.role='assistant'
        LEFT JOIN swarm_teams t ON t.group_id=r.agent_group_id
        WHERE a.status='saving' OR (a.id IS NULL AND m.status='running' AND r.pending_result!='')
        ORDER BY r.updated_at DESC,r.id LIMIT ?''',
        (root, MAX_CONTENT_CHARACTERS + 1, 128000, MAX_CONTENT_CHARACTERS + 1, MAX_LIVE_ITEMS))
    result = []
    for raw in rows:
        run = dict(raw)
        active = {'id': run['active_message_id'], 'role': 'user', 'status': run['input_status'],
                  'model': run['input_model']}
        replies = ([{'id': run['answer_id'], 'role': 'assistant', 'status': run['answer_status'],
                     'created_at': run['answer_created_at'], 'model': run['answer_model'],
                     'content': run['answer_content'], 'response_to_id': run['active_message_id']}]
                   if run['answer_id'] else [])
        for reply in public_messages(run, [active, *replies]):
            if reply['role'] != 'assistant' or reply['status'] in {'deleted', 'steered'} or not reply['content'].strip():
                continue
            # Legacy pending_result receipts have a synthetic negative message
            # ID. The turn identity remains stable when their real row arrives.
            reply.setdefault('response_to_id', run['active_message_id'])
            result.append(_message_item({**run, **reply}, root))
    return sorted(result, key=lambda item: (item['created_at'], item['id']))


def _watch_ids(value: str | None) -> list[str]:
    if not value:
        return []
    identities = value.split(',')
    if (len(value) > 8192 or len(identities) > 100
            or any(not re.fullmatch(r'(?:message:[1-9][0-9]{0,18}|response:[0-9a-f]{32}:[1-9][0-9]{0,18})', item)
                   or int(item.rsplit(':', 1)[1]) >= 2**63 for item in identities)):
        raise HTTPException(422, 'Watch at most 100 valid swarm message IDs.')
    return list(dict.fromkeys(identities))


def _revalidate(conn, root: str, identities: list[str], live: list[dict]) -> tuple[list[dict], list[str]]:
    """Refresh visible mutable messages even after the next turn has started.

    Watch IDs grant no access: the same subtree and public-message filter apply.
    Deleted, steered and no-longer-visible messages receive only a tombstone.
    """
    if not identities:
        return [], []
    conditions, params = [], [root, root]
    for identity in identities:
        parts = identity.split(':')
        if parts[0] == 'message':
            conditions.append('m.id=?')
            params.append(int(parts[1]))
        else:
            conditions.append('(m.run_id=? AND m.response_to_id=?)')
            params.extend([parts[1], int(parts[2])])
    keys = [dict(row) for row in conn.execute(_TREE + ', visible AS (' + _MESSAGES + ''')
        SELECT v.* FROM visible v JOIN messages m ON m.id=v.source_id WHERE ''' + ' OR '.join(conditions), params)]
    observed = {item['id']: item for item in [*live, *_items(conn, root, keys)] if item['id'] in identities}
    return sorted(observed.values(), key=lambda item: (item['created_at'], item['id'])), [
        identity for identity in identities if identity not in observed]


def read_page(store, root: str, public_messages: Callable, *, limit: int = 50,
              after: str | None = None, before: str | None = None, watch: str | None = None) -> dict:
    """Latest/older history or new deliveries, plus current public reply upserts.

    Initial/before ``has_more`` means older history; after means more new items.
    ``watch`` revalidates up to 100 visible mutable message IDs, returning
    upserts/tombstones even when a later turn already replaced the active turn.
    ``next_cursor`` always belongs to the forward stream, ``previous_cursor``
    to history. Retain the original forward cursor when loading older pages.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE or (after is not None and before is not None):
        raise HTTPException(422, 'Choose a page size from 1 to 100 and only one swarm chat cursor.')
    watching = _watch_ids(watch)
    cursor = _read_cursor(after, root, 'after') if after is not None else (
        _read_cursor(before, root, 'before') if before is not None else None)
    _project(store, root)
    with store.connect() as conn:
        conn.begin_read()
        _require_root(conn, root)
        mark = cursor['delivery'] if cursor else _tail(conn, root)
        if after is not None:
            keys, mark, has_more = _newer(conn, root, mark, limit)
        else:
            keys, has_more = _history(conn, root, mark, cursor, limit)
        items = _items(conn, root, keys)
        live = _live_items(conn, root, public_messages)
        updated, removed = _revalidate(conn, root, watching, live)
    return {'items': items, 'live_items': live, 'updated_items': updated, 'removed_ids': removed, 'has_more': has_more,
            'next_cursor': _cursor(root, mode='after', delivery=mark),
            'previous_cursor': _cursor(root, mode='before', key=list(_key(keys[0])), delivery=mark) if keys else None}
