"""Bounded project-scoped GitLab reads, revision-fenced diffs and CI evidence."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import quote

from pydantic_ai.exceptions import ToolFailed

from .gitlab_workflow_types import (
    Artifact,
    Compare,
    File,
    Item,
    Job,
    Log,
    MRRead,
    Page,
    PipelineJobs,
    Pipelines,
    RefRead,
    Search,
    Target,
    Tree,
)
from .product_gitlab_publication import Publications
from .product_gitlab_workflows import Workflows


class Reads:
    def __init__(self, publisher: Publications):
        self.p, self.w = publisher, Workflows(publisher)

    async def page(self, endpoint: str, request: Page, **params) -> dict:
        value = await self.p.request(
            "GET", self.p.prefix + endpoint, params={"page": request.page, "per_page": request.per_page, **params},
        )
        if not isinstance(value, list) or len(value) > request.per_page or any(not isinstance(item, dict) for item in value):
            raise ToolFailed("GitLab returned an invalid bounded page.")
        return {
            "items": value, "page": request.page, "per_page": request.per_page,
            "next_page": request.page + 1 if len(value) == request.per_page else None,
            "coverage": "page_only",
        }

    async def job(self, request: Job) -> dict:
        value = await self.p.request("GET", self.p.prefix + f"/jobs/{request.job_id}")
        if not isinstance(value, dict) or (
            value.get("id") != request.job_id or (value.get("commit") or {}).get("id") != request.sha
            or (value.get("pipeline") or {}).get("project_id") not in {None, self.p.target}
        ):
            raise ToolFailed("The GitLab job ID, project or exact commit changed.")
        return value

    async def call(self, name: str, request: Target) -> dict:
        p = self.p
        await p.check_target(request)
        if name == "repository":
            return (await p.project()).model_dump(mode="json")
        if name == "branches" and isinstance(request, Page):
            return await self.page("/repository/branches", request)
        if isinstance(request, Tree):
            return await self.page("/repository/tree", request, ref=request.ref, path=request.path, recursive=request.recursive)
        if isinstance(request, File):
            value = await p.request("GET", p.prefix + "/repository/files/" + quote(request.path, safe=""), params={"ref": request.ref})
            if not isinstance(value, dict) or value.get("file_path") != request.path or value.get("encoding") != "base64":
                raise ToolFailed("GitLab returned a different file or unsupported encoding.")
            return value
        if name in {"commits", "commit"} and isinstance(request, RefRead):
            if name == "commits":
                return await self.page("/repository/commits", request, ref_name=request.ref)
            value = await p.request("GET", p.prefix + "/repository/commits/" + quote(request.ref, safe=""))
            if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                raise ToolFailed("GitLab did not return a commit identity.")
            return value
        if isinstance(request, Compare):
            value = await p.request("GET", p.prefix + "/repository/compare", params={
                "from": request.base_sha, "to": request.head_sha, "straight": True,
            })
            if not isinstance(value, dict) or (value.get("commit") or {}).get("id") != request.head_sha:
                raise ToolFailed("GitLab returned a different comparison head.")
            diffs = value.get("diffs", [])
            return {**value, "base_sha": request.base_sha, "head_sha": request.head_sha,
                    "diff_coverage": "incomplete" if value.get("compare_timeout") or any(
                        item.get("too_large") or item.get("collapsed") for item in diffs
                    ) else "provider_response_may_be_limited"}
        if isinstance(request, Search):
            return await self.page("/search", request, scope=request.scope, search=request.query,
                                   **({"ref": request.ref} if request.ref else {}))
        if name in {"issues", "merge_requests"} and isinstance(request, Page):
            return await self.page("/" + name, request, state="all", scope="all")
        if name in {"issue", "issue_comments"} and isinstance(request, Item):
            path = f"/issues/{request.number}"
            if name == "issue_comments":
                return await self.page(path + "/notes", request)
            value = await p.request("GET", p.prefix + path)
            if not isinstance(value, dict) or value.get("iid") != request.number or value.get("project_id") != p.target:
                raise ToolFailed("GitLab returned a different issue identity.")
            return value
        if isinstance(request, MRRead):
            mr = await self.w.read_mr(request.number)
            if request.head_sha and request.head_sha != mr.sha:
                raise ToolFailed("The MR head changed between read pages.")
            if request.base_sha and (mr.diff_refs is None or request.base_sha != mr.diff_refs.base_sha):
                raise ToolFailed("The MR diff base changed between read pages.")
            if request.page > 1 and not request.head_sha:
                raise ToolFailed("Supply the first page's head_sha when following MR read pages.")
            if name == "merge_request_diffs" and request.page > 1 and not request.base_sha:
                raise ToolFailed("Supply the first diff page's base_sha as well as head_sha.")
            if name == "merge_request":
                return mr.model_dump(mode="json")
            suffix = {
                "merge_request_diffs": "diffs", "merge_request_comments": "notes",
                "merge_request_discussions": "discussions", "merge_request_commits": "commits",
                "merge_request_pipelines": "pipelines",
            }[name]
            result = await self.page(f"/merge_requests/{mr.iid}/{suffix}", request)
            after = await self.w.read_mr(request.number)
            if after.sha != mr.sha or after.diff_refs != mr.diff_refs or after.target_branch != mr.target_branch:
                raise ToolFailed("The MR changed while reading this page; discard the mixed revision.")
            return {
                **result, "head_sha": mr.sha, "base_sha": mr.diff_refs.base_sha if mr.diff_refs else None,
                **({"diff_coverage": "incomplete" if any(
                    item.get("collapsed") or item.get("too_large") for item in result["items"]
                ) else "page_only_provider_limits_apply"} if suffix == "diffs" else {}),
            }
        if isinstance(request, PipelineJobs):
            value = await p.request("GET", p.prefix + f"/pipelines/{request.pipeline_id}")
            if not isinstance(value, dict) or value.get("id") != request.pipeline_id or (
                value.get("sha") != request.sha or value.get("project_id") != p.target
            ):
                raise ToolFailed("The pipeline ID, project or exact commit changed.")
            return await self.page(f"/pipelines/{request.pipeline_id}/jobs", request, include_retried=True)
        if isinstance(request, Pipelines):
            return await self.page("/pipelines", request, sha=request.sha)
        if isinstance(request, Job):
            job = await self.job(request)
            if isinstance(request, Log):
                raw = await p.request("GET", p.prefix + f"/jobs/{request.job_id}/trace", raw=True)
                if not isinstance(raw, bytes) or len(raw) > 8 * 1024 * 1024:
                    raise ToolFailed("The job log exceeds the supported bounded source size.")
                revision = hashlib.sha256(raw).hexdigest()
                if request.revision and revision != request.revision:
                    raise ToolFailed("The job log changed. Restart reading from its first page.")
                if request.start_line > 1 and not request.revision:
                    raise ToolFailed("Follow log pages with the first page's revision.")
                lines = raw.decode("utf-8", errors="replace").splitlines()
                start, end = request.start_line - 1, request.start_line - 1 + request.max_lines
                return {"job_id": request.job_id, "sha": request.sha, "revision": revision,
                        "lines": lines[start:end], "next_line": end + 1 if end < len(lines) else None}
            if isinstance(request, Artifact):
                if any(part in {"", ".", ".."} for part in request.path.split("/")) or "\\" in request.path:
                    raise ToolFailed("Use an exact relative artifact path.")
                raw = await p.request(
                    "GET", p.prefix + f"/jobs/{request.job_id}/artifacts/" + quote(request.path, safe=""), raw=True,
                )
                if not isinstance(raw, bytes) or len(raw) > 2 * 1024 * 1024:
                    raise ToolFailed("The selected artifact exceeds the 2 MiB inline artifact limit.")
                await self.job(request)
                return {"job_id": request.job_id, "sha": request.sha, "path": request.path,
                        "encoding": "base64", "content": base64.b64encode(raw).decode(),
                        "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
            return {"job_id": request.job_id, "sha": request.sha, "artifacts": job.get("artifacts", []),
                    "artifacts_file": job.get("artifacts_file")}
        raise ToolFailed("Unknown finite GitLab read operation.")
