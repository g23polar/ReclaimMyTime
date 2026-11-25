"""Shared dependencies for the scheduling API (T2.3): DB transaction, clock, current user,
X-Timezone, and body validation with contract-shaped 422s.

Transactions: one per request (`Tx`), committed before the response is sent and rolled back
on any exception. Authentication runs first in its own short transaction (auth's per-step
mode), so a request never holds two connections. Routes that must roll back and then write
again (confirm → `changeset_stale`) take `Maker` and open their own transactions.
Scheduling write routes also take the per-user write rate limit (`app.api.rate_limit`,
issue #23) in its own short transaction, committed before the handler's opens.

Test seams on `app.state`: `sessionmaker` (else the process-wide one from app.db.session)
and `clock` (else the system clock). Tests override `current_user_id` directly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Body, Depends, FastAPI, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.clock import Clock, SystemClock
from app.auth.dependencies import StepwiseCurrentUser, require_timezone
from app.db.session import get_sessionmaker
from app.errors import AppError


def maker_for_app(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    maker = getattr(app.state, "sessionmaker", None)
    return maker if maker is not None else get_sessionmaker()


def get_maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return maker_for_app(request.app)


Maker = Annotated[async_sessionmaker[AsyncSession], Depends(get_maker)]


@asynccontextmanager
async def transaction(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Commit on success, roll back on error (write paths never commit)."""
    async with maker() as session, session.begin():
        yield session


async def get_tx(maker: Maker) -> AsyncIterator[AsyncSession]:
    async with transaction(maker) as session:
        yield session


# scope="function": the transaction ends (commit/rollback) before the response is sent.
Tx = Annotated[AsyncSession, Depends(get_tx, scope="function")]


def clock_for_app(app: FastAPI) -> Clock:
    clock: Clock | None = getattr(app.state, "clock", None)
    return clock or SystemClock()


def get_now(request: Request) -> datetime:
    return clock_for_app(request.app).now()


Now = Annotated[datetime, Depends(get_now)]


async def current_user_id(user: StepwiseCurrentUser) -> uuid.UUID:
    return user.id


UserId = Annotated[uuid.UUID, Depends(current_user_id)]
DeviceTz = Annotated[ZoneInfo, Depends(require_timezone)]
RawBody = Annotated[Any, Body()]


# ---------------------------------------------------------------- errors


def validation_failed(
    fields: list[dict[str, str]], message: str = "Request body is invalid."
) -> AppError:
    return AppError(422, "validation_failed", message, {"fields": fields})


def not_found(resource: str, ident: uuid.UUID | str) -> AppError:
    return AppError(
        404,
        "not_found",
        f"{resource.capitalize()} not found.",
        {"resource": resource, "id": str(ident)},
    )


def _path(loc: tuple[int | str, ...], tags: frozenset[str]) -> str:
    # Drop discriminated-union tags ("deadline", "edit", ...) and pydantic's function labels so
    # the path names the field the client sent (e.g. `target_date`, `span.start`).
    parts = [str(p) for p in loc if not (isinstance(p, str) and (p in tags or "[" in p))]
    return ".".join(parts)


def parse_body[T](
    adapter: TypeAdapter[T], raw: Any, *, union_tags: frozenset[str] = frozenset()
) -> T:
    """Validate a JSON body; failures → 422 `validation_failed` with `details.fields`."""
    if not isinstance(raw, dict):
        raise validation_failed([{"path": "", "message": "body must be a JSON object"}])
    try:
        return adapter.validate_python(raw)
    except ValidationError as exc:
        fields = [
            {"path": _path(tuple(e["loc"]), union_tags), "message": e["msg"]}
            for e in exc.errors(include_url=False)
        ]
        raise validation_failed(fields) from None
