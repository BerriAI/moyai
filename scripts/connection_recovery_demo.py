"""Local connection-check fixture; no external requests or real credentials."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.session_ui_demo import demo
from app.connector_errors import ConnectorError
import uvicorn

def create_demo(directory):
    app = demo(directory)
    app.state.connectors.save('linear', {'access_token': 'local-fixture-only'}, 'Local fixture')
    attempts = 0
    async def verify(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        status = [403, 429, 200][(attempts - 1) % 3]
        if status != 200:
            raise ConnectorError(f'Local fixture HTTP {status}')
        return 'Local fixture'
    app.state.connectors.verify = verify
    return app

if __name__ == '__main__':
    with TemporaryDirectory(prefix='connection-demo-', dir='/workspace') as directory:
        uvicorn.run(create_demo(directory), host='127.0.0.1', port=8830)
