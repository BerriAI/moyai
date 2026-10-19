"""Response models for the routes defined in api.py.

These are the source of the generated frontend types (native/web/generated/api).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .general_preferences import GeneralPreferences


class MeUser(BaseModel):
    id: str
    name: str
    role: str
    individual: bool


class Me(BaseModel):
    user: MeUser
    csrf: str
    models: list[str]
    default_model: str
    preferences: GeneralPreferences
    product_mode: Literal["existing", "standalone"]
