"""Saved picker choices and their actual gateway model IDs."""

ASTRA = 'openai/gpt-6-astra'
ASTRA_ULTRAFAST = 'openai/gpt-6-astra-ultrafast'
GLM = 'fireworks_ai/glm-5p3'


def gateway_model(model: str) -> str:
    # Ultrafast is a Moyai preference, not a separate gateway deployment.
    return ASTRA if model == ASTRA_ULTRAFAST else model


def gateway_payload(payload: dict, route: str) -> dict:
    """Translate only at the network boundary; admission keeps the saved choice."""
    selected = payload['model']
    result = {key: value for key, value in payload.items() if key != 'service_tier'}
    result['model'] = gateway_model(selected)
    if result['model'] == ASTRA:
        result['content_policy_fallbacks'] = [{ASTRA: [{'model': GLM, 'service_tier': None}]}]
    if route == '/v1/responses' and result['model'] == ASTRA:
        # Explicit default also overrides a gateway deployment's tier default.
        result['service_tier'] = 'ultrafast' if selected == ASTRA_ULTRAFAST else 'default'
    return result
