"""Authenticated capacity observations; no prompts, credentials or user IDs."""


def snapshot(store, settings):
    from .database_schema import SQLITE_OCCUPIED_SESSION, POSTGRES_OCCUPIED_SESSION
    # The legacy runner owns an in-process semaphore rather than a durable
    # reservation journal. Do not report its unobserved occupancy as zero.
    occupied = None
    if settings.temporal_enabled:
        predicate = POSTGRES_OCCUPIED_SESSION if store.database else SQLITE_OCCUPIED_SESSION
        occupied = store.rows(f'SELECT COUNT(*) AS count FROM durable_sessions WHERE {predicate}')[0]['count']
    pending = store.rows("""SELECT COUNT(*) AS count FROM runs
        WHERE status NOT IN ('idle','completed','failed','cancelled','interrupted')""")[0]['count']
    phases = store.rows("""SELECT json_text(state,'phase') AS phase,COUNT(*) AS count
        FROM durable_sessions WHERE state!='{}' GROUP BY json_text(state,'phase')""") if settings.temporal_enabled else []
    return {'runtime_role': settings.moyai_runtime_role,
            'sandbox': {'capacity': settings.max_concurrent_runs, 'occupied': occupied},
            'admission': {'capacity': settings.max_pending_runs, 'unfinished': pending},
            'phases': phases,
            'database': {'backend': 'postgresql' if store.database else 'sqlite',
                         'pool': store.database.pool.get_stats() if store.database else {}},
            'worker': {'activities_per_process': settings.temporal_worker_activities,
                       'workflow_cache_per_process': settings.temporal_workflow_cache_size},
            'execution_leases': store.rows("""SELECT COUNT(*) AS count FROM runtime_leases
                WHERE name LIKE 'session:%' AND expires_at>clock_timestamp()""")[0]['count'] if store.database else 0}
