"""Cooperative machine renewal, requested only between Hermes tool rounds."""
import time
import threading
from contextlib import contextmanager


class RotationDeadline:
    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.seconds = seconds
        self.deadline = None
        self.requested = False

    def step(self, agent):
        # Image/agent startup is not work. Always allow the first round before
        # considering a checkpoint, even when initialization was unusually slow.
        if self.seconds and self.deadline is None:
            self.deadline = self.clock() + self.seconds
            return
        # Hermes invokes step_callback before the next request with no tools
        # in flight. Never interrupt a running tool to rotate the machine.
        if not self.requested and self.deadline is not None and self.clock() >= self.deadline:
            agent.interrupt()
            self.requested = True

    def can_continue(self, result):
        if not self.requested or not result.get('interrupted') or result.get('failed'):
            return False
        messages = result.get('messages')
        if not isinstance(messages, list) or not messages:
            return False
        pending = set()
        for message in messages:
            for call in message.get('tool_calls') or []:
                pending.add(call['id'])
            if message.get('role') == 'tool':
                pending.discard(message.get('tool_call_id'))
        return not pending


class AgentWait(RotationDeadline):
    """Pause between complete tool rounds after a trusted broker delegation."""
    def __init__(self, relay):
        super().__init__(0)
        self.relay = relay
        self.group = ''
        self.credential = ''

    def step(self, agent):
        if getattr(self.relay,'wait_credential','') and not self.requested:
            self.credential = self.relay.wait_credential
            agent.interrupt()
            self.requested = True
        elif self.relay.wait_group and not self.requested:
            self.group = self.relay.wait_group
            agent.interrupt()
            self.requested = True


class AgentSteer(RotationDeadline):
    """Interrupt model generation or a complete tool round; never a running tool."""
    def __init__(self, relay):
        super().__init__(0)
        self.relay = relay
        self.message_id = None
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.model_pending = 0
        self.closed = False
        self.thread = None
        self.notify = lambda: None

    def listen(self, agent, notify=None):
        self.notify = notify or self.notify
        self.thread = threading.Thread(target=self._listen, args=(agent,), daemon=True)
        self.thread.start()

    @contextmanager
    def model_wait(self):
        # End this interval BEFORE the response reaches Hermes. Holding the
        # same lock through control/interrupt prevents a late monitor callback
        # from cancelling tools that have already started from that response.
        with self.changed:
            self.model_pending += 1
            self.changed.notify_all()
        try:
            yield
        finally:
            with self.changed:
                self.model_pending -= 1
                self.changed.notify_all()

    def _listen(self, agent):
        with self.changed:
            while not self.closed and not self.requested:
                if self.model_pending:
                    self.step(agent)
                self.changed.wait(timeout=1)

    def close(self):
        with self.changed:
            self.closed = True
            self.changed.notify_all()
        if self.thread:
            self.thread.join(timeout=6)

    def step(self, agent):
        with self.lock:
            if not self.requested and not self.closed:
                target = self.relay.control().get('steer_message_id')
                if isinstance(target, int) and not isinstance(target, bool) and target > 0:
                    self.message_id = target
                    self.requested = True
                    agent.interrupt()
                    self.notify()
