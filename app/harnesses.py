"""Session-level runtime selection; never silently fall back to another harness."""
from sandbox.harness_registry import HARNESSES, resolve
from .model_selection import ASTRA_ULTRAFAST


def validate_harness(harness, model):
    # Model authorization belongs to Settings.resolve_model, not the harness.
    # The gateway handles compatibility with each harness's native protocol.
    resolve(harness)
    if model == ASTRA_ULTRAFAST and harness != 'codex':
        raise ValueError('GPT-6 Astra Ultrafast requires Codex. Choose Codex for a new session or select regular GPT-6 Astra.')
    return harness


def choices():
    return [definition.public() for definition in HARNESSES.values()]
