"""Bedrock tool schema compatibility through the configured Responses model."""
import pytest
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.tools import ToolDefinition

from moyai.agent import inference_models
from moyai.settings import Settings


@pytest.mark.parametrize(("name", "strict"), [
    ("bedrock/us.anthropic.claude-sonnet-4-6", False),
    ("openai/gpt-5.4-mini", True),
    ("organization-alias", True),
])
async def test_provider_qualified_bedrock_tools_keep_bounds_without_strict_mode(name, strict):
    settings = Settings(_env_file=None, gateway_url="https://gateway.invalid/v1", default_model=name)
    model = inference_models(settings)[name]
    assert isinstance(model, OpenAIResponsesModel)
    try:
        tool = ToolDefinition(name="bounded", parameters_json_schema={
            "type": "object", "properties": {"count": {"type": "integer", "minimum": 1}}, "required": ["count"],
        })
        _, request = model.prepare_request(None, ModelRequestParameters(function_tools=[tool]))
        wire = model._map_tool_definition(request.function_tools[0], visibility="active")
        assert wire["strict"] is strict
        assert wire["parameters"]["properties"]["count"]["minimum"] == 1
    finally:
        await model.client.close()
