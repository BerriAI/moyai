"""Generic durable agent execution. Task procedures belong in skills, not here.

Version this workflow/agent name when changing its command sequence. Legacy
SessionWorkflow histories and persisted native harnesses remain replayable.
"""
from dataclasses import dataclass, field
from datetime import timedelta
import json
import base64

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from pydantic_ai import Agent, RunContext
    from pydantic_ai.capabilities import ResolveModelId
    from pydantic_ai.exceptions import RunCancelled
    from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow, TemporalDurability
    from pydantic_ai.messages import BinaryContent, ModelMessage, ModelRequest, TextPart, ToolReturn, ToolReturnPart, UserPromptPart, repair_messages
    from pydantic_ai.models import Model
    from pydantic_ai.toolsets import FunctionToolset
    from pydantic_ai.usage import UsageLimits
    from pydantic_ai_harness.compaction import SummarizingCompaction
    from .pydantic_context import current_runtime


@dataclass
class TurnScope:
    run_id: str
    message_id: int
    segment: int = 0
    actor_id: str | None = None
    model: str = ''
    input_budget: int = 1
    credential_request: str = ''
    credential_generation: int | None = None


@dataclass
class TurnInput:
    scope: TurnScope
    prompt: str
    instructions: str
    messages: list[ModelMessage] = field(default_factory=list)
    max_requests: int | None = None
    applied: list[int] = field(default_factory=list)


@dataclass
class TurnResult:
    result: dict
    messages: list[ModelMessage]
    applied: list[int]


class BrokerModel(Model):
    """Serializable identity only; credentials and provider clients stay in activities."""
    def __init__(self, scope: TurnScope):
        super().__init__(profile={'context_window': scope.input_budget})
        self.scope = scope

    @property
    def model_name(self) -> str:
        return self.scope.model

    @property
    def system(self) -> str:
        return 'moyai'

    async def request(self, messages, model_settings, model_request_parameters):
        return await current_runtime().model_request(self.scope, messages, model_settings, model_request_parameters)


async def resolve_model(ctx, model_id):
    return BrokerModel(ctx.deps) if model_id == 'moyai' else None


# Unknown app writes must never be repeated automatically. Computer operations
# have their own durable identities, but this generic bridge can also send mail,
# create PRs, etc. A lost activity result therefore requires reconciliation.
workspace = FunctionToolset(id='workspace_v1', sequential=True, metadata={
    'temporal': {'retry_policy': RetryPolicy(maximum_attempts=1),
                 'heartbeat_timeout': timedelta(seconds=5),
                 'cancellation_type': ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                 'start_to_close_timeout': timedelta(hours=12)},
})


@workspace.tool(metadata={'temporal': {'retry_policy': RetryPolicy(maximum_attempts=3)}})
async def workspace_tools(ctx: RunContext[TurnScope]) -> str:
    """List authorized workspace tools and their input schemas. Skills load on demand."""
    return json.dumps(await current_runtime().catalog(ctx.deps), ensure_ascii=False)


@workspace.tool
async def workspace_call(ctx: RunContext[TurnScope], name: str, arguments_json: str) -> ToolReturn | dict:
    """Invoke an authorized tool using a JSON object string; discover its schema first."""
    try:
        arguments = json.loads(arguments_json)
        if not isinstance(arguments, dict):
            raise ValueError()
    except (TypeError, ValueError):
        return {'isError': True, 'error': 'arguments_json must be a JSON object. No tool was called.'}
    result = await current_runtime().call_tool(ctx.deps, ctx.tool_call_id, name, arguments)
    value = result.get('result')
    if isinstance(value, dict) and isinstance(value.get('content'), list):
        images = [BinaryContent(data=base64.b64decode(block['data'], validate=True), media_type=block['mimeType'])
                  for block in value['content'] if block.get('type') == 'image']
        if images:
            value = {**value, 'content': [block for block in value['content'] if block.get('type') != 'image']}
            return ToolReturn(return_value={**result, 'result': value}, content=images)
    return result


agent = Agent(
    'moyai', defer_model_check=True,
    name='moyai_pydantic_v1', deps_type=TurnScope, toolsets=[workspace],
    capabilities=[
        ResolveModelId(resolve_model),
        SummarizingCompaction(max_fraction=.65, keep_messages=4, keep_user_messages=True),
        TemporalDurability(activity_config={
            'start_to_close_timeout': timedelta(minutes=15),
            'heartbeat_timeout': timedelta(seconds=5),
            'cancellation_type': ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            'retry_policy': RetryPolicy(maximum_attempts=3),
        }),
    ],
)


async def boundary(scope, applied, elapsed, text=None):
    return await workflow.execute_activity(
        'pydantic_boundary_v1', args=[scope, applied, elapsed, text], result_type=dict,
        start_to_close_timeout=timedelta(minutes=5), retry_policy=RetryPolicy(maximum_attempts=3),
    )


def wait_marker(request):
    # Only the bridge's typed outer receipt carries control, never a string
    # scraped from a tool's untrusted content.
    for part in request.parts:
        if isinstance(part, ToolReturnPart) and isinstance(part.content, dict):
            for key in ('wait_credential', 'wait_group'):
                if part.content.get(key):
                    return {key: part.content[key]}
    return {}


@workflow.defn(name='PydanticTurnV1')
class PydanticTurnWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [agent]

    @workflow.run
    async def run(self, scope: TurnScope) -> TurnResult:
        prepared = await workflow.execute_activity(
            'pydantic_prepare_v1', scope, result_type=TurnInput,
            start_to_close_timeout=timedelta(minutes=5), retry_policy=RetryPolicy(maximum_attempts=3),
        )
        scope = prepared.scope
        started = workflow.now()
        applied = list(prepared.applied)
        messages, prompt = prepared.messages, prepared.prompt
        requests = 0
        while True:
            redirected = {}
            try:
                async with agent.iter(
                    prompt, model='moyai', deps=scope, instructions=prepared.instructions,
                    message_history=messages, usage_limits=UsageLimits(request_limit=prepared.max_requests),
                ) as run:
                    node = run.next_node
                    while not Agent.is_end_node(node):
                        if Agent.is_call_tools_node(node):
                            redirected = await boundary(scope, applied, (workflow.now() - started).total_seconds())
                            if redirected.get('input') or redirected.get('handoff') or redirected.get('reply'):
                                run.cancel()  # Repair the library's cancellation snapshot below.
                                await run.next(node)
                        if Agent.is_model_request_node(node):
                            control = await boundary(scope, applied, (workflow.now() - started).total_seconds())
                            waiting = wait_marker(node.request)
                            # Completed tool returns have not entered history until
                            # this ModelRequestNode runs. Preserve them when yielding.
                            if waiting or control.get('handoff') or requests >= 50:
                                history = [*run.all_messages(), node.request]
                                return TurnResult({
                                    'completed': False, 'continuation': not control.get('handoff', False),
                                    'message': '', **waiting,
                                    **({'steer_message_id': control['steer_message_id']} if control.get('handoff') else {}),
                                }, history, applied)
                            if item := control.get('input'):
                                node.request.parts.append(UserPromptPart(item['content']))
                                applied.append(item['id'])
                            if control.get('reply'):
                                return TurnResult({'completed': True, 'message': control['reply']},
                                                  [*run.all_messages(), node.request], applied)
                            requests += 1
                        previous = node
                        node = await run.next(node)
                        if Agent.is_model_request_node(previous):
                            response = run.all_messages()[-1]
                            text = '\n'.join(p.content for p in response.parts if isinstance(p, TextPart))
                            if text:
                                await workflow.execute_activity(
                                    'pydantic_publish_v1', args=[scope, str(requests), text],
                                    start_to_close_timeout=timedelta(minutes=1),
                                    retry_policy=RetryPolicy(maximum_attempts=3),
                                )
                    messages = run.all_messages()
                    answer = str(run.result.output) if run.result else ''
            except RunCancelled as cancelled:
                messages = repair_messages(cancelled.all_messages())
                if redirected.get('handoff'):
                    return TurnResult({'completed': False, 'message': '',
                        'steer_message_id': redirected['steer_message_id']}, messages, applied)
                if redirected.get('reply'):
                    return TurnResult({'completed': True, 'message': redirected['reply']}, messages, applied)
                if item := redirected.get('input'):
                    prompt = item['content']
                    applied.append(item['id'])
                    continue
                raise
            control = await boundary(scope, applied, (workflow.now() - started).total_seconds(), answer)
            if control.get('handoff'):
                return TurnResult({'completed': False, 'message': '',
                                   'steer_message_id': control['steer_message_id']}, messages, applied)
            if item := control.get('input'):
                prompt = item['content']
                applied.append(item['id'])
                continue
            if control.get('continue'):
                prompt = control['continue']
                if requests >= 50:
                    messages = [*messages, ModelRequest(parts=[UserPromptPart(prompt)])]
                    return TurnResult({'completed': False, 'continuation': True, 'message': ''}, messages, applied)
                continue
            return TurnResult({'completed': True, 'message': control.get('reply') or answer}, messages, applied)
