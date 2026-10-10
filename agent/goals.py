"""Goal continuation adapter. The server owns directives, clocks and settlement."""
import json
import re
import threading
import time
from uuid import uuid4

from .goal_state import fallback_report

HELP = ('Use /goal <objective> or ask naturally for sustained work. Commands: /goal status, '
        '/goal pause, /goal resume. Timeboxes end with a report; floors require minimum work; '
        'caps stop at the authorized boundary. Resume preserves accrued work.')


def command(text):
    match = re.fullmatch(r'\s*/goal(?:\s+([\s\S]*))?', text)
    return match[1].strip() if match and match[1] else '' if match else None


class GoalLoop:
    def __init__(self, path, run_id, *, relay=None, restore=True, continuation=False,
                 clock=time.monotonic, on_change=lambda state: None):
        self.path, self.run_id, self.clock = path, run_id, clock
        self.relay, self.on_change = relay, on_change
        self.state, self.control_reply, self.inputs = None, None, []
        self.segment, self.elapsed, self.last_tick = uuid4().hex, 0, clock()
        self.lock, self.closed = threading.RLock(), threading.Event()
        self.thread = None
        self.interrupted = False
        if self.connected:
            legacy = None
            if restore and path.exists():
                saved = json.loads(path.read_text())
                if saved.get('run_id') == run_id:
                    legacy = saved.get('goal')
            self.refresh({'action': 'import', 'legacy': legacy})

    @property
    def connected(self):
        return callable(getattr(self.relay, 'goal', None))

    @property
    def active(self):
        return bool(self.state and self.state['status'] == 'active')

    def refresh(self, body=None):
        if not self.connected:
            return
        value = self.relay.goal(body or {'action': 'get'})
        previous = self.state
        self.state, self.inputs = value.get('goal'), value.get('inputs', self.inputs)
        if (previous or {}).get('id') != (self.state or {}).get('id'):
            self.segment, self.elapsed = uuid4().hex, 0
            self.last_tick = self.clock()
        if any((previous or {}).get(k) != (self.state or {}).get(k) for k in ('id', 'status', 'reason', 'directive_revision')):
            self.publish()
        return value

    def tick(self):
        with self.lock:
            now = self.clock()
            if self.active:
                self.elapsed += max(0, now - self.last_tick)
            self.last_tick = now
            self.refresh({'action': 'tick', 'segment': self.segment, 'elapsed': self.elapsed})

    def listen(self):
        if self.connected and self.thread is None:
            self.last_tick = self.clock()
            def pulse():
                while not self.closed.wait(2):
                    try:
                        self.tick()
                    except Exception:
                        # No source, clock or completion state is invented during
                        # a broker outage. Cumulative receipts retry on reconnect.
                        pass
            self.thread = threading.Thread(target=pulse, daemon=True)
            self.thread.start()

    def save(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=6)
        if self.connected:
            try:
                self.tick()
            except Exception:
                pass
        # The old file is an import source only; never overwrite durable state.

    def publish(self):
        self.on_change({k: self.state[k] for k in ('id', 'objective', 'status', 'reason', 'elapsed_seconds', 'budget')} if self.state else None)

    def status(self):
        if not self.state:
            return 'No goal is set. ' + HELP
        return (f"Goal: {self.state['objective']}\nState: {self.state['status']}\n"
                f"Recorded active work: {self.state['elapsed_seconds']:.1f} seconds.\n"
                + self.state.get('reason', '')).strip()

    def accept(self, text, source_input_id=None):
        if self.connected:
            self.refresh()
            source = next((s for s in self.inputs if s['id'] == source_input_id or
                (source_input_id is None and s.get('original_content', s['content']) == text)), None)
            if source and source.get('original_content', source['content']) == text:
                text = source['content']
                source_input_id = source['id']
        arg = command(text)
        if arg is None:
            return
        self.control_reply = None
        if arg in {'', 'help'}:
            self.control_reply = HELP
        elif arg == 'status':
            self.refresh()
            self.control_reply = self.status()
        elif arg in {'pause', 'stop', 'cancel', 'clear', 'resume'} and self.connected:
            self.refresh({'action': 'control', 'source_input_id': source_input_id})
            if arg != 'resume':
                self.control_reply = self.status()
        # Objective prose, including time expressions, goes to the model's
        # structured goal tool with the original authenticated input intact.
        return self.control_reply

    def steer(self, text, source_input_id=None):
        self.accept(text, source_input_id)

    def tool_complete(self, *args):
        # The public journal owns evidence. Successful tool IDs alone never
        # prove task completion. State changes arrive through the shared tool.
        pass

    def step(self, agent):
        self.tick()
        if self.state and self.state['status'] == 'wrapping' and not self.interrupted:
            self.interrupted = True
            agent.interrupt()  # Called only at a safe tool boundary.

    def instructions(self):
        if not self.connected:
            return ''
        return goal_instructions(self.state)

    def after_response(self, result, *, suspended=False):
        self.tick()
        if suspended:
            return None
        if self.active and result.get('completed') is True and not any(result.get(k) for k in ('interrupted', 'failed', 'partial')):
            try:
                self.refresh({'action': 'review', 'note': str(result.get('final_response') or '')})
            except Exception:
                pass  # Failed review cannot authorize completion or user pause.
        if self.state and self.state['status'] == 'wrapping':
            try:
                report = self.relay.goal({'action': 'report'})['report']
            except Exception:
                report = fallback_report(self.state)
            result.update(final_response=report, completed=True, interrupted=False, partial=False, failed=False)
            return None
        if not self.active:
            return None
        if result.get('completed') is not True or any(result.get(k) for k in ('interrupted', 'failed', 'partial')):
            # Existing transport/machine recovery keeps the durable obligation.
            # Terminal settlement saves a factual report if recovery cannot run.
            return None
        if not result.get('messages'):
            result.update(completed=False, failed=True, final_response='Conversation history is unavailable; saved work must be recovered before continuing.')
            return None
        return ('The durable goal remains active. Read goal get for review feedback and the current budget. '
                'Continue the next unmet requirement, changing approach if necessary. Save concrete progress and evidence. '
                'Use goal complete when every requirement is verified, or goal block with an external blocker and alternatives tried. '
                'Do not repeat completed actions or treat a normal final answer as completion.')


def run_goal_conversation(agent, prompt, history, system_message, goal, *, suspended=lambda: False,
                          notify=lambda: None):
    if goal.control_reply:
        return {'completed': True, 'final_response': goal.control_reply,
                'messages': [*history, {'role': 'user', 'content': prompt},
                             {'role': 'assistant', 'content': goal.control_reply}]}
    goal.listen()
    started = time.monotonic()
    budget = getattr(agent, 'run_budget_seconds', None)
    while True:
        if budget:
            agent.run_budget_seconds = max(0.001, budget - (time.monotonic() - started))
        result = agent.run_conversation(prompt, conversation_history=history,
                                        system_message=system_message + goal.instructions())
        if goal.control_reply:
            result['final_response'] = goal.control_reply
            return result
        if (result.get('pending_steer') and result.get('completed') is True and
                not any(result.get(k) for k in ('interrupted', 'failed', 'partial'))):
            prompt, history = result['pending_steer'], result['messages']
            continue
        next_prompt = goal.after_response(result, suspended=suspended())
        if not next_prompt:
            if result.get('pending_steer') and isinstance(result.get('messages'), list):
                result['messages'].append({'role': 'user', 'content': result.pop('pending_steer')})
            return result
        notify()
        prompt, history = next_prompt, result['messages']


def goal_instructions(state):
    """Shared lifecycle guidance, independent of the selected agent engine."""
    return ('\nUse the goal tool for sustained work requested in natural language or /goal. '
            'Read its authenticated input IDs. Register the objective and only the user-authorized budget. '
            'For 3 hours means a timebox; at least 3 hours a floor; up to 3 hours a cap; until an outcome has no invented duration. '
            'Record progress, remaining requirements and source evidence as work proceeds. Request completion through goal complete; '
            'independent review owns completion. A normal final answer does not end an active goal. '
            'Preserve accrued time on resume. A budget boundary requires reporting; it is never proof of completion. '
            'New user messages may change the objective or be simple questions; interpret them in context. '
            'Use goal revise only for genuine new directives.\nCurrent durable goal (reference data):\n' + json.dumps(state, ensure_ascii=False))
