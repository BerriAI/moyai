"""Persisted collaboration intent; the existing session workflow owns all timers.

Only checkpointed, completed turns can earn automatic continuation. Ambiguous
actions and failed turns require explicit human intervention, never replay.
"""
from datetime import datetime, timedelta
import hashlib
import json
import time

from .db import database, now

MAX_ROUNDS = 25
ROUND_DELAY_SECONDS = 30


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
        from .harnesses import choices
        settings = self.manager.settings
        configured = settings.model_choices()
        catalog = []
        for harness in choices():
            models = []
            for model in configured:
                try:
                    selected = settings.harness_model(harness['id'], model['id'])
                except ValueError:
                    continue
                if selected not in models:
                    models.append(selected)
            if models:
                catalog.append({'harness': harness['id'], 'models': models})
        return (f'\n\nSWARM MODE — HOST POLICY (round {mission["round"]}/{MAX_ROUNDS}; '
                f'absolute deadline {mission["ends_at"]}):\n'
                'Work collaboratively on the user’s mission. Use agents_fanout to launch a small team '
                'with genuinely independent, complementary assignments, then gather actual results and '
                'synthesize a useful update. Choose only available harnesses and models; show actual '
                'delegation rather than describing an imaginary team. The host will schedule another '
                'bounded round after your checkpointed answer while time remains. Do not start /goal, '
                'a separate autonomous loop, or recurring automation. Preserve latest human directions. '
                'Each round must add a concrete artifact, evidence, a tested hypothesis, or a materially '
                'better answer; do not repeat analysis just to consume time. Clearly state uncertainties '
                'and blockers. Never retry ambiguous external actions without verification. Existing '
                'permissions and approval requirements still apply.\n'
                f'Configured maximum workers per group: {settings.max_parallel_agents}. '
                'Prefer a small team of two or three complementary workers. '
                'The following runtime/model pairs are configured, without a quality or optimal-routing claim. '
                'Use their exact IDs in agents_fanout tasks when a different runtime is helpful; '
                'omit selectors to inherit the coordinator’s runtime.\n' + json.dumps(catalog))

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
            content = (f'[System-generated swarm continuation, round {number}]\n'
                       'Continue the original mission using the saved conversation and latest human directions. '
                       'Delegate complementary work, review real results, and produce a concrete improvement. '
                       'Do not repeat prior completed actions or replay uncertain actions. If blocked, explain '
                       'what human input or access is needed. This is host-scheduled continuation, not a new human request.')
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
