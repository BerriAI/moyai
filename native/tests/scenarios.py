"""Controlled model responses for SDK integration tests, never live-provider proof."""

from __future__ import annotations

import asyncio
import json

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel


def integration_model(scenario: str, *, gates: tuple[asyncio.Event, asyncio.Event] | None = None) -> FunctionModel:
    def response(messages, info):
        prompts = "\n".join(
            str(part.content) for message in messages for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        completed = {
            part.tool_name for message in messages for part in message.parts
            if isinstance(part, ToolReturnPart)
        }
        if scenario == "steer":
            if "VERIFY_CORRECTED_CHILD" in prompts:
                if "write_file" not in completed:
                    return ModelResponse(parts=[ToolCallPart(
                        "write_file", {"path": "steered.txt", "content": "corrected"},
                        tool_call_id="child-write",
                    )])
                return ModelResponse(parts=[TextPart("Child completed.")])
            if "NEW GUIDANCE" in prompts:
                if "delegate_task" not in completed:
                    return ModelResponse(parts=[ToolCallPart(
                        "delegate_task", {"agent_name": "self", "task": "VERIFY_CORRECTED_CHILD"},
                        tool_call_id="delegate-after-guidance",
                    )])
                return ModelResponse(parts=[TextPart("Followed the new guidance.")])
            return ModelResponse(parts=[ToolCallPart(
                "write_file", {"path": "forbidden-old.txt", "content": "old guidance"},
                tool_call_id="old-action",
            )])
        if any(
            isinstance(part, UserPromptPart)
            and "Return the phrase native-child-result." in str(part.content)
            for message in messages for part in message.parts
        ):
            if scenario == "tools" and "write_plan" not in completed:
                return ModelResponse(parts=[ToolCallPart(
                    "write_plan", {"items": [{"content": "Inspect the delegated task", "status": "completed"}]},
                    tool_call_id="child-plan",
                )])
            return ModelResponse(parts=[TextPart("native-child-result")])
        completed = {
            part.tool_name for message in messages for part in message.parts
            if isinstance(part, ToolReturnPart)
        }
        if scenario == "question":
            calls = [("ask_user_question", {
                "questions": [{
                    "header": "Choice", "question": "Which output should I create?",
                    "options": [{"label": "Text"}, {"label": "JSON"}],
                }]
            })]
        elif scenario == "tools":
            loader = next(tool for tool in info.function_tools if tool.name == "load_capability")
            loader_key = next(iter(loader.parameters_json_schema["properties"]))
            calls = [
                ("load_capability", {loader_key: "coding"}),
                ("write_plan", {"items": [{"content": "Verify native execution", "status": "in_progress"}]}),
                ("write_file", {"path": "proof.txt", "content": "Written by the native file capability.\n"}),
                ("save_artifact", {"path": "proof.txt"}),
                ("write_memory", {"content": "The native capability proof succeeded."}),
                ("delegate_task", {"agent_name": "self", "task": "Return the phrase native-child-result."}),
            ]
        elif scenario == "stop":
            calls = [("shell", {
                "command": "printf started > started.txt; sleep 30; printf finished > forbidden.txt",
                "timeout": 45,
            })]
        else:
            calls = []
        for index, (name, arguments) in enumerate(calls):
            if name not in completed:
                return ModelResponse(parts=[
                    ToolCallPart(name, arguments, tool_call_id=f"native-{scenario}-{index}")
                ])
        return ModelResponse(parts=[TextPart("Native capabilities completed.")])

    async def stream(messages, info):
        result = response(messages, info)
        if (
            gates is not None and scenario == "steer"
            and any(isinstance(part, ToolCallPart) and part.tool_call_id == "old-action" for part in result.parts)
        ):
            gates[0].set()
            await gates[1].wait()
        for part in result.parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id
                )}

    return FunctionModel(response, stream_function=stream)
