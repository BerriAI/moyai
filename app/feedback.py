"""Human 👍/👎 feedback on assistant answers, exported onto the answer's trace.

One rating per person per answer; the latest choice wins. Each change emits a
new child span under the turn's agent span so LiteLLM Agent Traces and Lens see
the human verdict next to the run it judges.
"""
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import now

Rating = Literal['up', 'down']
SLACK_REACTIONS = {'+1': 'up', 'thumbsup': 'up', '-1': 'down', 'thumbsdown': 'down'}


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    rating: Rating | None
    comment: str = Field(default='', max_length=2000)


class Feedback:
    def __init__(self, store, security, checkpoints):
        self.store, self.security, self.checkpoints = store, security, checkpoints
        with store.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS message_feedback (
                message_id INTEGER NOT NULL REFERENCES messages(id), user_id TEXT NOT NULL,
                run_id TEXT NOT NULL, rating TEXT NOT NULL CHECK(rating IN ('up','down')),
                comment TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
                PRIMARY KEY(message_id,user_id))''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_feedback_run ON message_feedback(run_id)')

    def answer(self, conn, run_id, message_id):
        row = conn.execute("SELECT * FROM messages WHERE id=? AND run_id=? AND role='assistant'",
                           (message_id, run_id)).fetchone()
        if not row:
            raise ValueError('Feedback applies to Moyai answers in this session.')
        return row

    def turn_id(self, conn, answer):
        if answer['reply_to']:
            return answer['reply_to']
        # Answers saved before reply_to existed: the nearest earlier top-level input.
        row = conn.execute("""SELECT id FROM messages WHERE run_id=? AND role='user' AND id<?
            AND steering_parent_id IS NULL AND status!='deleted' ORDER BY id DESC LIMIT 1""",
            (answer['run_id'], answer['id'])).fetchone()
        return row['id'] if row else 0

    def record(self, run_id, message_id, user_id, rating, comment='', source='web'):
        """Save (or clear, with rating=None) one person's feedback and trace it."""
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            answer = self.answer(conn, run_id, message_id)
            previous = conn.execute('SELECT * FROM message_feedback WHERE message_id=? AND user_id=?',
                                    (message_id, user_id)).fetchone()
            if previous and previous['rating'] == rating and previous['comment'] == comment:
                return dict(previous)
            if rating is None and not previous:
                return None
            revision = (previous['revision'] + 1) if previous else 1
            if rating is None:
                conn.execute('DELETE FROM message_feedback WHERE message_id=? AND user_id=?', (message_id, user_id))
            else:
                conn.execute('''INSERT INTO message_feedback(message_id,user_id,run_id,rating,comment,source,revision,updated_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(message_id,user_id) DO UPDATE SET rating=excluded.rating,
                    comment=excluded.comment,source=excluded.source,revision=excluded.revision,updated_at=excluded.updated_at''',
                    (message_id, user_id, run_id, rating, comment, source, revision, now()))
            tracing = self.store.tracing
            if tracing and tracing.enabled:
                tracing.feedback(dict(answer), self.turn_id(conn, answer), user_id, rating, comment, source,
                                 revision, connection=conn)
        self.store.event(run_id, 'feedback', 'Feedback cleared' if rating is None else
                         ('Marked helpful' if rating == 'up' else 'Marked not helpful'),
                         {'message_id': message_id, 'rating': rating})
        return None if rating is None else {'rating': rating, 'comment': comment, 'revision': revision}

    def for_run(self, run_id, user_id):
        rows = self.store.rows('SELECT message_id,user_id,rating,comment FROM message_feedback WHERE run_id=?', (run_id,))
        summary = {}
        for row in rows:
            item = summary.setdefault(row['message_id'], {'up': 0, 'down': 0, 'mine': None, 'comment': ''})
            item[row['rating']] += 1
            if row['user_id'] == user_id:
                item['mine'], item['comment'] = row['rating'], row['comment']
        return summary

    def slack_reaction(self, team, channel, ts, user, reaction, added):
        """Map 👍/👎 on a delivered Moyai answer in Slack to feedback."""
        rating = SLACK_REACTIONS.get(reaction)
        if not rating:
            return None
        rows = self.store.rows("""SELECT o.run_id,o.dedupe_key FROM slack_outbox o JOIN slack_threads t ON t.run_id=o.run_id
            WHERE o.kind='answer' AND o.status='sent' AND o.slack_ts=? AND t.team_id=? AND t.channel=?""", (ts, team, channel))
        if not rows:
            return None
        run_id, message_id = rows[0]['run_id'], int(rows[0]['dedupe_key'].split(':')[1])
        with self.store.connect() as conn:
            user_id = self.store.slack_identity_in(conn, team, user)
        if not added:
            current = self.store.rows('SELECT rating FROM message_feedback WHERE message_id=? AND user_id=?', (message_id, user_id))
            if not current or current[0]['rating'] != rating:
                return None
            rating = None
        return self.record(run_id, message_id, user_id, rating, '', 'slack')

    def routes(self):
        router = APIRouter()

        @router.post('/api/runs/{run_id}/messages/{message_id}/feedback')
        async def give(run_id: str, message_id: int, body: FeedbackBody, request: Request):
            self.security.require(request, mutation=True)
            user_id = self.store.identity(self.security.session_info(request))
            if body.rating is None and body.comment:
                raise HTTPException(422, 'Choose helpful or not helpful to add a comment.')
            try:
                saved = self.record(run_id, message_id, user_id, body.rating, body.comment)
            except ValueError as exc:
                raise HTTPException(404, str(exc))
            await self.checkpoints.flush()
            return {'feedback': saved, 'summary': self.for_run(run_id, user_id).get(message_id)}

        return router
