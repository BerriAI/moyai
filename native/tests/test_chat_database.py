"""Native chat reads over disposable PostgreSQL and deterministic Slack membership."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from statistics import median
from time import perf_counter

import httpx
import pytest
import pytest_asyncio
import test_slack_audience as fixtures
from sqlalchemy import event, select, text, update
from test_product_identity import google_cookie

from moyai.api import create_app
from moyai.contracts import NewSession
from moyai.models import Event, Message, MessageSource, Session, Turn, User
from moyai.runtime import Runtime

pytestmark = pytest.mark.skipif(not os.getenv('MOYAI_TEST_POSTGRES'), reason='Disposable local PostgreSQL required.')
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
slack_admission = fixtures.slack_admission
audience = fixtures.audience


@pytest_asyncio.fixture
async def chat_product(audience):
    product = audience
    runtime = Runtime(product.settings.model_copy(update={'product_mode': 'standalone'}))
    await runtime.database.close()
    runtime.database = product.database
    runtime.product_identity = product.adapter
    runtime.security = product.database.security
    # Fixtures create a unique disposable database and reject remote hosts.
    await runtime.stores.start(provision=True)
    cookie, _ = google_cookie(product.adapter, sub=product.subject)
    product.runtime = runtime
    product.cookie = cookie
    try:
        yield product
    finally:
        await runtime.close()


async def seed_chat(product, *, turns: int, linked: bool, start=0, session_id=None):
    database = product.database
    if linked:
        session_id, author_id = session_id or product.session_id, product.raw_principal.id
        async with database.sessions.begin() as db:
            for row in await db.scalars(select(Turn).where(Turn.session_id == session_id)):
                row.status = 'completed'
    else:
        session_id = session_id or (await database.create_session(product.actor, NewSession())).id
        author_id = product.actor.id
    async with database.sessions.begin() as db:
        (await db.get(Session, session_id)).status = 'idle'
        for i in range(start, start + turns):
            turn_id = f'chat-turn-{i}'
            stamp = (datetime(2026, 10, 1, tzinfo=UTC) + timedelta(seconds=i)).isoformat()
            db.add(Turn(id=turn_id, session_id=session_id, actor_id=author_id, request_key=str(i),
                        request_fingerprint='a' * 64, prompt=database.security.seal('Question'), model='test',
                        status='completed', workflow_id=turn_id, created_at=stamp, finished_at=stamp))
            await db.flush()
            for j in range(2):
                identity = f'chat-message-{i}-{j}'
                db.add(Message(session_id=session_id, turn_id=turn_id, author_id=author_id if j == 0 else None,
                               role='user' if j == 0 else 'assistant', identity=identity,
                               content=database.security.seal('Public message https://github.com/example/project/pull/1'),
                               created_at=stamp))
                await db.flush()
                db.add(MessageSource(message_identity=identity, kind='web', mirror=True,
                                     data=database.security.seal({'kind': 'web'})))
    return session_id


async def seed_context(product, session_id):
    """A small public transcript can still retain megabytes of private context."""
    context = product.database.security.seal(os.urandom(840_000).hex())
    async with product.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == session_id).values(conversation=context))
        await db.execute(update(Turn).where(Turn.session_id == session_id).values(
            conversation=context, preparation=product.database.security.seal({'prepared': True}),
        ))


async def measure_chat(product, session_id, *, samples=7, app_factory=create_app):
    runtime = product.runtime
    app = app_factory(runtime, run_dispatcher=False)
    stats, results, rows = {}, {}, []
    active = None
    original_unseal = runtime.security.unseal

    def unseal(value):
        started = perf_counter()
        result = original_unseal(value)
        if active is not None:
            active['decrypt_ms'] += (perf_counter() - started) * 1000
        return result

    def before(conn, cursor, statement, parameters, context, many):
        context.chat_started = perf_counter()

    def after(conn, cursor, statement, parameters, context, many):
        if active is not None:
            active['sql_ms'] += (perf_counter() - context.chat_started) * 1000
            active['queries'] += 1
            active['statements'][' '.join(statement.split())] += 1
            projection = statement.partition('FROM')[0]
            if any(column in projection for column in (
                'moyai_sessions.conversation', 'moyai_sessions.snapshot',
                'moyai_turns.conversation', 'moyai_turns.preparation', 'moyai_turns.prompt',
            )):
                active['context_reads'] += 1

    engines = [runtime.database.engine.sync_engine, product.source.sync_engine]
    for engine in engines:
        event.listen(engine, 'before_cursor_execute', before)
        event.listen(engine, 'after_cursor_execute', after)
    runtime.security.unseal = unseal
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                    cookies={'workspace_session': product.cookie}) as client:
            for sample in range(samples + 1):
                workflow_started = perf_counter()
                for name, path in (
                    ('chat', f'/api/sessions/{session_id}?resolve=true&include_activity=false&include_messages=true'),
                    ('workspace', f'/api/sessions/{session_id}/workspace'),
                ):
                    active = {'queries': 0, 'sql_ms': 0., 'decrypt_ms': 0., 'context_reads': 0,
                              'statements': Counter()}
                    started = perf_counter()
                    response = await client.get(path)
                    elapsed = (perf_counter() - started) * 1000
                    assert response.status_code == 200, response.text
                    if name not in results:
                        results[name] = response.json()
                    assert response.json() == results[name]
                    if sample:
                        rows.append((name, elapsed, active))
                if sample:
                    parts = [info for _, _, info in rows[-2:]]
                    combined = {key: sum(info[key] for info in parts)
                                for key in ('queries', 'sql_ms', 'decrypt_ms', 'context_reads')}
                    combined['statements'] = parts[0]['statements'] + parts[1]['statements']
                    rows.append(('chat_and_workspace', (perf_counter() - workflow_started) * 1000, combined))
    finally:
        runtime.security.unseal = original_unseal
        for engine in engines:
            event.remove(engine, 'before_cursor_execute', before)
            event.remove(engine, 'after_cursor_execute', after)
    for name in ('chat', 'workspace', 'chat_and_workspace'):
        measured = [(ms, info) for kind, ms, info in rows if kind == name]
        stats[name] = {'http_ms': round(median(ms for ms, _ in measured), 2),
                       'sql_ms': round(median(info['sql_ms'] for _, info in measured), 2),
                       'decrypt_ms': round(median(info['decrypt_ms'] for _, info in measured), 2),
                       'queries': measured[-1][1]['queries'],
                       'context_reads': measured[-1][1]['context_reads'],
                       'top_queries': measured[-1][1]['statements'].most_common(6)}
    return stats, results


@pytest.mark.parametrize('linked', [False, True])
async def test_chat_and_workspace_queries_do_not_grow_per_turn_or_message(chat_product, linked):
    product = chat_product
    session_id = await seed_chat(product, turns=20, linked=linked)
    small, _ = await measure_chat(product, session_id, samples=1)
    await seed_chat(product, turns=80, start=20, linked=linked, session_id=session_id)
    large, result = await measure_chat(product, session_id, samples=1)
    for name in ('chat', 'workspace'):
        assert large[name]['queries'] <= small[name]['queries'] + 2
    assert len(result['chat']['turns']) == 100 and len(result['chat']['messages']) == 200
    assert all(turn['is_self'] for turn in result['chat']['turns'])
    assert result['chat']['session']['can_manage']
    assert result['workspace']['links'][0]['url'] == 'https://github.com/example/project/pull/1'


@pytest.mark.parametrize('linked', [False, True])
async def test_idle_chat_never_loads_private_checkpoints(chat_product, linked):
    product = chat_product
    session_id = await seed_chat(product, turns=1, linked=linked)
    await seed_context(product, session_id)
    stats, result = await measure_chat(product, session_id, samples=1)
    assert all(row['context_reads'] == 0 for row in stats.values())
    assert result['chat']['messages'] and result['workspace']['links']


async def test_waiting_chat_that_finishes_during_authorization_has_no_stale_question(chat_product, monkeypatch):
    from pydantic_ai import Conversation, ConversationTypeAdapter

    product = chat_product
    session_id = await seed_chat(product, turns=1, linked=False)
    checkpoint = product.database.security.seal(ConversationTypeAdapter.dump_json(Conversation(messages=[])))
    async with product.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == session_id).values(
            status='waiting', active_turn='chat-turn-0', conversation=checkpoint))
    original = product.database.check_source_audience
    changed = False

    async def finish_during_read(db, session, actor, **kwargs):
        nonlocal changed
        await original(db, session, actor, **kwargs)
        if not changed and session.id == session_id:
            changed = True
            async with product.database.sessions.begin() as writer:
                await writer.execute(update(Session).where(Session.id == session_id).values(
                    status='idle', active_turn=None))

    monkeypatch.setattr(product.database, 'check_source_audience', finish_during_read)
    app = create_app(product.runtime, run_dispatcher=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                cookies={'workspace_session': product.cookie}) as client:
        response = await client.get(f'/api/sessions/{session_id}?include_activity=false&include_messages=true')
    assert response.status_code == 200, response.text
    assert response.json()['session']['status'] == 'idle'
    assert response.json()['session']['active_turn'] is None
    assert not response.json().get('pending') and not response.json().get('can_answer')


async def test_mixed_authors_and_transcript_pagination(chat_product):
    product = chat_product
    session_id = await seed_chat(product, turns=110, linked=True)
    async with product.database.sessions.begin() as db:
        db.add(User(id='unrelated-author', username='unrelated-author', display_name='Another person',
                    password='', role='member'))
        await db.flush()
        await db.execute(update(Turn).where(Turn.id == 'chat-turn-109').values(actor_id='unrelated-author'))
        await db.execute(update(Message).where(Message.identity == 'chat-message-109-0').values(
            author_id='unrelated-author'))
    _, result = await measure_chat(product, session_id, samples=1)
    assert result['chat']['session']['can_manage']
    assert sum(not turn['is_self'] for turn in result['chat']['turns']) == 1
    page = result['chat']['messages']
    app = create_app(product.runtime, run_dispatcher=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                cookies={'workspace_session': product.cookie}) as client:
        tail = await client.get(f'/api/sessions/{session_id}/messages?after={page[-1]["id"]}')
        assert tail.status_code == 200
        messages = page + tail.json()
        assert len(messages) == len({row['id'] for row in messages}) == 221
        assert [row['id'] for row in messages] == sorted(row['id'] for row in messages)
        assert [row['is_self'] for row in tail.json() if row['author_id'] == 'unrelated-author'] == [False]
        async with product.database.sessions.begin() as db:
            await db.execute(update(Session).where(Session.id == session_id).values(
                visibility='private', owner_id='unrelated-author'))
        for suffix in ('?resolve=true&include_messages=true', '/workspace', '/messages'):
            assert (await client.get(f'/api/sessions/{session_id}' + suffix)).status_code == 404


async def test_batched_lineage_preserves_order_and_filters_other_turns(chat_product):
    from moyai.history_handoff import turn_run_ids, turn_run_ids_many

    product = chat_product
    session_id = await seed_chat(product, turns=3, linked=False)
    async with product.database.sessions.begin() as db:
        for index, (turn_id, run_id) in enumerate([
            ('chat-turn-0', 'chat-turn-0:segment:2'),
            ('chat-turn-1', 'chat-turn-1:segment:1'),
            ('chat-turn-0', 'chat-turn-0:segment:2'),
            ('chat-turn-0', 'chat-turn-1:segment:9'),
            ('chat-turn-0', 'chat-turn-0:segment:0'),
            ('chat-turn-2', 'chat-turn-2:segment:1'),
            ('chat-turn-0', 'chat-turn-0:segment:1'),
        ]):
            db.add(Event(session_id=session_id, turn_id=turn_id, kind='history_segment',
                         identity=f'segment-fixture-{index}', content=product.database.security.seal({'run_id': run_id})))
    expected = {'chat-turn-0': ['chat-turn-0', 'chat-turn-0:segment:2', 'chat-turn-0:segment:1'],
                'chat-turn-1': ['chat-turn-1', 'chat-turn-1:segment:1']}
    assert await turn_run_ids_many(product.runtime, list(expected)) == expected
    for turn_id, values in expected.items():
        assert await turn_run_ids(product.runtime, turn_id) == values
    assert await turn_run_ids_many(product.runtime, []) == {}


@pytest.mark.parametrize('change', ['stale_profile', 'disabled_sender', 'membership'])
async def test_chat_relationship_and_access_are_fresh_on_next_read(chat_product, change):
    product = chat_product
    session_id = await seed_chat(product, turns=3, linked=True)
    if change != 'membership':
        # Shared web history may still be read after a sender link expires,
        # but that link must no longer confer self/management controls.
        async with product.database.sessions.begin() as db:
            await db.execute(update(Session).where(Session.id == session_id).values(surface='web'))
    _, before = await measure_chat(product, session_id, samples=1)
    assert all(turn['is_self'] for turn in before['chat']['turns'])
    if change == 'stale_profile':
        async with product.source.begin() as db:
            await db.execute(text("UPDATE moyai.users SET profile_checked_at='2000-01-01T00:00:00+00:00' "
                                  'WHERE id=:id'), {'id': product.raw_principal.id})
    elif change == 'disabled_sender':
        async with product.database.sessions.begin() as db:
            await db.execute(update(User).where(User.id == product.raw_principal.id).values(enabled=False))
    else:
        product.state['members'] = []
        product.state['clock'] += fixtures.AUDIENCE_TTL
        app = create_app(product.runtime, run_dispatcher=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                    cookies={'workspace_session': product.cookie}) as client:
            for suffix in ('?resolve=true&include_activity=false&include_messages=true', '/workspace'):
                assert (await client.get(f'/api/sessions/{session_id}' + suffix)).status_code == 404
        return
    _, after = await measure_chat(product, session_id, samples=1)
    assert all(not turn['is_self'] for turn in after['chat']['turns'])
    assert not after['chat']['session']['can_manage']
    assert after['chat']['transcript_version'] != before['chat']['transcript_version']
    assert [row['content'] for row in after['chat']['messages']] == [
        row['content'] for row in before['chat']['messages']]


def baseline_app(monkeypatch, tmp_path, product):
    """Load exact original routes for opt-in comparison, without editing checkout files."""
    base = '1b9c1a2d5f78fd90f79c54867e068a208f5a512a'
    root = Path(__file__).resolve().parents[2]
    modules = {}
    for name in ('database', 'product_routes', 'history_handoff', 'workspace', 'api'):
        path = f'native/src/moyai/{name}.py'
        source = subprocess.check_output(['git', 'show', f'{base}:{path}'], cwd=root, text=True)
        saved = tmp_path / f'{name}.py'
        saved.write_text(source)
        spec = spec_from_file_location(f'moyai._chat_baseline_{name}', saved)
        assert spec is not None and spec.loader is not None
        module = module_from_spec(spec)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        spec.loader.exec_module(module)
        modules[name] = module
    monkeypatch.setitem(sys.modules, 'moyai.product_routes', modules['product_routes'])
    monkeypatch.setattr('moyai.history_handoff.turn_run_ids', modules['history_handoff'].turn_run_ids)
    for name in ('allowed_session', 'execution_session', 'check_source_audience', 'session', 'messages'):
        monkeypatch.setattr(type(product.database), name, getattr(modules['database'].Database, name))
    modules['api'].workspace_routes = modules['workspace'].routes
    return modules['api'].create_app


@pytest.mark.skipif(not os.getenv('MOYAI_BENCHMARK_CHAT'), reason='Opt-in comparative benchmark.')
@pytest.mark.parametrize('linked,turns,context', [
    (False, 1, True), (True, 0, True), (False, 20, False), (True, 20, False), (True, 100, True),
])
async def test_chat_database_benchmark(chat_product, linked, turns, context, monkeypatch, tmp_path):
    product = chat_product
    session_id = await seed_chat(product, turns=turns, linked=linked)
    if linked and turns == 0:
        async with product.database.sessions.begin() as db:
            turn_id = await db.scalar(select(Turn.id).where(Turn.session_id == session_id))
            db.add(Message(session_id=session_id, turn_id=turn_id, role='assistant', identity='small-reply',
                           content=product.database.security.seal('Public reply')))
    if context:
        await seed_context(product, session_id)
    with monkeypatch.context() as original:
        before, expected = await measure_chat(product, session_id,
                                             app_factory=baseline_app(original, tmp_path, product))
    after, actual = await measure_chat(product, session_id)
    assert actual == expected
    print('\nCHAT_DATABASE ' + json.dumps({'linked': linked, 'turns': turns, 'context': context,
                                          'before': before, 'after': after}))
