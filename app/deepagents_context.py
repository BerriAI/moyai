"""Bind request services to an activity, never to serialized workflow dependencies."""
from contextvars import ContextVar
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from temporalio.deepagents import DeepAgentsPlugin
from temporalio.worker import ActivityInboundInterceptor, Interceptor

_runtime = ContextVar('moyai_deepagents_runtime')


def current_runtime():
    return _runtime.get()


class RuntimeInterceptor(Interceptor):
    def __init__(self, runtime):
        self.runtime = runtime

    def intercept_activity(self, next):
        runtime = self.runtime

        class BoundActivity(ActivityInboundInterceptor):
            async def execute_activity(self, input):
                token = _runtime.set(runtime)
                try:
                    return await self.next.execute_activity(input)
                finally:
                    _runtime.reset(token)

        return BoundActivity(next)


# The plugin resolves this worker-side adapter. Scope is injected by trusted
# middleware, encrypted with activity inputs, and never exposed in tool schemas.


class BrokerModel(BaseChatModel):
    @property
    def _llm_type(self):
        return 'moyai-broker'

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)

    def _generate(self, *args, **kwargs):
        raise NotImplementedError('Moyai uses async model activities.')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from .deepagents_workflow import TurnScope
        scope = TurnScope(**kwargs.pop('moyai_scope'))
        tools = kwargs.pop('tools', [])
        message = await current_runtime().model_request(scope, messages, kwargs, tools)
        return ChatResult(generations=[ChatGeneration(message=message)])


def model_provider(name):
    if name != 'moyai:broker-v1':
        raise ValueError('Unknown durable model adapter')
    return BrokerModel()


def deepagents_plugin():
    return DeepAgentsPlugin(model_provider=model_provider)
