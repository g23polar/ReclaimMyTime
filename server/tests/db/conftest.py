"""Postgres-backed fixtures for the DB layer (T2.1).

Every DB test is skipped unless TEST_DATABASE_URL is set; it must name a dedicated test
database and never falls back to DATABASE_URL (tests/dbguard.py, M6 gate finding 7). Each
test gets a throwaway schema migrated by the REAL Alembic migration (not `create_all`), so
the invariants are tested against the shipped DDL. Extensions are installed once in
`public` (shared by schemas).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Connection, insert, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from app.db.models import User
from app.db.models.staged import StagedMutation, parse_staged
from app.db.solver_io import week_horizon
from app.scheduler import Interval
from tests.dbguard import resolve_test_database_url

DATABASE_URL = resolve_test_database_url()
requires_db = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set")
SERVER_DIR = Path(__file__).resolve().parents[2]

# The world: one user in Los Angeles, week of Mon 2026-11-16 (as in contracts/ fixtures).
TZ = "America/Los_Angeles"
WEEK = date(2026, 11, 16)
HORIZON: Interval = week_horizon(WEEK, TZ)
T0 = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)  # Mon 06:00 local, before the first window
WORKOUT = uuid.UUID("5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b02")
PORTFOLIO = uuid.UUID("5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b01")
GRANT = uuid.UUID("5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b04")
EXC = uuid.UUID("e7c2a9d4-3f1b-4e6a-8d20-5b9c1a7e3f01")


def alembic_config(connection: Connection) -> Config:
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_DIR / "alembic"))
    cfg.set_main_option("path_separator", "os")
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    return cfg


def run_alembic(connection: Connection, action: str, target: str) -> None:
    getattr(command, action)(alembic_config(connection), target)


async def make_engine(schema: str) -> AsyncEngine:
    assert DATABASE_URL
    return create_async_engine(
        DATABASE_URL, connect_args={"server_settings": {"search_path": f"{schema},public"}}
    )


async def ensure_extensions() -> None:
    assert DATABASE_URL
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist SCHEMA public"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext SCHEMA public"))
    await engine.dispose()


@pytest.fixture
async def schema() -> AsyncIterator[str]:
    """A fresh, empty schema; dropped afterwards (test-database cleanup only)."""
    await ensure_extensions()
    name = f"t21_{uuid.uuid4().hex[:10]}"
    engine = await make_engine(name)
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{name}"'))
    try:
        yield name
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        await engine.dispose()


@pytest.fixture
async def engine(schema: str) -> AsyncIterator[AsyncEngine]:
    eng = await make_engine(schema)
    async with eng.begin() as conn:
        await conn.run_sync(run_alembic, "upgrade", "head")
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def user_id(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    uid = uuid.uuid4()
    async with maker() as s, s.begin():
        await s.execute(insert(User).values(id=uid, created_at=T0))
    return uid


# ---------------------------------------------------------------- staged mutation builders


def at(day_offset: int, hhmm: str) -> datetime:
    """Local LA wall time on WEEK + day_offset, as UTC (no DST change in this week)."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, 16, tzinfo=UTC) + timedelta(days=day_offset, hours=h + 8, minutes=m)


def span(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


def staged(*items: dict[str, Any]) -> list[StagedMutation]:
    return parse_staged([{"source": "agent", "interpretation": None, **i} for i in items])


def prefs(**over: Any) -> dict[str, Any]:
    days = {k: [["08:00", "20:00"]] for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    p = {
        "timezone": TZ,
        "schedulable_windows": days,
        "day_diversity": "mixed",
        "buffer_minutes": 15,
        "week_start": 1,
    }
    return {"kind": "preferences_updated", "payload": {"preferences": p | over}}


def workout(pid: uuid.UUID = WORKOUT, hours: float = 3.0) -> dict[str, Any]:
    return {
        "kind": "project_created",
        "payload": {
            "project_id": str(pid),
            "project": {
                "name": "Workout",
                "kind": "perennial",
                "weekly_target_hours": hours,
                "max_block_minutes": 60,
                "requirements": ["gym"],
            },
        },
    }


def deadline_project(
    pid: uuid.UUID = PORTFOLIO, hours: float = 6.0, target: str = "2026-11-30", name: str = "P"
) -> dict[str, Any]:
    return {
        "kind": "project_created",
        "payload": {
            "project_id": str(pid),
            "project": {
                "name": name,
                "kind": "deadline",
                "target_date": target,
                "estimated_total_hours": hours,
                "max_block_minutes": 90,
            },
        },
    }


def exception_added(start: datetime, end: datetime, eid: uuid.UUID = EXC) -> dict[str, Any]:
    return {
        "kind": "exception_added",
        "payload": {
            "exception_id": str(eid),
            "span": span(start, end),
            "blocks_all": True,
            "blocks_tags": [],
            "reason": "away",
        },
    }


# ---------------------------------------------------------------- flow helpers


async def propose(
    maker: async_sessionmaker[AsyncSession],
    user: uuid.UUID,
    items: list[dict[str, Any]],
    now: datetime = T0,
    horizon: Interval | None = HORIZON,
) -> uuid.UUID:
    from app.db.write_paths import propose_changeset

    async with maker() as s, s.begin():
        return await propose_changeset(s, user, staged(*items), horizon=horizon, now=now)


async def confirm(
    maker: async_sessionmaker[AsyncSession], user: uuid.UUID, cs: uuid.UUID, now: datetime = T0
) -> Any:
    from app.db.write_paths import confirm_changeset

    async with maker() as s, s.begin():
        return await confirm_changeset(s, user, cs, now=now)


async def reject(
    maker: async_sessionmaker[AsyncSession], user: uuid.UUID, cs: uuid.UUID, now: datetime = T0
) -> None:
    from app.db.write_paths import reject_changeset

    async with maker() as s, s.begin():
        await reject_changeset(s, user, cs, now=now)


async def bootstrap(maker: async_sessionmaker[AsyncSession], user: uuid.UUID) -> uuid.UUID:
    """Onboard: prefs + Workout + Portfolio, confirmed at T0. Returns the live plan id."""
    cs = await propose(maker, user, [prefs(), workout(), deadline_project()])
    result = await confirm(maker, user, cs)
    assert result.plan_id is not None
    return result.plan_id


async def rows(maker: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> list[Any]:
    async with maker() as s:
        return list((await s.execute(text(sql), params)).all())


async def constraint_snapshot(
    maker: async_sessionmaker[AsyncSession], user: uuid.UUID
) -> dict[str, list[Any]]:
    """Committed scheduling state (staged project rows excluded — they are not state)."""
    q = {
        "projects": "SELECT id, name, status::text, estimated_total_hours, weekly_target_hours,"
        " max_block_minutes, min_block_minutes, requirements FROM projects"
        " WHERE user_id = :u AND status <> 'staged' ORDER BY id",
        "prefs": "SELECT timezone, schedulable_windows::text, buffer_minutes FROM user_preferences"
        " WHERE user_id = :u",
        "exceptions": "SELECT id, span::text, blocks_all FROM availability_exceptions"
        " WHERE user_id = :u ORDER BY id",
        "pins": "SELECT id, span::text, kind::text FROM block_pins WHERE user_id = :u ORDER BY id",
        "tags": "SELECT tag FROM user_tags WHERE user_id = :u ORDER BY tag",
    }
    return {k: await rows(maker, sql, u=user) for k, sql in q.items()}
