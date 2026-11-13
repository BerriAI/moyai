"""Credential-free repository identities. Parsing a URL never authorizes access."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit

RepositoryProvider = Literal["github", "gitlab"]


def canonical_repository_url(value: str) -> str:
    """Keep the full origin and nested project path; accept no URL instructions.

    Loopback HTTP is syntactically valid for explicitly configured local GitLab
    installations. Callers must compare the origin with a current selected grant
    before reading credentials or performing any network I/O.
    """
    if value == "":
        return ""
    try:
        if (
            not isinstance(value, str) or len(value) > 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(c in value for c in "\\%?#")
        ):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        if not host or parsed.username is not None or parsed.password is not None:
            raise ValueError
        port = parsed.port
        if port == 0 or host.endswith(".") or parsed.netloc.endswith(":"):
            raise ValueError
        path = parsed.path.removeprefix("/").removesuffix("/").removesuffix(".git")
        parts = path.split("/")
        if (
            len(parts) < 2 or len(path) > 1024
            or any(
                not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                or part in {".", ".."} or len(part) > 255
                for part in parts
            )
        ):
            raise ValueError
        if host == "github.com":
            # Preserve the existing GitHub URL contract, including no port.
            if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or len(parts) != 2:
                raise ValueError
            return "https://github.com/" + path
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            if not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", host,
            ):
                raise ValueError
        loopback = host == "localhost" or bool(address and address.is_loopback)
        if parsed.scheme == "http":
            if not loopback:
                raise ValueError
        elif parsed.scheme != "https" or loopback or (address and not address.is_global):
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        return f"{parsed.scheme}://{authority}/{path}"
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Use a credential-free GitHub or GitLab repository URL without query, fragment, or traversal.") from None


def repository_provider(value: str) -> RepositoryProvider:
    canonical = canonical_repository_url(value)
    return "github" if not canonical or urlsplit(canonical).hostname == "github.com" else "gitlab"


def repository_origin(value: str) -> str:
    canonical = canonical_repository_url(value)
    if not canonical:
        return ""
    parsed = urlsplit(canonical)
    return f"{parsed.scheme}://{parsed.netloc}"


def canonical_origin(value: str) -> str:
    """Normalize a configured origin without permitting a repository/API path."""
    if not isinstance(value, str) or urlsplit(value).path not in {"", "/"}:
        raise ValueError("Use an origin without a path.")
    return repository_origin(value.rstrip("/") + "/moyai/identity")
