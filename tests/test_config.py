from app.config import Settings
import pytest
from pydantic import ValidationError


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
    ('astra', 'codex'), ('6-astra', 'codex'), ('GPT-6 Astra', 'codex'),
    ('opus', 'claude-agent-sdk'), ('Claude Opus 5.5', 'claude-agent-sdk'),
    ('glm', 'claude-agent-sdk'), ('sol', 'codex'), ('GPT-6.1 Sol', 'codex'),
    ('custom-gateway-model', 'claude-agent-sdk'),
])
def test_new_session_default_harness_resolves_models(model, harness, monkeypatch):
    monkeypatch.delenv('AGENT_HARNESS', raising=False)
    settings = Settings(_env_file=None, agent_model='custom-gateway-model')
    assert settings.default_harness(model) == harness
    with pytest.raises(ValueError, match='enabled'):
        settings.default_harness('not-configured')


@pytest.mark.parametrize(('model', 'harness'), [
    ('openai/future-model', 'codex'),
    ('anthropic/future-model', 'claude-agent-sdk'),
    ('openai-compatible/gpt-next', 'claude-agent-sdk'),
    ('other/openai/gpt-next', 'claude-agent-sdk'),
    ('custom-gateway-model', 'claude-agent-sdk'),
    ('openai', 'claude-agent-sdk'),
])
def test_custom_default_harness_uses_provider_namespace(model, harness, monkeypatch):
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
