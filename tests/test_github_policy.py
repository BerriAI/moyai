import asyncio

import httpx
import pytest

from app.connector_errors import ConnectorError
from app.github_request_policy import CLASSIC_PROTECTION_QUERY, REBASE_REF_MUTATION, REVIEW_THREADS_QUERY, validate_request


@pytest.mark.parametrize("method,path,payload", [
    ("PUT", "/repositories/101/pulls/7/merge", {}),
    ("POST", "/repos/owner/repository/pulls/7/merge", {}),
    ("POST", "/repositories/101/pulls/7/reviews", {"event": "APPROVE"}),
    ("POST", "/repositories/101/pulls/7/reviews/8/events", {"event": "APPROVE"}),
    ("POST", "/repositories/101/pulls/7/reviews", {"event": "approve"}),
    ("PATCH", "/repositories/101/pulls/7", {"auto_merge": True}),
    ("PATCH", "/repositories/101/git/refs/heads/feature", {"sha": "a" * 40, "force": True}),
    ("POST", "/repositories/101/git/refs", {"ref": "refs/pull/7/merge", "sha": "a" * 40}),
    ("POST", "/repositories/101/pulls", {"head": "feature", "base": "main", "draft": True}),
    ("POST", "/repositories/101/merge-queue", {}),
    ("POST", "/repositories/101/merges", {"head": "feature", "base": "main"}),
    ("POST", "/repos/owner/repository/stacks/3/merge", {}),
    ("POST", "/repos/owner/repository/stacks/3/enqueue", {}),
    ("POST", "/repos/owner/repository/stacks/3/unstack", {"merge": True}),
    ("POST", "/graphql", {"query": "mutation{mergePullRequest(input:{pullRequestId:\"P\"}){clientMutationId}}"}),
    ("POST", "/graphql", {"query": "mutation{enablePullRequestAutoMerge(input:{pullRequestId:\"P\"}){clientMutationId}}"}),
    ("POST", "/graphql", {"query": "mutation{enqueuePullRequest(input:{pullRequestId:\"P\"}){clientMutationId}}"}),
    ("POST", "/graphql", {"query": "mutation{submitPullRequestReview(input:{pullRequestReviewId:\"R\",event:APPROVE}){clientMutationId}}"}),
    ("POST", "/graphql", {"query": "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}} mergePullRequest(input:{pullRequestId:$id}){clientMutationId}}", "variables": {"id": "P"}}),
    ("POST", "//attacker.example/pulls", {}),
    ("POST", "/repositories/101/%2e%2e/pulls/7/merge", {}),
])
def test_forbidden_actions_are_rejected_before_provider_dispatch(method: str, path: str, payload: object) -> None:
    with pytest.raises(ConnectorError) as caught:
        validate_request(method, path, payload)
    assert caught.value.dispatched is False
    assert caught.value.rejected is True


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/repositories/101/pulls/7/merge", None),
    ("POST", "/repositories/101/pulls", {"head": "feature", "base": "release", "draft": False}),
    ("PATCH", "/repositories/101/pulls/7", {"base": "release"}),
    ("PATCH", "/repositories/101/pulls/7", {"state": "open"}),
    ("POST", "/repositories/101/pulls/7/reviews", {"event": "REQUEST_CHANGES", "body": "Please add a test."}),
    ("POST", "/repositories/101/pulls/7/reviews", {"event": "COMMENT", "body": "The word APPROVE in prose is not an approval."}),
    ("POST", "/repositories/101/pulls/7/reviews", {"body": "A pending review."}),
    ("POST", "/repositories/101/pulls/7/reviews/8/events", {"event": "COMMENT"}),
    ("POST", "/repositories/101/git/refs", {"ref": "refs/heads/test-base", "sha": "a" * 40}),
    ("PATCH", "/repositories/101/git/refs/heads/feature%2Fsafe", {"sha": "b" * 40, "force": False}),
    ("POST", "/repositories/101/actions/runs/4/rerun", {}),
    ("POST", "/repos/owner/repository/stacks", {"pull_requests": [7, 8]}),
    ("POST", "/repos/owner/repository/stacks/3/add", {"pull_requests": [9]}),
    ("POST", "/repos/owner/repository/stacks/3/unstack", None),
    ("POST", "/app/installations/10/access_tokens", {"repository_ids": [101], "permissions": {"contents": "write"}}),
    ("POST", "/graphql", {"query": "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}", "variables": {"id": "thread"}}),
    ("POST", "/graphql", {"query": "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{id isDraft}}}", "variables": {"id": "pull"}}),
    ("POST", "/graphql", {"query": REVIEW_THREADS_QUERY, "variables": {"owner": "owner", "name": "repository", "number": 7, "cursor": None}}),
])
def test_fixed_nonapproval_workflows_remain_permitted(method: str, path: str, payload: object) -> None:
    validate_request(method, path, payload)


@pytest.mark.parametrize("method,path,payload", [
    ("PUT", "/repositories/101/pulls/7/merge", {}),
    ("POST", "/repositories/101/pulls/7/reviews", {"event": "APPROVE"}),
    ("POST", "/graphql", {"query": "mutation{mergePullRequest(input:{pullRequestId:\"P\"}){clientMutationId}}"}),
])
def test_actual_github_request_rejects_forbidden_effects_before_http(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, payload: object,
) -> None:
    from app.github import GitHub

    dispatched: list[bool] = []

    def client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        dispatched.append(True)
        raise AssertionError("A prohibited operation reached the HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", client)
    github = object.__new__(GitHub)
    with pytest.raises(ConnectorError):
        asyncio.run(github.request(method, path, token="synthetic-private-token", json=payload))
    assert dispatched == []


def test_fixed_classic_protection_query_is_read_only_and_scoped() -> None:
    validate_request("POST", "/graphql", {
        "query": CLASSIC_PROTECTION_QUERY,
        "variables": {"owner": "BerriAI", "name": "moyai", "cursor": None},
    })
    with pytest.raises(ConnectorError):
        validate_request("POST", "/graphql", {
            "query": CLASSIC_PROTECTION_QUERY + " mutation{mergePullRequest(input:{pullRequestId:\"P\"}){clientMutationId}}",
            "variables": {"owner": "BerriAI", "name": "moyai", "cursor": None},
        })


@pytest.mark.parametrize("change", [
    {"name": "refs/pull/7/merge"},
    {"name": "refs/tags/release"},
    {"name": "refs/heads/../main"},
    {"name": "refs/heads/feature?operation=merge"},
    {"beforeOid": "0" * 40},
    {"afterOid": "0" * 40},
    {"beforeOid": ""},
    {"afterOid": "not-a-commit"},
    {"force": True},
    {"refUpdates": [{"name": "refs/heads/main", "afterOid": "b" * 40}]},
])
def test_atomic_rebase_rejects_creation_deletion_and_arbitrary_ref_updates(change: dict[str, object]) -> None:
    variables: dict[str, object] = {
        "repositoryId": "R_repository",
        "name": "refs/heads/feature",
        "beforeOid": "a" * 40,
        "afterOid": "b" * 40,
    }
    with pytest.raises(ConnectorError):
        validate_request("POST", "/graphql", {"query": REBASE_REF_MUTATION, "variables": variables | change})


def test_fixed_rebase_contract_requires_an_atomic_expected_head() -> None:
    validate_request("POST", "/graphql", {
        "query": REBASE_REF_MUTATION,
        "variables": {
            "repositoryId": "R_repository", "name": "refs/heads/feature",
            "beforeOid": "a" * 40, "afterOid": "b" * 40,
        },
    })
    with pytest.raises(ConnectorError):
        validate_request("POST", "/graphql", {
            "query": REBASE_REF_MUTATION.replace("beforeOid:$beforeOid,", ""),
            "variables": {
                "repositoryId": "R_repository", "name": "refs/heads/feature",
                "beforeOid": "a" * 40, "afterOid": "b" * 40,
            },
        })


@pytest.mark.parametrize("path", [
    "/repositories/101/git/ref/heads/feature%23topic",
    "/repositories/101/contents/report%23summary.md",
    "/repositories/101/git/ref/heads/literal%2523name",
])
def test_encoded_delimiters_remain_path_data_in_actual_http_builder(path: str) -> None:
    request = httpx.Request("GET", "https://api.github.com" + path)
    assert request.url.host == "api.github.com"
    assert request.url.raw_path == path.encode()
    assert request.url.query == b""
    assert request.url.fragment == ""
    validate_request("GET", path)


@pytest.mark.parametrize("suffix", ["?operation=merge", "#fragment", "%0a"])
def test_raw_url_delimiters_and_encoded_controls_remain_forbidden(suffix: str) -> None:
    with pytest.raises(ConnectorError):
        validate_request("GET", "/repositories/101/git/ref/heads/feature" + suffix)
