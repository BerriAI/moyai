import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import billing_sources
from app.billing_sources import BillingUnavailable, temporal_csv
from app.infrastructure_costs import InfrastructureCosts, MonthlyBill
from test_spend import active, sign_in
from test_workspace import workspace


def infra(app):
    return app.state.spend.infrastructure


def bill(provider='render', amount='31', month='2026-09', **extra):
    return {'provider': provider, 'month': month, 'amount': amount, **extra}


def query(client, start='2026-09-01', end='2026-09-30'):
    response = client.get('/api/admin/spend', params={'start': start, 'end': end})
    assert response.status_code == 200
    return response.json()


def test_cost_equation_exact_decimals_overrides_and_persistence(workspace):
    app, client = workspace
    run = active(app)
    rid = app.state.spend.begin(run, run['model'])
    app.state.store.execute("UPDATE model_requests SET created_at='2026-09-02T00:00:00+00:00',cost='0.123456789',status='completed' WHERE id=?", (rid,))
    for provider, amount in [('render','25.25'),('modal','13.02'),('temporal','4.5'),('raindrop','2.25')]:
        assert client.put('/api/admin/spend/infrastructure/bills', json=bill(provider,amount)).status_code == 200
    app.state.store.execute('INSERT INTO infrastructure_daily_costs VALUES(?,?,?,?,?)', ('modal',infra(app).scope('modal'),'2026-09-02','1000','2026-10-01T00:00:00+00:00'))
    data = query(client)
    assert data['cost_summary'] == {'llm':'0.123456789','infrastructure':'45.02','total':'45.143456789','estimated':'0','incomplete':False}
    reopened = InfrastructureCosts(app.state.store, app.state.settings, app.state.security, infra(app).checkpoints)
    assert reopened.report(date(2026,9,1),date(2026,9,30))['spend'] == '45.02'
    assert client.delete('/api/admin/spend/infrastructure/bills',params={'provider':'modal','month':'2026-09','revision':1}).status_code == 200
    assert query(client)['cost_summary']['infrastructure'] == '1032.00'
    assert len(app.state.store.rows('SELECT * FROM infrastructure_bill_audit')) == 5


def test_prorating_estimates_credits_and_cross_month_boundaries(workspace):
    app, client = workspace
    for data in [bill('render','31','2024-01',kind='estimate'),bill('render','29','2024-02'),bill('modal','-2.9','2024-02')]:
        assert client.put('/api/admin/spend/infrastructure/bills',json=data).status_code == 200
    data = query(client,'2024-01-31','2024-02-02')
    assert Decimal(data['cost_summary']['infrastructure']) == Decimal('2.8')
    assert Decimal(data['cost_summary']['estimated']) == 1
    assert data['cost_summary']['incomplete'] is True
    assert next(p for p in data['infrastructure']['providers'] if p['provider']=='temporal')['covered_days'] == 0


def test_admin_csrf_validation_and_stale_bill_edits(workspace):
    app, client = workspace
    path = '/api/admin/spend/infrastructure/bills'
    assert client.put(path,json=bill(),headers={'X-CSRF-Token':''}).status_code == 403
    for invalid in [bill(amount='NaN'),bill(amount='Infinity'),bill(month='2026-13'),bill(provider='<script>'),bill(amount='1.0000001')]:
        assert client.put(path,json=invalid).status_code == 422
    assert client.put(path,json=bill()).status_code == 200
    assert client.put(path,json=bill(amount='88')).status_code == 409
    assert client.put(path,json=bill(amount='88',revision=1)).status_code == 200
    assert client.delete(path,params={'provider':'render','month':'2026-09','revision':1}).status_code == 409
    assert client.post('/api/admin/spend/infrastructure/sync',json={'start':'2026-01-01','end':'2026-09-01'}).status_code == 422
    sign_in(app,client,'bob','bob@berri.ai')
    assert client.put(path,json=bill()).status_code == 403
    assert client.post('/api/admin/spend/infrastructure/sync',json={'start':'2026-09-01','end':'2026-09-30'}).status_code == 403
    client.cookies.clear()
    assert client.put(path,json=bill()).status_code == 401


async def test_sync_replaces_late_data_and_failure_retains_previous_costs(workspace,monkeypatch):
    app, client = workspace
    costs = infra(app)
    client.portal.call(costs.close)
    app.state.settings.modal_billing_enabled = True
    app.state.settings.modal_token_id = 'test'
    app.state.settings.modal_token_secret = 'private-secret'
    costs.enqueue(date(2026,1,1),date(2026,1,2))
    job = app.state.store.rows('SELECT * FROM infrastructure_sync_jobs')[0]
    monkeypatch.setattr(billing_sources,'modal_costs',AsyncMock(return_value={'2026-01-01':Decimal('0.04')}))
    await costs.process(job)
    assert costs.report(date(2026,1,1),date(2026,1,2))['spend'] == '0.04'
    costs.enqueue(date(2026,1,1),date(2026,1,2))
    assert len(app.state.store.rows('SELECT * FROM infrastructure_sync_jobs')) == 1
    monkeypatch.setattr(billing_sources,'modal_costs',AsyncMock(return_value={'2026-01-01':Decimal('0.06')}))
    await costs.process(job)
    assert costs.report(date(2026,1,1),date(2026,1,2))['spend'] == '0.06'
    monkeypatch.setattr(billing_sources,'modal_costs',AsyncMock(side_effect=RuntimeError('private-secret signed-download-url')))
    await costs.process(job)
    report = costs.report(date(2026,1,1),date(2026,1,2))
    assert report['spend'] == '0.06'
    assert 'private-secret' not in json.dumps(report)
    assert next(p for p in report['providers'] if p['provider']=='modal')['sync']['status'] == 'error'
    app.state.settings.modal_app_name = 'different-project'
    assert costs.report(date(2026,1,1),date(2026,1,2))['spend'] == '0'


async def test_modal_uses_exact_app_and_allocated_resources_only(workspace,monkeypatch):
    app, _ = workspace
    app.state.settings.modal_billing_object_ids = 'vo-moyai'
    rows = [SimpleNamespace(object_id=key,cost=cost,interval_start=datetime(2026,9,1,tzinfo=timezone.utc))
            for key,cost in [('ap-moyai','1.00001'),('ap-other','999'),('vo-moyai','0.01')]]
    lookup = AsyncMock(return_value=SimpleNamespace(app_id='ap-moyai'))
    monkeypatch.setattr(billing_sources.modal.Client,'from_credentials',SimpleNamespace(aio=AsyncMock(return_value='client')))
    monkeypatch.setattr(billing_sources.modal.App,'lookup',SimpleNamespace(aio=lookup))
    monkeypatch.setattr(billing_sources.modal.Workspace,'from_context',lambda **kw: SimpleNamespace(billing=SimpleNamespace(report=SimpleNamespace(aio=AsyncMock(return_value=rows)))))
    assert await billing_sources.modal_costs(app.state.settings,date(2026,9,1),date(2026,9,2)) == {'2026-09-01':Decimal('1.01001')}
    assert lookup.call_args.kwargs['create_if_missing'] is False


CSV = '''ResourceID,BillingCurrency,ContractedCost,ChargePeriodStart,ChargePeriodEnd
moyai.account,USD,123.456,2026-09-01T00:00:00Z,2026-09-02T00:00:00Z
other.account,USD,9999999,2026-09-01T00:00:00Z,2026-09-02T00:00:00Z
moyai.account,USD,-3,2026-09-01T00:00:00Z,2026-09-02T00:00:00Z
'''


def test_temporal_cents_namespace_filtering_and_invalid_formats():
    assert temporal_csv(CSV,'moyai.account',date(2026,9,1),date(2026,9,2)) == {'2026-09-01':Decimal('1.20456')}
    for content in [CSV.replace('USD','EUR'),CSV.replace('123.456','NaN'),CSV.replace('2026-09-02','2026-10-01'),'not,a,report']:
        with pytest.raises(BillingUnavailable):
            temporal_csv(content,'moyai.account',date(2026,9,1),date(2026,9,2))


async def test_temporal_report_resumes_and_downloads_all_files_without_credentials(workspace,monkeypatch):
    app, _ = workspace
    app.state.settings.temporal_billing_api_key = 'private-key'
    app.state.settings.temporal_namespace = 'moyai.account'
    generated, calls = False, []
    def service(request):
        calls.append(request)
        if request.url.host == 'reports.s3.amazonaws.com':
            assert 'authorization' not in request.headers
            return httpx.Response(200,text=CSV)
        assert request.headers['authorization'] == 'Bearer private-key'
        if request.method == 'POST':
            body = json.loads(request.content)
            assert body['asyncOperationId'] == 'durable-id'
            assert body['spec']['startTimeInclusive'].startswith('2026-09-01')
            assert body['spec']['endTimeExclusive'].startswith('2026-10-01')
            return httpx.Response(200,json={'billingReportId':'report-123'})
        return httpx.Response(200,json={'billingReport':{'state':'BILLING_REPORT_STATE_GENERATED' if generated else 'BILLING_REPORT_STATE_IN_PROGRESS','downloadInfo':[{'fileFormat':'FILE_FORMAT_CSV','url':'https://reports.s3.amazonaws.com/one'},{'fileFormat':'FILE_FORMAT_CSV','url':'https://reports.s3.amazonaws.com/two'}]}})
    real = httpx.AsyncClient
    monkeypatch.setattr(billing_sources.httpx,'AsyncClient',lambda **kw: real(transport=httpx.MockTransport(service),**kw))
    state,saved = {'operation_id':'durable-id'},[]
    assert await billing_sources.temporal_costs(app.state.settings,date(2026,9,1),date(2026,9,2),state,lambda x:saved.append(dict(x))) is None
    assert saved == [{'operation_id':'durable-id','report_id':'report-123'}]
    generated = True
    assert await billing_sources.temporal_costs(app.state.settings,date(2026,9,1),date(2026,9,2),state,lambda x:None) == {'2026-09-01':Decimal('2.40912')}
    assert len([r for r in calls if r.method=='POST']) == 1


async def test_temporal_download_rejects_internal_urls(workspace,monkeypatch):
    app, _ = workspace
    real = httpx.AsyncClient
    def service(request):
        assert request.url.host == 'saas-api.tmprl.cloud'
        return httpx.Response(200,json={'billingReport':{'state':'BILLING_REPORT_STATE_GENERATED','downloadInfo':[{'fileFormat':'FILE_FORMAT_CSV','url':'https://127.0.0.1/private'}]}})
    monkeypatch.setattr(billing_sources.httpx,'AsyncClient',lambda **kw: real(transport=httpx.MockTransport(service),**kw))
    with pytest.raises(BillingUnavailable,match='unsupported report download host'):
        await billing_sources.temporal_costs(app.state.settings,date(2026,9,1),date(2026,9,2),{'report_id':'existing'},lambda x:None)
