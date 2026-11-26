"""Compatibility imports for the shared personal connection attention lifecycle."""

from .connection_attention import configure, status

GOOGLE_KINDS = frozenset({"gmail", "google_calendar"})

__all__ = ["GOOGLE_KINDS", "configure", "status"]
