import pytest

from evals.agent import runtime_selection


@pytest.mark.parametrize(('model', 'resolved', 'harness'), [
    ('openai/gpt-6.1-sol', 'openai/gpt-6.1-sol', 'codex'),
    ('anthropic/claude-opus-5-5', 'anthropic/claude-opus-5-5', 'claude-agent-sdk'),
    # A configured custom gateway alias is a model ID, not a UI picker shortcut.
    ('team-model', 'team-model', 'claude-agent-sdk'),
])
def test_should_use_production_model_resolution_and_native_harness_selection(model, resolved, harness):
    assert runtime_selection({'AGENT_MODEL': model}) == (resolved, harness)


def test_should_honor_explicit_deployment_harness_and_eval_override():
    config = {'AGENT_MODEL': 'openai/gpt-6.1-sol', 'AGENT_HARNESS': 'claude-agent-sdk'}
    assert runtime_selection(config) == ('openai/gpt-6.1-sol', 'claude-agent-sdk')
    assert runtime_selection({**config, 'MOYAI_EVAL_HARNESS': 'codex'}) == ('openai/gpt-6.1-sol', 'codex')


def test_should_keep_configured_gateway_alias_when_explicit_harness_is_selected():
    assert runtime_selection({'AGENT_MODEL': 'sol', 'AGENT_HARNESS': 'codex'}) == ('sol', 'codex')


def test_should_not_inherit_unrelated_process_or_dotenv_configuration(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AGENT_MODEL', 'anthropic/claude-opus-5-5')
    monkeypatch.setenv('AGENT_HARNESS', 'claude-agent-sdk')
    (tmp_path / '.env').write_text('AGENT_MODEL=anthropic/claude-opus-5-5\nAGENT_HARNESS=claude-agent-sdk\n')
    assert runtime_selection({'AGENT_MODEL': 'openai/gpt-6.1-sol'}) == ('openai/gpt-6.1-sol', 'codex')


@pytest.mark.parametrize('key', ['AGENT_HARNESS', 'MOYAI_EVAL_HARNESS'])
def test_should_reject_unsupported_explicit_harness(key):
    with pytest.raises(ValueError, match='must be codex or claude-agent-sdk'):
        runtime_selection({'AGENT_MODEL': 'openai/gpt-6.1-sol', key: 'unknown'})
