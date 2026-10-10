"""Real settlement, private draft review, acceptance and normal skill loading."""
import asyncio
import json
import threading

import httpx
import pytest
from fastapi import HTTPException

from app.skill_learning import Preferences, Review
from app.skills import SkillForm
from app.model_slots import ModelSlots
from test_memory_review import review_app

OWNER = 'google:alice'
REPO = 'https://github.com/example/project'
REQUEST = 'Check the benchmark report and use the offline fixtures to validate every case.'


def enable(app, enabled=True):
    service = app.state.skill_learning
    prefs = service.listing(OWNER)['preferences']
    service.set_preferences(OWNER, Preferences(enabled=enabled, revision=prefs['revision']))
    app.state.settings.skill_learning_idle_seconds = 0


def turn(app, *, actor=OWNER, status='completed', command=True, text=REQUEST, mode='modal', parent='', repo=REPO):
    store = app.state.store
    run = store.create_run(text, repo, mode, [], chat_enabled=True, user_id=actor, model=app.state.settings.agent_model)
    if parent:
        store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent, run['id']))
    message = store.claim_message(run['id'])
    if command:
        store.event(run['id'], 'tool', 'Run command', {'tool':'terminal','category':'command','phase':'completed',
            'exit_code':0,'command':'uv run pytest tests/test_benchmark.py -q','output':'PRIVATE OUTPUT MUST NOT BE REVIEWED'})
    store.finish_message(run['id'], message['id'], 'Checked offline fixtures. Benchmark cases passed.', status)
    store.update_run(run['id'], status='idle')
    return run, message['id']


def proposal(data, **changes):
    source = data['sources'][0]
    return {'name':'check-benchmark-fixtures','description':'Validate benchmark reports in example/project using offline fixtures.',
        'instructions':f'# Benchmark fixtures\n\nUse for {REPO}. Run `uv run pytest tests/test_benchmark.py -q` and inspect failures. Keep checks offline.',
        'reason':'The completed benchmark check used the offline fixtures successfully.',
        'target_id':'','target_revision':0,
        'evidence':[{'message_id':source['message_id'],'quote':source['request'],'tool_ids':[c['id'] for c in source['commands']]}], **changes}


def completion(items):
    return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'suggestions':items})}}],
        'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}}, headers={'x-litellm-response-cost':'0.002'})


async def process(app, handler=None):
    service = app.state.skill_learning
    def respond(request):
        data = json.loads(json.loads(request.content)['messages'][1]['content'])
        return handler(data) if handler else completion([proposal(data)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service.client = client
        return await service.process_next()


def form(draft, **changes):
    return SkillForm(**{k:draft[k] for k in ('name','description','instructions')}, scope='personal',
                     revision=draft['target_revision'], client_id='accept-workflow-draft', **changes)


def drafts(app):
    return app.state.skill_learning.listing(OWNER)['suggestions']


def jobs(app):
    return app.state.store.rows('SELECT * FROM skill_learning_jobs ORDER BY message_id')


async def test_opt_in_durable_private_draft_accept_and_load(review_app):
    app = review_app
    service, store = app.state.skill_learning, app.state.store
    turn(app)
    assert jobs(app) == []
    enable(app)
    run, message = turn(app)
    store.finish_message(run['id'], message, 'duplicate')
    assert len(jobs(app)) == 1
    seen = []
    await process(app, lambda data: seen.append(data) or completion([proposal(data)]))
    assert 'PRIVATE OUTPUT' not in json.dumps(seen)
    assert seen[0]['current_message_id'] == message
    assert app.state.skills.rows_for(OWNER) == []
    draft = service.detail(OWNER, drafts(app)[0]['id'])
    assert not service.listing('google:bob')['suggestions']
    with pytest.raises(HTTPException) as error:
        service.detail('google:bob', draft['id'])
    assert error.value.status_code == 404
    persisted = store.rows('SELECT * FROM skill_suggestions')[0]
    assert draft['name'] not in json.dumps(persisted)
    assert draft['instructions'] == json.loads(service.security.decrypt(persisted['encrypted']))['proposal']['instructions']
    result = service.accept(OWNER, False, draft['id'], form(draft))
    assert service.accept(OWNER, False, draft['id'], form(draft)) == result
    assert len(app.state.skills.rows_for(OWNER)) == 1 and drafts(app) == []
    next_run = store.create_run('/personal:check-benchmark-fixtures Check benchmark', REPO, 'modal', [], chat_enabled=True, user_id=OWNER)
    store.claim_message(next_run['id'])
    assert 'Keep checks offline.' in app.state.skills.context(store.run(next_run['id']))
    assert not await process(app, lambda _: pytest.fail('completed job replayed'))
    receipt = store.rows('SELECT * FROM model_requests')[0]
    assert (receipt['user_id'],receipt['message_id'],receipt['total_tokens'],receipt['status']) == (OWNER,message,150,'completed')


@pytest.mark.parametrize('kwargs',[{'status':'failed'}, {'mode':'demo'}, {'parent':'parent-run'}, {'actor':'google:bob'}])
def test_ineligible_tasks_never_enqueue(review_app, kwargs):
    enable(review_app)
    turn(review_app, **kwargs)
    assert jobs(review_app) == []


async def test_opt_out_during_extraction_and_forward_only_reenable(review_app):
    app = review_app
    enable(app)
    turn(app)
    def disable(data):
        enable(app, False)
        return completion([proposal(data)])
    await process(app, disable)
    assert drafts(app) == [] and jobs(app)[0]['status'] == 'skipped'
    turn(app)
    enable(app)
    turn(app)
    seen = []
    await process(app, lambda data: seen.append(data) or completion([proposal(data)]))
    assert len(seen[0]['sources']) == 1
    enable(app, False)
    assert not drafts(app)
    assert app.state.store.rows('SELECT encrypted FROM skill_suggestions')[0]['encrypted'] == ''


@pytest.mark.parametrize('change',['deleted','participant','edited'])
async def test_sources_revalidated_before_apply_and_accept(review_app, change):
    app = review_app
    enable(app)
    run, message = turn(app)
    await process(app)
    draft = app.state.skill_learning.detail(OWNER, drafts(app)[0]['id'])
    if change == 'deleted':
        app.state.store.execute("UPDATE runs SET deleted_at='deleted' WHERE id=?", (run['id'],))
    elif change == 'participant':
        app.state.store.execute("INSERT INTO messages(run_id,role,content,status,user_id,created_at) VALUES(?,'user','another participant','queued','google:bob','now')", (run['id'],))
    else:
        app.state.store.execute("UPDATE messages SET content='Changed request' WHERE id=?", (message,))
    assert drafts(app) == []
    with pytest.raises(HTTPException) as error:
        app.state.skill_learning.accept(OWNER, False, draft['id'], form(draft))
    assert error.value.status_code == 409 and not app.state.skills.rows_for(OWNER)


@pytest.mark.parametrize('change',['quote','tool','scope','secret'])
async def test_unsubstantiated_or_sensitive_proposals_retry_boundedly(review_app, change):
    app = review_app
    enable(app)
    turn(app)
    def invalid(data):
        item = proposal(data)
        if change == 'quote': item['evidence'][0]['quote'] = 'Unrelated words not spoken by the user'
        if change == 'tool': item['evidence'][0]['tool_ids'] = [9999]
        if change == 'scope': item['instructions'] = 'Use this in every repository.'
        if change == 'secret': item['instructions'] += ' password=secret-value'
        return completion([item])
    for attempt in range(3):
        with pytest.raises((ValueError, HTTPException)):
            await process(app, invalid)
        app.state.store.execute("UPDATE skill_learning_jobs SET available_at='' WHERE status='pending'")
    assert jobs(app)[0]['status'] == 'failed' and jobs(app)[0]['attempts'] == 3 and not drafts(app)


async def test_recurrence_can_support_non_command_workflows_and_dismiss_deduplicates(review_app):
    app = review_app
    enable(app)
    turn(app, command=False)
    await process(app, lambda _: completion([]))
    turn(app, command=False)
    def repeated(data):
        return completion([proposal(data, evidence=[{'message_id':s['message_id'],'quote':s['request'],'tool_ids':[]} for s in data['sources']])])
    await process(app, repeated)
    draft = drafts(app)[0]
    app.state.skill_learning.dismiss(OWNER, draft['id'])
    turn(app)
    await process(app)
    assert drafts(app) == []
    assert len(app.state.store.rows('SELECT * FROM skill_suggestions')) == 1


async def test_stale_update_and_org_scope_do_not_bypass_existing_permissions(review_app):
    app = review_app
    enable(app)
    skills = app.state.skills
    skill_id = skills.save(SkillForm(name='benchmark',description='Check benchmarks.',instructions='Old procedure',scope='personal',client_id='old-benchmark'), OWNER, False)
    turn(app)
    await process(app, lambda data: completion([proposal(data, target_id=skill_id,target_revision=1)]))
    draft = app.state.skill_learning.detail(OWNER, drafts(app)[0]['id'])
    organization = form(draft).model_copy(update={'scope':'organization'})
    with pytest.raises(HTTPException) as error:
        app.state.skill_learning.accept(OWNER, False, draft['id'], organization)
    assert error.value.status_code == 403
    skills.save(SkillForm(name='benchmark',description='Check benchmarks.',instructions='Manually improved procedure',scope='personal',revision=1,client_id='manual-benchmark'),OWNER,False,skill_id)
    with pytest.raises(HTTPException) as error:
        app.state.skill_learning.accept(OWNER, False, draft['id'], form(draft))
    assert error.value.status_code == 409
    assert skills.get(skill_id,OWNER)['revision'] == 2 and len(drafts(app)) == 1


async def test_foreground_preempts_and_cancellation_drains_database_claim(review_app, monkeypatch):
    app = review_app
    enable(app)
    turn(app)
    service = app.state.skill_learning
    service.slots = ModelSlots(1)
    entered, release = asyncio.Event(), asyncio.Event()
    async def block(job, data):
        async with service.slots:
            entered.set()
            await release.wait()
            return Review(suggestions=[])
    monkeypatch.setattr(service,'extract',block)
    task = service.slots.run_maintenance(service.process_next())
    await asyncio.wait_for(entered.wait(),2)
    async with asyncio.timeout(2):
        async with service.slots:
            assert task.cancelled()
    assert jobs(app)[0]['status'] == 'pending'
    app.state.store.execute("UPDATE skill_learning_jobs SET available_at=''")
    claimed, finish = threading.Event(), threading.Event()
    original = service.claim
    def blocked_claim():
        result = original()
        claimed.set()
        finish.wait(3)
        return result
    monkeypatch.setattr(service,'claim',blocked_claim)
    task = asyncio.create_task(service.process_next())
    await asyncio.to_thread(claimed.wait,2)
    task.cancel()
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert jobs(app)[0]['status'] == 'pending'


async def test_http_review_owner_csrf_scope_and_idempotent_accept(review_app):
    from fastapi.responses import Response
    app = review_app
    enable(app)
    turn(app)
    await process(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=app.state.settings.public_url) as client:
        assert (await client.get('/api/skill-learning')).status_code == 401
        response = Response()
        sid = app.state.security.new_session(response,identity={'sub':'alice','email':'alice@berri.ai','domain':'berri.ai','name':'Alice'})
        cookie = response.headers['set-cookie'].split('workspace_session=')[1].split(';')[0]
        client.cookies.set('workspace_session',cookie)
        listing = await client.get('/api/skill-learning')
        assert listing.status_code == 200 and listing.headers['cache-control'] == 'no-store'
        suggestion_id = listing.json()['suggestions'][0]['id']
        path = '/api/skill-learning/suggestions/'+suggestion_id
        draft = (await client.get(path)).json()
        assert (await client.post(path+'/accept',json=form(draft).model_dump())).status_code == 403
        client.headers.update({'x-csrf-token':app.state.security.csrf(sid),'origin':app.state.settings.public_url})
        missing_scope = form(draft).model_dump()
        del missing_scope['scope']
        assert (await client.post(path+'/accept',json=missing_scope)).status_code == 422
        first = await client.post(path+'/accept',json=form(draft).model_dump())
        assert first.status_code == 200
        assert (await client.post(path+'/accept',json=form(draft).model_dump())).json() == first.json()
        assert (await client.get('/api/skills')).json()['skills'][0]['name'] == draft['name']


async def test_input_budget_secret_filter_and_restart_recovery(review_app):
    from app.skill_learning import MAX_INPUT_CHARS
    app = review_app
    enable(app)
    turn(app, text='Do not store this password=example-sensitive-value')
    assert await process(app, lambda _:pytest.fail('Sensitive input reached reviewer'))
    for i in range(3):
        turn(app, text=REQUEST+('\x01😃'*5000))
    job = app.state.skill_learning.claim()
    data = app.state.skill_learning.inputs(job)
    assert len(json.dumps(data,ensure_ascii=False)) <= MAX_INPUT_CHARS
    app.state.skill_learning.recover()
    assert all(j['status'] != 'running' for j in jobs(app))


async def test_successful_update_preserves_supporting_files(review_app):
    from app.skill_tools import bundle
    app = review_app
    enable(app)
    skills = app.state.skills
    skill_id = skills.save(SkillForm(name='benchmark',description='Check benchmark.',instructions='Old instructions',scope='personal',client_id='update-benchmark'),OWNER,False)
    # The ordinary library path handles references for every instruction revision.
    with app.state.store.connect() as conn:
        conn.begin_write()
        files = {'references/fixture.md':'Use offline fixtures.'}
        conn.execute('INSERT INTO skill_bundles VALUES(?,?,?,?)', (skill_id,1,skills.security.encrypt(json.dumps(files)),'[]'))
    turn(app)
    await process(app,lambda data:completion([proposal(data,target_id=skill_id,target_revision=1)]))
    draft = app.state.skill_learning.detail(OWNER,drafts(app)[0]['id'])
    app.state.skill_learning.accept(OWNER,False,draft['id'],form(draft))
    assert skills.get(skill_id,OWNER)['revision'] == 2
    with app.state.store.connect() as conn:
        assert bundle(skills,conn,skill_id,2) == {'references/fixture.md':'Use offline fixtures.'}


async def test_request_changed_while_model_is_running_cannot_create_a_draft(review_app):
    app=review_app
    enable(app)
    run,message=turn(app)
    def alter(data):
        app.state.store.execute("UPDATE runs SET deletion_requested_at='requested' WHERE id=?",(run['id'],))
        return completion([proposal(data)])
    await process(app,alter)
    assert not drafts(app) and jobs(app)[0]['status']=='skipped'


async def test_single_unverified_request_cannot_be_promoted(review_app):
    app=review_app
    enable(app)
    turn(app,command=False)
    with pytest.raises(ValueError,match='recurrence'):
        await process(app)
    assert not drafts(app)


async def test_preemption_during_spend_insert_keeps_attribution_and_closes_receipt(review_app,monkeypatch):
    app=review_app
    enable(app)
    turn(app)
    service=app.state.skill_learning
    service.slots=ModelSlots(1)
    inserted,release=threading.Event(),threading.Event()
    original=service.spend.begin
    def slow_begin(*args):
        result=original(*args)
        inserted.set()
        release.wait(3)
        return result
    monkeypatch.setattr(service.spend,'begin',slow_begin)
    task=service.slots.run_maintenance(service.process_next())
    assert await asyncio.to_thread(inserted.wait,2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    receipts=app.state.store.rows('SELECT * FROM model_requests')
    assert len(receipts)==1 and receipts[0]['status']=='interrupted'
    assert jobs(app)[0]['status']=='pending'
