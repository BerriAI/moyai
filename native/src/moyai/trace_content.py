"""Redact credentials in exported content without erasing diagnostic prose."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_CREDENTIAL_FIELD = re.compile(
    r"(?i)(?:^|[_. -])(?:password|passwd|secret|token|api[_. -]?key|"
    r"(?:private|secret)[_. -]?key|authorization|cookie|credentials?)$"
)
_CREDENTIAL_VALUE = re.compile(
    r"""(?ix)
    (\b(?:authorization|api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|password|(?:client[_-]?)?secret|secret[_-]?key)
    \s*[:=]\s*)(?:"[^"]*"|'[^']*'|(?:Bearer\s+|Basic\s+)?[^"'\s,;&}]+)
    """
)
_AUTH_VALUE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]+)\b")
_URL_PASSWORD = re.compile(r"([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@/\s]+@", re.IGNORECASE)
_DATA_URL = re.compile(r"data:[^\s\"']+")


def sensitive_field(name: str) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return bool(_CREDENTIAL_FIELD.search(separated))


def redact_content(value: Any, secrets: list[str]) -> Any:
    """Keep JSON structure and text; never modify model-visible objects."""
    if isinstance(value, Mapping):
        return {
            key: "[redacted]" if sensitive_field(str(key)) else redact_content(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_content(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    # Parse before replacing so credentials containing JSON escapes are removed.
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        pass
    else:
        if isinstance(parsed, (dict, list)):
            return json.dumps(redact_content(parsed, secrets), ensure_ascii=False)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    value = _CREDENTIAL_VALUE.sub(r"\1[redacted]", value)
    value = _AUTH_VALUE.sub(r"\1 [redacted]", value)
    value = _TOKEN.sub("[redacted]", value)
    value = _URL_PASSWORD.sub(r"\1[redacted]@", value)
    return _DATA_URL.sub("[media omitted]", value)
