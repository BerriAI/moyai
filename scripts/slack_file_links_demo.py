"""Run: uv run python scripts/slack_file_links_demo.py; open http://127.0.0.1:8798/demo.

Real Slack answer collection, Moyai UI and archive API with synthetic data.
No Slack messages, provider requests, or production credentials are used.
"""
import html
from io import BytesIO
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi.responses import HTMLResponse

from app.config import Settings
from app.main import create_app

REPORT = '''# Nightly preflight — handoff

**Status:** Ready for review.

## Verified

- The handoff report was saved to the workspace.
- The exact report opens from the link in the Slack reply.
- Download keeps the original Markdown file.

## Next step

Review the report with the team.

This is synthetic data for local verification.
'''
ANSWER = '[dry-run details](/workspace/nightly-preflight/cutover-status.md) for the handoff.'


def demo(directory):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8798', auto_prepare_repositories=False,
                  session_titles_enabled=False, slack_bot_enabled=False)
    app = create_app(Settings(_env_file=None, **values))
    store = app.state.store
    actor = store.identity({'method': 'local', 'role': 'admin'})
    run = store.create_run('Prepare the nightly preflight handoff report', '', 'demo', [], chat_enabled=True, user_id=actor)
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], ANSWER)
    store.update_run(run['id'], status='idle')
    raw = BytesIO()
    with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('new-files/nightly-preflight/cutover-status.md', REPORT)
        archive.writestr('new-files/other/report.md', 'A different file — do not open this one.')
    store.artifacts.save(run['id'] + '.zip', raw.getvalue())
    store.execute('INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts) VALUES(?,?,?,?,?)',
                  ('T12345678', 'C12345678', '1790719000.123456', run['id'], '1790719000.123456'))
    with store.connect() as conn:
        binding = conn.execute('SELECT * FROM slack_threads WHERE run_id=?', (run['id'],)).fetchone()
        app.state.slack.chat.collect_answers_in(conn, binding, True)
    payload = store.rows("SELECT text FROM slack_outbox WHERE kind='answer'")[0]['text']
    # A payload inspector, not a replacement Slack client or live delivery.
    display = re.sub(r'<(https?://[^|>]+)\|([^>]+)>',
                     lambda m: f'<a href="{html.escape(html.unescape(m[1]), quote=True)}">{m[2]}</a>', payload)

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moyai · Slack file links</title><style>
body{{font:16px/1.6 system-ui;background:#f8f7fc;color:#252238;margin:0;padding:40px 24px}}main{{max-width:900px;margin:auto}}h1{{font-size:30px;line-height:1.2}}p{{color:#625d72}}section{{background:white;border:1px solid #ddd7ed;border-radius:12px;padding:24px;margin:24px 0}}h2{{font-size:16px;margin:0 0 16px}}code,pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}}a{{color:#5b3fd1}}.answer{{font-size:21px;white-space:pre-wrap}}summary{{cursor:pointer}}
</style></head><body><main><p>Moyai · Local verification</p><h1>Open the handoff report from Slack</h1>
<p>Real outgoing message formatting and saved file viewer. Synthetic report; no live Slack message is sent.</p>
<section><h2>Original agent response</h2><code>{html.escape(ANSWER)}</code></section>
<section><h2>Link prepared for Slack</h2><div class="answer">{display}</div></section>
<p>Click <strong>dry-run details</strong> to open the exact saved Markdown report in Moyai, then use Download.</p>
<details><summary>Inspect Slack message text</summary><pre>{html.escape(payload)}</pre></details>
</main></body></html>'''

    print(f"Local demo: http://127.0.0.1:8798/demo (session {run['id']})", flush=True)
    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='moyai-slack-file-links-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8798)
