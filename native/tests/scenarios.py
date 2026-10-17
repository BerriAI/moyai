"""Controlled model responses for SDK integration tests, never live-provider proof."""

from __future__ import annotations

import asyncio
import json

from pydantic_ai.messages import (
    BinaryContent,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel


def integration_model(scenario: str, *, gates: tuple[asyncio.Event, asyncio.Event] | None = None) -> FunctionModel:
    def response(messages, info):
        if scenario == "image":
            images = [
                value for message in messages for part in message.parts
                if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
                for value in part.content if isinstance(value, BinaryContent)
            ]
            assert len(images) == 1 and images[0].media_type == "image/png"
            assert images[0].data.startswith(b"\x89PNG\r\n\x1a\n")
            return ModelResponse(parts=[TextPart("PNG image received natively.")])
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
        if scenario == "history_retry":
            return ModelResponse(parts=[ToolCallPart(
                "retry_target", {}, tool_call_id=f"retry-{len(messages)}",
            )])
        if scenario == "history":
            if "NEW GUIDANCE" in prompts:
                return ModelResponse(parts=[TextPart("Guidance preserved after handoff.")])
            if "load_capability" not in completed:
                loader = next(tool for tool in info.function_tools if tool.name == "load_capability")
                key = next(iter(loader.parameters_json_schema["properties"]))
                return ModelResponse(parts=[ToolCallPart(
                    "load_capability", {key: "coding"}, tool_call_id="history-load",
                )])
            count = sum(
                isinstance(part, ToolReturnPart) and part.tool_name == "shell"
                for message in messages for part in message.parts
            )
            if count < 6:
                return ModelResponse(parts=[ToolCallPart(
                    "shell", {"command": f"printf '{count + 1}\\n' >> handoff-ledger.txt"},
                    tool_call_id=f"history-append-{count + 1}",
                )])
            return ModelResponse(parts=[TextPart("Six effects completed exactly once.")])
        if scenario == "swarm":
            offered = {tool.name for tool in info.function_tools}
            assert {"swarm_board", "swarm_claim_lead", "swarm_publish", "swarm_assign"} <= offered
            assert "delegate_task" not in offered
            board = next((
                part.content for message in messages for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == "swarm_board"
            ), None)
            if board is None:
                calls = [("swarm_board", {})]
            elif not board["you"]["coordinator"]:
                calls = [
                    ("swarm_claim_lead", {"plan": "Coordinate the independent checks."}),
                    ("swarm_publish", {"message": "Observed and verified the native test result."}),
                ]
            else:
                calls = []
        elif scenario in {"question", "optional_question"}:
            calls = [("ask_user_question", {
                "questions": [{
                    "header": "Choice", "question": "Which output should I create?",
                    "options": [{"label": "Text"}, {"label": "JSON"}],
                }],
                **({"required": False} if scenario == "optional_question" else {}),
            })]
        elif scenario == "stream":
            calls = [("delegate_task", {"agent_name": "self", "task": "Return the phrase native-child-result."})]
        elif scenario in {"tools", "workspace"}:
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
            if scenario == "workspace":
                calls = [(name, arguments) for name, arguments in calls
                         if name in {"load_capability", "write_file", "save_artifact", "delegate_task"}]
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
