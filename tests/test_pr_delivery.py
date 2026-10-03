import hashlib
import json

from app import captures, pr_delivery
from test_computer import PNG, WEBM
from test_slack import slack_app
from test_slack_chat import publication, start
from test_workspace import workspace


def test_pr_cards_require_a_receipt_from_this_run_or_a_direct_child(workspace):
    app, _ = workspace
    store = app.state.store
    runs = [store.create_run('PR handoff', '', 'demo', [])['id'] for _ in range(4)]
    parent, other, child, grandchild = runs
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (parent, child))
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (child, grandchild))
    urls = [publication(app, run_id, number) for number, run_id in enumerate(runs, 100)]
    answer = '\n'.join(f'[View PR]({url})' for url in urls)
    with store.connect() as conn:
        selected = pr_delivery.select_prs(conn, parent, answer)
        assert [pr.url for pr in selected] == [urls[0], urls[2]]
        # A child cannot borrow its parent's publication receipt.
        assert [pr.url for pr in pr_delivery.select_prs(conn, child, answer)] == [urls[2], urls[3]]


def test_pr_selection_requires_the_exact_url_and_supports_legacy_receipts(workspace):
    app, _ = workspace
    store = app.state.store
    run_id = store.create_run('Legacy PR handoff', '', 'demo', [])['id']
    url = publication(app, run_id)
    legacy = {'number': 100, 'repository': 'berriai/moyai-devin', 'url': url}
    store.execute('UPDATE github_publications SET result=? WHERE run_id=?', (json.dumps(legacy), run_id))
    with store.connect() as conn:
        for answer in [url + '0', url + '/files', url + '?fake=1',
                       url.replace('github.com', 'github.com.evil.example'),
                       'https://github.com/BerriAI/moyai-devin/pull/999']:
            assert pr_delivery.select_prs(conn, run_id, answer) == []
        selected = pr_delivery.select_prs(conn, run_id, f'<{url}|View PR>\n{url}.')
        assert len(selected) == 1
        assert selected[0].url == url and selected[0].title == 'Pull request'
    # Even a stored result must agree with its own repository and PR number.
    store.execute('UPDATE github_publications SET result=? WHERE run_id=?',
                  (json.dumps({**legacy, 'number': 999}), run_id))
    with store.connect() as conn:
        assert pr_delivery.select_prs(conn, run_id, url) == []


def test_capture_selection_uses_this_answers_exact_paths_and_one_of_each_kind(workspace):
    app, _ = workspace
    run_id = app.state.store.create_run('Capture handoff', '', 'demo', [])['id']
    root = captures.directory(app.state.settings, run_id)
    root.mkdir(parents=True)
    for name, raw in [('first.png', PNG), ('second.png', PNG + b'second'),
                      ('flow.webm', WEBM), ('other.webm', WEBM + b'other')]:
        (root / name).write_bytes(raw)
    answer = ('[Video](/workspace/moyai-captures/flow.webm)\n'
              '![Result](moyai-captures/second.png)\n'
              '[Extra](moyai-captures/first.png) [Extra video](moyai-captures/other.webm)')
    selected = pr_delivery.select_captures(app.state.settings, run_id, answer)
    assert [capture.name for capture in selected] == ['flow.webm', 'second.png']
    assert [capture.sha256 for capture in selected] == [
        hashlib.sha256(WEBM).hexdigest(), hashlib.sha256(PNG + b'second').hexdigest()]
    assert pr_delivery.select_captures(app.state.settings, run_id, 'No demo in this answer.') == []
    for reference in [
        '/workspace/moyai-captures/first.png.extra',
        '/workspace/moyai-captures/first.png?download=true',
        '/workspace/moyai-captures/first.png/child',
        '/workspace/private/moyai-captures/first.png',
        '/workspace/moyai-captures/../first.png',
        'https://other.example/moyai-captures/first.png',
        'https://other.example/?file=/workspace/moyai-captures/first.png',
        'https://workspace.example/api/runs/' + 'f' * 32 + '/computer/captures/first.png',
    ]:
        assert pr_delivery.select_captures(app.state.settings, run_id, f'[Other]({reference})') == [], reference


def test_capture_selection_skips_symlinks_invalid_bytes_and_oversized_files(workspace, monkeypatch):
    app, _ = workspace
    run_id = app.state.store.create_run('Invalid captures', '', 'demo', [])['id']
    root = captures.directory(app.state.settings, run_id)
    root.mkdir(parents=True)
    (root / 'valid.png').write_bytes(PNG)
    (root / 'link.png').symlink_to(root / 'valid.png')
    (root / 'corrupt.png').write_bytes(b'<html>not an image</html>')
    (root / 'large.png').write_bytes(PNG + b'x' * 100)
    monkeypatch.setattr(captures, 'MAX_FILE', len(PNG))
    answer = '\n'.join(f'[Capture](moyai-captures/{name})' for name in
                       ['link.png', 'corrupt.png', 'large.png', 'missing.png', 'valid.png'])
    assert [capture.name for capture in pr_delivery.select_captures(app.state.settings, run_id, answer)] == ['valid.png']


async def test_entire_capture_batch_is_revalidated_before_any_external_upload(slack_app, monkeypatch):
    app, _, run_id = start(slack_app)
    channel = app.state.slack.channel
    source = channel.source_for_run(run_id, {'kind': 'answer'})
    credentials = await app.state.connectors.credentials('slack')
    credentials['bot']['scope'] += ',files:write'
    app.state.connectors.save('slack', credentials, 'Test organization')
    root = captures.directory(app.state.settings, run_id)
    root.mkdir(parents=True)
    (root / 'flow.webm').write_bytes(WEBM)
    (root / 'result.png').write_bytes(PNG)
    selected = pr_delivery.select_captures(app.state.settings, run_id,
        '[Video](moyai-captures/flow.webm) ![Image](moyai-captures/result.png)')
    assert [capture.name for capture in selected] == ['flow.webm', 'result.png']
    (root / 'result.png').write_bytes(PNG + b'replaced after answer collection')
    uploads = []

    async def request(method, url, **kwargs):
        if '/files.' in url:
            uploads.append(url)
            raise AssertionError('A changed batch reached Slack')
        return {'ok': True}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    assert await channel.upload_captures(source, [capture.model_dump() for capture in selected]) is False
    assert uploads == []
