"""Persisted collaboration intent; the existing session workflow owns all timers.

Only checkpointed, completed turns can earn automatic continuation. Ambiguous
actions and failed turns require explicit human intervention, never replay.
"""
from datetime import datetime, timedelta
import hashlib
import json
import time
from uuid import uuid4

from .db import database, now

# Agent behavior is shared with SDK consumers; this module owns persistence.
from agent.swarm.planning import (
    INITIAL_TEAM_SIZE, MAX_ROUNDS, ROUND_DELAY_SECONDS,
    Runtime, continuation_message, coordinator_prompt, plan_team,
)

INITIAL_TEAM_KEY = 'host-swarm-initial-team'


def initialize_schema(store):
    store.execute('''CREATE TABLE IF NOT EXISTS swarm_missions (
        run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'active', budget_seconds INTEGER NOT NULL,
        ends_at TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 1,
        reason TEXT NOT NULL DEFAULT '', next_at REAL NOT NULL DEFAULT 0,
        settled_message_id INTEGER NOT NULL DEFAULT 0,
        answer_digest TEXT NOT NULL DEFAULT '', no_progress INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def public_mission(row):
    return {key: row[key] for key in ('status', 'budget_seconds', 'ends_at', 'round', 'reason')}


def execution_policy(store, run_id, *, connection=None):
    rows = store.rows('''WITH RECURSIVE ancestors(id,parent_run_id) AS (
        SELECT id,parent_run_id FROM runs WHERE id=? UNION
        SELECT r.id,r.parent_run_id FROM runs r JOIN ancestors a ON r.id=a.parent_run_id
    ) SELECT s.* FROM swarm_missions s JOIN ancestors a ON a.id=s.run_id LIMIT 1''',
        (run_id,), connection=connection)
    return rows[0] if rows else None


def remaining_seconds(mission):
    return datetime.fromisoformat(mission['ends_at']).timestamp() - time.time()


def create_in(conn, run_id, budget_seconds, stamp):
    if type(budget_seconds) is not int or not 60 <= budget_seconds <= 86400:
        raise ValueError('Swarm time budget must be between 1 minute and 24 hours.')
    ends_at = (datetime.fromisoformat(stamp) + timedelta(seconds=budget_seconds)).isoformat()
    conn.execute('INSERT INTO swarm_missions(run_id,budget_seconds,ends_at,created_at,updated_at) VALUES(?,?,?,?,?)',
                 (run_id, budget_seconds, ends_at, stamp, stamp))


class SwarmMissions:
    def __init__(self, store, manager):
        self.store, self.manager = store, manager

    def get(self, run_id):
        rows = self.store.rows('SELECT * FROM swarm_missions WHERE run_id=?', (run_id,))
        return rows[0] if rows else None

    def runtime_catalog(self, model, *, for_dispatch=False):
        """Resolve administrator-approved pairs before the agent plans a team."""
        from .harnesses import choices
        settings = self.manager.settings
        selected = [item.strip() for item in settings.swarm_models.split(',') if item.strip()] or [model]
        # Never draw from the broad display catalog implicitly. Administrators
        # explicitly opt models into automatic work; otherwise inherit the
        # already selected coordinator model.
        # A saved mission can still render its context after an administrator
        # removes a catalog entry; actual new dispatch remains strict.
        models = list(dict.fromkeys(settings.resolve_model(value) for value in selected)) if for_dispatch else selected
        catalog = []
        for harness in choices():
            supported = []
            for candidate in models:
                try:
                    supported.append(settings.harness_model(harness['id'], candidate))
                except ValueError:
                    continue
            if for_dispatch:
                provider = {'codex': 'openai/', 'claude-agent-sdk': 'anthropic/'}.get(harness['id'])
                preferred = [candidate for candidate in supported if provider and candidate.startswith(provider)]
                supported = preferred or supported
            catalog.extend(Runtime(harness['id'], candidate) for candidate in supported)
        if for_dispatch and not catalog:
            raise ValueError('No configured model can run the swarm. Check SWARM_MODELS and the selected model.')
        return catalog

    def initial_plan(self, task, model):
        if self.manager.settings.max_parallel_agents < INITIAL_TEAM_SIZE:
            raise ValueError('Swarm mode needs a worker-group limit of at least 10. Ask the workspace administrator to raise MAX_PARALLEL_AGENTS.')
        return plan_team(task, runtimes=self.runtime_catalog(model, for_dispatch=True))

    def bootstrap_in(self, conn, run_id, message_id, stamp):
        """Commit real child sessions and their dispatch wakes with the mission.

        These agents start from the original task, rather than a coordinator
        snapshot. Each gets its own scoped copy of the initial uploads. Runtime
        concurrency still belongs to normal admission, including queueing.
        """
        existing = conn.execute('SELECT id FROM agent_groups WHERE parent_id=? AND request_key=?',
                                (run_id, INITIAL_TEAM_KEY)).fetchone()
        if existing:
            return existing['id']
        mission = execution_policy(self.store, run_id, connection=conn)
        if not mission or mission['status'] != 'active' or remaining_seconds(mission) <= 0:
            raise ValueError('This swarm is not active or its time budget has ended. No new workers can start.')
        run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        plan = self.initial_plan(run['prompt'], run['model'])
        # IDs and the chosen roster are persisted once, inside the create transaction.
        runtimes = [{'id': uuid4().hex, 'harness': task.harness, 'model': task.model} for task in plan]
        pending = conn.execute("SELECT COUNT(*) FROM runs WHERE status NOT IN ('completed','failed','cancelled','interrupted','idle')").fetchone()[0]
        if pending + INITIAL_TEAM_SIZE > self.manager.settings.max_pending_runs:
            raise ValueError('The session queue needs room for all 10 swarm workers. Wait for a task to finish.')
        uploads = conn.execute('SELECT * FROM attachments WHERE message_id=?', (message_id,)).fetchall()
        if uploads:
            cutoff = (datetime.fromisoformat(stamp) - timedelta(days=1)).isoformat()
            used = conn.execute("SELECT COALESCE(SUM(size + CASE WHEN preview_ref!='' THEN preview_size ELSE length(preview) END),0) FROM attachments WHERE message_id IS NOT NULL OR created_at>=?", (cutoff,)).fetchone()[0]
            copies = INITIAL_TEAM_SIZE * sum(upload['size'] + (upload['preview_size'] if upload['preview_ref'] else len(upload['preview'])) for upload in uploads)
            if used + copies > self.manager.settings.attachment_storage_limit_mb * 1024 * 1024:
                raise ValueError('Attachment storage needs room for the 10 swarm workers. Use smaller files or raise the workspace storage limit.')
        group_id = uuid4().hex
        conn.execute('''INSERT INTO agent_groups(id,parent_id,message_id,request_key,payload,status,created_at,runtime_assignments)
            VALUES(?,?,?,?,?,'running',?,?)''',
            (group_id, run_id, message_id, INITIAL_TEAM_KEY,
             json.dumps({'host_bootstrap': True, 'source_message_id': message_id}), stamp, json.dumps(runtimes)))
        for task, runtime in zip(plan, runtimes, strict=True):
            label, prompt = task.label, task.prompt
            child_id = runtime['id']
            conn.execute('''INSERT INTO runs(id,prompt,repo_url,mode,status,plugins,created_at,updated_at,chat_enabled,
                model,active_model,owner_id,active_user_id,parent_run_id,agent_group_id,agent_label,
                environment_id,environment_build_id,harness,github_repository_id,sandbox_provider)
                VALUES(?,?,?,'modal','queued',?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (child_id, prompt, run['repo_url'], run['plugins'], stamp, stamp,
                 runtime['model'], runtime['model'], run['owner_id'], run['owner_id'], run_id, group_id, label,
                 run['environment_id'], run['environment_build_id'], runtime['harness'], run['github_repository_id'], run['sandbox_provider']))
            if run['side_chat_context']:
                conn.execute('UPDATE runs SET side_chat_context=? WHERE id=?', (run['side_chat_context'], child_id))
            child_message = conn.execute("""INSERT INTO messages(run_id,role,content,status,client_id,created_at,model,user_id)
                VALUES(?,'user',?,'queued','initial',?,?,?) RETURNING id""",
                (child_id, prompt, stamp, runtime['model'], run['owner_id'])).fetchone()[0]
            # Independent rows keep the existing run-scoped attachment broker
            # authorization. Object-store references are shared immutably; no
            # parent attachment access is granted to an arbitrary child.
            for upload in uploads:
                copy = dict(upload) | {'id': uuid4().hex, 'message_id': child_message}
                columns = list(copy)
                conn.execute('INSERT INTO attachments(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')',
                             tuple(copy[column] for column in columns))
            self.manager.submit_in(conn, {'id': child_id})
        conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'agents',?,?,?)",
                     (run_id, 'Queued 10 agents across configured harnesses. Workers start as capacity becomes available.',
                      json.dumps({'group_id': group_id, 'phase': 'swarm_team_queued', 'workers': INITIAL_TEAM_SIZE}), stamp))
        return group_id

    def initial_group(self, run_id, message_id=None):
        rows = self.store.rows('SELECT id,message_id,status FROM agent_groups WHERE parent_id=? AND request_key=?',
                               (run_id, INITIAL_TEAM_KEY))
        if not rows or (message_id is not None and rows[0]['message_id'] != message_id):
            return None
        return rows[0]

    def change_status(self, run_id, status, reason, *, allowed=('active',)):
        with self.store.connect(write_scope=run_id) as conn:
            conn.begin_write()
            changed = conn.execute('UPDATE swarm_missions SET status=?,reason=?,updated_at=? WHERE run_id=? AND status IN (' + ','.join('?' for _ in allowed) + ')',
                                   (status, reason, now(), run_id, *allowed)).rowcount
            if changed:
                self.manager.submit_in(conn, {'id': run_id})
                conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'status',?,?,?)",
                             (run_id, reason, json.dumps({'phase': 'swarm', 'swarm_status': status}), now()))
            return bool(changed)

    def stop(self, run_id):
        return self.change_status(run_id, 'stopped', 'Swarm stopped. Completed work is preserved.', allowed=('active', 'paused', 'blocked'))

    async def enforce(self, run_id):
        mission = await database(execution_policy, self.store, run_id)
        if not mission:
            return None
        root = await database(self.store.run, mission['run_id'])
        if mission['status'] == 'active' and root and root['status'] in {'interrupted', 'failed', 'cancelled'}:
            await database(self.change_status, mission['run_id'], 'blocked', 'The previous execution ended without a confirmed continuation. Review its saved work before resuming.')
            mission = await database(self.get, mission['run_id'])
        if mission['status'] in {'active', 'paused'} and remaining_seconds(mission) <= 0:
            await database(self.change_status, mission['run_id'], 'expired', 'The swarm time budget ended. Completed work is preserved.', allowed=('active', 'paused'))
            mission = await database(self.get, mission['run_id'])
        if mission['status'] != 'active':
            # Do not replace paused/expired/blocked intent with a Stop marker.
            await self.manager.cancel(run_id, swarm_stop=False)
        return mission

    def prompt(self, run_id):
        mission = self.get(run_id)
        if not mission:
            return ''
        run = self.store.run(run_id)
        return coordinator_prompt(round_number=mission['round'], ends_at=mission['ends_at'],
                                  max_workers=self.manager.settings.max_parallel_agents,
                                  runtimes=self.runtime_catalog(run['model']))

    def settle(self, run_id, state):
        """Idempotently record a checkpointed outcome and the next durable wake."""
        with self.store.connect(write_scope=run_id) as conn:
            conn.begin_write()
            mission = conn.execute('SELECT * FROM swarm_missions WHERE run_id=?', (run_id,)).fetchone()
            # Human steering can claim a newer message before an older queued
            # continuation. Session ownership serializes settlement, not IDs.
            if not mission or mission['settled_message_id'] == state['message_id']:
                return
            status, reason = mission['status'], mission['reason']
            digest = hashlib.sha256(state.get('response', '').strip().encode()).hexdigest()
            repeats = mission['no_progress'] + 1 if digest == mission['answer_digest'] else 0
            if status == 'active':
                if state['outcome'] not in {'completed', 'steered'}:
                    status, reason = 'blocked', 'The last response could not be confirmed. Review its saved work before resuming.'
                elif remaining_seconds(mission) <= 0:
                    status, reason = 'expired', 'The swarm time budget ended. Completed work is preserved.'
                elif state['outcome'] == 'completed' and repeats >= 2:
                    status, reason = 'blocked', 'The team repeated its answer without progress. Review the result and give a new direction.'
                elif mission['round'] >= MAX_ROUNDS:
                    status, reason = 'paused', 'The swarm reached its 25-round limit. Start a new mission to continue.'
            conn.execute('''UPDATE swarm_missions SET status=?,reason=?,settled_message_id=?,answer_digest=?,
                no_progress=?,next_at=?,updated_at=? WHERE run_id=?''',
                (status, reason, state['message_id'], digest, repeats, time.time() + ROUND_DELAY_SECONDS, now(), run_id))
            self.manager.submit_in(conn, {'id': run_id})

    def tick(self, run_id):
        """Called at idle boundaries; queue + round + outbox share one commit."""
        with self.store.connect(write_scope=run_id) as conn:
            conn.begin_write()
            mission = conn.execute('SELECT * FROM swarm_missions WHERE run_id=?', (run_id,)).fetchone()
            if not mission or mission['status'] != 'active':
                return None
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if run['status'] in {'stopping', 'cancelled', 'interrupted', 'failed'} or run['deleted_at'] or run['deletion_requested_at']:
                return None
            remaining = remaining_seconds(mission)
            if remaining <= 0:
                return {'retry_seconds': 0.01}  # enforce() owns cascading cancellation.
            if conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status IN ('queued','running','injected')", (run_id,)).fetchone():
                return None  # Real human inputs always win over automatic work.
            delay = mission['next_at'] - time.time()
            if delay > 0:
                return {'retry_seconds': max(0.01, min(delay, remaining))}
            if mission['round'] >= MAX_ROUNDS:
                return None
            number = mission['round'] + 1
            direction = conn.execute("""SELECT content FROM messages WHERE run_id=? AND role='user'
                AND status!='deleted' AND COALESCE(client_id,'') NOT LIKE 'swarm:%'
                AND id>(SELECT MIN(id) FROM messages WHERE run_id=? AND role='user')
                ORDER BY id DESC LIMIT 1""", (run_id, run_id)).fetchone()
            content = continuation_message(number, run['prompt'], direction['content'] if direction else None)
            try:
                self.store.enqueue_message_in(conn, run_id, content, f'swarm:round:{number}',
                                              model=run['model'], user_id=run['owner_id'], restore_archived=False)
            except ValueError as exc:
                # Admission and per-session caps are policy boundaries, not
                # transient activity failures to retry invisibly forever.
                reason = 'Swarm continuation needs attention: ' + str(exc)
                conn.execute("UPDATE swarm_missions SET status='blocked',reason=?,updated_at=? WHERE run_id=?", (reason, now(), run_id))
                self.manager.submit_in(conn, {'id': run_id})
                return None
            conn.execute('UPDATE swarm_missions SET round=?,next_at=0,updated_at=? WHERE run_id=?', (number, now(), run_id))
            self.manager.submit_in(conn, {'id': run_id})
        return None

    async def pause(self, run_id):
        mission = self.get(run_id)
        if not mission:
            raise ValueError('This session is not a swarm.')
        if mission['status'] == 'active':
            await database(self.change_status, run_id, 'paused', 'Swarm paused. Resume explicitly when ready.')
            await self.manager.cancel(run_id, swarm_stop=False)

    async def resume(self, run_id):
        async with self.manager.session_guard(run_id):
            with self.store.connect(write_scope=run_id) as conn:
                conn.begin_write()
                mission = conn.execute('SELECT * FROM swarm_missions WHERE run_id=?', (run_id,)).fetchone()
                run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
                journal = conn.execute('SELECT state FROM durable_sessions WHERE run_id=?', (run_id,)).fetchone()
                if not mission or not run or run['deleted_at'] or run['deletion_requested_at']:
                    raise ValueError('Swarm not found.')
                if mission['status'] == 'active':
                    return
                if mission['status'] not in {'paused', 'blocked'} or remaining_seconds(mission) <= 0 or mission['round'] >= MAX_ROUNDS:
                    raise ValueError('This swarm has ended. Start a new mission with a new time budget.')
                if run['status'] == 'stopping' or (journal and json.loads(journal['state']).get('phase', 'idle') != 'idle'):
                    raise ValueError('Wait for the current work to stop and save before resuming.')
                if conn.execute("SELECT 1 FROM runs WHERE parent_run_id=? AND status NOT IN ('idle','completed','failed','cancelled','interrupted')", (run_id,)).fetchone():
                    raise ValueError('Wait for the delegated agents to stop before resuming.')
                conn.execute("UPDATE swarm_missions SET status='active',reason='',no_progress=0,next_at=0,updated_at=? WHERE run_id=?", (now(), run_id))
                conn.execute("UPDATE runs SET status='idle',error='' WHERE id=?", (run_id,))
                self.manager.submit_in(conn, {'id': run_id})
