from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # This standalone project's explicit .env should not silently inherit an
        # unrelated global gateway key. Containers do not contain the .env file.
        return init_settings, dotenv_settings, env_settings, file_secret_settings
    data_dir: Path = Path(".data")
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
    agent_model: str = ""
    agent_models: str = "openai/gpt-6-astra,anthropic/claude-opus-5-5"
    modal_token_id: str = ""
    modal_token_secret: str = ""
    modal_app_name: str = "hermes-workspace"
    modal_vm_runtime: bool = False
    hermes_revision: str = "7968c72a3cb80beaae51948378944dd6e3423b96"
    max_concurrent_runs: int = Field(default=2, ge=1, le=10)
    # Zero means no overall response deadline or iteration cap.
    run_timeout_seconds: int = Field(default=0, ge=0, le=82800)
    snapshot_timeout_seconds: int = Field(default=180, ge=10, le=600)
    max_agent_iterations: int = Field(default=0, ge=0)
    sandbox_rotation_seconds: int = Field(default=82800, ge=60, le=82800)
    temporal_enabled: bool = False
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: str = ""
    temporal_tls: bool = True
    temporal_task_queue: str = "moyai-sessions-v1"
    temporal_checkpoint_seconds: int = Field(default=600, ge=30, le=3600)
    # Remains off until provider billing access and shared-fee allocation are ready.
    operating_costs_enabled: bool = False
    operating_costs_modal_object_ids: str = ""
    demo_step_seconds: float = Field(default=0.8, ge=0, le=10)
    linear_client_id: str = ""
    linear_client_secret: str = ""
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_signing_secret: str = ""
    slack_bot_enabled: bool = False
    slack_thread_chat_enabled: bool = True
    slack_dm_enabled: bool = True
    # Comma-separated Slack user IDs, or * for all users in the installed team.
    # Empty disables inbound sessions even when the bot is installed.
    slack_session_users: str = ""
    notion_client_id: str = ""
    notion_client_secret: str = ""

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
        return list(dict.fromkeys(x.strip() for x in [self.agent_model, *self.agent_models.split(',')] if x.strip()))

    def resolve_model(self, value: str | None = None, fallback: str = '') -> str:
        aliases = {
            'astra': 'openai/gpt-6-astra', '6-astra': 'openai/gpt-6-astra',
            'openai/6-astra': 'openai/gpt-6-astra', 'gpt-6-astra': 'openai/gpt-6-astra',
            'opus': 'anthropic/claude-opus-5-5', 'opus-5-5': 'anthropic/claude-opus-5-5',
            'claude/opus-5-5': 'anthropic/claude-opus-5-5', 'claude-opus-5-5': 'anthropic/claude-opus-5-5',
        }
        selected = value if value is not None else fallback or self.agent_model or (self.allowed_models() or [''])[0]
        selected = aliases.get(selected.strip().lower(), selected.strip())
        if selected not in self.allowed_models():
            raise ValueError('Choose a model enabled for this workspace.')
        return selected

    def model_choices(self) -> list[dict[str, str]]:
        names = {'openai/gpt-6-astra': 'GPT-6 Astra', 'anthropic/claude-opus-5-5': 'Claude Opus 5.5'}
        return [{'id': model, 'name': names.get(model, model)} for model in self.allowed_models()]

    def google_domains(self) -> set[str]:
        return {value.strip().lower() for value in self.google_allowed_domains.split(",") if value.strip()}

    def google_admins(self) -> set[str]:
        return {value.strip().lower() for value in self.google_admin_emails.split(",") if value.strip()}

    def missing_cloud(self) -> list[str]:
        required = {
            "MODAL_TOKEN_ID": self.modal_token_id,
            "MODAL_TOKEN_SECRET": self.modal_token_secret,
            "LITELLM_API_BASE": self.litellm_api_base,
            "LITELLM_API_KEY": self.litellm_api_key,
            "AGENT_MODEL": self.agent_model,
        }
        return [key for key, value in required.items() if not value]
