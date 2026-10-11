import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.db import Store
from app.temporal_runtime import TemporalRunManager
from test_durable import durable, drive
from test_swarms import enable


@pytest.mark.parametrize('independent', [False, True])
async def test_live_extension_survives_original_deadline_without_relaunch(durable, monkeypatch, independent):
    manager, cloud, run_id = durable
    manager.settings.independent_agent_runtime = independent
    manager.store.execute("UPDATE runs SET harness='codex' WHERE id=?", (run_id,))
    enable(manager, run_id, 60)
    original_deadline = datetime.fromisoformat(manager.swarms.get(run_id)['ends_at']).timestamp()
    cloud.finished = False
    await drive(manager, run_id, phase='monitor')
    sandbox_id = manager.state(run_id)['sandbox_id']

    await manager.swarms.set_time(run_id, 2 * 3600)
    monkeypatch.setattr('app.swarms.time', SimpleNamespace(time=lambda: original_deadline + 1))
    await manager.advance(run_id)

    assert manager.swarms.get(run_id)['status'] == 'active'
    assert manager.state(run_id)['phase'] == 'monitor'
    assert manager.state(run_id)['sandbox_id'] == sandbox_id
    assert len(cloud.launches) == 1 and not cloud.terminations
    assert cloud.machines[0].spec['timeout'] is None
    assert 'deadline_at' not in cloud.machines[0].spec
    assert cloud.machines[0].spec['independent_runtime'] is independent


async def test_unlimited_pause_survives_restart_with_serializable_timer(durable):
    manager, cloud, run_id = durable
    enable(manager, run_id, 0)
    await manager.swarms.pause(run_id)
    replacement = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    result = await replacement.advance(run_id)
    json.dumps(result, allow_nan=False)
    assert result['retry_seconds'] > 0
    assert replacement.swarms.get(run_id)['status'] == 'paused'
    await replacement.swarms.resume(run_id)
    assert replacement.swarms.get(run_id)['ends_at'] == ''
    assert replacement.swarms.get(run_id)['status'] == 'active'


@pytest.mark.parametrize('independent', [False, True])
async def test_unlimited_swarm_preserves_administrator_response_timeout(durable, independent):
    manager, cloud, run_id = durable
    manager.settings.independent_agent_runtime = independent
    manager.store.execute("UPDATE runs SET harness='codex' WHERE id=?", (run_id,))
    manager.settings.run_timeout_seconds = 600
    enable(manager, run_id, 0)
    await drive(manager, run_id, phase='launch')
    assert 0 < cloud.machines[0].spec['timeout'] <= 600
    assert 'deadline_at' not in cloud.machines[0].spec
