"""Public response contracts used by the generated frontend client."""
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .user_preferences import ChatPreferences


class SessionIdentity(BaseModel):
    model_config = ConfigDict(extra='allow')
    email: str
    name: str | None = None


class WorkspaceSession(BaseModel):
    authenticated: bool
    csrf: str
    local: bool
    role: Literal['admin', 'member'] | None
    identity: SessionIdentity | None
    user_id: str | None
    preferences: ChatPreferences
    google_enabled: bool
    google_domains: list[str]
    password_enabled: bool


class SessionTitleConfiguration(BaseModel):
    model: str
    enabled: bool
    gateway_configured: bool


class SavedTitleModel(BaseModel):
    model: str


class MemorySource(BaseModel):
    type: Literal['manual', 'chat', 'observation']
    run_id: str = ''
    quote: str = ''
    scope: str = ''
    evidence: str = ''


class MemoryNote(BaseModel):
    id: str
    key: str
    title: str
    content: str
    kind: Literal['preference', 'feedback', 'project', 'reference']
    repo_url: str
    revision: int
    created_at: str = ''
    updated_at: str
    expires_at: str = ''
    source: MemorySource


class MemoryPreferences(BaseModel):
    enabled: bool
    auto_save: bool
    revision: int


class MemoryReviewResult(BaseModel):
    status: Literal['completed', 'failed']
    updated_at: str
    saved_count: int


class MemoryReview(BaseModel):
    enabled: bool
    configured: bool
    pending: int
    latest: MemoryReviewResult | None


class MemoryLibrary(BaseModel):
    memories: list[MemoryNote]
    preferences: MemoryPreferences
    limit: int
    review: MemoryReview | None = None


class SavedMemory(BaseModel):
    saved: bool
    id: str
    revision: int


class ForgottenMemory(BaseModel):
    forgotten: bool
