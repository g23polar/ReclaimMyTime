"""API fixtures: the T2.3 routers on a fresh FastAPI app, backed by the per-test migrated schema
from tests/db (skipped without TEST_DATABASE_URL), a settable clock, and an overridable
current user (auth itself is T2.2's and tested there)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import install
from app.api.deps import current_user_id
from app.auth.crypto import TokenCipher
from app.db.models import Block, User, WeekPlan
from app.errors import register_error_handlers
from app.gcal.runtime import GcalRuntime
from tests.db.conftest import (  # noqa: F401  (pytest fixtures re-used from tests/db)
    T0,
    bootstrap,
    engine,
    maker,
    schema,
    user_id,
)
from tests.gcal.calendar_fakes import FakeCalendar, new_cipher
from tests.gcal.calendar_fakes import install as install_gcal
from tests.gcal.fakes import FakeGoogle

TZ_HEADERS = {"X-Timezone": "America/Los_Angeles"}
NOW = datetime(2026, 11, 17, 18, 30, tzinfo=UTC)  # Tue 10:30 local (the fixtures' story)


class SettableClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


@dataclass
class Who:
    user_id: uuid.UUID


def build_app() -> FastAPI:
    application = FastAPI()
    register_error_handlers(application)
    install(application)
    return application


@pytest.fixture
def clock() -> SettableClock:
    return SettableClock(T0)


@pytest.fixture
def who(user_id: uuid.UUID) -> Who:  # noqa: F811
    return Who(user_id)


@pytest.fixture
async def app(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    clock: SettableClock,
    who: Who,
) -> AsyncIterator[FastAPI]:
    application = build_app()
    application.state.sessionmaker = maker
    application.state.clock = clock
    application.dependency_overrides[current_user_id] = lambda: who.user_id
    yield application
    # T4.3a: confirm kicks a background write-back drain; let it finish before the schema
    # (and its engine) is torn down.
    registry = getattr(application.state, "gcal_outbox", None)
    if registry is not None:
        await registry.idle(30)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=TZ_HEADERS
    ) as c:
        yield c


async def new_user(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:  # noqa: F811
    uid = uuid.uuid4()
    async with maker() as s, s.begin():
        await s.execute(insert(User).values(id=uid, created_at=T0))
    return uid


async def live_blocks(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> list[Block]:  # noqa: F811
    async with maker() as s:
        rows = await s.execute(
            select(Block)
            .join(WeekPlan, WeekPlan.id == Block.plan_id)
            .where(Block.user_id == uid, WeekPlan.status == "live")
        )
        return sorted(rows.scalars(), key=lambda b: b.span.lower)


async def started_block(maker: Any, uid: uuid.UUID, now: datetime = NOW) -> Block:  # noqa: F811
    return next(b for b in await live_blocks(maker, uid) if b.span.lower <= now)


async def future_block(maker: Any, uid: uuid.UUID, now: datetime = NOW) -> Block:  # noqa: F811
    return next(b for b in await live_blocks(maker, uid) if b.span.lower > now)


@pytest.fixture
async def world(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    user_id: uuid.UUID,  # noqa: F811
    clock: SettableClock,
) -> uuid.UUID:
    """Onboarded at T0 (prefs + Workout + Portfolio confirmed); clock moved to NOW. Returns
    the live plan id."""
    plan_id = await bootstrap(maker, user_id)
    clock.current = NOW
    return plan_id


# ---------------------------------------------------------------- Google (T4.2, recorded HTTP)


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle(now=NOW)


@pytest.fixture
def cal() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
def cipher() -> TokenCipher:
    return new_cipher()


@pytest.fixture
def gcal(
    app: FastAPI, google: FakeGoogle, cal: FakeCalendar, cipher: TokenCipher, clock: SettableClock
) -> GcalRuntime:
    """A gcal runtime on `app` whose OAuth and Calendar clients hit the fakes."""
    return install_gcal(app, google, cal, cipher, clock)
