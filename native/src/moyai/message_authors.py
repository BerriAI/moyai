"""Human-readable identity from saved profiles, never from message content."""

from __future__ import annotations

from .models import User


def author_name(author: User | None) -> str | None:
    if author is None:
        return None
    name = author.display_name.strip()
    if author.id.startswith("slack:"):
        slack_id = author.id.rsplit(":", 1)[-1]
        if name in {author.id, slack_id, "Slack " + slack_id}:
            return None
    if name:
        return name
    # Product usernames are opaque internal keys, not public profile names.
    if not author.username.startswith("product:") and not author.id.startswith("slack:"):
        return author.username.strip() or None
    return None
