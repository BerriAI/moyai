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
