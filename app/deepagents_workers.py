"""Separate model/control capacity from tools that can wait on computers or I/O."""
import asyncio
from contextlib import AsyncExitStack


class AgentWorkers:
    def __init__(self, *workers):
        self.workers = workers

    @property
    def is_running(self):
        return all(worker.is_running for worker in self.workers)

    async def run(self):
        tasks = [asyncio.create_task(worker.run()) for worker in self.workers]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await self.shutdown()
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result

    async def shutdown(self):
        await asyncio.gather(*(worker.shutdown() for worker in self.workers if worker.is_running))

    async def __aenter__(self):
        self.stack = AsyncExitStack()
        try:
            for worker in self.workers:
                await self.stack.enter_async_context(worker)
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        return await self.stack.__aexit__(*exc)


def agent_workers(client, runtime, task_queue, *, max_concurrent_activities=100,
                  max_cached_workflows=1000, graceful_shutdown_timeout=None):
    from temporalio.worker import Worker
    from temporalio.deepagents import DeepAgentsPlugin
    from .deepagents_context import RuntimeInterceptor, deepagents_plugin
    from .deepagents_workflow import DeepAgentsTurnWorkflow
    from .native_agent_workflow import NativeAgentWorkflow
    from .native_agent_activities import NativeActivities
    native = NativeActivities(runtime)
    plugins = [] if any(isinstance(p, DeepAgentsPlugin) for p in client.config().get('plugins', [])) else [deepagents_plugin()]
    options = dict(plugins=plugins, interceptors=[RuntimeInterceptor(runtime)],
                   max_concurrent_activities=max_concurrent_activities,
                   max_cached_workflows=max_cached_workflows)
    if graceful_shutdown_timeout is not None:
        options['graceful_shutdown_timeout'] = graceful_shutdown_timeout
    return AgentWorkers(
        Worker(client, task_queue=task_queue, workflows=[DeepAgentsTurnWorkflow, NativeAgentWorkflow], **options),
        Worker(client, task_queue=task_queue + '-native-model',
               activities=[native.model, native.catalog, native.checkpoint], **options),
        Worker(client, task_queue=task_queue + '-native-tools', activities=[native.tool], **options),
        Worker(client, task_queue=task_queue + '-tools',
               activities=[runtime.computer] if hasattr(runtime, 'computer') else [], **options),
        Worker(client, task_queue=task_queue + '-control',
               activities=[runtime.prepare, runtime.boundary, runtime.publish,
                           native.admission, native.prepare, native.report], **options))
