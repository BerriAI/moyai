"""Start one shared task with typed participants and a durable host backend.

``AgentSwarm(backend).start(task=goal, agents=[Agent(), Agent()])`` lets members
organize their work. Explicit assignments remain available through ``delegate``.
"""
from .client import AgentSwarm, GroupResult, Swarm, SwarmBackend, SwarmRun, WorkerRef, WorkerResult
from .contracts import Task
from .planning import PlannedTask, Role, Runtime, plan_team
from .types import Agent, Harness

__all__ = ['Agent', 'AgentSwarm', 'GroupResult', 'Harness', 'PlannedTask', 'Role', 'Runtime', 'Swarm', 'SwarmBackend',
           'SwarmRun', 'Task', 'WorkerRef', 'WorkerResult', 'plan_team']
