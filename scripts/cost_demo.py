"""Local cost dashboard with clearly labeled sample data; no provider credentials."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from app.config import Settings
from app.infrastructure_costs import MonthlyBill
from app.main import create_app

# Explicit defaults prevent loading provider credentials from the host environment.
values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
values.update(data_dir=Path('.data/cost-demo'), public_url='http://127.0.0.1:8791',
              organization_name='Cost tracking · demo data', demo_step_seconds=0.01)
app = create_app(Settings(_env_file=None, **values))
store = app.state.store
costs = app.state.spend.infrastructure
if not store.rows('SELECT id FROM model_requests'):
    user = store.identity({'method':'local','sid':'cost-demo','role':'admin'})
    run = store.create_run('Sample data · cost dashboard verification', '', 'demo', [], user_id=user)
    request_id = app.state.spend.begin(run, 'openai/gpt-6-astra')
    store.execute("UPDATE model_requests SET cost='18.75',status='completed',created_at='2026-09-15T12:00:00+00:00',total_tokens=125000,cost_source='response_header' WHERE id=?", (request_id,))
    store.update_run(run['id'],status='completed',summary='Sample cost data only; no model call was made.')
    for provider, value in [('render','25.25'),('modal','14.00'),('temporal','5.50')]:
        costs.save_bill(MonthlyBill(provider=provider,month='2026-09',amount=value,note='Sample data for local demo; not a real invoice.'),'demo')

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8791)
