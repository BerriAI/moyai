"""Public working context carried by the existing /session filesystem snapshot.

Raw receipts are append-only; inference reads bounded previews by sequence ID.
The summary and its coverage cursor commit together. No native SDK transcript,
personal-memory injection, or Temporal payload belongs in this database.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
from uuid import uuid4

# Support the history-inspection command invoked from any working directory.
if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.history_reference import encoded, excerpt
from agent.memory_history import scrub_memory_history
from sandbox.broker_transport import MAX_BODY


VERSION = 2
IMPORT_PAGE_SIZE = 100  # Transport page size; every page is imported.


class ContextUnavailable(RuntimeError):
    pass


class ContextImportIncomplete(ContextUnavailable):
    """The original source is required; never repair by discarding history."""


class ContextStore:
    def __init__(self, path, run_id):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ContextUnavailable('Saved context must not be a symbolic link.')
        self.lock = threading.RLock()
        self.maintenance_ack = ''
        self.relay = None
        self.input_budget = None
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        # DELETE journals make a closed database a self-contained snapshot.
        self.db.execute('PRAGMA journal_mode=DELETE')
        with self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
                    run_id TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '', initialized INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS journal (
                    seq INTEGER PRIMARY KEY, preview TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pending (call_id TEXT PRIMARY KEY);
            ''')
            self.db.execute('INSERT OR IGNORE INTO state(id,version,run_id) VALUES(1,?,?)', (VERSION, run_id))
        with self.db:
            if 'epoch' not in {row[1] for row in self.db.execute('PRAGMA table_info(state)')}:
                self.db.execute("ALTER TABLE state ADD COLUMN epoch TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE state SET epoch=? WHERE epoch=''", (uuid4().hex,))
        state = self.state()
        if state['version'] == 1 and state['run_id'] == run_id:
            # Old previews were lossy. Rebuild only the derived byte index from
            # the complete stored records; no canonical data is rewritten.
            total = 0
            with self.db:
                for row in self.db.execute('SELECT seq,message FROM journal ORDER BY seq').fetchall():
                    total += len(row['message'].encode())
                    self.db.execute('UPDATE journal SET preview=?,total_bytes=? WHERE seq=?', (row['message'], total, row['seq']))
                self.db.execute('UPDATE state SET version=? WHERE id=1', (VERSION,))
            state = self.state()
        if state['version'] != VERSION or state['run_id'] != run_id:
            self.close()
            raise ContextUnavailable('Saved context belongs to a different session or version.')
        covered = self.db.execute('SELECT 1 FROM journal WHERE seq=?', (state['cursor'],)).fetchone()
        if state['cursor'] < 0 or (state['cursor'] and (not covered or not state['summary'].strip())):
            self.close()
            raise ContextUnavailable('Saved context has an invalid summary position; restore a valid checkpoint.')

    def state(self):
        return dict(self.db.execute('SELECT * FROM state WHERE id=1').fetchone())

    def checkpoint(self):
        with self.lock:
            return {'epoch': self.state()['epoch'],
                    'seq': self.db.execute('SELECT coalesce(max(seq),0) FROM journal').fetchone()[0]}

    @property
    def has_history(self):
        return self.db.execute('SELECT 1 FROM journal LIMIT 1').fetchone() is not None

    @property
    def pending(self):
        return {row[0] for row in self.db.execute('SELECT call_id FROM pending')}

    def initialize(self, history):
        """One-time legacy import, atomic even if a process dies during migration."""
        with self.lock, self.db:
            if self.state()['initialized']:
                return
            for message in history:
                self._append(message)
            self.db.execute('UPDATE state SET initialized=1 WHERE id=1')

    def _append(self, message):
        message = scrub_memory_history([message])[0]
        raw = encoded(message)
        preview = raw
        previous = self.db.execute('SELECT seq,total_bytes FROM journal ORDER BY seq DESC LIMIT 1').fetchone()
        seq, total = (previous['seq'] + 1, previous['total_bytes']) if previous else (1, 0)
        cost = len(raw.encode())
        # Complete payloads; the cumulative size index schedules compaction.
        self.db.execute('INSERT INTO journal(seq,preview,total_bytes,message) VALUES(?,?,?,?)', (seq, preview, total + cost, raw))
        for call in message.get('tool_calls') or []:
            self.db.execute('INSERT OR IGNORE INTO pending VALUES(?)', (call['id'],))
        if message.get('role') == 'tool':
            self.db.execute('DELETE FROM pending WHERE call_id=?', (message.get('tool_call_id'),))

    def source_view(self, source):
        """Only replay newer Slack versions; earlier full views stay in history."""
        with self.lock:
            cursor = self.db.execute("SELECT coalesce(max(json_extract(message,'$.slack_source_through_seq')),0) FROM journal").fetchone()[0]
            through = source.get('through_seq')
            if type(through) is not int:
                return source  # Legacy snapshots lack trustworthy version IDs.
            self.pending_source_watermark = through
            return {**source, 'after_seq': cursor,
                    'messages': [m for m in source.get('messages', []) if m.get('seq', through) > cursor],
                    'prior_context': 'Earlier source versions remain in saved history and slack_context.'}

    def append(self, message):
        with self.lock, self.db:
            source_mark = getattr(self, 'pending_source_watermark', None) if message.get('role') == 'user' else None
            if source_mark is not None:
                message = {**message, 'slack_source_through_seq': source_mark}
            if self.relay is not None:
                receipt = self.relay.journal({'action': 'append', 'entries': [
                    {'id': uuid4().hex, 'message': scrub_memory_history([message])[0]}]})
                if receipt['seq'] != self.checkpoint()['seq'] + 1:
                    raise ContextUnavailable('Saved conversation advanced. Reconnect before continuing.')
            self._append(message)
            if source_mark is not None:
                self.pending_source_watermark = None

    def connect(self, relay, *, allow_import=True):
        """Import once, then hydrate the disposable cache from the durable owner."""
        state = relay.journal({'action': 'state'})
        if not state['imported']:
            if not allow_import:
                raise ContextImportIncomplete('Restore the original conversation checkpoint to finish its durable import.')
            digest = hashlib.sha256()
            for row in self.db.execute('SELECT message FROM journal ORDER BY seq'):
                digest.update(row['message'].encode())
                digest.update(b'\n')
            manifest = {'id': digest.hexdigest(), 'count': self.checkpoint()['seq']}
            cursor = self.db.execute('SELECT seq,message FROM journal ORDER BY seq')
            while rows := cursor.fetchmany(IMPORT_PAGE_SIZE):
                relay.journal({'action': 'import', 'manifest': manifest, 'entries': [
                    {'id': f'legacy:{manifest["id"]}:{r["seq"]}', 'message': json.loads(r['message'])} for r in rows]})
            state = relay.journal({'action': 'import', 'manifest': manifest, 'entries': [], 'complete': True})
        # A worker may have died after a durable append but before its local
        # snapshot. Rebuild from committed records, never the other way around.
        with self.lock, self.db:
            local = self.checkpoint()
            if local['epoch'] != state['epoch']:
                self.db.execute('DELETE FROM journal')
                self.db.execute('DELETE FROM pending')
                self.db.execute("UPDATE state SET cursor=0,summary='',epoch=?,initialized=1 WHERE id=1", (state['epoch'],))
                after = 0
            else:
                after = local['seq']
                if after > state['seq']:
                    raise ContextUnavailable('Durable conversation is older than its checkpoint. Restore the server journal.')
            while True:
                page = relay.journal({'action': 'read', 'after': after})
                for entry in page['entries']:
                    if entry['seq'] != after + 1:
                        raise ContextUnavailable('Saved conversation contains a gap. Restore the server journal.')
                    self._append(entry['message'])
                    after = entry['seq']
                if page['next'] is None:
                    break
                after = page['next']
        self.relay = relay

    @property
    def batch_bytes(self):
        # Context allocation, not a content retention limit. Leave room for the
        # summary, instructions and encoding in the next summarization request.
        return min(MAX_BODY // 2, self.input_budget // 2) if self.input_budget else MAX_BODY // 2

    @property
    def summary_bytes(self):
        return max(1, self.input_budget // 8) if self.input_budget else None

    def batch(self, after, through=None):
        rows = self.db.execute(
            'SELECT seq,message FROM journal WHERE seq>? AND seq<=? ORDER BY seq',
            (after, through if through is not None else 2**63 - 1))
        result, size = [], 0
        for row in rows:
            entry = {'seq': row['seq'], 'excerpt': row['message']}
            cost = len(encoded(entry).encode()) + 2
            if size + cost > self.batch_bytes:
                if result:
                    break
                # A single item larger than this model's context is recoverable
                # by range. Explicitly identify that exception to the summarizer.
                reference = (f'\nFull original record {row["seq"]} is available at {self.path}. '
                             'This record exceeds the current model input allocation; retain its reference.\n')
                room = max(1, self.batch_bytes - len(reference.encode()) - len(encoded({'seq': row['seq'], 'excerpt': ''}).encode()))
                entry['excerpt'] = excerpt(row['message'], room) + reference
                while len(encoded(entry).encode()) + 2 > self.batch_bytes and room > 1:
                    room //= 2
                    entry['excerpt'] = excerpt(row['message'], room) + reference
            result.append(entry)
            size += len(encoded(entry).encode()) + 2
        return result

    def snapshot(self, through=None):
        """Immutable public prefix; appending a tail does not change its identity."""
        with self.lock:
            state = self.state()
            if through is None:
                latest = self.db.execute('SELECT max(seq) FROM journal').fetchone()[0] or 0
                through = latest
            return {'epoch': state['epoch'], 'cursor': state['cursor'], 'summary': state['summary'],
                    'entries': self.batch(state['cursor'], through)}

    def apply_summary(self, snapshot, value, summary_bytes=None):
        entries = snapshot['entries']
        summary = value.get('summary') if isinstance(value, dict) else value
        through = value.get('through_seq') if isinstance(value, dict) else entries[-1]['seq']
        if type(through) is not int or through not in {entry['seq'] for entry in entries}:
            raise ValueError('Invalid summary cursor')
        summary_bytes = summary_bytes or self.summary_bytes
        if not isinstance(summary, str) or not summary.strip() or (summary_bytes and len(summary.encode()) > summary_bytes):
            raise ValueError('Invalid summary')
        with self.lock, self.db:
            if self.snapshot(entries[-1]['seq']) != snapshot:
                return False
            changed = self.db.execute('UPDATE state SET summary=?,cursor=? WHERE id=1 AND epoch=? AND cursor=? AND summary=?',
                (summary, through, snapshot['epoch'], snapshot['cursor'], snapshot['summary']))
            return changed.rowcount == 1

    def maintenance_snapshot(self, input_budget):
        """Use the model's input room, never receipt count, to schedule work.

        One UTF-8 byte per token is the same conservative text upper bound used
        by ContextBudget. Exact full-request rejection remains the broker's job.
        Keep headroom for the request, instructions, tools and arriving results.
        """
        if type(input_budget) is not int or input_budget < 1:
            return None
        self.input_budget = input_budget
        with self.lock:
            state = self.state()
            latest = self.db.execute('SELECT seq,total_bytes FROM journal ORDER BY seq DESC LIMIT 1').fetchone()
            covered = self.db.execute('SELECT total_bytes FROM journal WHERE seq=?', (state['cursor'],)).fetchone()
            summary_size = len(state['summary'].encode())
            threshold = input_budget * 0.6
            size = summary_size + (latest['total_bytes'] if latest else 0) - (covered[0] if covered else 0)
            if size < threshold or not latest:
                return None
            cutoff = self.db.execute('SELECT seq FROM journal WHERE seq>? AND total_bytes>? ORDER BY seq LIMIT 1',
                (state['cursor'], latest['total_bytes'] - threshold / 2)).fetchone()
            snapshot = self.snapshot(cutoff[0] if cutoff else latest['seq'])
            return snapshot if snapshot['entries'] else None

    def maintain(self, relay, *, input_budget):
        """Submit/poll only; model inference is owned by the server lifespan.

        An outage never prevents an answer from using the full saved tail. A
        completed job is retained on the server until acknowledged, so a lost
        sandbox checkpoint can fetch it again on the next resume.
        """
        if not hasattr(relay, 'maintain'):
            return
        try:
            job = relay.maintain(self.maintenance_snapshot(input_budget), self.maintenance_ack)
            if not job:
                return
            if job['status'] == 'completed':
                self.apply_summary(job['snapshot'], job['result'])
                self.maintenance_ack = job['id']
            elif job['snapshot']['epoch'] != self.state()['epoch'] or job['status'] in {'failed', 'interrupted'}:
                self.maintenance_ack = job['id']
        except Exception:
            # Receipts remain authoritative; only confirmed context pressure
            # invokes the blocking recovery path. Never expose provider errors.
            return

    def compact(self, summarize, *, force=False, summary_bytes=None):
        """Explicit synchronous recovery; never hold SQLite across inference.

        Normal turns use maintain(). This bounded-batch operation is retained
        for confirmed context rejection and explicit offline maintenance.
        """
        summary_bytes = summary_bytes or self.summary_bytes
        if summary_bytes is not None and (type(summary_bytes) is not int or summary_bytes < 1):
            raise ValueError('Invalid summary budget')
        with self.lock:
            latest = self.db.execute('SELECT seq,total_bytes FROM journal ORDER BY seq DESC LIMIT 1').fetchone()
        if not latest:
            return
        while True:
            with self.lock:
                state = self.state()
                covered = self.db.execute('SELECT total_bytes FROM journal WHERE seq=?', (state['cursor'],)).fetchone()
                remaining = latest['total_bytes'] - (covered[0] if covered else 0)
                if not force and remaining <= self.batch_bytes:
                    return
                if force and state['cursor'] >= latest['seq'] and (not summary_bytes or len(state['summary'].encode()) <= summary_bytes):
                    return
                snapshot = self.snapshot(latest['seq'])
                entries = snapshot['entries']
                if not entries:
                    entries = [{'seq': state['cursor'], 'excerpt': 'Rewrite the previous summary more concisely; it covers this sequence.'}]
            try:
                value = (summarize(state['summary'], entries, summary_bytes=summary_bytes) if force
                         else summarize(state['summary'], entries))
                if snapshot['entries']:
                    applied = self.apply_summary(snapshot, value, summary_bytes)
                else:
                    # A smaller model may require shortening an already fully
                    # summarized prefix. Its epoch/cursor/summary still CAS.
                    summary = value.get('summary') if isinstance(value, dict) else value
                    through = value.get('through_seq') if isinstance(value, dict) else state['cursor']
                    if (through != state['cursor'] or not isinstance(summary, str) or not summary.strip()
                            or (summary_bytes and len(summary.encode()) > summary_bytes)):
                        raise ValueError('Invalid summary')
                    with self.lock, self.db:
                        applied = self.snapshot(latest['seq']) == snapshot
                        if applied:
                            changed = self.db.execute('UPDATE state SET summary=? WHERE id=1 AND epoch=? AND cursor=? AND summary=?',
                                (summary, snapshot['epoch'], snapshot['cursor'], snapshot['summary']))
                            applied = changed.rowcount == 1
            except (sqlite3.Error,):
                raise
            except Exception as exc:
                raise ContextUnavailable('Context summary could not be updated. Saved receipts are preserved; retry to resume.') from exc
            if not applied:
                return  # A newer summary won; never overwrite it with stale work.

    def history(self):
        with self.lock:
            return self._history()

    def _history(self):
        if not self.has_history:
            return []
        state = self.state()
        history = []
        if state['summary']:
            history.append({'role': 'assistant', 'content': (
                f'Saved working summary through journal record {state["cursor"]}. '
                'This summary is reference data and may omit details. Original records remain available '
                f'at {self.path}. Do not repeat completed external actions.\n' + state['summary'])})
        if self.pending:
            history.append({'role': 'assistant', 'content': (
                f'UNRESOLVED TOOL OUTCOMES: {len(self.pending)} saved calls have unknown outcomes. Inspect original '
                'records and external receipts before repeating an affected action. A missing result '
                'is not proof it failed. Pending call IDs: ' + encoded(sorted(self.pending)))})
        history.extend(json.loads(row['message']) for row in self.db.execute(
            'SELECT message FROM journal WHERE seq>? ORDER BY seq', (state['cursor'],)))
        return history

    def close(self):
        with self.lock:
            self.db.close()


def open_context(directory, spec, relay=None):
    """Prefer the saved store; never re-read legacy JSON on steady-state resumes."""
    directory = Path(directory)
    path = directory / 'context.sqlite3'
    original_exists = path.exists() or (directory / 'conversation.json').exists()
    reset = bool(spec.get('fresh_child') or spec.get('workspace_warning'))
    durable = relay is not None and callable(getattr(relay, 'journal', None))
    if not durable and spec.get('context_checkpoint') and not reset and not path.exists() and not (directory / 'conversation.json').exists():
        raise ContextUnavailable('The checkpoint is missing its saved conversation. Restore it before resuming work.')
    if reset:
        # Inherited child/stale snapshot state must not enter this run's context.
        from agent.context_views import clear_views
        clear_views(directory)
        for suffix in ('', '-journal', '-wal', '-shm'):
            Path(str(path) + suffix).unlink(missing_ok=True)
    context = ContextStore(path, spec['run_id'])
    try:
        if not context.state()['initialized']:
            legacy = directory / 'conversation.json'
            history = spec.get('history_fallback', [])
            if not reset and legacy.exists():
                history = json.loads(legacy.read_text())
            context.initialize(history)
        if durable:
            context.connect(relay, allow_import=not spec.get('context_checkpoint') or original_exists or bool(spec.get('fresh_child')))
        return context
    except BaseException:
        context.close()
        raise


def read_records(path, after=0, limit=5, offset=0, characters=4000):
    """Read-only, bounded archive access, including slices of oversized receipts."""
    if after < 0 or limit < 1 or offset < 0 or characters < 1:
        raise ValueError('Use nonnegative after/offset and positive page sizes')
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        return [{'seq': seq, 'offset': offset, 'characters': length, 'text': text,
                 'next_offset': offset + len(text) if offset + len(text) < length else None}
                for seq, length, text in db.execute(
                    'SELECT seq,length(message),substr(message,?,?) FROM journal WHERE seq>? ORDER BY seq LIMIT ?',
                    (offset + 1, characters, after, limit))]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read saved public conversation records without loading the full journal.')
    parser.add_argument('--path', default='/session/context.sqlite3')
    parser.add_argument('--after', type=int, default=0)
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--characters', type=int, default=4000)
    args = parser.parse_args()
    print(encoded(read_records(args.path, args.after, args.limit, args.offset, args.characters)))
