from app.config import Settings
import pytest
from pydantic import ValidationError


def test_independent_runtime_defaults_on_and_preserves_explicit_opt_out(monkeypatch):
    monkeypatch.delenv('INDEPENDENT_AGENT_RUNTIME', raising=False)
    assert Settings(_env_file=None).independent_agent_runtime is True
    monkeypatch.setenv('INDEPENDENT_AGENT_RUNTIME', 'false')
    assert Settings(_env_file=None).independent_agent_runtime is False


@pytest.mark.parametrize('value', ['', 'a1' * 20, 'B2' * 32])
def test_build_sha_accepts_only_full_optional_commit_identity(monkeypatch, value):
    monkeypatch.setenv('MOYAI_BUILD_SHA', value)
    assert Settings(_env_file=None).moyai_build_sha == value.lower()


@pytest.mark.parametrize('value', ['main', 'abcdef1', 'a' * 39, 'a' * 41, 'a' * 63, 'a' * 65, 'g' * 40, ' ' + 'a' * 40])
def test_build_sha_rejects_partial_or_invalid_commit_identity(monkeypatch, value):
    monkeypatch.setenv('MOYAI_BUILD_SHA', value)
    with pytest.raises(ValidationError, match='MOYAI_BUILD_SHA'):
        Settings(_env_file=None)


def test_build_identity_remains_optional_and_environment_keeps_its_default(monkeypatch):
    monkeypatch.delenv('MOYAI_BUILD_SHA', raising=False)
    monkeypatch.delenv('TRACE_ENVIRONMENT', raising=False)
    settings = Settings(_env_file=None)
    assert settings.moyai_build_sha == ''
    assert settings.trace_environment == 'development'


def test_project_gateway_key_overrides_unrelated_shell_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "unrelated-shell-key")
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=project-key\n")
    assert Settings(_env_file=env).litellm_api_key == "project-key"
    env.write_text("LITELLM_API_KEY=\n")
    assert Settings(_env_file=env).litellm_api_key == ""
    assert Settings(_env_file=None).litellm_api_key == "unrelated-shell-key"


def test_deployment_cannot_select_github_repositories(monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORIES', 'OtherOrg/private')
    settings = Settings(_env_file=None)
    assert 'github_repositories' not in type(settings).model_fields
    assert 'github_repository' not in type(settings).model_fields


def test_transport_recovery_window_is_configurable_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('TRANSPORT_RECOVERY_SECONDS', raising=False)
    assert Settings(_env_file=None).transport_recovery_seconds == 600
    for seconds in (30, 3600):
        monkeypatch.setenv('TRANSPORT_RECOVERY_SECONDS', str(seconds))
        assert Settings(_env_file=None).transport_recovery_seconds == seconds
    for seconds in (29, 3601):
        monkeypatch.setenv('TRANSPORT_RECOVERY_SECONDS', str(seconds))
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


@pytest.mark.parametrize('source', ['environment', 'dotenv'])
@pytest.mark.parametrize('legacy_models', ['openai/gpt-6-astra,anthropic/claude-opus-5-5', ''])
def test_stale_deployment_model_list_cannot_hide_code_models(tmp_path, monkeypatch, source, legacy_models):
    env_file = tmp_path / '.env'
    if source == 'environment':
        monkeypatch.setenv('AGENT_MODELS', legacy_models)
    else:
        env_file.write_text(f'AGENT_MODELS={legacy_models}\n')
    settings = Settings(_env_file=env_file, agent_model='openai/gpt-6-astra')
    assert {'id': 'fireworks_ai/glm-5p3', 'name': 'GLM-5.3'} in settings.model_choices()
    assert settings.resolve_model('glm') == 'fireworks_ai/glm-5p3'
    assert {'id': 'openai/gpt-6.1-sol', 'name': 'GPT-6.1 Sol'} in settings.model_choices()
    assert settings.resolve_model('sol') == 'openai/gpt-6.1-sol'
    assert settings.resolve_model() == 'openai/gpt-6-astra'
    with pytest.raises(ValueError):
        settings.resolve_model('unapproved-model')


@pytest.mark.parametrize('alias', [
    'openai/gpt-6.1-sol', 'sol', '6.1-sol', 'openai/6.1-sol', 'gpt-6.1-sol',
    'GPT-6.1 Sol', 'GPT 6.1 Sol', '  SOL  ',
])
def test_sol_aliases_resolve_without_changing_default(alias):
    settings = Settings(_env_file=None, agent_model='openai/gpt-6-astra')
    assert settings.resolve_model(alias) == 'openai/gpt-6.1-sol'
    assert settings.resolve_model() == 'openai/gpt-6-astra'


def test_custom_default_stays_selectable_alongside_code_catalog(monkeypatch):
    monkeypatch.setenv('AGENT_MODEL', 'custom-gateway-model')
    settings = Settings(_env_file=None)
    assert settings.resolve_model() == 'custom-gateway-model'
    assert settings.model_choices()[0] == {'id': 'custom-gateway-model', 'name': 'custom-gateway-model'}
    assert settings.resolve_model('glm') == 'fireworks_ai/glm-5p3'
    assert {'id': 'openai/gpt-6.1-sol', 'name': 'GPT-6.1 Sol'} in settings.model_choices()
    assert settings.resolve_model('sol') == 'openai/gpt-6.1-sol'


@pytest.mark.parametrize(('model', 'harness'), [
    ('astra', 'deepagents-temporal'), ('6-astra', 'deepagents-temporal'), ('GPT-6 Astra', 'deepagents-temporal'),
    ('opus', 'deepagents-temporal'), ('Claude Opus 5.5', 'deepagents-temporal'),
    ('glm', 'deepagents-temporal'), ('sol', 'deepagents-temporal'), ('GPT-6.1 Sol', 'deepagents-temporal'),
    ('custom-gateway-model', 'deepagents-temporal'),
])
def test_new_session_default_harness_resolves_models(model, harness, monkeypatch):
    monkeypatch.delenv('AGENT_HARNESS', raising=False)
    settings = Settings(_env_file=None, agent_model='custom-gateway-model')
    assert settings.default_harness(model) == harness
    with pytest.raises(ValueError, match='enabled'):
        settings.default_harness('not-configured')


@pytest.mark.parametrize(('model', 'harness'), [
    ('openai/future-model', 'deepagents-temporal'),
    ('anthropic/future-model', 'deepagents-temporal'),
    ('openai-compatible/gpt-next', 'deepagents-temporal'),
    ('other/openai/gpt-next', 'deepagents-temporal'),
    ('custom-gateway-model', 'deepagents-temporal'),
    ('openai', 'deepagents-temporal'),
])
def test_custom_models_share_durable_default(model, harness, monkeypatch):
    monkeypatch.delenv('AGENT_HARNESS', raising=False)
    settings = Settings(_env_file=None, agent_model=model)
    assert settings.default_harness() == harness
    assert settings.default_harness(model) == harness


@pytest.mark.parametrize('source', ['init', 'environment', 'dotenv'])
@pytest.mark.parametrize('harness', ['hermes', 'claude-agent-sdk', 'codex'])
def test_explicit_harness_configuration_overrides_model_pairings(tmp_path, monkeypatch, source, harness):
    monkeypatch.delenv('AGENT_HARNESS', raising=False)
    env_file, kwargs = None, {}
    if source == 'init':
        kwargs['agent_harness'] = harness
    elif source == 'environment':
        monkeypatch.setenv('AGENT_HARNESS', harness)
    else:
        env_file = tmp_path / '.env'
        env_file.write_text(f'AGENT_HARNESS={harness}\n')
    settings = Settings(_env_file=env_file, agent_model='openai/gpt-6-astra', **kwargs)
    assert settings.default_harness() == harness
    assert settings.default_harness('opus') == harness
    assert settings.default_harness('sol') == harness


def test_lens_feedback_target_derives_gateway_and_standalone_origins_and_honors_override():
    gateway = Settings(_env_file=None, litellm_trace_endpoint='https://gateway.example/lens-ingest/v1/traces',
                       litellm_trace_api_key='gateway-key')
    assert gateway.lens_feedback_target() == (
        'https://gateway.example/lens/feedback', {'Authorization': 'Bearer gateway-key'})

    standalone = Settings(_env_file=None, litellm_trace_endpoint='https://lens.example/v1/traces',
                          litellm_trace_api_key='lens-key')
    assert standalone.lens_feedback_target() == (
        'https://lens.example/lens/feedback', {'Authorization': 'Bearer lens-key'})

    explicit = Settings(_env_file=None, lens_feedback_endpoint='https://feedback.example/lens/feedback',
                        litellm_trace_endpoint='https://gateway.example/v1/traces',
                        litellm_trace_api_key='explicit-key')
    assert explicit.lens_feedback_target() == (
        'https://feedback.example/lens/feedback', {'Authorization': 'Bearer explicit-key'})

    feedback_key = Settings(_env_file=None, lens_feedback_endpoint='https://feedback.example/lens/feedback',
                            litellm_trace_api_key='trace-key', lens_feedback_api_key='feedback-key')
    assert feedback_key.lens_feedback_target() == (
        'https://feedback.example/lens/feedback', {'Authorization': 'Bearer feedback-key'})
    assert 'feedback-key' not in repr(feedback_key)
    assert 'trace-key' not in repr(feedback_key)

    feedback_key_only = Settings(_env_file=None, lens_feedback_endpoint='https://feedback.example/lens/feedback',
                                 lens_feedback_api_key='feedback-only-key')
    assert feedback_key_only.lens_feedback_target() == (
        'https://feedback.example/lens/feedback', {'Authorization': 'Bearer feedback-only-key'})

    assert Settings(_env_file=None, litellm_trace_endpoint='https://gateway.example/v1/traces').lens_feedback_target() is None
    assert Settings(_env_file=None, litellm_trace_api_key='key').lens_feedback_target() is None


@pytest.mark.parametrize('endpoint', [
    'http://lens.example/lens/feedback',
    'https://user:password@lens.example/lens/feedback',
    'https://lens.example/lens/feedback?token=secret',
    'https://lens.example/lens/feedback#fragment',
    'https://lens.example/other',
])
def test_lens_feedback_endpoint_requires_https_feedback_path_without_url_extras(endpoint):
    with pytest.raises(ValueError):
        Settings(_env_file=None, lens_feedback_endpoint=endpoint)
