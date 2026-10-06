import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .attachments import Attachments


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory: Path, default_model: str = '', *, auto_link_identities=False, max_pending_runs=1000):
        self.auto_link_identities = auto_link_identities
        self.max_pending_runs = max_pending_runs
        self.generation = 0
        self.default_model = default_model
        self.tracing = None
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "workspace.db"
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, prompt TEXT NOT NULL, repo_url TEXT NOT NULL,
                    mode TEXT NOT NULL, status TEXT NOT NULL, plugins TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    sandbox_id TEXT NOT NULL DEFAULT '', snapshot_id TEXT NOT NULL DEFAULT '',
                    token_hash TEXT NOT NULL DEFAULT '', model_calls INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run_id_id ON events(run_id, id);
                CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
                    client_id TEXT, created_at TEXT NOT NULL,
                    UNIQUE(run_id, client_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id,id);
                CREATE TABLE IF NOT EXISTS connections (
                    provider TEXT PRIMARY KEY, encrypted TEXT NOT NULL,
                    label TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connection_policies (
                    provider TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    read_only INTEGER NOT NULL DEFAULT 0 CHECK(read_only IN (0,1)),
                    checked_at TEXT, check_status TEXT
                );
                CREATE TABLE IF NOT EXISTS organization (
                    id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connection_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL, action TEXT NOT NULL,
                    actor TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slack_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
                    user_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    reply_status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    tool TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, result TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_run_id ON approvals(run_id);
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    session_id TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_states (
                    state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL,
                    nonce TEXT NOT NULL, verifier TEXT NOT NULL,
                    return_path TEXT NOT NULL, expires REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS slack_threads (
                    team_id TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id), started_ts TEXT NOT NULL,
                    paused INTEGER NOT NULL DEFAULT 0, last_message_id INTEGER NOT NULL DEFAULT 0,
                    last_progress REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(team_id,channel,thread_ts)
                );
                CREATE TABLE IF NOT EXISTS slack_receipts (
                    event_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, channel TEXT NOT NULL,
                    message_ts TEXT NOT NULL, user_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id),
                    message_id INTEGER, command TEXT NOT NULL DEFAULT '', handled INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(team_id,channel,message_ts)
                );
                CREATE INDEX IF NOT EXISTS idx_slack_receipts_message ON slack_receipts(run_id,message_id);
                CREATE TABLE IF NOT EXISTS slack_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                    dedupe_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', slack_ts TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_slack_outbox_status ON slack_outbox(status,id);
                CREATE TABLE IF NOT EXISTS slack_activity (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id), status TEXT NOT NULL DEFAULT '',
                    refreshed_at REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0
                );
                PRAGMA optimize;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL, linked_user_id TEXT REFERENCES users(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS identity_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
                    source_id TEXT NOT NULL, target_id TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            if 'metadata' not in {row['name'] for row in conn.execute('PRAGMA table_info(slack_outbox)')}:
                conn.execute("ALTER TABLE slack_outbox ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
            for table, names in [('runs', ('owner_id', 'active_user_id')), ('messages', ('user_id',))]:
                existing = {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}
                for name in names:
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            for name, default in [('environment_id', 'auto'), ('environment_build_id', '')]:
                if name not in {row['name'] for row in conn.execute('PRAGMA table_info(runs)')}:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(users)')}
            for name in ('link_method', 'link_status', 'profile_checked_at'):
                if name not in columns:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            for name in ('profile_eligible', 'profile_next_check', 'profile_conflict'):
                if name not in columns:
                    conn.execute(f'ALTER TABLE users ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
            # Every pre-existing link was explicitly chosen by an administrator.
            conn.execute("UPDATE users SET link_method='manual',link_status='manual' WHERE linked_user_id IS NOT NULL AND link_method=''")
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(identity_audit)')}
            for name in ('reason', 'previous_target_id'):
                if name not in columns:
                    conn.execute(f"ALTER TABLE identity_audit ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            if 'active_message_id' not in {row['name'] for row in conn.execute('PRAGMA table_info(runs)')}:
                conn.execute('ALTER TABLE runs ADD COLUMN active_message_id INTEGER')
            if "model_calls" not in {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}:
                conn.execute("ALTER TABLE runs ADD COLUMN model_calls INTEGER NOT NULL DEFAULT 0")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            for name in ("chat_enabled", "turn_model_calls"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            for name in ('model', 'active_model', 'pending_result', 'checkpoint_error', 'parent_run_id', 'agent_group_id', 'agent_label', 'side_chat_of', 'side_chat_context'):
                if name not in columns:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner_id,id)')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_participant ON messages(user_id,run_id) WHERE role='user'")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_users_linked ON users(linked_user_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_runs_side_chat ON runs(side_chat_of,created_at)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_runs_parent_updated ON runs(parent_run_id,updated_at DESC,created_at DESC,id DESC)')
            if 'model' not in {row['name'] for row in conn.execute('PRAGMA table_info(messages)')}:
                conn.execute("ALTER TABLE messages ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(messages)')}
            for name in ('revision', 'queue_locked'):
                if name not in columns:
                    conn.execute(f'ALTER TABLE messages ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
            if 'started_at' not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN started_at TEXT NOT NULL DEFAULT ''")
                conn.execute("UPDATE messages SET started_at=created_at WHERE status!='queued'")
            if 'steering_parent_id' not in columns:
                conn.execute('ALTER TABLE messages ADD COLUMN steering_parent_id INTEGER')
            if 'steer_message_id' not in {row['name'] for row in conn.execute('PRAGMA table_info(runs)')}:
                conn.execute('ALTER TABLE runs ADD COLUMN steer_message_id INTEGER')
            if default_model:
                conn.execute("UPDATE runs SET model=? WHERE model=''", (default_model,))
                conn.execute("UPDATE runs SET active_model=model WHERE active_model='' AND status NOT IN ('idle','completed','failed','cancelled','interrupted')")
                conn.execute("UPDATE messages SET model=(SELECT model FROM runs WHERE runs.id=messages.run_id) WHERE model='' AND status IN ('queued','running')")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(slack_events)")}
            for name, default in (("mention_ts", ""), ("context_status", "legacy"), ("context_json", "{}")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE slack_events ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
        self.path.chmod(0o600)
        self.attachments = Attachments(self)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
            if conn.total_changes:
                self.generation += 1
        finally:
            conn.close()

    def execute(self, sql, params=()):
        with self.connect() as conn:
            return conn.execute(sql, params).rowcount

    def rows(self, sql, params=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def sidebar_run_ids(self, user_id=None, extra_ids=()):
        """Filter before the recent limit and apply the same scope to filed/focused runs.

        Identity links affect this shared-workspace view only, never authorization.
        A submission remains participation even when subsequently soft-deleted.
        """
        prefix, params, predicate = '', [], "parent_run_id=''"
        if user_id is not None:
            prefix = """WITH identities AS (
                SELECT ? AS id WHERE ? != ''
                UNION SELECT id FROM users WHERE kind='slack' AND linked_user_id=?
            ), contributed AS (
                SELECT id FROM runs WHERE owner_id IN (SELECT id FROM identities)
                UNION SELECT run_id FROM messages
                    WHERE role='user' AND user_id IN (SELECT id FROM identities)
            ), mine AS (
                SELECT CASE WHEN parent_run_id='' THEN id ELSE parent_run_id END AS id
                FROM runs WHERE id IN (SELECT id FROM contributed)
            ) """
            params = [user_id, user_id, user_id]
            predicate += ' AND id IN (SELECT id FROM mine)'
        query = prefix + 'SELECT id FROM runs WHERE ' + predicate
        ids = [row['id'] for row in self.rows(query + ' ORDER BY updated_at DESC,created_at DESC,id DESC LIMIT 100', params)]
        # Bound placeholders even for accounts with many personally filed sessions.
        extras = list(dict.fromkeys(extra_ids))
        for offset in range(0, len(extras), 500):
            batch = extras[offset:offset + 500]
            ids.extend(row['id'] for row in self.rows(query + ' AND id IN (' + ','.join('?' for _ in batch) + ')', [*params, *batch]))
        return list(dict.fromkeys(ids))

    def run(self, run_id: str):
        rows = self.rows("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            return None
        row = rows[0]
        row["plugins"] = json.loads(row["plugins"])
        return row

    def create_run(self, prompt: str, repo_url: str, mode: str, plugins: list[str], *, chat_enabled=False, model='', user_id='', attachment_ids=None, client_id=None, environment_id='auto', side_chat_of=''):
        run_id = uuid4().hex
        stamp = now()
        model = model or self.default_model
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if client_id:
                previous = conn.execute("SELECT m.*,r.repo_url,r.mode,r.plugins,r.environment_id,r.side_chat_of FROM messages m JOIN runs r ON r.id=m.run_id WHERE m.client_id=? AND m.user_id=? AND m.role='user'", ('new:' + client_id, user_id)).fetchone()
                if previous:
                    if (previous['content'] != prompt or previous['model'] != model or previous['repo_url'] != repo_url
                            or previous['mode'] != mode or json.loads(previous['plugins']) != plugins or previous['environment_id'] != environment_id or previous['side_chat_of'] != side_chat_of
                            or self.attachments.message_ids(conn, previous['id']) != set(attachment_ids or [])):
                        raise ValueError('That submission ID was already used for different content.')
                    return self.run(previous['run_id'])
            context = ''
            if side_chat_of:
                parent = conn.execute('SELECT prompt,summary FROM runs WHERE id=?', (side_chat_of,)).fetchone()
                if not parent:
                    raise ValueError('The original session no longer exists.')
                recent = conn.execute("SELECT role,content FROM messages WHERE run_id=? AND status NOT IN ('queued','deleted') ORDER BY id DESC LIMIT 30", (side_chat_of,)).fetchall()
                context_data = {'task': parent['prompt'][:4000], 'latest_result': parent['summary'][:8000],
                                'conversation': [dict(row) | {'content': row['content'][:3000]} for row in reversed(recent)]}
                while len(json.dumps(context_data, ensure_ascii=False)) > 48000:
                    if context_data['conversation']:
                        context_data['conversation'].pop(0)
                    else:
                        field = max(('task', 'latest_result'), key=lambda key: len(context_data[key]))
                        context_data[field] = context_data[field][:len(context_data[field]) // 2]
                context = json.dumps(context_data, ensure_ascii=False)
            pending = conn.execute("SELECT COUNT(*) FROM runs WHERE status NOT IN ('completed','failed','cancelled','interrupted','idle')").fetchone()[0]
            if pending >= self.max_pending_runs:
                raise ValueError('The session queue is full. Wait for a task to finish.')
            conn.execute(
                "INSERT INTO runs(id,prompt,repo_url,mode,status,plugins,created_at,updated_at,chat_enabled,model,active_model,owner_id,active_user_id,environment_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, prompt, repo_url, mode, "queued", json.dumps(plugins), stamp, stamp, chat_enabled, model, model, user_id, user_id, environment_id),
            )
            if side_chat_of:
                conn.execute('UPDATE runs SET side_chat_of=?,side_chat_context=?,agent_label=? WHERE id=?', (side_chat_of, context, 'Side chat · ' + prompt[:70], run_id))
            if chat_enabled:
                message_id = conn.execute("INSERT INTO messages(run_id,role,content,status,client_id,created_at,model,user_id) VALUES(?,'user',?,'queued',?,?,?,?)", (run_id, prompt, 'new:' + client_id if client_id else 'initial', stamp, model, user_id)).lastrowid
                self.attachments.bind_in(conn, attachment_ids, message_id, user_id)
            elif attachment_ids:
                raise ValueError('Attachments require a chat session.')
        self.event(run_id, "status", "Task queued")
        return self.run(run_id)

    def create_slack_run(self, event_id, prompt, plugins, channel, thread_ts, user_id, mention_ts=None, team_id='', file_ids=()):
        # Slack retries deliveries. Reserve the event and its run in the same
        # transaction so parallel deliveries cannot create multiple sandboxes.
        run_id, stamp = uuid4().hex, now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM slack_events WHERE event_id=?", (event_id,)).fetchone():
                return None
            actor_id = self.slack_identity_in(conn, team_id, user_id)
            pending = conn.execute("SELECT COUNT(*) FROM runs WHERE status NOT IN ('completed','failed','cancelled','interrupted','idle')").fetchone()[0]
            if pending >= self.max_pending_runs:
                raise ValueError("The session queue is full.")
            conn.execute("INSERT INTO runs(id,prompt,repo_url,mode,status,plugins,created_at,updated_at,chat_enabled,model,owner_id) VALUES(?,?,'','modal','queued',?,?,?,1,?,?)",
                         (run_id, prompt, json.dumps(plugins), stamp, stamp, self.default_model, actor_id))
            conn.execute("INSERT INTO messages(run_id,role,content,status,client_id,created_at,model,user_id) VALUES(?,'user',?,'queued','initial',?,?,?)", (run_id, prompt, stamp, self.default_model, actor_id))
            if file_ids:
                message_id = conn.execute('SELECT id FROM messages WHERE run_id=?', (run_id,)).fetchone()[0]
                conn.execute('INSERT INTO slack_audio_inputs(message_id,files_json) VALUES(?,?)', (message_id, json.dumps(file_ids)))
            conn.execute("INSERT INTO slack_events(event_id,run_id,channel,thread_ts,user_id,created_at,mention_ts,context_status) VALUES(?,?,?,?,?,?,?,'pending')",
                         (event_id, run_id, channel, thread_ts, user_id, stamp, mention_ts or thread_ts))
            conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'status','Session requested from Slack','{}',?)", (run_id, stamp))
        return self.run(run_id)

    def slack_source(self, run_id):
        rows = self.rows("SELECT channel,thread_ts,mention_ts,user_id,context_status,context_json FROM slack_events WHERE run_id=?", (run_id,))
        if not rows:
            return None
        row = rows[0]
        context = json.loads(row.pop("context_json"))
        if context.get('kind') == 'channel':
            # Older snapshots included neighboring channel discussions. Do not
            # expose them as this thread's context in the UI or new agent input.
            messages = [item for item in context.get('messages', []) if item.get('ts') == row['mention_ts']]
            context.update(kind='thread', messages=messages,
                           truncated=any(item.get('text_truncated') for item in messages),
                           warning='Neighboring channel messages are excluded.')
        return {**context, **row}

    def messages(self, run_id):
        messages = self.rows("""SELECT m.id,m.role,m.content,m.status,m.created_at,m.started_at,m.model,
            m.user_id,m.revision,m.queue_locked,m.steering_parent_id,
            COALESCE(NULLIF(linked.email,''),NULLIF(u.email,''),linked.name,u.name,'Earlier message') AS user_name,
            u.name AS sender_name,u.email AS sender_email,
            (SELECT r.user_id FROM slack_receipts r WHERE r.message_id=m.id AND r.run_id=m.run_id
                AND m.user_id='slack:'||r.team_id||':'||r.user_id
                AND NOT EXISTS(SELECT 1 FROM slack_events e WHERE e.event_id=r.event_id)
                LIMIT 1) AS slack_reply_user
            FROM messages m LEFT JOIN users u ON u.id=m.user_id
            LEFT JOIN users linked ON linked.id=u.linked_user_id
            WHERE m.run_id=? AND m.status!='deleted'
            ORDER BY CASE WHEN m.role='user' AND m.started_at='' THEN 1 ELSE 0 END,
                COALESCE(NULLIF(m.started_at,''),m.created_at),m.id""", (run_id,))
        for message in messages:
            slack_user = message.pop('slack_reply_user')
            name, email = message.pop('sender_name'), message.pop('sender_email')
            prefix = f'Slack reply from {slack_user}:\n'
            # Resolve only our generated attribution, including saved history.
            # Keep canonical content intact for agents, retries and queue edits.
            if message['role'] == 'user' and slack_user and message['content'].startswith(prefix):
                name = (name or '').strip()
                if name in {'', slack_user, 'Slack ' + slack_user, message['user_id']}:
                    name = (email or '').strip() or 'Slack teammate'
                message['display_content'] = f'Slack reply from {name}:\n' + message['content'][len(prefix):]
        # Inputs that never started (cancelled/failed while waiting) stay where
        # they were sent: before the first later-sent input that did run.
        ordered = [m for m in messages if not (m['role'] == 'user' and not m['started_at'])]
        for orphan in (m for m in messages if m['role'] == 'user' and not m['started_at'] and m['status'] != 'queued'):
            index = next((i for i, m in enumerate(ordered) if m['role'] == 'user' and m['started_at']
                          and (m['created_at'], m['id']) > (orphan['created_at'], orphan['id'])), len(ordered))
            ordered.insert(index, orphan)
        messages = ordered + [m for m in messages if m['role'] == 'user' and not m['started_at'] and m['status'] == 'queued']
        return self.attachments.messages(run_id, messages)

    def enqueue_message(self, run_id, content, client_id, model=None, user_id='', attachment_ids=None, send_now=False):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result, created = self.enqueue_message_in(conn, run_id, content, client_id, model, user_id, attachment_ids, send_now)
        if created:
            self.event(run_id, "chat", "Message queued", {"message_id": result["id"]})
        return result, created

    def enqueue_message_in(self, conn, run_id, content, client_id, model=None, user_id='', attachment_ids=None, send_now=False):
        """Caller owns a write transaction, including any transport receipt."""
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or not row["chat_enabled"]:
            raise ValueError("This older task has no saved chat workspace. Start a new session.")
        existing = conn.execute("SELECT * FROM messages WHERE run_id=? AND client_id=?", (run_id, client_id)).fetchone()
        if existing:
            if (existing["content"] != content or existing['user_id'] != user_id or (model is not None and existing['model'] != model)
                    or self.attachments.message_ids(conn, existing['id']) != set(attachment_ids or [])):
                raise ValueError("That message ID was already used for different text or model.")
            return dict(existing), False
        if row["status"] == "stopping":
            raise ValueError("Wait for the current response to stop before sending another message.")
        if send_now and conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status='queued' AND queue_locked=1", (run_id,)).fetchone():
            raise ValueError('Moyai is already picking up another queued message. Wait or queue this normally.')
        pending = conn.execute("SELECT COUNT(*) FROM messages WHERE run_id=? AND status='queued'", (run_id,)).fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE run_id=? AND role='user' AND status!='deleted'", (run_id,)).fetchone()[0]
        if pending >= 5 or count >= 100:
            raise ValueError("This session allows 5 queued messages and 100 turns. Wait, or start a new session.")
        if conn.execute("SELECT COUNT(*) FROM runs WHERE status NOT IN ('completed','failed','cancelled','interrupted','idle')").fetchone()[0] >= self.max_pending_runs and row["status"] in {"idle", "completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("The session queue is full. Wait for a response to finish.")
        stamp = now()
        model = model if model is not None else row['model'] or self.default_model
        message_id = conn.execute("INSERT INTO messages(run_id,role,content,status,client_id,created_at,model,user_id) VALUES(?,'user',?,'queued',?,?,?,?)", (run_id, content, client_id, stamp, model, user_id)).lastrowid
        self.attachments.bind_in(conn, attachment_ids, message_id, user_id)
        conn.execute('UPDATE runs SET model=?,updated_at=? WHERE id=?', (model, stamp, run_id))
        if send_now:
            conn.execute('UPDATE runs SET steer_message_id=? WHERE id=?', (message_id, run_id))
            conn.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
        running = conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status='running'", (run_id,)).fetchone()
        if not running:
            conn.execute("UPDATE runs SET status='queued',error='',updated_at=? WHERE id=?", (stamp, run_id))
        return {"id": message_id, "status": "queued", "model": model}, True

    def claim_message(self, run_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT status,steer_message_id FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] in {"stopping", "cancelled", "interrupted"}:
                return None
            if conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status='running'", (run_id,)).fetchone():
                return None
            row = conn.execute("SELECT * FROM messages WHERE run_id=? AND role='user' AND status='queued' ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,id LIMIT 1", (run_id, run['steer_message_id'])).fetchone()
            if not row:
                return None
            conn.execute("UPDATE messages SET status='running',started_at=? WHERE id=?", (now(), row["id"]))
            if row['id'] == run['steer_message_id']:
                conn.execute('UPDATE runs SET steer_message_id=NULL WHERE id=?', (run_id,))
            conn.execute("UPDATE runs SET status='queued',turn_model_calls=0,error='',summary='',pending_result='',active_model=?,active_user_id=?,active_message_id=? WHERE id=?", (row['model'], row['user_id'], row['id'], run_id))
        self.event(run_id, "chat", "Response started", {"message_id": row["id"], "model": row['model']})
        return dict(row)

    def finish_message(self, run_id, message_id, content, status="completed"):
        with self.connect() as conn:
            changed = conn.execute("UPDATE messages SET status=? WHERE id=? AND run_id=? AND status='running'", (status, message_id, run_id)).rowcount
            if changed:
                conn.execute("UPDATE messages SET status=? WHERE run_id=? AND steering_parent_id=? AND status='injected'", (status, run_id, message_id))
                conn.execute("UPDATE messages SET steering_parent_id=NULL,queue_locked=0 WHERE run_id=? AND steering_parent_id=? AND status='queued'", (run_id, message_id))
                if status != 'steered':
                    # Input models remain the original admission receipt. The
                    # answer records the final model after a conversational switch.
                    conn.execute("""INSERT INTO messages(run_id,role,content,status,created_at,model,user_id)
                        SELECT ?,'assistant',?,?,?,CASE WHEN r.active_message_id=m.id AND r.active_model!=''
                        THEN r.active_model ELSE m.model END,m.user_id FROM messages m JOIN runs r ON r.id=m.run_id WHERE m.id=?""",
                                 (run_id, content, status, now(), message_id))
                if self.tracing:
                    # Commit the answer and its pending span together. A crash
                    # after saving the answer must not lose its root trace.
                    self.tracing.finish_turn(run_id, message_id, content, status, connection=conn)
        self.event(run_id, "chat", "Response saved", {"message_id": message_id})

    def has_queued_messages(self, run_id):
        return bool(self.rows("SELECT id FROM messages WHERE run_id=? AND status='queued' LIMIT 1", (run_id,)))

    def identity(self, info):
        """Only call with the server-verified session, never request JSON."""
        if info.get('method') == 'google':
            identity = info['identity']
            user_id = 'google:' + identity['sub']
            kind, email, name = 'google', identity['email'].strip().lower(), identity.get('name') or identity['email']
        else:
            kind, email = 'shared', ''
            user_id = 'shared:' + info.get('method', 'password') + ':' + info.get('role', 'admin')
            name = 'Local preview' if info.get('method') == 'local' else 'Shared password sign-in'
        stamp = now()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
            conn.execute('INSERT INTO users(id,kind,email,name,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET email=excluded.email,name=excluded.name,updated_at=excluded.updated_at',
                         (user_id, kind, email, name, stamp, stamp))
            if kind == 'google':
                for value in {email, old['email'] if old else email}:
                    self.reconcile_email_in(conn, value)
        return user_id

    def reconcile_email_in(self, conn, email):
        """Accounting links only. Never authenticate a user or grant a role here.

        The email must have come from a checked Slack profile or Google OIDC.
        Once bound, provider IDs stay authoritative; changes need admin review.
        """
        if not email or not self.auto_link_identities:
            return
        google = conn.execute("SELECT id FROM users WHERE kind='google' AND email=?", (email,)).fetchall()
        slack = conn.execute("SELECT * FROM users WHERE kind='slack' AND email=? AND profile_eligible=1", (email,)).fetchall()
        fresh_after = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        # Retain collision detection for older/deactivated Slack identities too:
        # a recycled corporate email must not move old spend to another person.
        slack_count = conn.execute("SELECT COUNT(*) FROM users WHERE kind='slack' AND email=?", (email,)).fetchone()[0]
        ambiguous = len(google) > 1 or slack_count > 1
        for row in slack:
            if row['link_method'] == 'manual':
                continue
            target = google[0]['id'] if len(google) == 1 else None
            if row['linked_user_id']:
                status = 'linked' if not ambiguous and row['linked_user_id'] == target else 'review'
            elif ambiguous:
                status = 'review'
            elif row['profile_checked_at'] < fresh_after:
                status = 'pending_profile'
            elif target:
                conn.execute("UPDATE users SET linked_user_id=?,link_method='email',updated_at=? WHERE id=?", (target, now(), row['id']))
                conn.execute("INSERT INTO identity_audit(actor_id,source_id,target_id,created_at,reason) VALUES('system:email-match',?,?,?,'automatic_email_match')", (row['id'], target, now()))
                status = 'linked'
            else:
                status = 'awaiting_google'
            conn.execute('UPDATE users SET link_status=? WHERE id=?', (status, row['id']))

    @staticmethod
    def slack_identity_in(conn, team, user):
        user_id, stamp = f'slack:{team}:{user}', now()
        conn.execute("INSERT OR IGNORE INTO users(id,kind,name,created_at,updated_at) VALUES(?,'slack',?,?,?)", (user_id, 'Slack ' + user, stamp, stamp))
        return user_id

    def update_run(self, run_id: str, **fields):
        allowed = {"status", "summary", "error", "sandbox_id", "snapshot_id", "token_hash", "pending_result", "checkpoint_error"}
        if not fields.keys() <= allowed:
            raise ValueError("Unsupported run update")
        fields["updated_at"] = now()
        keys = ",".join(f"{key}=?" for key in fields)
        self.execute(f"UPDATE runs SET {keys} WHERE id=?", (*fields.values(), run_id))

    def event(self, run_id: str, kind: str, message: str, data=None):
        data = dict(data) if isinstance(data, dict) else {}
        # Publication is a server decision, shared by both web renderers and Slack.
        data.pop('public_update', None)
        data.pop('public_reply_to', None)
        data.pop('live_status', None)
        if kind == 'status' and data.get('phase') == 'steering':
            # Only MessageQueue.acknowledge can publish a delivered-input receipt.
            data.pop('message_id', None)
        if kind == 'status' and data.get('phase') == 'focus':
            from .progress import record_focus
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                record_focus(conn, run_id, message, data, now())
            return
        if kind == 'message' and data.get('phase') != 'processing':
            from .progress import record
            with self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                record(conn, run_id, message, data, now())
            return
        if kind != 'chat':
            # A queued user's creation time can precede the current response.
            # Bind work to the server's claimed turn, never a sandbox-supplied ID.
            active = self.rows('SELECT active_message_id FROM runs WHERE id=?', (run_id,))
            data['turn_id'] = active[0]['active_message_id'] if active else 0
        # Keep bounded event history even if an agent floods stdout.
        count = self.rows("SELECT COUNT(*) AS n FROM events WHERE run_id=?", (run_id,))[0]["n"]
        if count >= 2000 and kind not in {"result", "error", "artifact", "approval", "status", "chat"}:
            if not self.rows("SELECT 1 FROM events WHERE run_id=? AND kind='status' AND message='Detailed activity limit reached' LIMIT 1", (run_id,)):
                self.event(run_id, 'status', 'Detailed activity limit reached',
                           {'detail': 'Additional tool details are no longer recorded for this session. Response and workspace saves continue.',
                            'activity_version': 1, 'phase': 'limited'})
            return
        self.execute(
            "INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)",
            (run_id, kind, message[:32000], json.dumps(data or {}), now()),
        )

    def events(self, run_id: str, after: int = 0, limit: int = 200):
        rows = self.rows("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?", (run_id, after, limit))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    def approvals(self, run_id: str):
        rows = self.rows("SELECT * FROM approvals WHERE run_id=? ORDER BY created_at", (run_id,))
        for row in rows:
            row["arguments"] = json.loads(row["arguments"])
        return rows
