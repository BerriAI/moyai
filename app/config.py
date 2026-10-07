from pathlib import Path
from typing import Literal
import base64
import re

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from .context_budget import ModelContextLimits


# Keep picker IDs and labels in code so a stale deployment environment cannot
# hide models added by a release. AGENT_MODEL only chooses the default.
MODEL_CATALOG: dict[str, str] = {
    'openai/gpt-6-astra': 'GPT-6 Astra',
    'openai/gpt-6.1-sol': 'GPT-6.1 Sol',
    'anthropic/claude-opus-5-5': 'Claude Opus 5.5',
    'fireworks_ai/glm-5p3': 'GLM-5.3',
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # This standalone project's explicit .env should not silently inherit an
        # unrelated global gateway key. Containers do not contain the .env file.
        return init_settings, dotenv_settings, env_settings, file_secret_settings
    data_dir: Path = Path(".data")
    attachment_storage_limit_mb: int = Field(default=256, ge=50, le=100000)
    checkpoint_dir: Path | None = None
    modal_volume_name: str = ""
    trust_modal_proxy: bool = False
    public_url: str = "http://127.0.0.1:8787"
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
    model_context_limits: dict[str, ModelContextLimits] = Field(default_factory=dict)
    session_titles_enabled: bool = True
    session_title_model: str = Field(default="openai/gpt-4.1-nano", min_length=1, max_length=200)
    session_title_timeout_seconds: float = Field(default=8, ge=0.1, le=60)
    session_title_concurrency: int = Field(default=2, ge=1, le=8)
    session_title_backfill_limit: int = Field(default=50, ge=0, le=128)
    audio_transcription_model: str = "gpt-transcribe"
    audio_transcription_prompt: str = Field(default="", max_length=800)
    # Separate destination/key; enabling traces never reroutes inference.
    litellm_trace_endpoint: str = ""
    litellm_trace_api_key: str = ""
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
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_api_key: str = ""
    langsmith_project: str = "moyai"
    langsmith_workspace_id: str = ""
    braintrust_api_url: str = "https://api.braintrust.dev"
    braintrust_api_key: str = ""
    braintrust_parent: str = "project_name:moyai"
    agent_model: str = ""
    agent_harness: str = 'claude-agent-sdk'
    sandbox_provider: Literal['modal', 'substrate'] = 'modal'
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
    max_concurrent_runs: int = Field(default=100, ge=1, le=100)
    max_pending_runs: int = Field(default=1000, ge=100, le=5000)
    max_parallel_agents: int = Field(default=100, ge=1, le=100)
    # Bound response buffers on the web worker independently of sandbox count.
    max_concurrent_model_requests: int = Field(default=8, ge=1, le=100)
    # Zero means no overall response deadline or iteration cap.
    run_timeout_seconds: int = Field(default=0, ge=0, le=82800)
    snapshot_timeout_seconds: int = Field(default=180, ge=10, le=600)
    max_agent_iterations: int = Field(default=0, ge=0)
    sandbox_rotation_seconds: int = Field(default=82800, ge=60, le=82800)
    sandbox_idle_seconds: int = Field(default=300, ge=0, le=3600)
    temporal_enabled: bool = False
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: str = ""
    temporal_tls: bool = True
    temporal_task_queue: str = "moyai-sessions-v1"
    temporal_checkpoint_seconds: int = Field(default=600, ge=30, le=3600)
    startup_recovery_seconds: int = Field(default=600, ge=30, le=3600)
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

    @field_validator('modal_billing_object_ids')
    @classmethod
    def billing_objects(cls, value):
        items = sorted(set(x.strip() for x in value.split(',') if x.strip()))
        if len(items) > 100 or any(not re.fullmatch(r'[A-Za-z0-9-]{1,100}', item) for item in items):
            raise ValueError('Use at most 100 comma-separated Modal object IDs.')
        return ','.join(items)

    @field_validator('litellm_trace_endpoint', 'raindrop_trace_endpoint')
    @classmethod
    def validate_trace_endpoint(cls, value):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if value and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                      parsed.password or parsed.query or parsed.fragment or parsed.path != '/v1/traces'):
            raise ValueError('Use an HTTPS trace endpoint ending in /v1/traces, without credentials or query parameters.')
        return value

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

    def sandbox_lifetime_seconds(self) -> int:
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
        """Choose once for a new session; explicit deployment settings win."""
        model = self.resolve_model(value)
        if 'agent_harness' in self.model_fields_set:
            return self.agent_harness
        return {
            'openai/gpt-6-astra': 'codex',
            'anthropic/claude-opus-5-5': 'claude-agent-sdk',
        }.get(model, self.agent_harness)

    def harness_model(self, harness: str, value: str | None = None) -> str:
        from .harnesses import validate_harness
        model = self.resolve_model(value)
        validate_harness(harness, model)
        return model

    def resolve_model(self, value: str | None = None, fallback: str = '') -> str:
        aliases = {
            'astra': 'openai/gpt-6-astra', '6-astra': 'openai/gpt-6-astra',
            'openai/6-astra': 'openai/gpt-6-astra', 'gpt-6-astra': 'openai/gpt-6-astra',
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

    def missing_sandbox(self, provider=None) -> list[str]:
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
