"""A closed mutation vocabulary for the native GitHub publication adapter."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_ai.exceptions import ToolFailed

from .github_workflow_types import SHA, branch_name

REBASE = """mutation($repositoryId:ID!,$name:GitRefname!,$beforeOid:GitObjectID!,$afterOid:GitObjectID!){
  updateRefs(input:{repositoryId:$repositoryId,refUpdates:[{name:$name,beforeOid:$beforeOid,afterOid:$afterOid,force:true}]}){
    clientMutationId}}"""
ADVANCE = REBASE.replace("force:true", "force:false")
DRAFT = """mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){
  pullRequest{number isDraft url}}}"""
PROTECTION = """query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){databaseId branchProtectionRules(first:100,after:$cursor){
    nodes{pattern} pageInfo{hasNextPage endCursor}}}}"""

MUTATIONS = (
    ("POST", r"/git/blobs", frozenset({"content", "encoding"})),
    ("POST", r"/git/trees", frozenset({"base_tree", "tree"})),
    ("POST", r"/git/commits", frozenset({"message", "tree", "parents"})),
    ("POST", r"/git/refs", frozenset({"ref", "sha"})),
    ("PATCH", r"/git/refs/heads/[^?#]+", frozenset({"sha", "force"})),
    ("POST", r"/pulls", frozenset({"title", "body", "head", "base", "draft", "maintainer_can_modify"})),
    ("PATCH", r"/pulls/[1-9][0-9]*", frozenset({"state", "base"})),
    ("POST", r"/pulls/[1-9][0-9]*/reviews", frozenset({"body", "event", "commit_id", "comments"})),
    ("POST", r"/pulls/[1-9][0-9]*/reviews/[1-9][0-9]*/events", frozenset({"body", "event"})),
    ("PUT", r"/pulls/[1-9][0-9]*/reviews/[1-9][0-9]*", frozenset({"body"})),
    ("DELETE", r"/pulls/[1-9][0-9]*/reviews/[1-9][0-9]*", frozenset()),
    ("POST", r"/stacks", frozenset({"pull_requests"})),
    ("POST", r"/stacks/[1-9][0-9]*/add", frozenset({"pull_requests"})),
)


class RefUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repositoryId: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=1035)
    beforeOid: SHA
    afterOid: SHA

    @field_validator("name")
    @classmethod
    def head_ref(cls, value: str) -> str:
        if not value.startswith("refs/heads/"):
            raise ValueError("Only branch heads can be rebased.")
        branch_name(value.removeprefix("refs/heads/"))
        return value


class NodeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=256)


class ProtectionVariables(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    owner: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    cursor: str | None = Field(max_length=1024)


def validate_request(method: str, path: str, payload: object = None) -> None:
    decoded = unquote(path)
    if (
        not path.startswith("/") or path.startswith("//")
        or any(character in decoded for character in "?#\\")
        or any(ord(character) < 32 for character in decoded)
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise ToolFailed("Invalid GitHub request path.")
    if method == "GET":
        return
    if not isinstance(payload, Mapping):
        raise ToolFailed("GitHub mutations require a validated object.")
    if (
        method == "POST" and path == "/graphql" and set(payload) == {"query", "variables"}
        and isinstance(payload["query"], str) and payload["query"] in {REBASE, ADVANCE, DRAFT, PROTECTION}
    ):
        schema = {REBASE: RefUpdate, ADVANCE: RefUpdate, DRAFT: NodeIdentity,
                  PROTECTION: ProtectionVariables}[payload["query"]]
        try:
            schema.model_validate(payload["variables"])
        except (ValidationError, ValueError, TypeError):
            raise ToolFailed("The GitHub operation variables are not permitted.") from None
        return
    for verb, suffix, fields in MUTATIONS:
        if verb != method or not re.fullmatch(r"/(?:repositories/[1-9][0-9]*|repos/[\w.-]+/[\w.-]+)" + suffix, path):
            continue
        if set(payload) - fields:
            break
        if "event" in payload and payload["event"] not in {"COMMENT", "REQUEST_CHANGES"}:
            break
        if suffix.endswith("/events") and payload.get("event") not in {"COMMENT", "REQUEST_CHANGES"}:
            break
        if suffix == "/pulls" and payload.get("draft") is not False:
            break
        if "state" in payload and payload["state"] not in {"open", "closed"}:
            break
        if "force" in payload and payload["force"] is not False:
            break
        if suffix == "/git/refs":
            ref = payload.get("ref")
            if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
                break
            try:
                branch_name(ref.removeprefix("refs/heads/"))
            except ValueError:
                break
        return
    raise ToolFailed("GitHub approval, merge, auto-merge, queue actions and arbitrary mutations are unavailable.")
