from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.skill_templates import install_pack, load_pack
from app.config import Settings
from app.main import create_app
from app.skills import Skills
from test_skills import create, edit, skill_context
from test_spend import active, sign_in
from test_workspace import workspace


def test_default_startup_installs_pack_without_starting_services(tmp_path):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=tmp_path, litellm_api_key='', litellm_api_base='')
    app = create_app(Settings(_env_file=None, **values))
    try:
        assert len(app.state.skills.rows_for('google:alice')) == 3
        assert app.state.store.rows('SELECT * FROM runs') == []
        assert app.state.store.rows('SELECT * FROM model_requests') == []
        assert len(app.state.store.rows('SELECT * FROM skill_bundles')) == 3
    finally:
        app.state.store.close()


def test_discovery_load_and_reference_use_existing_private_broker_path(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    assert len(install_pack(app.state.skills)['installed']) == 3
    run = active(app)
    app.state.store.execute('UPDATE messages SET content=? WHERE id=?',
                           ('Fix Bugbot findings and failing CI on the existing PR', run['active_message_id']))
    context = skill_context(app, run)
    assert context['available'][0]['reference'] == 'org:pr-feedback-loop'
    assert context['loaded'] == []
    assert all(set(row) == {'reference', 'description', 'revision'} for row in context['available'])
    # Discovery and loading never re-read bundled files after startup.
    monkeypatch.setattr('app.skill_templates.load_pack', lambda: pytest.fail('pack read on inference path'))
    headers = {'Authorization': 'Bearer capability'}
    loaded = client.post(f"/broker/{run['id']}/tools/call", headers=headers,
                         json={'name': 'skills_load', 'arguments': {'name': 'org:pr-feedback-loop'}})
    assert loaded.status_code == 200 and loaded.json()['loaded']
    assert 'instructions' not in loaded.json()
    body = skill_context(app, run)['loaded'][0]
    assert body['instructions'] and body['excerpts'] == []
    path = loaded.json()['files'][0]['path']
    read = client.post(f"/broker/{run['id']}/tools/call", headers=headers,
                      json={'name': 'skills_read_file', 'arguments': {'name': 'org:pr-feedback-loop', 'path': path}})
    assert read.status_code == 200 and 'content' not in read.json()
    assert skill_context(app, run)['loaded'][0]['excerpts'][0]['content']
    # The catalog is public to members, but instruction bodies stay encrypted at rest.
    template = load_pack()[0]
    assert template.instructions not in str(app.state.store.rows('SELECT * FROM skills'))


def test_restart_and_concurrent_install_are_idempotent(workspace):
    app, _ = workspace
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: install_pack(app.state.skills), range(2)))
    assert sum(len(r['installed']) for r in results) == 3
    restarted = Skills(app.state.store, app.state.security, app.state.skills.same_requester)
    assert len(install_pack(restarted)['preserved']) == 3
    assert len(app.state.store.rows('SELECT * FROM skills')) == 3
    assert len(app.state.store.rows('SELECT * FROM skill_audit')) == 3
    assert len(app.state.store.rows('SELECT * FROM skill_bundles')) == 3


def test_templates_and_public_procedures_keep_separate_discovery_and_reads(workspace):
    from agent.skills import catalog, read

    app, client = workspace
    sign_in(app, client)
    install_pack(app.state.skills)
    templates = {template.name for template in load_pack()}
    public = {item['name']: item for item in catalog()}
    assert templates.isdisjoint(public)
    run = active(app)
    headers = {'Authorization': 'Bearer capability'}
    response = client.post(f"/broker/{run['id']}/tools/call", headers=headers,
        json={'name': 'bundled_skill_read', 'arguments': {
            'name': 'coding', 'revision': public['coding']['revision']}})
    assert response.status_code == 200 and response.json()['content']
    template = load_pack()[0]
    assert client.post('/api/skills/' + template.id + '/archive',
        json={'archived': True, 'revision': 1}).status_code == 200
    assert template.name not in {row['name'] for row in app.state.skills.rows_for('google:alice')}
    with pytest.raises(ValueError, match='not found'):
        read(template.name, public['coding']['revision'])


def test_upgrade_pins_loaded_body_and_references_and_never_downgrades(workspace):
    app, client = workspace
    sign_in(app, client)
    template = load_pack()[0]
    install_pack(app.state.skills, [template])
    run = active(app)
    app.state.skills.load(run, 'org:' + template.name)
    path = next(iter(template.files))
    app.state.skills.call(run, 'skills_read_file', {'name': 'org:' + template.name, 'path': path})
    updated = replace(template, version=2, instructions='New verified procedure.', files={path: 'New reference.'})
    assert install_pack(app.state.skills, [updated])['updated'] == [template.name]
    body = skill_context(app, run)['loaded'][0]
    assert body['revision'] == 1 and body['instructions'] == template.instructions
    assert body['excerpts'][0]['content'] == template.files[path]
    fresh = active(app)
    app.state.skills.load(fresh, 'org:' + template.name)
    assert skill_context(app, fresh)['loaded'][0]['instructions'] == 'New verified procedure.'
    assert install_pack(app.state.skills, [template])['preserved'] == [template.name]
    assert app.state.skills.get(template.id, 'google:alice')['revision'] == 2


@pytest.mark.parametrize('change', ['instructions', 'description', 'name', 'icon', 'files', 'archived'])
def test_upgrade_preserves_admin_customizations_and_archive(workspace, change):
    app, client = workspace
    sign_in(app, client)
    template = load_pack()[0]
    install_pack(app.state.skills, [template])
    if change == 'archived':
        assert client.post('/api/skills/' + template.id + '/archive', json={'archived': True, 'revision': 1}).status_code == 200
    elif change == 'files':
        run = active(app)
        app.state.skills.call(run, 'skills_save', {'name': template.name, 'description': template.description, 'scope': 'organization',
            'expected_revision': 1, 'request_id': 'custom-reference',
            'files': [{'path': 'references/team.md', 'content': 'Our team procedure.'}]})
    else:
        value = {'instructions': 'Our procedure.', 'description': 'Our description.', 'name': 'team-runbook', 'icon': 'review'}[change]
        assert edit(client, template.id, **{change: value}).status_code == 200
    before = dict(app.state.skills.get(template.id, 'google:alice'))
    assert install_pack(app.state.skills, [replace(template, version=2)])['preserved'] == [template.name]
    assert dict(app.state.skills.get(template.id, 'google:alice')) == before
    assert len(app.state.store.rows('SELECT * FROM skills')) == 1


def test_existing_names_and_personal_precedence_are_preserved(workspace):
    app, client = workspace
    sign_in(app, client)
    template = load_pack()[0]
    own = create(client, scope='organization', name=template.name).json()['id']
    assert install_pack(app.state.skills, [template])['preserved'] == [template.name]
    assert app.state.skills.find('org:' + template.name, 'google:alice')['id'] == own
    other = load_pack()[1]
    personal = create(client, name=other.name, client_id='personal-collision').json()['id']
    install_pack(app.state.skills, [other])
    assert app.state.skills.find(other.name, 'google:alice')['id'] == personal
    assert app.state.skills.find('org:' + other.name, 'google:alice')['id'] == other.id


def test_members_cannot_modify_templates_and_archive_removes_discovery(workspace):
    app, client = workspace
    sign_in(app, client)
    template = load_pack()[0]
    install_pack(app.state.skills, [template])
    sign_in(app, client, 'bob', 'bob@berri.ai')
    row = client.get('/api/skills/' + template.id).json()
    assert row['instructions'] and not row['can_manage']
    assert edit(client, template.id, instructions='Member edit.').status_code == 403
    assert client.post('/api/skills/' + template.id + '/archive', json={'archived': True, 'revision': 1}).status_code == 404
    sign_in(app, client)
    assert client.post('/api/skills/' + template.id + '/archive', json={'archived': True, 'revision': 1}).status_code == 200
    assert app.state.skills.rows_for('google:bob') == []
    install_pack(app.state.skills, [template])
    assert app.state.skills.rows_for('google:bob') == []


def test_install_preserves_existing_library_entries(workspace):
    app, client = workspace
    sign_in(app, client)
    saved = create(client, scope='organization').json()['id']
    before = app.state.skills.get(saved, 'google:alice')
    assert len(install_pack(app.state.skills)['installed']) == 3
    assert app.state.skills.get(saved, 'google:alice') == before
    assert len(app.state.store.rows('SELECT * FROM skills')) == 4


def test_disabled_startup_has_no_templates(tmp_path):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=tmp_path, bundled_skills_enabled=False)
    app = create_app(Settings(_env_file=None, **values))
    try:
        assert app.state.skills.rows_for('google:alice') == []
    finally:
        app.state.store.close()


def test_pack_rejects_symlinked_supporting_files(tmp_path):
    entry = tmp_path / 'test-skill'
    entry.mkdir()
    (entry / 'SKILL.md').write_text('---\nname: test-skill\ndescription: A test skill.\nmetadata:\n  moyai:\n    icon: code\n    version: 1\n---\nSome instructions.')
    (tmp_path / 'outside.txt').write_text('Not a bundled resource.')
    (entry / 'reference.md').symlink_to(tmp_path / 'outside.txt')
    with pytest.raises(ValueError, match='symlink'):
        load_pack(tmp_path)
