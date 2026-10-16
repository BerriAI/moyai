"""Self-contained inline skill references in encrypted, backwards-compatible text.

The API authors the display name/icon from the authorized catalog. The marker is
never an HTML link and never contains instructions or credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

REFERENCE = re.compile(r"(?:personal|org|shared|bundled):[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
MARKER = re.compile(r"\[/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})\]\(moyai-skill:([A-Za-z0-9%_.~-]+)\?icon=([A-Za-z0-9%_.~-]*)\)")


@dataclass(frozen=True)
class SkillMention:
    start: int
    end: int
    reference: str
    icon: str

    @property
    def name(self) -> str:
        return self.reference.split(":", 1)[1]


def mentions(text: str) -> list[SkillMention]:
    """Recognize explicit markers outside escaped text and code literals."""
    result = []
    index = 0
    while index < len(text):
        if (index == 0 or text[index - 1] == "\n") and (text.startswith("    ", index) or text[index] == "\t"):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "`":
            end = index + 1
            while end < len(text) and text[end] == "`":
                end += 1
            close = text.find(text[index:end], end)
            index = len(text) if close < 0 else close + end - index
            continue
        if text.startswith("~~~", index) and not text[text.rfind("\n", 0, index) + 1:index].strip():
            close = re.search(r"(?m)^ {0,3}~{3,}[ \t]*(?:\n|$)", text[index + 3:])
            index = len(text) if close is None else index + 3 + close.end()
            continue
        match = MARKER.match(text, index)
        if match:
            reference, icon = unquote(match[2]), unquote(match[3])
            if REFERENCE.fullmatch(reference) and match[1] == reference.split(":", 1)[1]:
                result.append(SkillMention(index, match.end(), reference, safe_icon(icon)))
            index = match.end()
        else:
            index += 1
    return result


def safe_icon(value: str | None) -> str:
    value = value or "auto"
    return value if len(value) <= 32 and all(ord(char) >= 32 for char in value) else "auto"


def marker(reference: str, icon: str | None = None) -> str:
    if not REFERENCE.fullmatch(reference):
        raise ValueError("Invalid skill reference.")
    return f"[/{reference.split(':', 1)[1]}](moyai-skill:{quote(reference, safe='')}?icon={quote(safe_icon(icon), safe='')})"


def display_text(text: str) -> str:
    for mention in reversed(mentions(text)):
        text = text[:mention.start] + f"/{mention.name}" + text[mention.end:]
    return text
