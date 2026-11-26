"""Read-tool attenuation for attention; the original account grant stays intact."""

from app.connection_attention_models import AttentionReason, ConnectionAttentionView
from pydantic import BaseModel, ConfigDict, Field

from .connection_policy import ConnectionPolicy
from .contracts import Grant

__all__ = ["AttentionReason", "ConnectionAttentionView"]

PERSONAL_OAUTH_KINDS = frozenset({"github", "linear", "notion", "gmail", "google_calendar"})


class ConnectionAttentionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    consent_revision: int | None = Field(default=None, ge=0)
    use_proactively: bool
    readable_tools: list[str] | None = Field(default=None, max_length=200)


class ConnectionAttentionUpdated(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    revision: int
    attention: ConnectionAttentionView


class ConnectionAttentionTools(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    revision: int
    readable_tools: list[str]


def declared_reads(kind: str, policy: ConnectionPolicy) -> frozenset[str] | None:
    """Local definitions only. Dynamic MCP tools must be selected after discovery."""
    if policy.oauth and kind == "github":
        # These reviewed operations are also used by restricted GitHub discovery.
        # Harness's other GitHub definitions are remote, not a static read catalog.
        from .dot_sources import GITHUB_READS

        return GITHUB_READS
    personal_slack = (
        kind == "slack" and policy.product is not None
        and policy.product.get("personal_owner") and policy.product.get("access") == "personal_reads"
    )
    if personal_slack:
        # "me" and as_bot reads require organization authority, never personal consent.
        return frozenset({"search", "channel", "thread"})
    if policy.oauth and kind in PERSONAL_OAUTH_KINDS:
        from app.connectors import TOOLS

        return frozenset(
            name.removeprefix(kind + "_") for name, spec in TOOLS.items() if spec[0] == kind and not spec[1]
        )
    return None


def attention_reads(grant: Grant) -> frozenset[str]:
    """Intersect consent with ordinary authority; never manufacture a broader grant."""
    policy = ConnectionPolicy.model_validate(grant.policy)
    if grant.kind in {"gmail", "google_calendar"} and (not policy.oauth or not grant.read_only):
        return frozenset()
    declared = declared_reads(grant.kind, policy)
    if policy.product is not None:
        if declared is None or not grant.read_only:
            return frozenset()
        selected = declared  # Owner consent is checked separately against this exact canonical ID.
    elif policy.use_proactively:
        selected = policy.attention_tools
        if selected is None and grant.kind in {"gmail", "google_calendar"}:
            selected = policy.allowed_tools  # Historical Google consent predates the separate purpose scope.
    elif grant.read_only:
        # Existing explicitly configured attention definitions retain their consent.
        selected = policy.allowed_tools
    else:
        return frozenset()
    return frozenset(
        name for name in (selected or [])
        if policy.allows(name) and (declared is None or name in declared)
        and not (grant.kind == "litellm" and name.startswith("skills_"))
    )
