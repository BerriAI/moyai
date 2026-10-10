"""Bind request services to an activity, never to serialized workflow dependencies."""
from contextvars import ContextVar
from temporalio.worker import ActivityInboundInterceptor, Interceptor

_runtime = ContextVar('moyai_pydantic_runtime')


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
