"""Local real dashboard/API backed by synthetic SQLite timing samples.

uv run python scripts/performance_ui_demo.py --port 8852
Open /demo/performance-login. No cloud, providers, SSO or background jobs run.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.responses import HTMLResponse, RedirectResponse
from scripts.session_ui_demo import demo


def build(directory, port):
    app = demo(directory, port)
    store = app.state.store
    current = datetime.now(timezone.utc)
    owner = store.identity({'method': 'google', 'identity': {
        'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex'}})
    # Historical fixtures, not production measurements. The real report queries
    # these rows and calculates every aggregate, percentile and trend interval.
    for index in range(96):
        received = current - timedelta(minutes=14 * index + 1)
        startup = [1.2, 2.3, 3.8, 1.8, 6.4, 12.5, 2.7, 4.1][index % 8]
        pickup = .4 + (index % 5) * .2
        model = ['openai/gpt-6-astra', 'anthropic/claude-opus-4-6'][index % 2]
        run = store.create_run('Synthetic performance sample', '', 'modal', [], chat_enabled=True,
                               user_id=owner, model=model, received_at=received.isoformat())
        message = store.messages(run['id'])[0]
        send = index % 3 == 0
        status = 'queued' if index < 2 else 'failed' if index == 2 else 'injected' if send else 'completed'
        with store.connect() as conn:
            conn.execute('UPDATE messages SET status=? WHERE id=?', (status, message['id']))
            conn.execute("UPDATE runs SET status='idle' WHERE id=?", (run['id'],))
            conn.execute('''UPDATE performance_samples SET kind=?, picked_up_at=?, pickup_kind=?,
                first_model_at=?, send_now_at=?, send_now_kind=? WHERE message_id=?''',
                ('follow_up' if send else 'new_chat',
                 (received + timedelta(seconds=pickup)).isoformat() if index > 2 else None,
                 ('steering' if send else 'turn') if index > 2 else None,
                 (received + timedelta(seconds=startup)).isoformat() if index > 2 and not send else None,
                 received.isoformat() if send else None,
                 ('automatic' if index % 2 else 'explicit') if send else None, message['id']))

    # Only the real HTTP handlers run; no executor or external connection starts.
    @asynccontextmanager
    async def local_lifespan(app):
        yield
        store.close()
    app.router.lifespan_context = local_lifespan
    app.state.manager.submit = lambda run: None

    @app.get('/demo/performance-workspace')
    async def workspace():
        html = (Path(__file__).resolve().parents[1] / 'app/static/index.html').read_text()
        badge = '<div style="position:fixed;right:16px;top:14px;z-index:1000;background:#192330;color:#f8f4e9;border:1px solid #4b647a;border-radius:6px;padding:6px 12px;font:12px system-ui">Local demo · synthetic samples</div>'
        return HTMLResponse(html.replace('</body>', badge + '</body>'))

    @app.get('/demo/performance-login')
    async def login():
        response = RedirectResponse('/demo/performance-workspace#performance')
        app.state.security.new_session(response, identity={
            'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'})
        return response
    return app


if __name__ == '__main__':
    import argparse
    from tempfile import TemporaryDirectory
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8852)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='performance-ui-') as directory:
        uvicorn.run(build(directory, args.port), host='127.0.0.1', port=args.port)
