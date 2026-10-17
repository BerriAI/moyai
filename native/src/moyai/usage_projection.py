"""Incrementally retain billing fields, without retaining model response content.

Responses' terminal event repeats the complete output. Its size must not decide
whether usage is recorded. Validate the JSON stream while skipping unrelated
values; only the small allowlisted projection is materialized.
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

type Rule = dict[str, Rule] | bool
_DETAILS: dict[str, Rule] = dict.fromkeys((
    "cached_tokens", "cache_write_tokens", "cache_creation_tokens", "reasoning_tokens",
), True)
_USAGE: dict[str, Rule] = {
    **dict.fromkeys((
        "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens", "reasoning_tokens",
        "x_litellm_response_cost", "cost",
    ), True),
    **dict.fromkeys((
        "input_tokens_details", "prompt_tokens_details", "output_tokens_details", "completion_tokens_details",
    ), _DETAILS),
}
_PAYLOAD: dict[str, Rule] = {
    **dict.fromkeys(("id", "model", "created_at", "created", "x_litellm_response_cost"), True),
    "usage": _USAGE,
}
_ROOT: dict[str, Rule] = {**_PAYLOAD, "type": True, "response": _PAYLOAD, "choices": True}
_STRING_SPECIAL = re.compile(r'["\\\x00-\x1f]')
_TOKEN_END = re.compile(r"[\s,\]}:]")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_HEX = frozenset("0123456789abcdefABCDEF")
_SPACE = frozenset(" \t\r\n")


class ProjectionError(ValueError):
    """A fixed diagnostic; never include response data."""


@dataclass
class _Frame:
    kind: str
    rule: Rule
    state: str
    value: dict[str, Any] | list[Any] | None
    key: str | None = None


class UsageProjection:
    def __init__(self, *, max_bytes: int = 256 * 1024, max_depth: int = 64):
        self.max_bytes, self.max_depth = max_bytes, max_depth
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.frames: list[_Frame] = []
        self.done = False
        self.value: Any = None
        self.mode: str | None = None
        self.capture = False
        self.key_token = False
        self.token = ""
        self.escape = 0
        self.retained = 0

    def _rule(self) -> Rule:
        if not self.frames:
            return _ROOT
        frame = self.frames[-1]
        if frame.kind == "object" and isinstance(frame.rule, dict):
            return frame.rule.get(frame.key or "", False)
        return False

    def _append(self, text: str) -> None:
        if not self.capture:
            return
        # A longer object key cannot be one of the allowlisted keys. Skip it
        # rather than letting an unrelated provider key exhaust our budget.
        if self.key_token and len(self.token) + len(text) > 256:
            self.capture, self.token = False, ""
            return
        if not self.key_token:
            self.retained += len(text.encode("utf-8"))
            if self.retained > self.max_bytes:
                raise ProjectionError("oversized_usage_metadata")
        self.token += text

    def _complete(self, value: Any) -> None:
        if not self.frames:
            if self.done:
                raise ProjectionError("invalid_usage_json")
            self.value, self.done = value, True
            return
        frame = self.frames[-1]
        if frame.kind == "object":
            if frame.state != "value":
                raise ProjectionError("invalid_usage_json")
            if isinstance(frame.rule, dict) and frame.key in frame.rule:
                assert isinstance(frame.value, dict)
                frame.value[frame.key] = value
            frame.key, frame.state = None, "comma"
        else:
            if frame.state not in {"first", "value"}:
                raise ProjectionError("invalid_usage_json")
            # Only array emptiness is relevant (Chat Completions choices).
            # Retain one sentinel for a nonempty array, never its contents.
            if isinstance(frame.value, list) and not frame.value:
                frame.value.append(None)
            frame.state = "comma"

    def _start_string(self, *, key: bool) -> None:
        self.mode, self.key_token, self.escape = "string", key, 0
        self.capture = (
            isinstance(self.frames[-1].rule, dict) if key else bool(self._rule())
        )
        self.token = ""
        self._append('"')

    def _string(self, text: str, offset: int) -> int:
        while offset < len(text):
            if self.escape:
                char = text[offset]
                if self.escape == 1:
                    if char == "u":
                        self.escape = 5
                    elif char in '"\\/bfnrt':
                        self.escape = 0
                    else:
                        raise ProjectionError("invalid_usage_json")
                else:
                    if char not in _HEX:
                        raise ProjectionError("invalid_usage_json")
                    self.escape -= 1
                    if self.escape == 1:
                        self.escape = 0
                self._append(char)
                offset += 1
                continue
            match = _STRING_SPECIAL.search(text, offset)
            end = match.start() if match else len(text)
            self._append(text[offset:end])
            offset = end
            if match is None:
                return offset
            char = text[offset]
            if char == "\\":
                self._append(char)
                self.escape = 1
                offset += 1
            elif char == '"':
                self._append(char)
                value = json.loads(self.token) if self.capture else None
                if self.key_token:
                    frame = self.frames[-1]
                    frame.key, frame.state = value, "colon"
                else:
                    self._complete(value)
                self.mode, self.token = None, ""
                return offset + 1
            else:
                raise ProjectionError("invalid_usage_json")
        return offset

    def _finish_token(self) -> None:
        raw = self.token
        if raw not in {"true", "false", "null"} and _NUMBER.fullmatch(raw) is None:
            raise ProjectionError("invalid_usage_json")
        value = json.loads(raw, parse_float=Decimal) if self.capture else None
        self._complete(value)
        self.mode, self.token = None, ""

    def _text(self, text: str) -> None:
        offset = 0
        while offset < len(text):
            if self.mode == "string":
                offset = self._string(text, offset)
                continue
            if self.mode == "token":
                match = _TOKEN_END.search(text, offset)
                end = match.start() if match else len(text)
                self.token += text[offset:end]
                if len(self.token) > 256:
                    raise ProjectionError("oversized_usage_metadata")
                offset = end
                if match is None:
                    return
                self._finish_token()
                continue
            char = text[offset]
            if char in _SPACE:
                offset += 1
                continue
            frame = self.frames[-1] if self.frames else None
            if frame and frame.kind == "object" and frame.state in {"first", "key"}:
                if char == "}" and frame.state == "first":
                    self.frames.pop()
                    self._complete(frame.value)
                elif char == '"':
                    self._start_string(key=True)
                else:
                    raise ProjectionError("invalid_usage_json")
            elif frame and frame.state == "colon":
                if char != ":":
                    raise ProjectionError("invalid_usage_json")
                frame.state = "value"
            elif frame and frame.state == "comma":
                if char == ("}" if frame.kind == "object" else "]"):
                    self.frames.pop()
                    self._complete(frame.value)
                elif char == ",":
                    frame.state = "key" if frame.kind == "object" else "value"
                else:
                    raise ProjectionError("invalid_usage_json")
            elif frame and frame.kind == "array" and frame.state == "first" and char == "]":
                self.frames.pop()
                self._complete(frame.value)
            else:
                if self.done and frame is None:
                    raise ProjectionError("invalid_usage_json")
                if char in "{[":
                    if len(self.frames) >= self.max_depth:
                        raise ProjectionError("usage_nesting_limit")
                    rule = self._rule()
                    kind = "object" if char == "{" else "array"
                    value = ({} if kind == "object" else []) if rule else None
                    self.frames.append(_Frame(kind, rule, "first", value))
                elif char == '"':
                    self._start_string(key=False)
                elif char in "-0123456789tfn":
                    self.mode, self.token, self.capture = "token", char, bool(self._rule())
                else:
                    raise ProjectionError("invalid_usage_json")
            offset += 1

    def feed(self, data: bytes) -> None:
        self._text(self.decoder.decode(data))

    def finish(self) -> dict[str, Any]:
        self._text(self.decoder.decode(b"", final=True))
        if self.mode == "token":
            self._finish_token()
        if self.mode is not None or self.frames or not self.done or not isinstance(self.value, dict):
            raise ProjectionError("invalid_usage_json")
        return self.value
