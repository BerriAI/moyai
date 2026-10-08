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
    def accept_input(self, item):
        inputs = getattr(self, 'inputs', None)
        if inputs is not None:
            return inputs.accept(item)
        # Hermes owns its native inbox; other runtimes reject input until their
        # public journal is ready instead of acknowledging an unowned message.
        return None if callable(getattr(self, 'redirect', None)) else False

    @abstractmethod
    def validate(self): ...

    @abstractmethod
    def run_conversation(self, prompt, *, conversation_history, system_message): ...

    @abstractmethod
    def interrupt(self): ...

    @abstractmethod
    def close(self): ...


class HarnessInputs:
    """Journaled corrections owned by one live SDK conversation."""
    def __init__(self, journal):
        self.journal = journal
        self.lock = threading.Lock()
        self.accepted = set()
        self.items = []
        self.closed = False

    def accept(self, item):
        if (not isinstance(item, dict) or type(item.get('id')) is not int or item['id'] <= 0
                or not isinstance(item.get('content'), str)):
            return False
        with self.lock:
            if self.closed:
                return False
            if item['id'] in self.accepted:
                return True
            text = '[User correction to the current task]\n' + item['content']
            try:
                with self.journal.lock:
                    self.journal.append({'role': 'user', 'content': text})
            except Exception:
                return False  # Persistence must succeed before a delivery receipt.
            self.items.append(text)
            self.accepted.add(item['id'])
            return True

    def take(self):
        with self.lock:
            items, self.items = self.items, []
            return items

    @property
    def pending(self):
        with self.lock:
            return bool(self.items)

    def close_if_empty(self):
        with self.lock:
            if self.items:
                return False
            self.closed = True
            return True

    def close(self):
        with self.lock:
            self.closed = True


class TurnJournal:
    """Save public runtime events, not intercepted inference request payloads."""
    def __init__(self, history, prompt, context_store=None):
        self.context_store = context_store
        self.call_namespace = uuid4().hex
        self.messages = [*history]
        self.pending = set()
        self.completed_tools = 0
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
            if call_id in self.pending:
                self.completed_tools += 1
            self.pending.discard(call_id)

    def finish(self, text):
        with self.lock:
            self.append({'role': 'assistant', 'content': text})

    def prompt(self, current, history, *, cwd):
        if self.context_store is not None:
            return '\n'.join(m['content'] for m in history) + '\n\nCURRENT REQUEST:\n' + current
        return history_prompt(current, history, cwd=cwd)
