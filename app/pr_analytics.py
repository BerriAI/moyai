"""Receipt-backed PR outcomes and explicitly correlated session costs."""
import time
from collections import defaultdict
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from .session_pull_requests import Receipt, SessionPullRequests
from .security import digest
from .spend import cost_status, gateway_scope, period, stamp


class LinkedSpend(BaseModel):
    spend: Decimal = Decimal(0)
    requests: int = 0
    pending_costs: int = 0
    missing_costs: int = 0

    def add(self, other: 'LinkedSpend') -> None:
        self.spend += other.spend
        self.requests += other.requests
        self.pending_costs += other.pending_costs
        self.missing_costs += other.missing_costs


class Session(BaseModel):
    id: str
    title: str
    deleted: bool


class Contributor(BaseModel):
    user_id: str = 'unattributed'
    name: str = 'Unattributed'
    email: str = ''


class CorrelatedPR(Receipt):
    state: str = 'unknown'
    draft: bool | None = None
    created_at: str | None = None
    merged_at: str | None = None
    tracked_at: str
    stale: bool = False
    user_id: str
    user_name: str
    user_email: str
    sessions: list[Session] = Field(default_factory=list)
    costs: LinkedSpend = Field(default_factory=LinkedSpend)

    def public(self) -> dict[str, object]:
        return {**self.model_dump(mode='json', exclude={'costs'}), **self.costs.model_dump(mode='json')}


def report(service: SessionPullRequests, start: date | None = None, end: date | None = None) -> dict[str, object]:
    start, end, lower, upper = period(start, end)
    store = service.store
    # Read retained history, including soft-deleted sessions, in one DB snapshot.
    # Deletion hides navigation, not the financial/publication record.
    with store.connect() as conn:
        conn.execute('BEGIN')
        publications = [dict(row) for row in conn.execute('''SELECT p.*,r.owner_id,r.chat_enabled,
            roots.root_id,m.user_id AS actor_id FROM github_publications p
            JOIN runs r ON r.id=p.run_id JOIN run_roots roots ON roots.run_id=r.id
            LEFT JOIN messages m ON m.id=p.message_id AND m.run_id=p.run_id
            WHERE p.result!='' ORDER BY p.created_at,p.id''')]
        users = {row['id']: dict(row) for row in conn.execute('SELECT id,kind,name,email,linked_user_id FROM users')}
        runs = {row['id']: dict(row) for row in conn.execute('''SELECT id,display_title,prompt,deleted_at
            FROM runs WHERE id IN (SELECT root_id FROM run_roots WHERE run_id IN (SELECT run_id FROM github_publications))''')}
        ledger = [dict(row) for row in conn.execute('''SELECT q.run_id,q.cost,q.status,roots.root_id,
            q.key_hash,q.gateway_scope,q.cost_recovery_error
            FROM model_requests q JOIN run_roots roots ON roots.run_id=q.run_id
            WHERE roots.root_id IN (SELECT root_id FROM run_roots WHERE run_id IN (SELECT run_id FROM github_publications))''')]


    parsed: list[tuple[dict[str, object], Receipt]] = []
    receipt_ids: dict[str, set[int]] = defaultdict(set)
    for row in publications:
        try:
            receipt = Receipt.model_validate_json(row['result'])
        except ValidationError:
            continue
        parsed.append((row, receipt))
        if receipt.repository_id:
            receipt_ids[receipt.url.casefold()].add(receipt.repository_id)

    family_costs: dict[str, LinkedSpend] = defaultdict(LinkedSpend)
    settings = service.github.settings
    key_hash = digest(settings.litellm_api_key) if settings.litellm_api_key else ''
    scope = gateway_scope(settings.litellm_api_base)
    for row in ledger:
        family = row['root_id']
        billing = cost_status(row, key_hash=key_hash, gateway_scope=scope,
                              enabled=settings.litellm_spend_recovery_enabled)
        family_costs[family].add(LinkedSpend(spend=Decimal(row['cost'] or '0'), requests=1,
            pending_costs=int(billing == 'pending'), missing_costs=int(billing == 'unresolved')))

    def actor(row: dict[str, object]) -> Contributor:
        identity = row['actor_id']
        if not identity and not row['chat_enabled'] and row['message_id'] == 0:
            identity = row['owner_id']
        user = users.get(identity)
        if user and user['kind'] == 'slack' and user['linked_user_id']:
            user = users.get(user['linked_user_id'])
        if not user or user['kind'] not in {'google', 'cloudflare', 'slack'}:
            return Contributor()
        return Contributor(user_id=user['id'], name=user['name'], email=user['email'])

    context = service.context()
    correlated: dict[tuple[int | str, int], CorrelatedPR] = {}
    wanted: list[tuple[str, int, int]] = []
    clock = time.monotonic()
    for row, receipt in parsed:
        candidates = receipt_ids[receipt.url.casefold()]
        # Explicit old receipts establish identity before today's mutable alias.
        if receipt.repository_id is None and len(candidates) == 1:
            receipt = receipt.model_copy(update={'repository_id': next(iter(candidates))})
        # A reused historical name is not enough evidence to pick a repository.
        ambiguous = receipt.repository_id is None and len(candidates) > 1
        target, key, cached = service.status(receipt, None if ambiguous else context)
        if target and receipt.repository_id is None:
            receipt = receipt.model_copy(update={'repository_id': target})
        if key:
            wanted.append(key)
        identity = (target or receipt.repository_id or 'legacy:' + receipt.repository.casefold(), receipt.number)
        snapshot = cached.value if cached else None
        if identity not in correlated:
            person = actor(row)
            correlated[identity] = CorrelatedPR(**receipt.model_dump(),
                tracked_at=stamp(row['created_at']), user_id=person.user_id, user_name=person.name,
                user_email=person.email)
        pr = correlated[identity]
        if snapshot:
            pr.title, pr.url, pr.repository = snapshot.title, snapshot.url, snapshot.repository
            pr.state, pr.draft = snapshot.state, snapshot.draft
            pr.created_at, pr.merged_at = snapshot.created_at, snapshot.merged_at
        pr.stale = bool(cached and (cached.failed or (snapshot and cached.fresh_until <= clock)))
        root = row['root_id']
        if all(session.id != root for session in pr.sessions):
            run = runs[root]
            pr.sessions.append(Session(id=root, title=run['display_title'] or run['prompt'].split('\n')[0][:150],
                                       deleted=bool(run['deleted_at'])))
            pr.costs.add(family_costs[root])
    service.queue_refreshes(wanted)

    prs = list(correlated.values())
    created = sorted((pr for pr in prs if pr.created_at and lower <= pr.created_at < upper),
                     key=lambda pr: (pr.created_at, pr.url), reverse=True)
    merged = sorted((pr for pr in prs if pr.state == 'merged' and pr.merged_at and lower <= pr.merged_at < upper),
                    key=lambda pr: (pr.merged_at, pr.url), reverse=True)
    created_by_person: dict[str, list[CorrelatedPR]] = defaultdict(list)
    for pr in created:
        created_by_person[pr.user_id].append(pr)
    by_person: dict[str, list[CorrelatedPR]] = defaultdict(list)
    for pr in merged:
        by_person[pr.user_id].append(pr)
    leaderboard: list[dict[str, object]] = []
    contributors = created_by_person.keys() | by_person.keys()
    for user_id in contributors:
        entries, creations = by_person[user_id], created_by_person[user_id]
        person = (entries or creations)[0]
        statuses = dict.fromkeys(('merged', 'open', 'draft', 'closed', 'unknown'), 0)
        for pr in creations:
            statuses['draft' if pr.state == 'open' and pr.draft else pr.state] += 1
        roots = {session.id for pr in entries for session in pr.sessions}
        costs = LinkedSpend()
        for root in roots:
            costs.add(family_costs[root])
        leaderboard.append({'user_id': user_id, 'name': person.user_name, 'email': person.user_email,
                            'created_prs': len(creations), 'status_counts': statuses,
                            'merged_prs': len(entries), 'sessions': len(roots), **costs.model_dump(mode='json'),
                            'cost_per_merged_pr': str(costs.spend / len(entries)) if entries else None})
    leaderboard.sort(key=lambda person: (-person['merged_prs'], person['name'].casefold(), person['user_id']))
    return {'start': str(start), 'end': str(end), 'timezone': 'UTC', 'currency': 'USD',
            'pull_requests': [pr.public() for pr in sorted(prs, key=lambda pr: (pr.tracked_at, pr.url), reverse=True)
                              if lower <= pr.tracked_at < upper],
            'created_pull_requests': [pr.public() for pr in created], 'total_created': len(created),
            'merged_pull_requests': [pr.public() for pr in merged], 'leaderboard': leaderboard,
            'total_merged': len(merged), 'contributors': sum(identity != 'unattributed' for identity in contributors),
            'unknown_created_at': sum(pr.created_at is None for pr in prs),
            'unknown_status': sum(pr.state == 'unknown' or (pr.state == 'merged' and not pr.merged_at) for pr in prs),
            'stale_status': sum(pr.stale for pr in prs),
            'pending_refresh': any(key in service.pending for key in wanted)}
