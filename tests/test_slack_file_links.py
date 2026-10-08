import html
from urllib.parse import quote

import pytest

from app.file_links import file_link, markdown_reference, valid_file_return_path
from app.slack_chat import slack_text, split_reply
from test_slack import slack_app, wait_for
from test_slack_chat import finish, start

RUN = 'a' * 32
BASE = 'https://workspace.example'


@pytest.mark.parametrize('ref', [
    '/workspace/nightly-preflight/cutover-status.md', './report.md', 'report.markdown',
    'sandbox:/workspace/reports/notes.md', 'notes%20%26%20decisions.md',
    '/workspace/reports/notes.md#L2-L4', 'notes.md:2:3', 'café.md',
])
def test_markdown_links_and_inline_file_names_become_slack_native_links(ref):
    url = file_link(BASE, RUN, ref)
    assert url and valid_file_return_path(url.removeprefix(BASE))
    expected = html.escape(url, quote=False)
    assert slack_text(f'[dry-run details]({ref})', public_url=BASE, run_id=RUN) == f'<{expected}|dry-run details>'
    assert slack_text(f'`{ref}`', public_url=BASE, run_id=RUN) == f'<{expected}|{html.escape(ref, quote=False)}>'
    assert slack_text(f'```\n[example]({ref})\n```', public_url=BASE, run_id=RUN).startswith('```\n[example](')


@pytest.mark.parametrize('ref', [
    '/etc/report.md', '../report.md', '/workspace/../report.md', 'repo//report.md',
    'repo/./report.md', 'file:///workspace/report.md', 'https://other.example/report.md',
    'javascript:report.md', '//other.example/report.md', '%2e%2e/report.md',
    'report.md?token=x', 'report.md#other', 'report.md#L3-L2', 'report.md:0',
    'report.md:9007199254740992', 'report%00.md', 'report%ff.md', 'report%zz.md',
    'repo\\report.md', 'report\n.md', 'a' * 1025 + '.md',
])
def test_unsafe_references_cannot_become_file_links_or_sign_in_targets(ref):
    assert not markdown_reference(ref)
    assert file_link(BASE, RUN, ref) is None
    assert not valid_file_return_path(f'/#run={RUN}&file={quote(ref, safe="")}')


def test_existing_web_links_mentions_and_code_keep_their_behavior():
    text = '[docs](https://example.test/doc.md?a=1&b=2) <@U12345678> <!channel> `print(x)`'
    assert slack_text(text, {'U12345678'}, public_url=BASE, run_id=RUN) == (
        '<https://example.test/doc.md?a=1&amp;b=2|docs> <@U12345678> &lt;!channel&gt; `print(x)`')
    assert slack_text('[file](report.md)') == '[file](report.md)'
    for suffix in ['&other=value', '&file=second.md', '#extra', '\n']:
        assert not valid_file_return_path(f'/#run={RUN}&file=report.md{suffix}')


def test_long_paragraph_cannot_split_a_file_link_in_half():
    link = slack_text('[report](/workspace/report.md)', public_url=BASE, run_id=RUN)
    chunks = split_reply('x' * 2560 + ' ' + link + ' end')
    assert any(link in chunk for chunk in chunks)
    assert all(len(chunk) <= 2600 for chunk in chunks)


def test_completed_slack_answer_posts_a_clickable_file_link_in_its_original_thread(slack_app):
    app, _, run_id = start(slack_app)
    finish(app, run_id, '[dry-run details](/workspace/nightly-preflight/cutover-status.md) for the handoff.')
    messages = slack_app[3]
    wait_for(lambda: any('dry-run details' in message.get('text', '') for message in messages))
    posted = next(message for message in messages if 'dry-run details' in message.get('text', ''))
    assert posted['channel'] == 'C12345678' and posted['thread_ts'] == '1790719000.123456'
    assert f'#run={run_id}&amp;file=%2Fworkspace%2Fnightly-preflight%2Fcutover-status.md|dry-run details>' in posted['text']
    assert '[dry-run details](' not in posted['text']
    assert posted['blocks'][0]['text']['text'].startswith('<https://workspace.example/')
    app.state.slack.chat.collect()
    assert len(app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer'")) == 1
