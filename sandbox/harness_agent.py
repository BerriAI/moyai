"""Shared agent lifecycle, independent of model protocol and runtime SDK."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import threading

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
    def __init__(self, history, prompt):
        self.messages = [*history, {'role': 'user', 'content': prompt}]
        self.pending = set()
        self.lock = threading.RLock()

    def tool_started(self, call_id, name, arguments):
        with self.lock:
            self.pending.add(call_id)
            self.messages.append({'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': call_id, 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps(arguments)}}]})

    def tool_finished(self, call_id, output):
        with self.lock:
            self.pending.discard(call_id)
            self.messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': output})

    def finish(self, text):
        with self.lock:
            self.messages.append({'role': 'assistant', 'content': text})

    def prompt(self, current, history, *, cwd):
        return history_prompt(current, history, cwd=cwd)
