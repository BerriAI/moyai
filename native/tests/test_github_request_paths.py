"""Validate URI syntax without rejecting encoded Git ref data."""

import subprocess
from urllib.parse import quote

import httpx
import pytest
from pydantic import TypeAdapter
from pydantic_ai.exceptions import ToolFailed

from moyai.github_request_policy import validate_request
from moyai.github_workflow_types import Branch


@pytest.mark.parametrize("branch", ["fix#123", "feature/fix#123", "literal%23", "percent%name", "café"])
@pytest.mark.parametrize("method", ["GET", "PATCH"])
def test_valid_git_branch_names_remain_encoded_path_data(branch, method):
    subprocess.run(["git", "check-ref-format", "--branch", branch], check=True, capture_output=True)
    assert TypeAdapter(Branch).validate_python(branch) == branch
    prefix = "/repositories/123/git/" + ("ref" if method == "GET" else "refs") + "/heads/"
    path = prefix + quote(branch, safe="")
    validate_request(method, path, {"sha": "a" * 40, "force": False} if method == "PATCH" else None)
    request = httpx.Request(method, "https://api.github.com" + path)
    assert request.url.fragment == "" and request.url.query == b""
    assert request.url.raw_path.decode().endswith(quote(branch, safe=""))


@pytest.mark.parametrize("path", [
    "//other.example/path", "https://other.example/path",
    "/repositories/123/git/ref/heads/branch#fragment",
    "/repositories/123/git/ref/heads/branch?query=true",
    "/repositories/123/%2e%2e/pulls", "/repositories/123/%5cname",
    "/repositories/123/%00name", "/repositories/123/%7fname",
    "/repositories/123/%GG", "/repositories/123/%ff",
])
def test_request_paths_reject_uri_syntax_traversal_and_malformed_encoding(path):
    with pytest.raises(ToolFailed, match="path"):
        validate_request("GET", path)
