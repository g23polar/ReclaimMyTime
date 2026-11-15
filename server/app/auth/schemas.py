"""Request/response bodies for the auth routes (contracts/openapi.yaml, `auth` tag)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer


def _utc_z(value: datetime) -> str:
    # NOTES.md: the server always emits RFC 3339 in UTC with `Z`.
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[datetime, PlainSerializer(_utc_z, return_type=str)]


class AppleSignInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_token: str = Field(min_length=1)
    authorization_code: str = Field(min_length=1)
    nonce: str = Field(min_length=16)


class UserOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    email: str | None
    created_at: UtcDateTime


class SessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    created_at: UtcDateTime
    expires_at: UtcDateTime


class AuthSessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=32)
    session: SessionOut
    user: UserOut
    is_new_user: bool
