"""agent_turns write path (T6.2 eval capture): round-trip, NULL text when capture is off,
30-day global purge on write, FK behaviour, token window sum, CHECK constraints."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta
from typing import Any, get_args

import pytest
from sqlalchemy import delete, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.agent_turns import (
    RETENTION,
    AgentTurnOutcome,
    AgentTurnRecord,
    record_agent_turn,
    tokens_used_since,
)
from app.db.models import User
from app.db.models.agent import AGENT_TURN_OUTCOMES
from tests.db.conftest import T0, bootstrap, requires_db, rows

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]

STAGED: list[dict[str, Any]] = [
    {
        "kind": "pin_added",
        "source": "agent",
        "interpretation": "Gym Tue 7am",
        "payload": {"pin_id": "5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b09"},
    }
]


def record(user: uuid.UUID, **over: Any) -> AgentTurnRecord:
    base = AgentTurnRecord(
        user_id=user,
        model="claude-haiku-5-5",
        outcome="completed",
        error_code=None,
        stop_reason="end_turn",
        model_calls=2,
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=5000,
        cache_write_tokens=30,
        transcript_len=3,
        staged=STAGED,
        changeset_id=None,
        plan_id=None,
        user_message="move gym to tuesday",
        snapshot="P1 Workout ...",
    )
    return dataclasses.replace(base, **over)


async def write(maker: Maker, rec: AgentTurnRecord, now: Any = T0) -> None:
    async with maker() as s, s.begin():
        await record_agent_turn(s, rec, now=now)


async def new_user(maker: Maker) -> uuid.UUID:
    uid = uuid.uuid4()
    async with maker() as s, s.begin():
        await s.execute(insert(User).values(id=uid, created_at=T0))
    return uid


async def turn_times(maker: Maker, user: uuid.UUID) -> list[Any]:
    got = await rows(
        maker, "SELECT created_at FROM agent_turns WHERE user_id = :u ORDER BY created_at", u=user
    )
    return [r[0] for r in got]


async def test_round_trip(maker: Maker, user_id: uuid.UUID) -> None:
    cs = uuid.uuid4()
    async with maker() as s, s.begin():
        await s.execute(
            text("INSERT INTO changesets (id, user_id, staged) VALUES (:c, :u, '[]')"),
            {"c": cs, "u": user_id},
        )
    rec = record(user_id, changeset_id=cs, outcome="error", error_code="changeset_stale")
    await write(maker, rec)
    got = await rows(
        maker,
        "SELECT user_id, created_at, model, outcome, error_code, stop_reason, model_calls,"
        " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, transcript_len,"
        " staged, changeset_id, plan_id, user_message, snapshot FROM agent_turns",
    )
    assert got == [
        (
            user_id,
            T0,
            "claude-haiku-5-5",
            "error",
            "changeset_stale",
            "end_turn",
            2,
            1000,
            200,
            5000,
            30,
            3,
            STAGED,
            cs,
            None,
            "move gym to tuesday",
            "P1 Workout ...",
        )
    ]


async def test_capture_off_text_columns_are_sql_null(maker: Maker, user_id: uuid.UUID) -> None:
    await write(
        maker,
        record(user_id, user_message=None, snapshot=None, stop_reason=None, staged=[]),
    )
    got = await rows(
        maker,
        "SELECT user_message IS NULL, snapshot IS NULL, stop_reason IS NULL, error_code IS NULL,"
        " staged::text FROM agent_turns",
    )
    assert got == [(True, True, True, True, "[]")]


async def test_purge_is_global_and_keeps_exactly_30_days(maker: Maker, user_id: uuid.UUID) -> None:
    """Boundary (documented in app.db.agent_turns): created_at < now - 30d is purged; a row
    exactly 30 days old is kept. The purge covers EVERY user, so an inactive user's expired
    rows go on anyone's next turn, while their unexpired rows survive."""
    other = await new_user(maker)
    old = T0 - RETENTION - timedelta(microseconds=1)
    edge = T0 - RETENTION
    recent = T0 - timedelta(days=1)
    for t in (old, edge, recent):
        await write(maker, record(user_id), now=t)
    for t in (old, edge, recent):
        await write(maker, record(other), now=t)
    assert await turn_times(maker, user_id) == [old, edge, recent]

    await write(maker, record(user_id), now=T0)
    assert await turn_times(maker, user_id) == [edge, recent, T0]
    assert await turn_times(maker, other) == [edge, recent]  # inactive user's old row purged


async def test_purge_rolls_back_with_the_insert(maker: Maker, user_id: uuid.UUID) -> None:
    old = T0 - RETENTION - timedelta(days=1)
    await write(maker, record(user_id), now=old)
    async with maker() as s:
        async with s.begin():
            await record_agent_turn(s, record(user_id), now=T0)
            await s.rollback()
    assert await turn_times(maker, user_id) == [old]


async def test_user_delete_cascades(maker: Maker, user_id: uuid.UUID) -> None:
    other = await new_user(maker)
    await write(maker, record(user_id))
    await write(maker, record(other))
    async with maker() as s, s.begin():
        await s.execute(delete(User).where(User.id == user_id))
    assert await rows(maker, "SELECT user_id FROM agent_turns") == [(other,)]


async def test_changeset_and_plan_delete_set_null(maker: Maker, user_id: uuid.UUID) -> None:
    plan = await bootstrap(maker, user_id)
    (cs,) = (await rows(maker, "SELECT id FROM changesets WHERE user_id = :u", u=user_id))[0]
    await write(maker, record(user_id, changeset_id=cs, plan_id=plan))

    async with maker() as s, s.begin():
        await s.execute(text("DELETE FROM changesets WHERE id = :c"), {"c": cs})
    got = await rows(maker, "SELECT changeset_id, plan_id, model FROM agent_turns")
    assert got == [(None, plan, "claude-haiku-5-5")]

    async with maker() as s, s.begin():
        await s.execute(text("DELETE FROM week_plans WHERE id = :p"), {"p": plan})
    got = await rows(maker, "SELECT changeset_id, plan_id, model FROM agent_turns")
    assert got == [(None, None, "claude-haiku-5-5")]


async def test_tokens_used_since_sums_window_and_excludes_cache_reads(
    maker: Maker, user_id: uuid.UUID
) -> None:
    other = await new_user(maker)
    since = T0 - timedelta(hours=24)
    async with maker() as s:
        assert await tokens_used_since(s, user_id, since) == 0  # no rows -> 0, not None
    await write(maker, record(user_id, input_tokens=7, output_tokens=11), now=since)  # in
    await write(maker, record(user_id), now=T0)  # in: 1000 + 200 + 30
    await write(maker, record(user_id), now=since - timedelta(microseconds=1))  # out
    await write(maker, record(other), now=T0)  # other user
    async with maker() as s:
        assert await tokens_used_since(s, user_id, since) == (7 + 11 + 30) + (1000 + 200 + 30)
        assert await tokens_used_since(s, user_id, T0 + timedelta(seconds=1)) == 0


async def test_tokens_used_since_does_not_overflow_int(maker: Maker, user_id: uuid.UUID) -> None:
    big = 2**31 - 1
    await write(maker, record(user_id, input_tokens=big, output_tokens=big, cache_write_tokens=0))
    async with maker() as s:
        assert await tokens_used_since(s, user_id, T0) == 2 * big


@pytest.mark.parametrize(
    "over",
    [
        {"outcome": "timeout"},
        {"model_calls": -1},
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"cache_read_tokens": -1},
        {"cache_write_tokens": -1},
    ],
)
async def test_check_constraints_reject(
    maker: Maker, user_id: uuid.UUID, over: dict[str, Any]
) -> None:
    async with maker() as s:
        with pytest.raises(IntegrityError, match="ck_agent_turns_"):
            async with s.begin():
                await record_agent_turn(s, record(user_id, **over), now=T0)
    assert await rows(maker, "SELECT id FROM agent_turns") == []


def test_outcome_literal_matches_the_check_constant() -> None:
    assert get_args(AgentTurnOutcome) == AGENT_TURN_OUTCOMES
