"""Request/response models for the HTTP API.

All request models reject unknown fields (``extra="forbid"``) so a client can
never smuggle extra data into a handler, and every string is length-capped.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Deliberately conservative (no dependency on ``email-validator``): we only
#: need "looks like an address, at most 255 chars" for an account identifier.
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^\s.@]{1,63}\.[^@\s.]{1,63}$")


class StrictAPIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterIn(StrictAPIModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        candidate = value.strip().lower()
        if not EMAIL_RE.match(candidate):
            raise ValueError("Enter a valid email address.")
        return candidate


class LoginIn(StrictAPIModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return value.strip().lower()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict[str, Any]


class CorrectionIn(StrictAPIModel):
    field_path: str = Field(min_length=1, max_length=120)
    #: Required (may be ``null`` to clear an optional value).
    value: Any = Field(...)

    @field_validator("field_path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        path = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*", path):
            raise ValueError("field_path must look like 'total' or 'line_items.0.quantity'.")
        return path


class ApproveIn(StrictAPIModel):
    note: str | None = Field(default=None, max_length=300)


class RejectIn(StrictAPIModel):
    reason: str = Field(min_length=1, max_length=300)


PageQuery = int  # documented alias; actual clamping happens in the repository


Scope = Literal["mine", "all"]
ExportFormat = Literal["csv", "json"]
