"""Shared agent lifecycle, independent of model protocol and runtime SDK."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import threading
from uuid import uuid4

try:
    from .history_reference import history_prompt
except ImportError:
    from history_reference import history_prompt


@dataclass(frozen=True)
class HarnessContext:
    spec: dict
    relay: object
    config: dict
    activity: object
    step: object
    cwd: str


class HarnessAgent(ABC):
    """The only interface the workspace lifecycle needs from a harness."""
    @abstractmethod
    def validate(self): ...

    @abstractmethod
    def run_conversation(self, prompt, *, conversation_history, system_message): ...

    @abstractmethod
    def interrupt(self): ...

    @abstractmethod
    def close(self): ...


class TurnJournal:
    """Save public runtime events, not intercepted inference request payloads."""
    def __init__(self, history, prompt, context_store=None):
        self.context_store = context_store
        self.call_namespace = uuid4().hex
        self.messages = [*history]
        self.pending = set()
        self.lock = threading.RLock()
        self.append({'role': 'user', 'content': prompt})

    def append(self, message):
        if self.context_store is not None:
            # Fresh SDK invocations can reuse native IDs. Scope only saved IDs
            # so a new result cannot settle an older call with unknown outcome.
            saved = dict(message)
            if message.get('tool_calls'):
                saved['tool_calls'] = [{**call, 'id': self.call_namespace + ':' + call['id']}
                                       for call in message['tool_calls']]
            if message.get('role') == 'tool':
                saved['tool_call_id'] = self.call_namespace + ':' + message['tool_call_id']
            self.context_store.append(saved)
        self.messages.append(message)

    def tool_started(self, call_id, name, arguments):
        with self.lock:
            self.pending.add(call_id)
            self.append({'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': call_id, 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps(arguments)}}]})

    def tool_finished(self, call_id, output):
        with self.lock:
            self.append({'role': 'tool', 'tool_call_id': call_id, 'content': output})
            self.pending.discard(call_id)

    def finish(self, text):
        with self.lock:
            self.append({'role': 'assistant', 'content': text})

    def prompt(self, current, history, *, cwd):
        if self.context_store is not None:
            return '\n'.join(m['content'] for m in history) + '\n\nCURRENT REQUEST:\n' + current
        return history_prompt(current, history, cwd=cwd)
