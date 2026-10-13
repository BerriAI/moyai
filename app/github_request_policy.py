from collections.abc import Mapping
import re
from typing import Final
from urllib.parse import unquote

from agent.github_refs import validate_branch_name
from .connector_errors import ConnectorError


REPOSITORY: Final[str] = r"/(?:repositories/[1-9][0-9]*|repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
GRAPHQL_MUTATIONS: Final[frozenset[str]] = frozenset(
    "".join(document.split())
    for document in (
        "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}",
        "mutation($id:ID!){unresolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}",
        "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{id isDraft}}}",
        "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{number isDraft url}}}",
        "mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){pullRequest{id isDraft}}}",
        "mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){pullRequest{number isDraft url}}}",
    )
)
REVIEW_THREADS_QUERY: Final[str] = "".join("""
    query($owner:String!,$name:String!,$number:Int!,$cursor:String){
      repository(owner:$owner,name:$name){databaseId pullRequest(number:$number){reviewThreads(first:30,after:$cursor){
        nodes{id isResolved path line comments(first:30){nodes{databaseId body url} pageInfo{hasNextPage endCursor}}}
        pageInfo{hasNextPage endCursor}}}}}
""".split())
CLASSIC_PROTECTION_QUERY: Final[str] = """query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){databaseId
    branchProtectionRules(first:100,after:$cursor){
      nodes{id pattern blocksCreations} totalCount
      pageInfo{hasNextPage endCursor}}}}"""
REBASE_REF_MUTATION: Final[str] = """mutation($repositoryId:ID!,$name:GitRefname!,$beforeOid:GitObjectID!,$afterOid:GitObjectID!){
  updateRefs(input:{repositoryId:$repositoryId,refUpdates:[{name:$name,beforeOid:$beforeOid,afterOid:$afterOid,force:true}]}){
    clientMutationId}}"""
REST_MUTATIONS: Final[tuple[tuple[str, str, frozenset[str]], ...]] = (
    ("POST", r"/git/blobs", frozenset({"content", "encoding"})),
    ("POST", r"/git/trees", frozenset({"base_tree", "tree"})),
    ("POST", r"/git/commits", frozenset({"message", "tree", "parents"})),
    ("POST", r"/git/refs", frozenset({"ref", "sha"})),
    ("PATCH", r"/git/refs/heads/[^?#]+", frozenset({"sha", "force"})),
    ("POST", r"/pulls", frozenset({"title", "body", "head", "base", "draft", "maintainer_can_modify"})),
    ("PATCH", r"/pulls/[1-9][0-9]*", frozenset({"title", "body", "base", "state", "maintainer_can_modify"})),
    ("POST", r"/issues", frozenset({"title", "body", "labels", "assignees", "milestone"})),
    ("PATCH", r"/issues/[1-9][0-9]*", frozenset({"title", "body", "labels", "assignees", "milestone", "state"})),
    ("POST", r"/issues/[1-9][0-9]*/comments", frozenset({"body"})),
    ("PATCH", r"/(?:issues|pulls)/comments/[1-9][0-9]*", frozenset({"body"})),
    ("POST", r"/pulls/[1-9][0-9]*/comments/[1-9][0-9]*/replies", frozenset({"body"})),
    ("POST", r"/pulls/[1-9][0-9]*/requested_reviewers", frozenset({"reviewers", "team_reviewers"})),
    ("DELETE", r"/pulls/[1-9][0-9]*/requested_reviewers", frozenset({"reviewers", "team_reviewers"})),
    ("POST", r"/pulls/[1-9][0-9]*/reviews", frozenset({"body", "event", "commit_id", "comments"})),
    ("POST", r"/pulls/[1-9][0-9]*/reviews/[1-9][0-9]*/events", frozenset({"body", "event"})),
    ("PUT", r"/pulls/[1-9][0-9]*/reviews/[1-9][0-9]*", frozenset({"body"})),
    ("DELETE", r"/pulls/[1-9][0-9]*/reviews/[1-9][0-9]*", frozenset()),
    ("POST", r"/actions/runs/[1-9][0-9]*/(?:rerun|rerun-failed-jobs|cancel)", frozenset()),
    ("POST", r"/actions/jobs/[1-9][0-9]*/rerun", frozenset()),
    ("PUT", r"/rulesets/[1-9][0-9]*", frozenset({"rules"})),
    ("POST", r"/stacks", frozenset({"pull_requests"})),
    ("POST", r"/stacks/[1-9][0-9]*/add", frozenset({"pull_requests"})),
    ("POST", r"/stacks/[1-9][0-9]*/unstack", frozenset()),
)


def denied() -> ConnectorError:
    return ConnectorError(
        "This GitHub operation is not permitted. PR approval, merge, auto-merge and merge-queue actions are unavailable.",
        code="operation_not_permitted", dispatched=False, rejected=True,
    )


def object_payload(payload: object) -> Mapping[str, object]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
        raise denied()
    return {key: value for key, value in payload.items()}


def validate_graphql(payload: Mapping[str, object]) -> None:
    if set(payload) - {"query", "variables"}:
        raise denied()
    document = payload.get("query")
    variables = payload.get("variables", {})
    if not isinstance(document, str) or not isinstance(variables, Mapping):
        raise denied()
    normalized = "".join(document.split())
    if normalized == "".join(CLASSIC_PROTECTION_QUERY.split()):
        if (set(variables) != {"owner", "name", "cursor"}
                or any(not isinstance(variables[field], str)
                       or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", variables[field])
                       for field in ("owner", "name"))
                or (variables["cursor"] is not None
                    and (not isinstance(variables["cursor"], str) or len(variables["cursor"]) > 1024))):
            raise denied()
        return
    if normalized == "".join(REBASE_REF_MUTATION.split()):
        if (set(variables) != {"repositoryId", "name", "beforeOid", "afterOid"}
                or not isinstance(variables["repositoryId"], str)
                or not 1 <= len(variables["repositoryId"]) <= 256
                or not isinstance(variables["name"], str)
                or not variables["name"].startswith("refs/heads/")):
            raise denied()
        try:
            validate_branch_name(variables["name"].removeprefix("refs/heads/"))
        except ValueError:
            raise denied() from None
        for field in ("beforeOid", "afterOid"):
            value = variables[field]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
                raise denied()
        return
    if normalized in GRAPHQL_MUTATIONS:
        if set(variables) != {"id"} or not isinstance(variables["id"], str):
            raise denied()
        return
    if normalized == REVIEW_THREADS_QUERY:
        if set(variables) == {"owner", "name", "number", "cursor"}:
            return
    raise denied()


def validate_request(method: str, path: str, payload: object = None) -> None:
    decoded = unquote(path)
    if (not path.startswith("/") or path.startswith("//")
            or any(character in path for character in "?#\\") or "\\" in decoded
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
            or any(part in {".", ".."} for part in decoded.split("/"))):
        raise denied()
    if method == "GET":
        return
    data = object_payload(payload)
    if method == "POST" and path == "/graphql":
        validate_graphql(data)
        return
    if method == "POST" and re.fullmatch(r"/app/installations/[1-9][0-9]*/access_tokens", path):
        if set(data) <= {"repository_ids", "permissions"}:
            return
        raise denied()
    if method == "POST" and re.fullmatch(r"/app-manifests/[A-Za-z0-9_-]+/conversions", path) and not data:
        return
    for allowed_method, suffix, fields in REST_MUTATIONS:
        if method != allowed_method or not re.fullmatch(REPOSITORY + suffix, path):
            continue
        if set(data) - fields:
            raise denied()
        event = data.get("event")
        if "event" in fields and event is not None and event not in ("COMMENT", "REQUEST_CHANGES"):
            raise denied()
        if suffix.endswith("/events") and event not in ("COMMENT", "REQUEST_CHANGES"):
            raise denied()
        if suffix == r"/pulls" and data.get("draft") is not False:
            raise denied()
        if "state" in data and data["state"] not in ("open", "closed"):
            raise denied()
        if suffix == r"/git/refs":
            ref = data.get("ref")
            if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
                raise denied()
        if suffix.startswith(r"/git/refs/heads/") and data.get("force", False) is not False:
            raise denied()
        return
    raise denied()
