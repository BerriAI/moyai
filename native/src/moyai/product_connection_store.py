"""Finite read-only canonical Store seam on the caller's SQLAlchemy transaction."""

import re
from typing import Any

from sqlalchemy import text

QUERIES = {
    "SELECT * FROM connections WHERE provider=?": "connections",
    "SELECT enabled,read_only,checked_at,check_status FROM connection_policies WHERE provider=?": "connection_policies",
    "SELECT * FROM users WHERE id=?": "users",
    "SELECT id,email FROM users WHERE kind IN ('google','cloudflare')": "users",
    "SELECT * FROM users WHERE id IN (?,?)": "users",
    "SELECT * FROM personal_slack_grants WHERE owner_id=?": "personal_slack_grants",
    "SELECT encrypted FROM connections WHERE provider='github'": "connections",
    "SELECT encrypted FROM connections WHERE provider='slack'": "connections",
    "SELECT encrypted FROM github_app WHERE id=1": "github_app",
    "SELECT MAX(id) AS revision FROM connection_audit WHERE provider=?": "connection_audit",
}


class ConnectionSnapshot:
    schema_updates = False

    def __init__(self, session: Any, schema: str | None):
        if schema is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("Invalid canonical schema.")
        self.session, self.schema = session, schema

    def rows(self, query: str, parameters=(), *, connection=None):
        if connection is not None or query not in QUERIES:
            raise RuntimeError("The canonical grant query changed; review its transaction adapter.")
        table = QUERIES[query]
        qualified = f'"{self.schema}"."{table}"' if self.schema else f'"{table}"'
        query = query.replace("FROM " + table, "FROM " + qualified, 1)
        pieces = query.split("?")
        if len(parameters) != len(pieces) - 1:
            raise ValueError("The canonical grant query parameters changed.")
        statement = pieces[0] + "".join(f":p{i}" + part for i, part in enumerate(pieces[1:]))
        return [dict(row) for row in self.session.execute(
            text(statement), {f"p{i}": value for i, value in enumerate(parameters)},
        ).mappings()]
