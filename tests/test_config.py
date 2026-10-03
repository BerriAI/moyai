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


def test_github_allowlist_preserves_legacy_config_and_one_organization():
    assert Settings(_env_file=None, github_repository='BerriAI/litellm').allowed_github_repositories() == ['BerriAI/litellm']
    settings = Settings(_env_file=None, github_repositories=' BerriAI/litellm, BerriAI/moyai-devin,berriai/litellm ')
    assert [repo.lower() for repo in settings.allowed_github_repositories()] == ['berriai/litellm', 'berriai/moyai-devin']
    for value in ['BerriAI/litellm,OtherOrg/private', 'BerriAI/*', 'https://github.com/BerriAI/litellm', 'BerriAI/repo?token=bad']:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, github_repositories=value)


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
    assert settings.resolve_model() == 'openai/gpt-6-astra'
    with pytest.raises(ValueError):
        settings.resolve_model('unapproved-model')


def test_custom_default_stays_selectable_alongside_code_catalog(monkeypatch):
    monkeypatch.setenv('AGENT_MODEL', 'custom-gateway-model')
    settings = Settings(_env_file=None)
    assert settings.resolve_model() == 'custom-gateway-model'
    assert settings.model_choices()[0] == {'id': 'custom-gateway-model', 'name': 'custom-gateway-model'}
    assert settings.resolve_model('glm') == 'fireworks_ai/glm-5p3'
