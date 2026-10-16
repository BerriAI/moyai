"""Pure verified requester equivalence shared by credentials and native adapters."""

from datetime import UTC, datetime

from .identities import PROFILE_MAX_AGE_SECONDS


def same_requester(store, owner_id, actor_id, *, connection=None):
    if owner_id == actor_id:
        return bool(owner_id)
    rows = store.rows('SELECT * FROM users WHERE id IN (?,?)', (owner_id, actor_id), connection=connection)
    owner = next((u for u in rows if u['id'] == owner_id and u['kind'] in {'google', 'cloudflare'}), None)
    actor = next((u for u in rows if u['id'] == actor_id and u['kind'] == 'slack'), None)
    if not owner or not actor or not actor['profile_eligible'] or actor['profile_conflict'] or not actor['email']:
        return False
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(actor['profile_checked_at'])).total_seconds()
    except (ValueError, TypeError):
        return False
    return 0 <= age < PROFILE_MAX_AGE_SECONDS and owner['email'] == actor['email']
