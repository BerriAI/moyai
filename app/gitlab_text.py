"""Shared GitLab write-text policy."""


def safe_text(value: str) -> str:
    if any(line.lstrip().startswith("/") for line in value.splitlines()) or "\x00" in value:
        raise ValueError("GitLab quick-action lines are unavailable in descriptions and review notes.")
    return value
