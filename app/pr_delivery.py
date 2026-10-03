"""Select confirmed PRs and explicitly referenced captures for a Slack handoff."""
import hashlib
import html
import re
import sqlite3

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

from . import captures
from .config import Settings

TOKENS = re.compile(r'[^\s<>()`"\[\]]+')


class PullRequest(BaseModel):
    number: int = Field(gt=0, strict=True)
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
    url: str
    title: str = 'Pull request'

    @model_validator(mode='after')
    def canonical_url(self):
        if self.url.casefold() != f'https://github.com/{self.repository}/pull/{self.number}'.casefold():
            raise ValueError('PR URL does not match the publication receipt')
        return self


class Capture(BaseModel):
    name: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}\.(png|webm)$')
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def select_prs(conn: sqlite3.Connection, run_id: str, answer: str) -> list[PullRequest]:
    # A URL selects an existing receipt; it cannot establish a publication.
    urls = {url.rstrip('.,;!?') for url in re.findall(r'https://[^\s<>`"\)\]\|]+', answer)}
    result = []
    seen = set()
    rows = conn.execute('''SELECT p.result FROM github_publications p JOIN runs r ON r.id=p.run_id
        WHERE (r.id=? OR r.parent_run_id=?) AND p.result IS NOT NULL ORDER BY p.created_at''', (run_id, run_id))
    for row in rows:
        try:
            pr = PullRequest.model_validate_json(row['result'])
        except ValidationError:
            continue
        if pr.url in urls and pr.url not in seen:
            seen.add(pr.url)
            result.append(pr)
    return result[:10]


def select_captures(settings: Settings, run_id: str, answer: str) -> list[Capture]:
    tokens = TOKENS.findall(answer)
    names = [match[1] for token in tokens if (match := re.fullmatch(
        r'(?:/workspace/)?moyai-captures/([A-Za-z0-9_-]{1,100}\.(?:png|webm))', token))]
    result = []
    kinds = set()
    for name in names:
        kind = name.rsplit('.', 1)[-1]
        if kind in kinds:
            continue
        try:
            raw, _ = captures.read(captures.directory(settings, run_id) / name)
        except (OSError, HTTPException):
            continue
        result.append(Capture(name=name, sha256=hashlib.sha256(raw).hexdigest()))
        kinds.add(kind)
    return result


def link_captures(settings: Settings, run_id: str, answer: str, selected: list[Capture]) -> str:
    links = {}
    for capture in selected:
        url = f'{settings.public_url.rstrip("/")}/api/runs/{run_id}/computer/captures/{capture.name}'
        links['moyai-captures/' + capture.name] = url
        links['/workspace/moyai-captures/' + capture.name] = url
    return TOKENS.sub(lambda match: links.get(match[0], match[0]), answer)


def attachment(pr: PullRequest, public_url: str, run_id: str) -> dict:
    text = html.escape(f'#{pr.number} · {pr.title[:200]}', quote=False)
    return {'color': '#5B3FD1', 'fallback': f'{pr.repository} #{pr.number}: {pr.url}', 'blocks': [
        {'type': 'context', 'elements': [
            {'type': 'image', 'image_url': public_url.rstrip('/') + '/static/litellm-train.png', 'alt_text': 'LiteLLM train'},
            {'type': 'plain_text', 'text': 'LiteLLM · Moyai Devin'}]},
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': f'*<{pr.url}|{text}>*', 'verbatim': True}},
        {'type': 'context', 'elements': [{'type': 'plain_text', 'text': pr.repository + ' · Pull request'}]},
        {'type': 'actions', 'elements': [
            {'type': 'button', 'text': {'type': 'plain_text', 'text': 'View PR'}, 'url': pr.url, 'style': 'primary'},
            {'type': 'button', 'text': {'type': 'plain_text', 'text': 'Review changes'}, 'url': pr.url + '/files'},
            {'type': 'button', 'text': {'type': 'plain_text', 'text': 'Open Moyai session'},
             'url': public_url.rstrip('/') + '/#run=' + run_id}]}]}
