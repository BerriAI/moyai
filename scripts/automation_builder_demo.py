"""Run the real app with an isolated local database and no provider credentials.

    uv run python scripts/automation_builder_demo.py
    http://127.0.0.1:8850/#automations

Saves and manual runs use the real API. Manual runs are simulated; AI generation
is unavailable without a cloud runtime. The temporary database is removed on exit.
"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from app.automations import Definition, Save
from app.config import Settings
from app.main import create_app


def demo(directory):
    # Explicit defaults take precedence over environment variables on the host.
    options = {name: field.get_default(call_default_factory=True)
               for name, field in Settings.model_fields.items()}
    options.update(data_dir=Path(directory), public_url='http://127.0.0.1:8850',
                   organization_name='Moyai preview', demo_step_seconds=0.1)
    app = create_app(Settings(_env_file=None, **options))
    service = app.state.automations
    app.state.store.identity({'method': 'local', 'role': 'admin'})
    service.save(Save(definition=Definition(
        name='Weekly engineering digest', prompt='Review the past week’s work and summarize the main changes with links.',
        mode='demo', plugins=[], metadata={'team': 'engineering'},
        triggers=[{'id': 'weekly', 'schedule': {'frequency': 'weekly', 'weekday': 1}}],
    )), 'shared:local:admin')
    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='moyai-automation-builder-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8850, log_level='warning')
