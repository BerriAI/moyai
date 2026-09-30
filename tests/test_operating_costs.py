from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.operating_costs import modal_total, month_bounds
from test_spend import active, sign_in
from test_workspace import workspace


def statement(**changes):
    return {'month': '2025-01', 'usage_cost': '0.123456789012', 'allocated_fee': '2',
            'credits_applied': '0.10', 'scope': 'Moyai resources only',
            'source_reference': 'Invoice jan-2025', 'allocation_note': '2% of shared support fee',
            'observed_at': '2025-02-03T00:00:00Z', 'finalized': True, 'revision': 0, **changes}


def enable(app):
    app.state.settings.operating_costs_enabled = True
    app.state.operating_costs.initialize()


def test_disabled_by_default_does_not_touch_provider_or_create_tables(workspace):
    app, client = workspace
    assert client.get('/api/admin/operating-costs').json() == {'enabled': False}
    assert client.put('/api/admin/operating-costs/modal', json=statement()).status_code == 404
    assert client.post('/api/admin/operating-costs/modal/preview', json={'month': '2025-01'}).status_code == 404
    assert not app.state.store.rows("SELECT name FROM sqlite_master WHERE name='operating_statements'")


def test_all_routes_require_admin_and_mutations_require_csrf(workspace):
    app, client = workspace
    enable(app)
    sign_in(app, client)
    assert client.put('/api/admin/operating-costs/modal', json=statement(), headers={'X-CSRF-Token': ''}).status_code == 403
    assert client.post('/api/admin/operating-costs/modal/preview', json={'month': '2025-01'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/admin/operating-costs').status_code == 403
    assert client.get('/api/admin/operating-costs/audit/modal?month=2025-01').status_code == 403
    assert client.put('/api/admin/operating-costs/modal', json=statement()).status_code == 403
    assert client.post('/api/admin/operating-costs/modal/preview', json={'month': '2025-01'}).status_code == 403
    client.cookies.clear()
    assert client.get('/api/admin/operating-costs').status_code == 401


def test_exact_totals_missing_credits_and_month_isolation(workspace):
    app, client = workspace
    enable(app)
    assert client.put('/api/admin/operating-costs/modal', json=statement()).status_code == 200
    report = client.get('/api/admin/operating-costs?month=2025-01').json()
    assert report['known_infrastructure_cost'] == '2.123456789012'
    assert report['reported_providers'] == 1
    assert report['known_combined_after_credits'] is None
    assert report['providers'][1]['statement'] is None
    assert not report['all_statements_final']
    for provider in ['temporal', 'render']:
        assert client.put('/api/admin/operating-costs/'+provider, json=statement(usage_cost='1', allocated_fee='0', credits_applied='0')).status_code == 200
    report = client.get('/api/admin/operating-costs?month=2025-01').json()
    assert report['known_infrastructure_cost'] == '4.123456789012'
    assert report['known_combined_after_credits'] == '4.023456789012'
    assert report['all_statements_final']
    assert client.get('/api/admin/operating-costs?month=2025-02').json()['reported_providers'] == 0
    response = client.put('/api/admin/operating-costs/render', json=statement(revision=1, credits_applied=None, finalized=False))
    assert response.status_code == 200
    report = client.get('/api/admin/operating-costs?month=2025-01').json()
    assert report['known_combined_after_credits'] is None
    assert report['providers'][2]['statement']['net'] is None


def test_replace_idempotency_conflicts_and_durable_audit(workspace):
    app, client = workspace
    enable(app)
    sign_in(app, client)
    url = '/api/admin/operating-costs/modal'
    for _ in range(2):
        assert client.put(url, json=statement()).json() == {'revision': 1}
    assert client.put(url, json=statement(usage_cost='9')).status_code == 409
    assert client.put(url, json=statement(usage_cost='9', revision=1)).json() == {'revision': 2}
    report = client.get('/api/admin/operating-costs?month=2025-01').json()
    assert report['known_infrastructure_cost'] == '11'  # replaced, never added
    audit = client.get('/api/admin/operating-costs/audit/modal?month=2025-01').json()
    assert len(audit) == 2 and audit[0]['actor_id'] == 'google:alice'
    assert audit[1]['payload']['usage_cost'] == '0.123456789012'
    app.state.operating_costs.initialize()
    assert client.get('/api/admin/operating-costs?month=2025-01').json()['known_infrastructure_cost'] == '11'


@pytest.mark.parametrize('changes', [
    {'usage_cost': '-1'}, {'usage_cost': 'NaN'}, {'usage_cost': 'Infinity'}, {'usage_cost': '10000001'},
    {'usage_cost': '0.1234567890123'}, {'month': '2025-13'}, {'month': '2025-1'},
    {'credits_applied': '200'}, {'allocation_note': ''}, {'finalized': True, 'credits_applied': None},
    {'observed_at': '2025-01-15T00:00:00Z'}, {'observed_at': '2099-01-01T00:00:00Z'},
    {'observed_at': '2025-02-03T00:00:00'}, {'observed_at': '2024-12-31T23:59:59Z'},
    {'unexpected': 'value'},
])
def test_invalid_statements_do_not_write(workspace, changes):
    app, client = workspace
    enable(app)
    assert client.put('/api/admin/operating-costs/modal', json=statement(**changes)).status_code == 422
    assert not app.state.store.rows('SELECT * FROM operating_cost_audit')


def test_current_month_cannot_be_final_and_invalid_filters_are_422(workspace):
    app, client = workspace
    enable(app)
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    assert client.put('/api/admin/operating-costs/modal', json=statement(month=month, observed_at=datetime.now(timezone.utc).isoformat())).status_code == 422
    assert client.get('/api/admin/operating-costs?month=garbage').status_code == 422
    assert client.get('/api/admin/operating-costs/audit/modal?month=garbage').status_code == 422
    assert client.put('/api/admin/operating-costs/unknown', json=statement()).status_code == 422


def test_combined_model_total_retains_old_keys_and_unknown_costs(workspace):
    app, client = workspace
    enable(app)
    run = active(app)
    for key, cost in [('old', '1.123456789012'), ('new', '0.1'), ('new', None)]:
        app.state.settings.litellm_api_key = key
        rid = app.state.spend.begin(run, 'openai/gpt-6-astra')
        app.state.store.execute("UPDATE model_requests SET cost=?,created_at='2025-01-02T00:00:00+00:00',status='completed' WHERE id=?", (cost, rid))
    report = client.get('/api/admin/operating-costs?month=2025-01').json()
    assert report['llm']['recorded_cost'] == '1.223456789012'
    assert report['llm']['missing_costs'] == 1
    # Current-key user accounting stays unchanged.
    from datetime import date
    assert app.state.spend.report(date(2025, 1, 1), date(2025, 1, 31))['total']['spend'] == '0.1'


def test_month_boundaries_and_modal_allowlist():
    start, end = month_bounds('2025-12')
    assert end.isoformat() == '2026-01-01T00:00:00+00:00'
    rows = [SimpleNamespace(object_id=obj, interval_start=start, cost=Decimal(cost))
            for obj, cost in [('ap-moyai', '0.1'), ('vo-moyai', '0.2'), ('ap-other', '100')]]
    assert modal_total(rows, {'ap-moyai', 'vo-moyai'}, start, end) == (Decimal('0.3'), ['ap-moyai', 'vo-moyai'])
    assert modal_total(rows, {'ap-missing'}, start, end) == (Decimal(0), [])
    rows[0].interval_start = end
    with pytest.raises(ValueError):
        modal_total(rows, {'ap-moyai'}, start, end)


def test_modal_preview_is_scoped_does_not_save_and_redacts_errors(workspace, monkeypatch):
    app, client = workspace
    enable(app)
    url = '/api/admin/operating-costs/modal/preview'
    assert client.post(url, json={'month': '2025-01'}).status_code == 422
    app.state.settings.operating_costs_modal_object_ids = 'ap-moyai,vo-moyai'
    app.state.settings.modal_token_id = 'test-token'
    app.state.settings.modal_token_secret = 'private-test-secret'
    credentials = AsyncMock(return_value=object())
    monkeypatch.setattr('app.operating_costs.modal.Client.from_credentials', SimpleNamespace(aio=credentials))
    start, _ = month_bounds('2025-01')
    export = AsyncMock(return_value=[SimpleNamespace(object_id='ap-moyai', interval_start=start, cost=Decimal('1.25')),
                                   SimpleNamespace(object_id='ap-other', interval_start=start, cost=Decimal('100'))])
    monkeypatch.setattr('app.operating_costs.modal.Workspace.from_context', lambda **kw: SimpleNamespace(billing=SimpleNamespace(report=SimpleNamespace(aio=export))))
    response = client.post(url, json={'month': '2025-01'})
    assert response.status_code == 200 and response.json()['usage_cost'] == '1.25'
    assert response.json()['matched_object_ids'] == ['ap-moyai']
    assert not app.state.store.rows('SELECT * FROM operating_statements')
    assert export.call_args.kwargs['resolution'] == 'h'
    export.side_effect = RuntimeError('Forbidden private-test-secret')
    response = client.post(url, json={'month': '2025-01'})
    assert response.status_code == 503 and 'private-test-secret' not in response.text
    assert client.post(url, json={'month': '2099-01'}).status_code == 422


def test_provisional_statements_age_but_final_statements_do_not(workspace):
    app, client = workspace
    enable(app)
    client.put('/api/admin/operating-costs/modal', json=statement(finalized=False))
    client.put('/api/admin/operating-costs/temporal', json=statement())
    rows = client.get('/api/admin/operating-costs?month=2025-01').json()['providers']
    assert rows[0]['statement']['stale'] is True
    assert rows[1]['statement']['stale'] is False
