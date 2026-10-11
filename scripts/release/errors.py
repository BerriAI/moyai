"""Shared error identity for imported controllers and the ``python -m`` CLI."""


class ReleaseError(RuntimeError):
    """Only fixed, credential-free messages may be used here."""
