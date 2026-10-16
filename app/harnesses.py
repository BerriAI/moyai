"""Session-level runtime selection; never silently fall back to another harness."""
from app.harness_catalog import HARNESSES, resolve
from .model_selection import ASTRA_ULTRAFAST


def validate_harness(harness, model):
    # Model authorization belongs to Settings.resolve_model, not the harness.
    # The gateway handles compatibility with each harness's native protocol.
    resolve(harness)
    if model == ASTRA_ULTRAFAST and harness not in {'codex', 'deepagents-temporal'}:
        raise ValueError('GPT-6 Astra Ultrafast requires the Responses API. Choose Deep Agents for a new session or select regular GPT-6 Astra.')
    return harness


def choices():
    return [definition.public() for definition in HARNESSES.values()]
