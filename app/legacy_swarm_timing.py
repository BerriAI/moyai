"""Conservative, host-independent parsing of explicit owner timing commands.

Call this only for a complete message from the authenticated swarm owner, never
for agent output, board posts, quoted task material, or tool results. Recognizing
an intent grants no authority by itself; the host applies it to its owned swarm.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
import re


MIN_DURATION_SECONDS = 60
MAX_DURATION_SECONDS = 30 * 24 * 60 * 60


def validate_swarm_budget(seconds: int) -> int:
    """Validate an owner-selected budget: zero means until explicitly stopped."""
    if type(seconds) is not int or (
        seconds != 0 and not MIN_DURATION_SECONDS <= seconds <= MAX_DURATION_SECONDS
    ):
        raise ValueError('A swarm duration must be zero, or between one minute and 30 days.')
    return seconds


class SwarmTimeIntentKind(StrEnum):
    SET = 'set'
    EXTEND = 'extend'
    CONTINUE = 'continue'
    UNLIMITED = 'unlimited'


@dataclass(frozen=True, slots=True)
class SwarmTimeIntent:
    """A requested duration, added duration, or duration-free continuation.

    SET and EXTEND require seconds. CONTINUE leaves renewal policy to the host;
    UNLIMITED explicitly requests running without an owner-set deadline.
    """

    kind: SwarmTimeIntentKind
    seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SwarmTimeIntentKind):
            raise ValueError('The timing intent must use SwarmTimeIntentKind.')
        if self.kind in (SwarmTimeIntentKind.SET, SwarmTimeIntentKind.EXTEND):
            if type(self.seconds) is not int or not (
                MIN_DURATION_SECONDS <= self.seconds <= MAX_DURATION_SECONDS
            ):
                raise ValueError('A finite duration must be between one minute and 30 days.')
        elif self.seconds is not None:
            raise ValueError('Continuation and unlimited intents do not have a duration.')


_SMALL_NUMBERS = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19,
}
_TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
         'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90}
_UNIT_SECONDS = {'minute': 60, 'min': 60, 'hour': 3600, 'hr': 3600, 'day': 86400}
_ACTION = r'(?:run|work|go|continue|keep going|keep working|continue working)'
_DURATION = re.compile(r'(?P<quantity>[a-z0-9. -]+?)\s+(?P<unit>minutes?|mins?|hours?|hrs?|days?)')
_COMMANDS = (
    (SwarmTimeIntentKind.EXTEND, re.compile(r'(?:add|extend by) (?P<duration>.+)')),
    (SwarmTimeIntentKind.EXTEND, re.compile(_ACTION + r' (?:for )?another (?P<duration>.+)')),
    (SwarmTimeIntentKind.EXTEND, re.compile(_ACTION + r' (?:for )?(?P<quantity>.+) more (?P<unit>minutes?|mins?|hours?|hrs?|days?)')),
    (SwarmTimeIntentKind.SET, re.compile(_ACTION + r' for (?P<duration>.+)')),
)
_CONTINUATION = {'continue', 'keep going', 'keep working', 'continue working', 'resume', 'go on'}
_UNLIMITED = re.compile(
    r'(?:no time limit|remove (?:the )?time limit|'
    + _ACTION + r' (?:forever|indefinitely|without a time limit|until i stop you))'
)
_POLITE_PREFIX = re.compile(r'(?:(?:please|pls) )?(?:(?:can|could|would) you )?(?:(?:please|pls) )?')
_POLITE_SUFFIX = re.compile(r',? (?:please|pls)$')
_HOLD_COMMAND = re.compile(
    r'(?:(?:pause|stop)(?: now| working| the swarm)?|'
    r'(?:stay|remain) (?:paused|stopped)|'
    r'keep (?:it |the swarm )?(?:paused|stopped)|'
    r"(?:do not|don't|dont) (?:continue|resume|restart|run|work|keep going|keep working|go on)"
    r'(?: (?:the swarm|yet|again))?|no need to (?:continue|resume))'
)


def _under_hundred(words: list[str]) -> int | None:
    if len(words) == 1:
        return _SMALL_NUMBERS.get(words[0], _TENS.get(words[0]))
    if len(words) == 2 and words[0] in _TENS and words[1] in _SMALL_NUMBERS:
        ones = _SMALL_NUMBERS[words[1]]
        if 1 <= ones <= 9:
            return _TENS[words[0]] + ones
    return None


def _whole_number(text: str) -> int | None:
    words = text.replace('-', ' ').split()
    if len(words) >= 2 and words[1] == 'hundred' and words[0] in _SMALL_NUMBERS:
        hundreds = _SMALL_NUMBERS[words[0]]
        if not 1 <= hundreds <= 9:
            return None
        rest = words[2:]
        if not rest:
            return hundreds * 100
        if rest[0] == 'and':
            rest = rest[1:]
        remainder = _under_hundred(rest)
        if remainder is not None and remainder > 0:
            return hundreds * 100 + remainder
        return None
    return _under_hundred(words)


def _quantity(text: str) -> Decimal | None:
    if re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)', text):
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    if text in ('a', 'an'):
        return Decimal(1)
    if text in ('half', 'half a', 'half an'):
        return Decimal('0.5')
    half = text.endswith(' and a half')
    whole = _whole_number(text.removesuffix(' and a half'))
    if whole is None:
        return None
    return Decimal(whole) + (Decimal('0.5') if half else 0)


def _duration_seconds(text: str) -> int | None:
    match = _DURATION.fullmatch(text)
    if match is None:
        return None
    quantity = _quantity(match['quantity'])
    if quantity is None:
        return None
    with localcontext() as context:
        context.prec = len(text) + 10
        seconds = quantity * _UNIT_SECONDS[match['unit'].removesuffix('s')]
    if not MIN_DURATION_SECONDS <= seconds <= MAX_DURATION_SECONDS:
        return None
    # Reject fractional seconds instead of silently changing the requested time.
    if seconds != seconds.to_integral_value():
        return None
    return int(seconds)


def _owner_command(message: str) -> str | None:
    if not isinstance(message, str) or len(message) > 512:
        return None
    command = message.strip().casefold().replace('’', "'")
    if len(command.splitlines()) > 1:
        return None
    command = ' '.join(command.split()).rstrip('.!?').strip()
    command = _POLITE_PREFIX.sub('', command, count=1)
    return _POLITE_SUFFIX.sub('', command)


def is_swarm_hold_command(message: str) -> bool:
    """Whether a complete human message explicitly asks to remain inactive.

    Hosts must not interpret these messages as implicit permission to resume a
    paused, stopped or expired mission. This helper does not itself stop work.
    """
    command = _owner_command(message)
    return command is not None and _HOLD_COMMAND.fullmatch(command) is not None


def parse_swarm_time_intent(message: str) -> SwarmTimeIntent | None:
    """Recognize an entire explicit timing command; return None when uncertain.

    Examples: ``run for four hours``, ``add 2 days``, ``keep going for 3 more
    hours``, ``continue``, and ``run until I stop you``. Embedded requests,
    quoted text, negations, and combined commands are deliberately not parsed.
    """
    command = _owner_command(message)
    if command is None:
        return None
    if command in _CONTINUATION:
        return SwarmTimeIntent(SwarmTimeIntentKind.CONTINUE)
    if _UNLIMITED.fullmatch(command):
        return SwarmTimeIntent(SwarmTimeIntentKind.UNLIMITED)
    for kind, pattern in _COMMANDS:
        match = pattern.fullmatch(command)
        if match is None:
            continue
        groups = match.groupdict()
        duration = groups.get('duration') or f"{groups['quantity']} {groups['unit']}"
        seconds = _duration_seconds(duration)
        if seconds is not None:
            return SwarmTimeIntent(kind, seconds)
    return None
