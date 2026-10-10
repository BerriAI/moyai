from pathlib import Path
from typing import Literal
import base64
import re
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from .context_budget import ModelContextLimits
from .model_selection import ASTRA_ULTRAFAST


# Keep picker IDs and labels in code so a stale deployment environment cannot
# hide models added by a release. AGENT_MODEL only chooses the default.
MODEL_CATALOG: dict[str, str] = {
    'openai/gpt-6-astra': 'GPT-6 Astra',
    ASTRA_ULTRAFAST: 'GPT-6 Astra Ultrafast',
    'openai/gpt-6.1-sol': 'GPT-6.1 Sol',
    'anthropic/claude-opus-5-5': 'Claude Opus 5.5',
    'fireworks_ai/glm-5p3': 'GLM-5.3',
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # This standalone project's explicit .env should not silently inherit an
        # unrelated global gateway key. Containers do not contain the .env file.
        return init_settings, dotenv_settings, env_settings, file_secret_settings
    data_dir: Path = Path(".data")
    moyai_database_url: str = Field(default='', repr=False)
    moyai_database_initialize: bool = False
    moyai_schema_mode: Literal['auto', 'verify'] = 'auto'
    moyai_database_schema: str = Field(default='moyai', pattern=r'^moyai(?:_[a-z][a-z0-9_]{0,49})?$')
    moyai_database_pool_size: int = Field(default=8, ge=1, le=256)
    moyai_runtime_role: Literal['standalone', 'coordinator', 'worker', 'broker', 'api'] = 'standalone'
    moyai_separate_broker: bool = False
    temporal_startup_timeout_seconds: float = Field(default=60, ge=1, le=600)
    temporal_worker_activities: int = Field(default=120, ge=1, le=10000)
    temporal_workflow_cache_size: int = Field(default=200, ge=0, le=100000)
    temporal_dispatch_concurrency: int = Field(default=10, ge=1, le=1000)
    temporal_dispatch_batch_size: int = Field(default=200, ge=1, le=10000)
    sandbox_prepared_pool_size: int = Field(default=0, ge=0, le=20)
    sandbox_prepared_idle_seconds: int = Field(default=300, ge=30, le=3600)

    @model_validator(mode='after')
    def database_backend(self):
        if self.moyai_schema_mode == 'verify' and not self.moyai_database_url:
            raise ValueError('MOYAI_SCHEMA_MODE=verify requires PostgreSQL.')
        if self.sandbox_prepared_pool_size:
            if not self.temporal_enabled or self.sandbox_provider != 'modal':
                raise ValueError('Prepared workspaces require Temporal and the Modal provider.')
            if self.sandbox_prepared_pool_size >= self.max_concurrent_runs:
                raise ValueError('Prepared workspaces must leave capacity for active sessions.')
        if self.moyai_database_url:
            if urlsplit(self.moyai_database_url).scheme not in {'postgresql', 'postgres'}:
                raise ValueError('MOYAI_DATABASE_URL must be a Postgres connection URL.')
            if self.checkpoint_dir:
                raise ValueError('SQLite CHECKPOINT_DIR cannot be used with Postgres; use database backups and retain durable file storage.')
        if self.moyai_runtime_role != 'standalone':
            if not self.moyai_database_url or not self.temporal_enabled:
                raise ValueError('Distributed roles require PostgreSQL and Temporal.')
            if not self.object_storage_bucket or not self.session_secret or not self.encryption_key:
                raise ValueError('Distributed roles require shared object storage and explicit SESSION_SECRET and ENCRYPTION_KEY.')
        if self.moyai_runtime_role == 'api' and (self.moyai_schema_mode != 'verify' or not self.moyai_separate_broker):
            raise ValueError('API replicas require MOYAI_SCHEMA_MODE=verify and MOYAI_SEPARATE_BROKER=true.')
        if self.moyai_runtime_role == 'broker' and not self.moyai_separate_broker:
            raise ValueError('The broker role requires MOYAI_SEPARATE_BROKER=true on every cluster process.')
        if self.moyai_separate_broker and self.moyai_runtime_role == 'standalone':
            raise ValueError('A separate broker requires coordinator and worker roles.')
        return self

    attachment_storage_limit_mb: int = Field(default=256, ge=50, le=100000)
    media_share_storage_limit_mb: int = Field(default=1024, ge=64, le=100000)
    media_share_receipt_limit: int = Field(default=4096, ge=128, le=100000)
    object_storage_bucket: str = ''
    object_storage_endpoint: str = ''
    object_storage_region: str = 'us-east-1'
    object_storage_prefix: str = 'moyai'
    object_storage_access_key_id: str = Field(default='', repr=False)
    object_storage_secret_access_key: str = Field(default='', repr=False)
    object_storage_session_token: str = Field(default='', repr=False)
    checkpoint_dir: Path | None = None
    modal_volume_name: str = ""
    trust_modal_proxy: bool = False
    trusted_proxy_hops: int = Field(default=0, ge=0, le=5)
    # Maintenance commands also need the private service's origin; they don't
    # inherit the environment updates performed inside the server process.
    public_url: str = Field(default='http://127.0.0.1:8787',
                            validation_alias=AliasChoices('MOYAI_PUBLIC_URL', 'PUBLIC_URL', 'public_url'))
    cloudflare_access_team_domain: str = ''
    cloudflare_access_audience: str = ''
    cloudflare_access_broker_audience: str = ''
    cloudflare_access_client_id: str = Field(default='', repr=False)
    cloudflare_access_client_secret: str = Field(default='', repr=False)
    cloudflare_access_webhook_paths: list[str] = Field(default_factory=list)
    cloudflare_access_login: bool = False

    @model_validator(mode='after')
    def validate_cloudflare_access(self):
        values = (self.cloudflare_access_team_domain, self.cloudflare_access_audience,
                  self.cloudflare_access_broker_audience)
        if any(values) and not all(values):
            raise ValueError('Configure the Cloudflare team domain and both application audiences together.')
        if self.cloudflare_access_login and (not all(values) or not self.google_domains() or not self.google_admins()
                or any(email.rpartition('@')[2] not in self.google_domains() for email in self.google_admins())):
            raise ValueError('Access sign-in requires Cloudflare Access, allowed work domains and administrator emails.')
        if self.cloudflare_access_team_domain:
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.cloudflareaccess\.com', values[0]):
                raise ValueError('Use the Cloudflare team hostname without a scheme or path.')
            if values[1] == values[2]:
                raise ValueError('Employee and broker Access applications must have different audiences.')
            origin = urlsplit(self.public_url)
            if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                    or origin.path not in {'', '/'} or origin.query or origin.fragment
                    or any(char in self.public_url for char in '\r\n')):
                raise ValueError('Cloudflare Access requires an HTTPS PUBLIC_URL origin without a path or credentials.')
        credentials = (self.cloudflare_access_client_id, self.cloudflare_access_client_secret)
        if any(credentials) and (not all(credentials) or not all(values)):
            raise ValueError('Configure both broker service credentials and Cloudflare Access.')
        if any('\r' in value or '\n' in value for value in (*values, *credentials)):
            raise ValueError('Cloudflare configuration cannot contain line breaks.')
        if any(not re.fullmatch(r'/hooks/automations/[0-9a-f]{32}(?:/[a-z][a-z0-9_-]*)?', path)
               for path in self.cloudflare_access_webhook_paths):
            raise ValueError('Only exact automation webhook paths can be exempted from Access.')
        return self

    def broker_environment(self, token: str) -> dict[str, str]:
        # Explicit empty values remove stale credentials on reused sandboxes.
        return {'WORKSPACE_RUN_TOKEN': token,
                'WORKSPACE_ACCESS_ORIGIN': self.public_url.rstrip('/') if self.cloudflare_access_client_id else '',
                'WORKSPACE_ACCESS_CLIENT_ID': self.cloudflare_access_client_id,
                'WORKSPACE_ACCESS_CLIENT_SECRET': self.cloudflare_access_client_secret}

    def person_login_enabled(self) -> bool:
        return self.google_enabled() or self.cloudflare_access_login

    workspace_password: str = ""
    workspace_member_password: str = ""
    password_login_enabled: bool = True
    google_client_id: str = ""
    google_client_secret: str = ""
    google_allowed_domains: str = "berri.ai"
    google_admin_emails: str = ""
    organization_name: str = Field(default="Internal team", min_length=1, max_length=80)
    session_secret: str = ""
    encryption_key: str = ""
    litellm_api_base: str = ""
    litellm_api_key: str = ""
    litellm_spend_recovery_enabled: bool = True
    model_context_limits: dict[str, ModelContextLimits] = Field(default_factory=dict)
    session_titles_enabled: bool = True
    session_title_model: str = Field(default="openai/gpt-4.1-nano", min_length=1, max_length=200)
    session_title_timeout_seconds: float = Field(default=8, ge=0.1, le=60)
    session_title_concurrency: int = Field(default=2, ge=1, le=8)
    session_title_backfill_limit: int = Field(default=50, ge=0, le=128)
    memory_review_enabled: bool = True
    # Empty uses the completed turn's configured model; no new provider is needed.
    memory_review_model: str = Field(default='', max_length=200)
    memory_review_idle_seconds: float = Field(default=60, ge=0, le=3600)
    memory_review_timeout_seconds: float = Field(default=60, ge=1, le=180)
    memory_review_backfill_limit: int = Field(default=50, ge=0, le=200)
    audio_transcription_model: str = "gpt-transcribe"
    audio_transcription_prompt: str = Field(default="", max_length=800)
    # Separate destination/key; enabling traces never reroutes inference.
    litellm_trace_endpoint: str = ""
    litellm_trace_api_key: str = Field(default="", repr=False)
    lens_feedback_endpoint: str = ""
    lens_feedback_api_key: str = Field(default="", repr=False)
    raindrop_trace_endpoint: str = "https://api.raindrop.ai/v1/traces"
    raindrop_write_key: str = ""
    raindrop_project_id: str = ""
    langfuse_base_url: str = "https://us.cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_tracing_environment: str = Field(default="development", min_length=1, max_length=40,
                                             pattern=r"^[a-z0-9_-]+$")
    trace_environment: str = Field(default="development", min_length=1, max_length=40,
                                   pattern=r"^[a-z0-9_-]+$")
    moyai_build_sha: str = ""
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_api_key: str = ""
    langsmith_project: str = "moyai"
    langsmith_workspace_id: str = ""
    braintrust_api_url: str = "https://api.braintrust.dev"
    braintrust_api_key: str = ""
    braintrust_parent: str = "project_name:moyai"
    agent_model: str = ""
    agent_harness: str = 'claude-agent-sdk'
    sandbox_provider: Literal['modal', 'substrate', 'lambda'] = 'modal'
    lambda_region: str = 'us-east-1'
    lambda_image: str = ''
    lambda_image_version: str = ''
    lambda_checkpoint_bucket: str = ''
    lambda_checkpoint_prefix: str = 'moyai-lambda'
    lambda_execution_role_arn: str = ''
    lambda_egress_connector: str = ''
    # The server uses the standard AWS credential chain; no keys enter guests.
    lambda_profile: str = ''
    substrate_api_url: str = ''
    substrate_router_url: str = ''
    substrate_api_token: str = ''
    substrate_token_file: str = ''
    substrate_ca_cert: str = ''
    substrate_atespace: str = 'moyai'
    substrate_template: str = 'moyai'
    substrate_signing_key: str = ''
    substrate_egress_hosts: str = '*'
    modal_token_id: str = ""
    modal_token_secret: str = ""
    modal_app_name: str = "hermes-workspace"
    modal_vm_runtime: bool = False
    # Billing reports require Modal Team/Enterprise access. Additional objects
    # (e.g. dedicated Volumes) must belong only to Moyai.
    modal_billing_enabled: bool = False
    modal_billing_object_ids: str = ''
    # Separate billing permission; never reuse the namespace worker key implicitly.
    temporal_billing_api_key: str = ''

    auto_prepare_repositories: bool = True
    hermes_revision: str = "7968c72a3cb80beaae51948378944dd6e3423b96"
    max_concurrent_runs: int = Field(default=100, ge=1)
    max_pending_runs: int = Field(default=1000, ge=100)
    max_parallel_agents: int = Field(default=100, ge=1, le=100)
    swarm_models: str = ''
    # Bound response buffers on the web worker independently of sandbox count.
    max_concurrent_model_requests: int = Field(default=8, ge=1)
    # Zero means no overall response deadline or iteration cap.
    run_timeout_seconds: int = Field(default=0, ge=0, le=82800)
    snapshot_timeout_seconds: int = Field(default=180, ge=10, le=900)
    max_agent_iterations: int = Field(default=0, ge=0)
    sandbox_rotation_seconds: int = Field(default=82800, ge=60, le=82800)
    sandbox_idle_seconds: int = Field(default=300, ge=0, le=3600)
    codex_runtime_reuse: bool = True
    temporal_enabled: bool = False
    maintenance_drain: bool = False
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: str = ""
    temporal_tls: bool = True
    temporal_task_queue: str = "moyai-sessions-v1"
    temporal_checkpoint_seconds: int = Field(default=600, ge=30, le=3600)
    startup_recovery_seconds: int = Field(default=600, ge=30, le=3600)
    transport_recovery_seconds: int = Field(default=600, ge=30, le=3600)
    demo_step_seconds: float = Field(default=0.8, ge=0, le=10)
    linear_client_id: str = ""
    linear_client_secret: str = ""
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_signing_secret: str = ""
    slack_bot_enabled: bool = False
    slack_thread_chat_enabled: bool = True
    slack_dm_enabled: bool = True
    slack_identity_linking_enabled: bool = True
    # Comma-separated Slack user IDs, or * for all users in the installed team.
    # Empty disables inbound sessions even when the bot is installed.
    slack_session_users: str = ""
    notion_client_id: str = ""
    notion_client_secret: str = ""

    @model_validator(mode='after')
    def maintenance_requires_durability(self):
        if self.maintenance_drain and not self.temporal_enabled:
            raise ValueError('Maintenance draining requires Temporal to preserve queued work.')
        return self

    @model_validator(mode='after')
    def object_storage_credentials(self):
        if bool(self.object_storage_access_key_id) != bool(self.object_storage_secret_access_key):
            raise ValueError('Set both object-storage access key and secret key, or leave both empty for the AWS credential chain.')
        if self.object_storage_session_token and not self.object_storage_access_key_id:
            raise ValueError('An object-storage session token requires its access key and secret key.')
        return self

    @field_validator('object_storage_bucket')
    @classmethod
    def validate_object_bucket(cls, value):
        if value and not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', value):
            raise ValueError('Use an S3-compatible bucket name.')
        return value

    @field_validator('object_storage_prefix')
    @classmethod
    def validate_object_prefix(cls, value):
        if not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', value) or len(value) > 200:
            raise ValueError('Use a nonempty object prefix containing letters, numbers, hyphens, underscores and slashes.')
        return value

    @field_validator('object_storage_endpoint')
    @classmethod
    def validate_object_endpoint(cls, value):
        from urllib.parse import urlsplit
        if not value:
            return value
        parsed = urlsplit(value)
        local = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path not in {'', '/'} or (parsed.scheme != 'https' and not (local and parsed.scheme == 'http'))):
            raise ValueError('Use an HTTPS object-storage endpoint without credentials or a path. HTTP is allowed only on loopback for testing.')
        _ = parsed.port
        return value.rstrip('/')

    @field_validator('modal_billing_object_ids')
    @classmethod
    def billing_objects(cls, value):
        items = sorted(set(x.strip() for x in value.split(',') if x.strip()))
        if len(items) > 100 or any(not re.fullmatch(r'[A-Za-z0-9-]{1,100}', item) for item in items):
            raise ValueError('Use at most 100 comma-separated Modal object IDs.')
        return ','.join(items)

    @field_validator('moyai_build_sha')
    @classmethod
    def validate_build_sha(cls, value: str) -> str:
        if value and not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', value):
            raise ValueError('MOYAI_BUILD_SHA must be a full 40- or 64-character hexadecimal commit ID.')
        return value.lower()

    @field_validator('litellm_trace_endpoint', 'raindrop_trace_endpoint')
    @classmethod
    def validate_trace_endpoint(cls, value):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if value and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                      parsed.password or parsed.query or parsed.fragment or not parsed.path.endswith('/v1/traces')):
            raise ValueError('Use an HTTPS trace endpoint ending in /v1/traces, without credentials or query parameters.')
        return value

    @field_validator('lens_feedback_endpoint')
    @classmethod
    def validate_lens_feedback_endpoint(cls, value):
        parsed = urlsplit(value)
        if value and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                      parsed.password or parsed.query or parsed.fragment or
                      not parsed.path.endswith('/lens/feedback')):
            raise ValueError('Use an HTTPS feedback endpoint ending in /lens/feedback, without credentials or query parameters.')
        return value

    def lens_feedback_target(self) -> tuple[str, dict[str, str]] | None:
        endpoint = self.lens_feedback_endpoint
        if not endpoint and self.litellm_trace_endpoint:
            parsed = urlsplit(self.litellm_trace_endpoint)
            endpoint = f'{parsed.scheme}://{parsed.netloc}/lens/feedback'
        api_key = self.lens_feedback_api_key or self.litellm_trace_api_key
        if not endpoint or not api_key:
            return None
        return endpoint, {'Authorization': 'Bearer ' + api_key}

    @field_validator('langfuse_base_url', 'langsmith_endpoint', 'braintrust_api_url')
    @classmethod
    def validate_langfuse_base_url(cls, value):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if value and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                      parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Use an HTTPS tracing base URL without credentials or query parameters.')
        return value.rstrip('/')

    @field_validator('langsmith_project', 'langsmith_workspace_id', 'braintrust_parent')
    @classmethod
    def validate_trace_header(cls, value):
        if len(value) > 200 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError('Trace routing headers must be at most 200 printable ASCII characters.')
        return value.strip()

    @field_validator('braintrust_parent')
    @classmethod
    def validate_braintrust_parent(cls, value):
        if value and not re.fullmatch(r'(project_id|project_name):\S[^\r\n]*', value):
            raise ValueError('Use project_id:<id> or project_name:<name> for Braintrust traces.')
        return value

    def trace_destinations(self) -> list[tuple[str, str, dict[str, str]]]:
        """(outbox table, endpoint, headers) for each configured trace receiver."""
        destinations = []
        if self.litellm_trace_endpoint and self.litellm_trace_api_key:
            destinations.append(('trace_outbox', self.litellm_trace_endpoint,
                                 {'Authorization': 'Bearer ' + self.litellm_trace_api_key}))
        if self.raindrop_trace_endpoint and self.raindrop_write_key:
            project = {'X-Raindrop-Project-Id': self.raindrop_project_id} if self.raindrop_project_id else {}
            destinations.append(('trace_outbox_raindrop', self.raindrop_trace_endpoint,
                                 {'Authorization': 'Bearer ' + self.raindrop_write_key, **project}))
        if self.langfuse_base_url and self.langfuse_public_key and self.langfuse_secret_key:
            credentials = base64.b64encode(f'{self.langfuse_public_key}:{self.langfuse_secret_key}'.encode()).decode()
            destinations.append(('trace_outbox_langfuse', self.langfuse_base_url + '/api/public/otel/v1/traces',
                                 {'Authorization': 'Basic ' + credentials, 'x-langfuse-ingestion-version': '4'}))
        if self.langsmith_endpoint and self.langsmith_api_key and self.langsmith_project:
            headers = {'x-api-key': self.langsmith_api_key, 'Langsmith-Project': self.langsmith_project}
            if self.langsmith_workspace_id:
                headers['X-Tenant-Id'] = self.langsmith_workspace_id
            destinations.append(('trace_outbox_langsmith', self.langsmith_endpoint + '/otel/v1/traces', headers))
        if self.braintrust_api_url and self.braintrust_api_key and self.braintrust_parent:
            destinations.append(('trace_outbox_braintrust', self.braintrust_api_url + '/otel/v1/traces',
                                 {'Authorization': 'Bearer ' + self.braintrust_api_key,
                                  'x-bt-parent': self.braintrust_parent}))
        return destinations

    @field_validator('run_timeout_seconds')
    @classmethod
    def validate_run_timeout(cls, value):
        if 0 < value < 120:
            raise ValueError('Use 0 for no response deadline, or at least 120 seconds.')
        return value

    def sandbox_rotation_for(self, provider=None) -> int:
        if (provider or self.sandbox_provider) == 'lambda':
            # One hour for the current tool round, three checkpoint attempts,
            # termination confirmation, and control-plane delays.
            return min(self.sandbox_rotation_seconds, 7 * 3600)
        return self.sandbox_rotation_seconds

    def sandbox_lifetime_seconds(self, provider=None) -> int:
        if (provider or self.sandbox_provider) == 'lambda':
            return 8 * 3600
        if self.run_timeout_seconds:
            return self.run_timeout_seconds + self.snapshot_timeout_seconds + 60
        # Modal allows at most 24h. Leave a full hour after the cooperative
        # handoff boundary for an in-flight operation, snapshot and cleanup.
        return self.sandbox_rotation_seconds + 3600

    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def allowed_models(self) -> list[str]:
        # Preserve custom gateway defaults without replacing the shared catalog.
        return list(dict.fromkeys(x.strip() for x in [self.agent_model, *MODEL_CATALOG] if x.strip()))

    @field_validator('agent_harness')
    @classmethod
    def valid_harness(cls, value):
        from .harnesses import resolve
        return resolve(value).id

    def default_harness(self, value: str | None = None) -> str:
        """Choose once for a new session, honoring processing-mode requirements."""
        model = self.resolve_model(value)
        if model == ASTRA_ULTRAFAST:
            return 'codex'  # Ultrafast requires the native Responses API.
        if 'agent_harness' in self.model_fields_set:
            return self.agent_harness
        # Resolve aliases first, then use the provider namespace so new catalog
        # models inherit their native SDK without a version-specific pairing.
        provider = model.split('/', 1)[0] if '/' in model else ''
        return {
            'openai': 'codex',
            'anthropic': 'claude-agent-sdk',
        }.get(provider, self.agent_harness)

    def harness_model(self, harness: str, value: str | None = None) -> str:
        from .harnesses import validate_harness
        model = self.resolve_model(value)
        validate_harness(harness, model)
        return model

    def resolve_model(self, value: str | None = None, fallback: str = '') -> str:
        aliases = {
            'astra': 'openai/gpt-6-astra', '6-astra': 'openai/gpt-6-astra',
            'openai/6-astra': 'openai/gpt-6-astra', 'gpt-6-astra': 'openai/gpt-6-astra',
            'astra-ultrafast': ASTRA_ULTRAFAST, '6-astra-ultrafast': ASTRA_ULTRAFAST,
            'gpt-6-astra-ultrafast': ASTRA_ULTRAFAST,
            'astra ultrafast': ASTRA_ULTRAFAST, '6-astra ultrafast': ASTRA_ULTRAFAST,
            'sol': 'openai/gpt-6.1-sol', '6.1-sol': 'openai/gpt-6.1-sol',
            'openai/6.1-sol': 'openai/gpt-6.1-sol', 'gpt-6.1-sol': 'openai/gpt-6.1-sol',
            'gpt 6.1 sol': 'openai/gpt-6.1-sol',
            'opus': 'anthropic/claude-opus-5-5', 'opus-5-5': 'anthropic/claude-opus-5-5',
            'claude/opus-5-5': 'anthropic/claude-opus-5-5', 'claude-opus-5-5': 'anthropic/claude-opus-5-5',
            'glm': 'fireworks_ai/glm-5p3', 'glm-5.3': 'fireworks_ai/glm-5p3',
            'glm-5p3': 'fireworks_ai/glm-5p3',
            'glm 5.3': 'fireworks_ai/glm-5p3', 'glm 5p3': 'fireworks_ai/glm-5p3',
        }
        aliases.update({item['name'].lower(): item['id'] for item in self.model_choices()})
        selected = value if value is not None else fallback or self.agent_model or (self.allowed_models() or [''])[0]
        selected = aliases.get(selected.strip().lower(), selected.strip())
        if selected not in self.allowed_models():
            raise ValueError('Choose a model enabled for this workspace.')
        return selected

    def model_choices(self) -> list[dict[str, str]]:
        return [{'id': model, 'name': MODEL_CATALOG.get(model, model)} for model in self.allowed_models()]

    def google_domains(self) -> set[str]:
        return {value.strip().lower() for value in self.google_allowed_domains.split(",") if value.strip()}

    def google_admins(self) -> set[str]:
        return {value.strip().lower() for value in self.google_admin_emails.split(",") if value.strip()}

    @field_validator('substrate_api_url', 'substrate_router_url')
    @classmethod
    def substrate_endpoint(cls, value):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if value and (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                      or parsed.path not in ('', '/') or (parsed.scheme != 'https' and not
                      (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}))):
            raise ValueError('Use an HTTPS origin, or HTTP on loopback for a local port-forward.')
        return value.rstrip('/')

    @field_validator('substrate_atespace', 'substrate_template')
    @classmethod
    def substrate_name(cls, value):
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
            raise ValueError('Use a lowercase Substrate resource name, at most 63 characters.')
        return value

    @field_validator('substrate_signing_key')
    @classmethod
    def substrate_key(cls, value):
        if value:
            from sandbox.substrate_protocol import private_key
            private_key(value)
        return value

    @field_validator('substrate_egress_hosts')
    @classmethod
    def substrate_hosts(cls, value):
        hosts = [host.strip() for host in value.split(',') if host.strip()]
        if not hosts:
            raise ValueError('Enter at least one outbound hostname pattern.')
        return ','.join(hosts)

    @field_validator('lambda_region', 'lambda_checkpoint_bucket', 'lambda_checkpoint_prefix')
    @classmethod
    def lambda_names(cls, value):
        if value and not re.fullmatch(r'[a-z0-9][a-z0-9._/-]{0,180}', value):
            raise ValueError('Use an AWS region, bucket, or storage prefix without spaces or colons.')
        if '..' in value or value.endswith('/'):
            raise ValueError('Do not use relative components or a trailing slash.')
        return value

    def missing_sandbox(self, provider=None) -> list[str]:
        if (provider or self.sandbox_provider) == 'lambda':
            required = {'LAMBDA_REGION': self.lambda_region, 'LAMBDA_IMAGE': self.lambda_image,
                        'LAMBDA_IMAGE_VERSION': self.lambda_image_version,
                        'LAMBDA_CHECKPOINT_BUCKET': self.lambda_checkpoint_bucket,
                        'LAMBDA_CHECKPOINT_PREFIX': self.lambda_checkpoint_prefix}
            return [key for key, value in required.items() if not value]
        required = ({'MODAL_TOKEN_ID': self.modal_token_id, 'MODAL_TOKEN_SECRET': self.modal_token_secret}
                    if (provider or self.sandbox_provider) == 'modal' else {
                        'SUBSTRATE_API_URL': self.substrate_api_url, 'SUBSTRATE_ROUTER_URL': self.substrate_router_url,
                        'SUBSTRATE_API_TOKEN': self.substrate_api_token or self.substrate_token_file,
                        'SUBSTRATE_SIGNING_KEY': self.substrate_signing_key,
                        'SUBSTRATE_ATESPACE': self.substrate_atespace, 'SUBSTRATE_TEMPLATE': self.substrate_template})
        return [key for key, value in required.items() if not value]

    def missing_cloud(self, provider=None) -> list[str]:
        required = {'LITELLM_API_BASE': self.litellm_api_base, 'LITELLM_API_KEY': self.litellm_api_key,
                    'AGENT_MODEL': self.agent_model}
        return self.missing_sandbox(provider) + [key for key, value in required.items() if not value]
