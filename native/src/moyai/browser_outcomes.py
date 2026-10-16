"""Trusted browser dispatch evidence, separate from untrusted page content."""
from __future__ import annotations

from typing import Any, Literal

from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolReturn

BROWSER_WRITES = frozenset({"browser_open", "browser_action"})
BROWSER_OUTCOME = "moyai_browser_dispatch_v1"
BrowserOutcome = Literal["completed", "not_dispatched", "unknown"]


class BrowserNotDispatched(ToolFailed):
    """The trusted worker refused before invoking any guest operation."""


class BrowserOutcomeUnknown(ToolFailed):
    """A prior attempt may have dispatched; never automatically repeat it."""


def browser_result(outcome: BrowserOutcome, value: Any) -> ToolReturn:
    # Guest/page dictionaries stay inside return_value. Only the trusted tool
    # wrapper creates metadata consumed by EffectReceipts.
    return ToolReturn(return_value=value, metadata={BROWSER_OUTCOME: outcome})
