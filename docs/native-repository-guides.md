# Native repository orientation

The native GitHub startup context includes bounded guides from the existing
repository context index. It reads only repositories selected by the admitted
turn's installation grants. Personal OAuth grants do not inherit that shared
index. Current grant and installation authority are checked before and after
reading cached source.

Each guide carries the repository, default branch, immutable commit, indexing
time, and coverage information. At most three guides are included, each within
the existing 6,000-byte guide budget. They provide directory counts and excerpts
from repository orientation documents. This is not semantic search or complete
codebase coverage. The model must read source and verify commands against the
working revision before editing.

The native product broker starts and drains the existing index refresh service
under the canonical broker-maintenance owner. It uses the existing
`REPOSITORY_CONTEXT_ENABLED` and `REPOSITORY_CONTEXT_REFRESH_SECONDS` settings.
No new indexing service or provider credentials are introduced.

A cold, disabled, rotated, renamed, corrupt, or oversized cache contributes no
guide. The connected repository names and ordinary GitHub source tools remain
available. A cached default-branch snapshot may be older than the requested
branch; its text is explicitly marked as untrusted reference data.
