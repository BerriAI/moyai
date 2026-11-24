"""Saved picker choices and their actual gateway model IDs."""

ASTRA = 'openai/gpt-6-astra'
ASTRA_ULTRAFAST = 'openai/gpt-6-astra-ultrafast'


def gateway_model(model: str) -> str:
    # Ultrafast is a Moyai preference, not a separate gateway deployment.
    return ASTRA if model == ASTRA_ULTRAFAST else model
