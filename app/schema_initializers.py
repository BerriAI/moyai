"""Canonical schema and historical backfills, independent of execution engines.

Function bodies retain their versioned component identities in the schema manifest.
Historical tables and receipts remain available after retiring their old runtimes.
"""
import json

from .automation_sources import sources
from .database_schema import SQLITE_OCCUPIED_SESSION as OCCUPIED_SESSION, POSTGRES_OCCUPIED_SESSION


def now():
    # Store also uses these initializers during its own import/initialization.
    from .db import now as timestamp
    return timestamp()

TABLES = frozenset({'trace_outbox', 'trace_outbox_raindrop', 'trace_outbox_langfuse',
                   'trace_outbox_langsmith', 'trace_outbox_braintrust', 'trace_outbox_raindrop_events'})


def initialize_session_privacy(store):
    """Privacy is immutable on both databases, including direct SQL writers.

    The compatibility revision fences older APIs/workers before these sessions
    can exist. This is a coordinated migration, not an additive rolling change.
    """
    with store.connect() as conn:
        if 'private_owner_id' not in conn.column_names('runs'):
            conn.execute("ALTER TABLE runs ADD COLUMN private_owner_id TEXT NOT NULL DEFAULT ''")
        from .database_schema import PRIVATE_TRIGGERS, PRIVATE_FUNCTION, PRIVATE_POSTGRES
        if store.database:
            conn.execute(PRIVATE_FUNCTION)
            conn.execute(PRIVATE_POSTGRES)
        else:
            for statement in PRIVATE_TRIGGERS.values():
                conn.execute(statement.replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1))


def initialize_personal_slack(store):
    store.execute('''CREATE TABLE IF NOT EXISTS personal_slack_grants (
        owner_id TEXT PRIMARY KEY, revision TEXT NOT NULL, encrypted TEXT,
        label TEXT NOT NULL DEFAULT '', health TEXT NOT NULL DEFAULT 'absent')''')
    store.execute('''CREATE TABLE IF NOT EXISTS personal_slack_states (
        state_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL, owner_id TEXT NOT NULL,
        revision TEXT NOT NULL, expires REAL NOT NULL)''')


def initialize_user_roles(store):
    store.execute('''CREATE TABLE IF NOT EXISTS user_roles (
        email TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','member')),
        revision INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    store.execute('''CREATE TABLE IF NOT EXISTS user_role_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL,
        previous_role TEXT NOT NULL, role TEXT NOT NULL,
        actor TEXT NOT NULL, created_at TEXT NOT NULL)''')


def initialize_sandbox_settings(store):
    store.execute('CREATE TABLE IF NOT EXISTS sandbox_settings (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, encrypted TEXT NOT NULL)')


def initialize_github_repositories(store):
    store.execute('''CREATE TABLE IF NOT EXISTS github_repositories (
        installation_id INTEGER NOT NULL, repository_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL, full_name TEXT NOT NULL, aliases TEXT NOT NULL,
        PRIMARY KEY(installation_id,repository_id))''')


def initialize_github_write_access(store):
    store.execute('''CREATE TABLE IF NOT EXISTS github_write_access (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        connection_version TEXT NOT NULL, repository_id INTEGER NOT NULL,
        number INTEGER NOT NULL, head_repository_id INTEGER NOT NULL,
        branch TEXT NOT NULL, base_branch TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','revoked')),
        created_at TEXT NOT NULL)''')
    store.execute('''CREATE INDEX IF NOT EXISTS github_write_access_scope
        ON github_write_access(run_id,connection_version,repository_id,number)''')
    if store.database:
        from .database_schema import REVOKE_FUNCTION, REVOKE_POSTGRES
        with store.connect() as conn:
            conn.begin_write()
            conn.execute(REVOKE_FUNCTION.replace('CREATE FUNCTION', 'CREATE OR REPLACE FUNCTION'))
            conn.execute(REVOKE_POSTGRES.replace('CREATE TRIGGER', 'CREATE OR REPLACE TRIGGER'))
    else:
        store.execute('''CREATE TRIGGER IF NOT EXISTS revoke_github_write_access
            AFTER UPDATE OF status,deleted_at ON runs
            WHEN NEW.status IN ('stopping','cancelled') OR NEW.deleted_at!=''
            BEGIN UPDATE github_write_access SET status='revoked'
            WHERE run_id=NEW.id AND status IN ('pending','approved'); END''')


def initialize_github(store):
    store.execute('CREATE TABLE IF NOT EXISTS github_app (id INTEGER PRIMARY KEY CHECK(id=1), encrypted TEXT NOT NULL)')
    store.execute('''CREATE TABLE IF NOT EXISTS github_publications (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, message_id INTEGER NOT NULL,
        arguments_hash TEXT NOT NULL, branch TEXT NOT NULL, commit_sha TEXT NOT NULL DEFAULT '',
        result TEXT NOT NULL DEFAULT '', connection_version TEXT NOT NULL, created_at TEXT NOT NULL)''')
    with store.connect() as conn:
        if 'attempted' not in conn.column_names('github_publications'):
            conn.execute('ALTER TABLE github_publications ADD COLUMN attempted INTEGER NOT NULL DEFAULT 0')
            conn.execute("UPDATE github_publications SET attempted=1 WHERE commit_sha!='' AND result=''")

    store.execute("""CREATE TABLE IF NOT EXISTS github_followups (
        id TEXT PRIMARY KEY, arguments_hash TEXT NOT NULL, connection_version TEXT NOT NULL,
        commit_sha TEXT NOT NULL DEFAULT '', sent INTEGER NOT NULL DEFAULT 0,
        result TEXT NOT NULL DEFAULT '')""")


def initialize_repository_context(store):
    store.execute('''CREATE TABLE IF NOT EXISTS repository_context_snapshots (
        version TEXT NOT NULL, repository_id INTEGER NOT NULL, commit_sha TEXT NOT NULL,
        repository TEXT NOT NULL, branch TEXT NOT NULL, indexed_at TEXT NOT NULL,
        guide TEXT NOT NULL, catalog TEXT NOT NULL, documents TEXT NOT NULL,
        PRIMARY KEY(version,repository_id,commit_sha))''')
    store.execute('''CREATE TABLE IF NOT EXISTS repository_context_heads (
        version TEXT NOT NULL, repository_id INTEGER NOT NULL, commit_sha TEXT NOT NULL DEFAULT '',
        checked_at REAL NOT NULL, PRIMARY KEY(version,repository_id))''')
    store.execute('''CREATE TABLE IF NOT EXISTS repository_context_turns (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, message_id INTEGER NOT NULL,
        version TEXT NOT NULL, repository_id INTEGER NOT NULL, commit_sha TEXT NOT NULL,
        PRIMARY KEY(run_id,message_id,version,repository_id))''')


def initialize_session_pull_requests(store):
    store.execute('''CREATE TABLE IF NOT EXISTS github_pr_snapshots (
        connection_version TEXT NOT NULL, repository_id INTEGER NOT NULL, number INTEGER NOT NULL,
        snapshot TEXT NOT NULL, observed_at TEXT NOT NULL,
        PRIMARY KEY(connection_version,repository_id,number))''')


def initialize_durable_runner(store):
    # Auto-mode workers may initialize together. Hold the schema write lock
    # through replacement so a predicate upgrade has no committed index gap.
    with store.connect() as conn:
        conn.begin_write()
        conn.execute("""CREATE TABLE IF NOT EXISTS durable_sessions (
            run_id TEXT PRIMARY KEY REFERENCES runs(id), state TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0)""")
        conn.execute('DROP INDEX IF EXISTS idx_durable_sessions_occupied')
        conn.execute(f"""CREATE INDEX idx_durable_sessions_occupied
            ON durable_sessions(run_id) WHERE {(POSTGRES_OCCUPIED_SESSION if store.database else OCCUPIED_SESSION)}""")
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_durable_sessions_wake
            ON durable_sessions(run_id) WHERE revision>delivered''')


def initialize_prepared_sandboxes(store):
    store.execute('''CREATE TABLE IF NOT EXISTS prepared_sandboxes (
        name TEXT PRIMARY KEY, build TEXT NOT NULL, status TEXT NOT NULL,
        sandbox_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
        expires_at REAL NOT NULL)''')


def initialize_user_preferences(store):
    with store.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS user_preferences (
            user_id TEXT PRIMARY KEY REFERENCES users(id),
            send_immediately INTEGER NOT NULL DEFAULT 0 CHECK(send_immediately IN (0,1))
        )''')
        if 'omit_private_tool_payloads' not in conn.column_names('user_preferences'):
            conn.execute('''ALTER TABLE user_preferences ADD COLUMN omit_private_tool_payloads
                INTEGER NOT NULL DEFAULT 0 CHECK(omit_private_tool_payloads IN (0,1))''')
        if 'slack_notifications' not in conn.column_names('user_preferences'):
            conn.execute('''ALTER TABLE user_preferences ADD COLUMN slack_notifications
                INTEGER NOT NULL DEFAULT 1 CHECK(slack_notifications IN (0,1))''')
        if 'slack_notifications_since' not in conn.column_names('user_preferences'):
            conn.execute("ALTER TABLE user_preferences ADD COLUMN slack_notifications_since TEXT NOT NULL DEFAULT ''")


def initialize_environments(store):
    store.execute('''CREATE TABLE IF NOT EXISTS environments (
        id TEXT PRIMARY KEY, recipe TEXT NOT NULL, revision INTEGER NOT NULL,
        active_build TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,
        is_default INTEGER NOT NULL DEFAULT 0, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    store.execute('''CREATE TABLE IF NOT EXISTS environment_builds (
        id TEXT PRIMARY KEY, environment_id TEXT NOT NULL REFERENCES environments(id),
        revision INTEGER NOT NULL, recipe TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued',
        sandbox_id TEXT NOT NULL DEFAULT '', snapshot_id TEXT NOT NULL DEFAULT '',
        commit_sha TEXT NOT NULL DEFAULT '', log TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL)''')
    with store.connect() as conn:
        if 'sandbox_provider' not in conn.column_names('environment_builds'):
            conn.execute("ALTER TABLE environment_builds ADD COLUMN sandbox_provider TEXT NOT NULL DEFAULT 'modal'")
        columns = conn.column_names('environments')
        for name in ('refresh_daily', 'activate_on_ready'):
            if name not in columns:
                conn.execute(f'ALTER TABLE environments ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
        if 'resolved_recipe' not in conn.column_names('environment_builds'):
            conn.execute("ALTER TABLE environment_builds ADD COLUMN resolved_recipe TEXT NOT NULL DEFAULT ''")


def initialize_infrastructure_costs(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS infrastructure_bills (
                provider TEXT NOT NULL, month TEXT NOT NULL, amount TEXT NOT NULL,
                kind TEXT NOT NULL, note TEXT NOT NULL, revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(provider,month)
            );
            CREATE TABLE IF NOT EXISTS infrastructure_bill_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, month TEXT NOT NULL,
                actor TEXT NOT NULL, changed_at TEXT NOT NULL, previous TEXT NOT NULL, replacement TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS infrastructure_daily_costs (
                provider TEXT NOT NULL, scope TEXT NOT NULL, day TEXT NOT NULL, amount TEXT NOT NULL,
                synced_at TEXT NOT NULL, PRIMARY KEY(provider,scope,day)
            );
            CREATE TABLE IF NOT EXISTS infrastructure_sync_jobs (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, scope TEXT NOT NULL,
                start TEXT NOT NULL, "end" TEXT NOT NULL, status TEXT NOT NULL,
                state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                UNIQUE(provider,scope,start,"end")
            );
        ''')


def initialize_spend(store):
    with store.connect() as conn:
        if store.database:
            from .billing_schema import consolidated_billing_view
            if consolidated_billing_view(conn, store.database.schema):
                return
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS model_requests (
                id TEXT PRIMARY KEY, key_hash TEXT NOT NULL, gateway_id TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL REFERENCES runs(id), message_id INTEGER,
                user_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL, created_at TEXT NOT NULL,
                finished_at TEXT, status TEXT NOT NULL DEFAULT 'pending', cost TEXT,
                prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_model_requests_gateway ON model_requests(key_hash,gateway_id);
            CREATE INDEX IF NOT EXISTS idx_model_requests_time ON model_requests(key_hash,created_at);
            CREATE INDEX IF NOT EXISTS idx_model_requests_run ON model_requests(key_hash,run_id);
            CREATE INDEX IF NOT EXISTS idx_model_requests_org_time ON model_requests(created_at);
            CREATE INDEX IF NOT EXISTS idx_model_requests_org_run ON model_requests(run_id);
            CREATE INDEX IF NOT EXISTS idx_model_requests_user_time ON model_requests(user_id,created_at);
        ''')
        columns = conn.column_names('model_requests')
        if 'cost_source' not in columns:
            conn.execute("ALTER TABLE model_requests ADD COLUMN cost_source TEXT NOT NULL DEFAULT ''")
        for name in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
            if name not in columns:
                conn.execute(f'ALTER TABLE model_requests ADD COLUMN {name} INTEGER')
        for name, definition in (
            ('gateway_scope', "TEXT NOT NULL DEFAULT ''"),
            ('cost_receipt_id', "TEXT NOT NULL DEFAULT ''"),
            ('cost_next_attempt_at', "TEXT NOT NULL DEFAULT ''"),
            ('cost_recovery_attempts', 'INTEGER NOT NULL DEFAULT 0'),
            ('cost_recovery_error', "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                conn.execute(f'ALTER TABLE model_requests ADD COLUMN {name} {definition}')
        conn.executescript('''
            CREATE INDEX IF NOT EXISTS idx_model_requests_recovery
            ON model_requests(key_hash,gateway_scope,cost_next_attempt_at,id)
            WHERE cost IS NULL AND status!='pending';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_requests_receipt
            ON model_requests(gateway_scope,key_hash,cost_receipt_id) WHERE cost_receipt_id!='';
        ''')


def initialize_agents(store):
    store.execute('''CREATE TABLE IF NOT EXISTS agent_groups (
        id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES runs(id),
        message_id INTEGER NOT NULL, request_key TEXT NOT NULL, payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'preparing', snapshot_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, UNIQUE(parent_id,message_id,request_key))''')
    store.execute('''CREATE TABLE IF NOT EXISTS agent_retries (
        group_id TEXT NOT NULL REFERENCES agent_groups(id), request_key TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(group_id,request_key))''')
    if 'result_snapshot' not in store.column_names('agent_groups'):
        store.execute("ALTER TABLE agent_groups ADD COLUMN result_snapshot TEXT NOT NULL DEFAULT ''")
    if 'runtime_assignments' not in store.column_names('agent_groups'):
        store.execute("ALTER TABLE agent_groups ADD COLUMN runtime_assignments TEXT NOT NULL DEFAULT ''")


def initialize_tracing(store):
    with store.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS trace_contexts (
            run_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_id TEXT,
            session_id TEXT NOT NULL, agent_name TEXT NOT NULL,
            PRIMARY KEY(run_id,message_id))''')


def initialize_credentials(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS provider_secrets (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL,
                scope TEXT NOT NULL, owner_id TEXT NOT NULL, root_id TEXT NOT NULL DEFAULT '',
                encrypted TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT '',
                client_id TEXT NOT NULL, UNIQUE(owner_id,client_id)
            );
            CREATE TABLE IF NOT EXISTS credential_requests (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                message_id INTEGER NOT NULL, actor_id TEXT NOT NULL,
                provider TEXT NOT NULL, reason TEXT NOT NULL, request_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', secret_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, resolved_at TEXT NOT NULL DEFAULT '',
                UNIQUE(run_id,message_id,request_key)
            );
            CREATE TABLE IF NOT EXISTS credential_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
                secret_id TEXT NOT NULL, action TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS credential_source_uses (
                run_id TEXT NOT NULL REFERENCES runs(id), message_id INTEGER NOT NULL,
                secret_id TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY(run_id,message_id,secret_id,revision)
            );
            CREATE TABLE IF NOT EXISTS aws_login_requests (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                message_id INTEGER NOT NULL, actor_id TEXT NOT NULL, request_key TEXT NOT NULL,
                profile_json TEXT NOT NULL, lifetime TEXT NOT NULL, status TEXT NOT NULL,
                encrypted TEXT NOT NULL DEFAULT '', expires_at INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL DEFAULT 5, next_poll INTEGER NOT NULL DEFAULT 0,
                secret_id TEXT NOT NULL DEFAULT '', UNIQUE(run_id,message_id,request_key)
            );
        ''')
        for table, fields in {
            'provider_secrets': {'name': "TEXT NOT NULL DEFAULT ''", 'format': "TEXT NOT NULL DEFAULT 'env'",
                'env_var': "TEXT NOT NULL DEFAULT ''", 'lifetime': "TEXT NOT NULL DEFAULT 'persistent'",
                'expires_at': "TEXT NOT NULL DEFAULT ''", 'invalid_reason': "TEXT NOT NULL DEFAULT ''",
                'revision': 'INTEGER NOT NULL DEFAULT 1'},
            'credential_requests': {'name': "TEXT NOT NULL DEFAULT ''", 'format': "TEXT NOT NULL DEFAULT 'env'",
                'env_var': "TEXT NOT NULL DEFAULT ''", 'generation': 'INTEGER NOT NULL DEFAULT 0',
                'failure': "TEXT NOT NULL DEFAULT ''", 'revision': 'INTEGER NOT NULL DEFAULT 1',
                'secret_revision': 'INTEGER NOT NULL DEFAULT 1', 'setup_url': "TEXT NOT NULL DEFAULT ''",
                'setup_instructions': "TEXT NOT NULL DEFAULT ''", 'input_fields': "TEXT NOT NULL DEFAULT '[]'",
                'preferred_scope': "TEXT NOT NULL DEFAULT ''", 'scope_revision': 'INTEGER NOT NULL DEFAULT 0',
                'resolution_pending': 'INTEGER NOT NULL DEFAULT 0', 'resolution_error': "TEXT NOT NULL DEFAULT ''"},
        }.items():
            columns = conn.column_names(table)
            for field, declaration in fields.items():
                if field not in columns:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {field} {declaration}')
        conn.execute("UPDATE provider_secrets SET scope='personal',lifetime='session' WHERE scope='session'")


def initialize_skills(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS skill_bundles (
                skill_id TEXT NOT NULL, revision INTEGER NOT NULL,
                encrypted TEXT NOT NULL, manifest TEXT NOT NULL,
                PRIMARY KEY(skill_id,revision)
            );
            CREATE TABLE IF NOT EXISTS skill_saves (
                run_id TEXT NOT NULL, message_id INTEGER NOT NULL, request_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, fingerprint TEXT NOT NULL, skill_id TEXT NOT NULL,
                result TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(run_id,message_id,request_id)
            );
            CREATE TABLE IF NOT EXISTS skill_file_reads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, message_id INTEGER NOT NULL, actor_id TEXT NOT NULL,
                skill_id TEXT NOT NULL, path TEXT NOT NULL, "offset" INTEGER NOT NULL, length INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_skill_file_reads_turn ON skill_file_reads(run_id,message_id);
            CREATE TABLE IF NOT EXISTS skills (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                encrypted TEXT NOT NULL, scope TEXT NOT NULL, owner_id TEXT NOT NULL,
                namespace TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                client_id TEXT NOT NULL, UNIQUE(namespace,name), UNIQUE(owner_id,client_id)
            );
            CREATE TABLE IF NOT EXISTS skill_uses (
                run_id TEXT NOT NULL, message_id INTEGER NOT NULL, actor_id TEXT NOT NULL,
                skill_id TEXT NOT NULL, revision INTEGER NOT NULL, encrypted TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(run_id,message_id,skill_id)
            );
            CREATE TABLE IF NOT EXISTS skill_searches (
                run_id TEXT NOT NULL, message_id INTEGER NOT NULL, actor_id TEXT NOT NULL,
                skill_id TEXT NOT NULL, position INTEGER NOT NULL,
                PRIMARY KEY(run_id,message_id,skill_id)
            );
            CREATE TABLE IF NOT EXISTS skill_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, skill_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL
            );
        ''')
        if 'icon' not in conn.column_names('skills'):
            conn.execute("ALTER TABLE skills ADD COLUMN icon TEXT NOT NULL DEFAULT 'auto'")


def initialize_memory(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS memory_preferences (
                owner_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                auto_save INTEGER NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS personal_memories (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, key_hash TEXT NOT NULL,
                encrypted TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0, UNIQUE(owner_id,key_hash));
            CREATE INDEX IF NOT EXISTS memory_owner ON personal_memories(owner_id,deleted);
            CREATE TABLE IF NOT EXISTS memory_operations (
                owner_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY(owner_id,request_id));
            CREATE TABLE IF NOT EXISTS memory_selections (
                run_id TEXT NOT NULL, turn_id INTEGER NOT NULL, actor_id TEXT NOT NULL,
                owner_id TEXT NOT NULL, memory_id TEXT NOT NULL,
                PRIMARY KEY(run_id,turn_id,memory_id));
            CREATE TABLE IF NOT EXISTS memory_capture_barriers (
                owner_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL);
        ''')
        for name in ('read_offset', 'read_length'):
            if name not in conn.column_names('memory_selections'):
                conn.execute(f'ALTER TABLE memory_selections ADD COLUMN {name} INTEGER')


def initialize_memory_review(store):
    store.execute('''CREATE TABLE IF NOT EXISTS memory_reviews (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id), run_id TEXT NOT NULL,
        actor_id TEXT NOT NULL, owner_id TEXT NOT NULL, preferences_revision INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        available_at TEXT NOT NULL, updated_at TEXT NOT NULL, saved_count INTEGER NOT NULL DEFAULT 0)''')
    store.execute('CREATE INDEX IF NOT EXISTS memory_review_queue ON memory_reviews(status,available_at,message_id)')


def initialize_skill_learning(store):
    store.execute('''CREATE TABLE IF NOT EXISTS skill_learning_preferences (
        owner_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
        after_message_id INTEGER NOT NULL DEFAULT 0)''')
    store.execute('''CREATE TABLE IF NOT EXISTS skill_learning_jobs (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id), owner_id TEXT NOT NULL,
        preferences_revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL)''')
    store.execute('CREATE INDEX IF NOT EXISTS skill_learning_queue ON skill_learning_jobs(status,available_at,message_id)')
    store.execute('''CREATE TABLE IF NOT EXISTS skill_suggestions (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, workflow_key TEXT NOT NULL,
        message_id INTEGER NOT NULL REFERENCES messages(id), preferences_revision INTEGER NOT NULL,
        encrypted TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        skill_id TEXT NOT NULL DEFAULT '', accept_digest TEXT NOT NULL DEFAULT '', UNIQUE(owner_id,workflow_key))''')


def initialize_lens_feedback(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS lens_feedback (
                run_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                author TEXT NOT NULL,
                score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 10),
                comment TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('web','slack')),
                trace_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                delivered_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                failed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(run_id,message_id,author)
            );
            CREATE INDEX IF NOT EXISTS lens_feedback_pending
                ON lens_feedback(next_attempt_at,created_at)
                WHERE delivered_at IS NULL AND failed=0;
        ''')


def initialize_session_titles(store):
    store.execute('CREATE TABLE IF NOT EXISTS session_title_settings (id INTEGER PRIMARY KEY CHECK(id=1), model TEXT NOT NULL)')
    store.execute('CREATE TABLE IF NOT EXISTS session_title_requests (run_id TEXT PRIMARY KEY REFERENCES runs(id))')


def initialize_automations(store):
    store.execute('''CREATE TABLE IF NOT EXISTS automations (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), definition TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, synced_revision INTEGER NOT NULL DEFAULT 0,
        paused INTEGER NOT NULL DEFAULT 1, sync_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    store.execute('''CREATE TABLE IF NOT EXISTS automation_runs (
        occurrence TEXT PRIMARY KEY, automation_id TEXT NOT NULL REFERENCES automations(id),
        revision INTEGER NOT NULL, run_id TEXT UNIQUE REFERENCES runs(id),
        outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)''')
    store.execute('CREATE INDEX IF NOT EXISTS automation_runs_history ON automation_runs(automation_id,created_at)')
    store.execute('''CREATE TABLE IF NOT EXISTS automation_items (
        automation_id TEXT NOT NULL REFERENCES automations(id), item_key TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(id), created_at TEXT NOT NULL,
        PRIMARY KEY(automation_id,item_key))''')
    store.execute('''CREATE TABLE IF NOT EXISTS automation_schedules (
        automation_id TEXT NOT NULL REFERENCES automations(id), schedule_id TEXT PRIMARY KEY)''')
    with store.connect() as conn:
        conn.begin_write()
        if 'synced_schedule_version' not in conn.column_names('automations'):
            conn.execute('ALTER TABLE automations ADD COLUMN synced_schedule_version INTEGER NOT NULL DEFAULT 0')
    # Remember already-synced single-trigger schedules for later removal.
    for row in store.rows('SELECT * FROM automations WHERE synced_revision>0'):
        if 'triggers' not in json.loads(row['definition']) and not json.loads(row['definition']).get('event'):
            store.execute('INSERT INTO automation_schedules VALUES(?,?) ON CONFLICT DO NOTHING', (row['id'], 'moyai-automation-' + row['id']))


def initialize_automation_events(store):
    with store.connect() as conn:
        conn.begin_write()
        columns = conn.column_names('automation_webhooks')
        if columns and 'provider' not in columns:
            conn.execute('ALTER TABLE automation_webhooks RENAME TO automation_webhooks_legacy')
        conn.execute("""CREATE TABLE IF NOT EXISTS automation_webhooks (
            automation_id TEXT NOT NULL REFERENCES automations(id), provider TEXT NOT NULL,
            encrypted TEXT NOT NULL, PRIMARY KEY(automation_id,provider))""")
        if columns and 'provider' not in columns:
            for key in conn.execute('SELECT k.*,a.definition FROM automation_webhooks_legacy k JOIN automations a ON a.id=k.automation_id').fetchall():
                providers = {t.provider for _, t in sources(key) if t.provider != 'slack'}
                if len(providers) == 1:
                    conn.execute('INSERT INTO automation_webhooks VALUES(?,?,?)', (key['automation_id'], providers.pop(), key['encrypted']))
            conn.execute('DROP TABLE automation_webhooks_legacy')
    store.execute("""CREATE TABLE IF NOT EXISTS automation_events (
        occurrence TEXT PRIMARY KEY, automation_id TEXT NOT NULL REFERENCES automations(id),
        revision INTEGER NOT NULL, context TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        detail TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")
    store.execute('CREATE INDEX IF NOT EXISTS automation_events_pending ON automation_events(status,received_at)')
    store.execute('''CREATE TABLE IF NOT EXISTS automation_session_cursors (
        automation_id TEXT PRIMARY KEY REFERENCES automations(id), message_id INTEGER NOT NULL)''')


def initialize_automation_tools(store):
    store.execute('''CREATE TABLE IF NOT EXISTS automation_operations (
        owner_id TEXT NOT NULL, run_id TEXT NOT NULL, request_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL, automation_id TEXT NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY(owner_id,run_id,request_key))''')


def initialize_automation_permissions(store):
    store.execute('''CREATE TABLE IF NOT EXISTS automation_grants (
        automation_id TEXT NOT NULL REFERENCES automations(id),
        run_id TEXT NOT NULL REFERENCES runs(id), message_id INTEGER NOT NULL REFERENCES messages(id),
        owner_id TEXT NOT NULL, permissions TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(automation_id,run_id))''')


def initialize_model_tools(store):
    store.execute('''CREATE TABLE IF NOT EXISTS model_switch_operations (
        run_id TEXT NOT NULL, turn_id INTEGER NOT NULL, request_key TEXT NOT NULL,
        model TEXT NOT NULL, PRIMARY KEY(run_id,turn_id,request_key))''')


def initialize_session_folders(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS session_folders (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL, name_key TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(owner_id, id), UNIQUE(owner_id, name_key));
            CREATE TABLE IF NOT EXISTS session_folder_memberships (
                owner_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                folder_id TEXT NOT NULL, PRIMARY KEY(owner_id, run_id),
                FOREIGN KEY(owner_id, folder_id) REFERENCES session_folders(owner_id, id) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS idx_session_folder_members
                ON session_folder_memberships(owner_id, folder_id);
        ''')


def initialize_media_shares(store):
    with store.connect() as conn:
        conn.executescript('''CREATE TABLE IF NOT EXISTS media_shares (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            request_key TEXT NOT NULL, source TEXT NOT NULL, revision TEXT NOT NULL,
            token_hash TEXT NOT NULL, token_ciphertext TEXT NOT NULL, origin TEXT NOT NULL,
            reference TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
            mime TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT '',
            UNIQUE(run_id, request_key));
            CREATE INDEX IF NOT EXISTS idx_media_shares_run ON media_shares(run_id);''')


def initialize_computer(store):
    store.execute('CREATE TABLE IF NOT EXISTS computer_activity (run_id TEXT PRIMARY KEY, touched REAL NOT NULL)')


def initialize_github_setup(store):
    store.execute('CREATE TABLE IF NOT EXISTS github_registration (state_hash TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, owner_login TEXT NOT NULL)')


def initialize_swarms(store):
    store.execute('''CREATE TABLE IF NOT EXISTS swarm_missions (
        run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'active', budget_seconds INTEGER NOT NULL,
        ends_at TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 1,
        reason TEXT NOT NULL DEFAULT '', next_at REAL NOT NULL DEFAULT 0,
        settled_message_id INTEGER NOT NULL DEFAULT 0,
        answer_digest TEXT NOT NULL DEFAULT '', no_progress INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def initialize_swarm_collaboration(store):
    store.execute('''CREATE TABLE IF NOT EXISTS swarm_teams (
        group_id TEXT PRIMARY KEY REFERENCES agent_groups(id) ON DELETE CASCADE,
        task TEXT NOT NULL, leader_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL)''')
    store.execute('''CREATE TABLE IF NOT EXISTS swarm_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL REFERENCES swarm_teams(group_id) ON DELETE CASCADE,
        member_id TEXT NOT NULL, kind TEXT NOT NULL,
        target_id TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)''')
    store.execute('CREATE INDEX IF NOT EXISTS idx_swarm_posts_group_id_id ON swarm_posts(group_id,id)')


def initialize_swarm_chat(store):
    store.execute('''CREATE TABLE IF NOT EXISTS swarm_chat_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        root_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        source TEXT NOT NULL CHECK(source IN ('message','board')),
        source_id INTEGER NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(root_id,source,source_id))''')
    store.execute('CREATE INDEX IF NOT EXISTS idx_swarm_chat_delivery ON swarm_chat_entries(root_id,id)')
    store.execute('CREATE INDEX IF NOT EXISTS idx_swarm_chat_history ON swarm_chat_entries(root_id,created_at,source,source_id)')


def initialize_coordinator_handoff(store):
    with store.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS coordinator_hooks (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL, response TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
            retry_at REAL NOT NULL DEFAULT 0)''')
        conn.execute('CREATE INDEX IF NOT EXISTS coordinator_hooks_pending ON coordinator_hooks(status,kind,created_at,id)')


def initialize_broker_ownership(store):
    store.execute('''CREATE TABLE IF NOT EXISTS broker_work_owners (
        kind TEXT NOT NULL, work_id TEXT NOT NULL, owner_pid INTEGER NOT NULL,
        owner_started TEXT NOT NULL, PRIMARY KEY(kind,work_id))''')
    # Preserve statuses during the offline migration. An explicit orphan record
    # lets startup recover pre-protocol work without touching live broker rows.
    # Current broker routes never create executing approvals; legacy ambiguous
    # external writes become uncertain on recovery and are never replayed.
    for kind, table, key, status in (
            ('model', 'model_requests', 'id', 'pending'),
            ('context', 'context_jobs', 'operation_id', 'running'),
            ('approval', 'approvals', 'id', 'executing')):
        store.execute(f"INSERT INTO broker_work_owners(kind,work_id,owner_pid,owner_started) "
                      f"SELECT ?,{key},0,'' FROM {table} WHERE status=? ON CONFLICT DO NOTHING", (kind, status))


def initialize_performance(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS performance_samples (
                message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, received_at TEXT, persisted_at TEXT NOT NULL,
                send_now_at TEXT, send_now_kind TEXT,
                picked_up_at TEXT, pickup_kind TEXT, first_model_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_performance_received ON performance_samples(received_at);
            CREATE INDEX IF NOT EXISTS idx_performance_send_now ON performance_samples(send_now_at);
            CREATE INDEX IF NOT EXISTS idx_performance_run ON performance_samples(run_id);
        ''')
        # Nullable additions preserve old writers and never reinterpret their
        # admission timestamps as outbound dispatch observations.
        columns = conn.column_names('performance_samples')
        for name, kind in {
            'timing_version': 'INTEGER', 'dispatch_at': 'TEXT',
            'dispatch_broker_at': 'TEXT', 'dispatch_request_id': 'TEXT',
            'computer_requested_at': 'TEXT', 'computer_ready_at': 'TEXT',
            'computer_start_kind': 'TEXT',
        }.items():
            if name not in columns:
                conn.execute(f'ALTER TABLE performance_samples ADD COLUMN {name} {kind}')


def initialize_session_journal(store):
    store.execute('''CREATE TABLE IF NOT EXISTS session_journals (
        run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        epoch TEXT NOT NULL, imported INTEGER NOT NULL DEFAULT 0,
        import_id TEXT NOT NULL DEFAULT '', import_count INTEGER NOT NULL DEFAULT 0)''')
    store.execute('''CREATE TABLE IF NOT EXISTS session_items (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, item_id TEXT NOT NULL, message TEXT NOT NULL,
        reference TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL, digest TEXT NOT NULL,
        PRIMARY KEY(run_id,seq), UNIQUE(run_id,item_id))''')
    store.execute('''CREATE TABLE IF NOT EXISTS session_uploads (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        upload_id TEXT NOT NULL, part INTEGER NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(run_id,upload_id,part))''')
    store.execute('''CREATE TABLE IF NOT EXISTS session_upload_receipts (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        upload_id TEXT NOT NULL, digest TEXT NOT NULL, response TEXT NOT NULL,
        PRIMARY KEY(run_id,upload_id))''')


def initialize_goals(store):
    store.execute('''CREATE TABLE IF NOT EXISTS session_goals (
        run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1)''')
    store.execute('''CREATE TABLE IF NOT EXISTS goal_clock_receipts (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        segment TEXT NOT NULL, elapsed REAL NOT NULL, PRIMARY KEY(run_id,segment))''')


def initialize_slack_sources(store):
    store.execute('''CREATE TABLE IF NOT EXISTS slack_source_events (
        team_id TEXT NOT NULL, event_id TEXT NOT NULL, channel TEXT NOT NULL,
        thread_ts TEXT NOT NULL, message_ts TEXT NOT NULL, payload TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(team_id,event_id))''')
    store.execute('''CREATE TABLE IF NOT EXISTS slack_source_versions (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, team_id TEXT NOT NULL,
        channel TEXT NOT NULL, thread_ts TEXT NOT NULL, message_ts TEXT NOT NULL,
        version TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(team_id,channel,message_ts,version))''')
    store.execute('CREATE INDEX IF NOT EXISTS idx_slack_source_thread ON slack_source_versions(team_id,channel,thread_ts,seq)')
    store.execute('''CREATE TABLE IF NOT EXISTS slack_source_coverage (
        team_id TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
        status TEXT NOT NULL, cursor TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        PRIMARY KEY(team_id,channel,thread_ts))''')


def initialize_published_artifacts(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS artifact_uploads (
                run_id TEXT NOT NULL, upload_id TEXT NOT NULL, actor_id TEXT NOT NULL, turn_id INTEGER,
                capability TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(run_id,upload_id));
            CREATE TABLE IF NOT EXISTS artifact_parts (
                run_id TEXT NOT NULL, upload_id TEXT NOT NULL, part INTEGER NOT NULL, data BLOB NOT NULL,
                reference TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                PRIMARY KEY(run_id,upload_id,part));
            CREATE TABLE IF NOT EXISTS published_artifacts (
                run_id TEXT NOT NULL, path TEXT NOT NULL, revision TEXT NOT NULL, upload_id TEXT NOT NULL,
                size INTEGER NOT NULL, media_type TEXT NOT NULL, created_at TEXT NOT NULL, source_turn INTEGER,
                PRIMARY KEY(run_id,path,revision));
        ''')


def initialize_computer_operations(store):
    store.execute('''CREATE TABLE IF NOT EXISTS computer_operations (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL, turn_id INTEGER NOT NULL, actor_id TEXT,
        capability TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', machine_id TEXT NOT NULL DEFAULT '',
        result TEXT NOT NULL DEFAULT '{}', UNIQUE(run_id,request_id))''')
    store.execute('''CREATE INDEX IF NOT EXISTS idx_computer_operations_pending
        ON computer_operations(run_id,seq) WHERE status IN ('queued','dispatched')''')


def initialize_broker_payloads(store):
    store.execute('''CREATE TABLE IF NOT EXISTS broker_payload_parts (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        scope TEXT NOT NULL, route TEXT NOT NULL, digest TEXT NOT NULL,
        part INTEGER NOT NULL, encrypted TEXT NOT NULL,
        PRIMARY KEY(run_id,scope,route,digest,part))''')


def initialize_session_events(store):
    from .database_schema import SESSION_EVENT_FUNCTION, SESSION_EVENT_TRIGGERS
    with store.connect() as conn:
        conn.begin_write()
        conn.execute('CREATE TABLE IF NOT EXISTS session_event_consumers (name TEXT PRIMARY KEY)')
        conn.execute('''CREATE TABLE IF NOT EXISTS session_event_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consumer TEXT NOT NULL REFERENCES session_event_consumers(name) ON DELETE CASCADE,
            run_id TEXT NOT NULL, kind TEXT NOT NULL, UNIQUE(consumer,run_id))''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_session_event_inbox_order ON session_event_inbox(consumer,id)')
        if store.database:
            conn.execute(SESSION_EVENT_FUNCTION.replace('CREATE FUNCTION', 'CREATE OR REPLACE FUNCTION'))
        for sqlite, postgres in SESSION_EVENT_TRIGGERS.values():
            conn.execute(postgres.replace('CREATE TRIGGER', 'CREATE OR REPLACE TRIGGER') if store.database
                         else sqlite.replace('CREATE TRIGGER', 'CREATE TRIGGER IF NOT EXISTS'))


def initialize_slack_notifications(store):
    with store.connect() as conn:
        conn.executescript('''CREATE TABLE IF NOT EXISTS slack_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id), event_key TEXT NOT NULL,
            target_run_id TEXT NOT NULL, user_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            team_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            slack_ts TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            UNIQUE(run_id,event_key)
        );
        CREATE INDEX IF NOT EXISTS idx_slack_notifications_pending ON slack_notifications(status,retry_at,id);''')
        conn.execute('''CREATE TABLE IF NOT EXISTS slack_notification_state (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled_since TEXT NOT NULL)''')
        # Persist once at rollout, including when no one has saved preferences.
        # Restarts must still deliver new events created while the worker was down.
        conn.execute('INSERT INTO slack_notification_state VALUES(1,?) ON CONFLICT(id) DO NOTHING', (now(),))
        from .session_events import SessionEvents
        SessionEvents.subscribe_in(conn, 'slack_notifications')


def initialize_trace_outbox(store, table):
    if table not in TABLES:
        raise ValueError('Unknown trace outbox')
    with store.connect() as conn:
        conn.executescript(f'''
            CREATE TABLE IF NOT EXISTS {table} (
                trace_id TEXT NOT NULL, span_id TEXT NOT NULL, payload BLOB,
                created_at REAL NOT NULL, delivered_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(trace_id,span_id)
            );
            CREATE INDEX IF NOT EXISTS {table}_pending
                ON {table}(next_attempt_at,created_at) WHERE delivered_at IS NULL;
        ''')
