"""Human adoption derived from persisted submissions, not model/tool call volume."""
from datetime import datetime, timedelta, timezone

from .spend import period


def report(store, start=None, end=None, *, today=None):
    today = today or datetime.now(timezone.utc).date()
    end = end or today
    start = start or end - timedelta(days=29)
    start, end, _, _ = period(start, end)
    if end > today:
        from fastapi import HTTPException
        raise HTTPException(422, 'Choose dates on or before today (UTC).')
    # Compare complete UTC days only, never today's partial traffic to a full day.
    week_end = min(end, today - timedelta(days=1))
    week_start = week_end - timedelta(days=6)
    previous_start = week_start - timedelta(days=7)
    lower = min(start - timedelta(days=6), previous_start)
    upper = max(end, week_end) + timedelta(days=1)
    rows = store.rows('''
        SELECT date(m.created_at) AS day,
               CASE WHEN u.kind='shared' OR m.user_id LIKE 'shared:%' THEN NULL
                    ELSE COALESCE(NULLIF(u.linked_user_id,''), NULLIF(m.user_id,'')) END AS actor,
               COUNT(*) AS requests
        FROM messages m JOIN runs r ON r.id=m.run_id
        LEFT JOIN users u ON u.id=m.user_id
        WHERE m.role='user' AND r.mode='modal' AND r.parent_run_id=''
          AND julianday(m.created_at)>=julianday(?) AND julianday(m.created_at)<julianday(?)
          AND NOT (COALESCE(m.client_id,'')='initial' AND EXISTS (
              SELECT 1 FROM automation_runs a WHERE a.run_id=r.id))
        GROUP BY day, actor
    ''', (lower.isoformat(), upper.isoformat()))
    counts, actors = {}, {}
    for row in rows:
        day = row['day']
        counts[day] = counts.get(day, 0) + row['requests']
        if row['actor']:
            actors.setdefault(day, set()).add(row['actor'])

    def count(day):
        return counts.get(day.isoformat(), 0)

    daily, active = [], set()
    day = start
    while day <= end:
        users = actors.get(day.isoformat(), set())
        active.update(users)
        daily.append({'date': day.isoformat(), 'requests': count(day),
                      'active_users': len(users), 'partial': day >= today,
                      'seven_day_average': round(sum(count(day - timedelta(days=i)) for i in range(7)) / 7, 2)})
        day += timedelta(days=1)
    current = sum(count(week_start + timedelta(days=i)) for i in range(7))
    previous = sum(count(previous_start + timedelta(days=i)) for i in range(7))
    return {'start': str(start), 'end': str(end), 'timezone': 'UTC', 'daily': daily,
            'total_requests': sum(item['requests'] for item in daily), 'active_users': len(active),
            'weekly': {'start': str(week_start), 'end': str(week_end),
                       'previous_start': str(previous_start), 'previous_end': str(week_start - timedelta(days=1)),
                       'requests': current, 'previous_requests': previous, 'delta': current - previous,
                       'percent_change': round((current - previous) / previous * 100, 1) if previous else None}}
