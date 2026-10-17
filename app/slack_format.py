"""Slack formatting delegates provider rules to AgentChat."""
import re

from agentchat.channels.slack.rendering import markdown_text, split_text

from .file_links import file_link

ID = re.compile(r"^[A-Z][A-Z0-9]{7,30}$")
STAMP = re.compile(r"^\d{10,16}\.\d{1,9}$")
ASIDE_PREFIXES = ("!aside", "(aside)")


def slack_text(text, mentions=frozenset(), *, public_url='', run_id=''):
    """Keep application-owned file authorization outside provider formatting."""
    return markdown_text(
        text, mentions=mentions,
        resolve_link=lambda reference: file_link(public_url, run_id, reference),
    )


def split_reply(text, limit=2600):
    return split_text(text, limit=limit, trim_boundary_newlines=True) or ['No response text was returned.']
