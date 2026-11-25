"""Per-user write rate limit (issue #23): every scheduling write route shares one fixed-window
limit per user. At the limit → accepted; one over → 429 `rate_limited` with `Retry-After`, no
handler code and no state change. Authenticated requests count whatever they return; 401s and
GETs never do."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.busy import get_busy_provider
from app.api.deps import Maker as MakerDep
from app.api.deps import current_user_id
from app.api.rate_limit import WriteRateSettings
from app.auth.dependencies import build_runtime, sqlalchemy_repository_provider
from app.auth.settings import AuthSettings
from app.gcal.busy import BusyProvider
from tests.api.conftest import NOW, TZ_HEADERS, SettableClock, Who, new_user
from tests.db.conftest import WORKOUT, requires_db, rows

Maker = async_sessionmaker[AsyncSession]
WEEK = {d: [["08:00", "20:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
PREFS = {
    "timezone": "America/Los_Angeles",
    "schedulable_windows": WEEK,
    "day_diversity": "mixed",
    "buffer_minutes": 15,
    "week_start": 1,
}
TODO = {"kind": "one_off", "title": "Renew passport"}
READING = {"name": "Reading", "kind": "perennial", "weekly_target_hours": 2.0}
RATE_LIMITED = {
    "error": {
        "code": "rate_limited",
        "message": "Too many changes at once. Try again in a moment.",
    }
}
DEAD = "00000000-0000-4000-8000-00000000dead"


def limit(app: FastAPI, n: int, window_seconds: int = 60) -> None:
    app.state.write_rate_settings = WriteRateSettings(limit=n, window_seconds=window_seconds)


async def counter(maker: Maker, uid: uuid.UUID) -> list[tuple[datetime, int]]:
    return await rows(
        maker, "SELECT window_start, count FROM write_rate_counters WHERE user_id = :u", u=uid
    )


async def use_up(client: httpx.AsyncClient, n: int) -> None:
    for _ in range(n):
        resp = await client.post("/todos", json=TODO)
        assert resp.status_code == 201, resp.text


def assert_rate_limited(resp: httpx.Response, window_seconds: int = 60) -> int:
    assert resp.status_code == 429, resp.text
    assert resp.json() == RATE_LIMITED
    retry_after = int(resp.headers["Retry-After"])
    assert 1 <= retry_after <= window_seconds
    return retry_after


# ---------------------------------------------------------------- settings (no database)


def test_settings_default_to_60_per_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WRITE_RATE_LIMIT", raising=False)
    monkeypatch.delenv("WRITE_RATE_WINDOW_SECONDS", raising=False)
    assert WriteRateSettings() == WriteRateSettings(limit=60, window_seconds=60)
    assert WriteRateSettings.from_env() == WriteRateSettings(limit=60, window_seconds=60)


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WRITE_RATE_LIMIT", "5")
    monkeypatch.setenv("WRITE_RATE_WINDOW_SECONDS", "10")
    assert WriteRateSettings.from_env() == WriteRateSettings(limit=5, window_seconds=10)


def test_settings_must_be_integers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WRITE_RATE_LIMIT", "lots")
    with pytest.raises(ValueError):
        WriteRateSettings.from_env()


# ---------------------------------------------------------------- HTTP


@requires_db
async def test_at_the_limit_accepted_one_over_refused(
    app: FastAPI, client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, world: uuid.UUID
) -> None:
    limit(app, 3)
    for _ in range(3):
        assert (await client.put("/preferences", json=PREFS)).status_code == 202
    assert await counter(maker, user_id) == [(NOW, 3)]
    # The limit is shared: a todo write is refused once preferences writes used it up.
    assert assert_rate_limited(await client.post("/todos", json=TODO)) == 60
    assert await counter(maker, user_id) == [(NOW, 3)]


@requires_db
async def test_retry_after_counts_down_to_the_window_end(
    app: FastAPI, client: httpx.AsyncClient, clock: SettableClock, world: uuid.UUID
) -> None:
    limit(app, 1)
    await use_up(client, 1)
    clock.current = NOW + timedelta(seconds=44, milliseconds=500)
    assert assert_rate_limited(await client.post("/todos", json=TODO)) == 16  # 15.5 s, up
    clock.current = NOW + timedelta(seconds=59, milliseconds=999)
    assert assert_rate_limited(await client.post("/todos", json=TODO)) == 1


@requires_db
async def test_next_window_frees_the_user(
    app: FastAPI,
    client: httpx.AsyncClient,
    clock: SettableClock,
    maker: Maker,
    user_id: uuid.UUID,
    world: uuid.UUID,
) -> None:
    limit(app, 2)
    await use_up(client, 2)
    assert_rate_limited(await client.post("/todos", json=TODO))
    clock.current = NOW + timedelta(seconds=60)  # the window end
    assert (await client.post("/todos", json=TODO)).status_code == 201
    assert await counter(maker, user_id) == [(NOW + timedelta(seconds=60), 1)]


@requires_db
async def test_window_length_comes_from_settings(
    app: FastAPI, client: httpx.AsyncClient, clock: SettableClock, world: uuid.UUID
) -> None:
    limit(app, 1, window_seconds=10)
    await use_up(client, 1)
    assert_rate_limited(await client.post("/todos", json=TODO), window_seconds=10)
    clock.current = NOW + timedelta(seconds=10)
    assert (await client.post("/todos", json=TODO)).status_code == 201


@requires_db
async def test_users_are_limited_apart(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: Maker,
    who: Who,
    user_id: uuid.UUID,
    world: uuid.UUID,
) -> None:
    limit(app, 2)
    await use_up(client, 2)
    assert_rate_limited(await client.post("/todos", json=TODO))
    b = await new_user(maker)
    who.user_id = b
    await use_up(client, 2)
    assert await counter(maker, b) == [(NOW, 2)]
    assert await counter(maker, user_id) == [(NOW, 2)]


@requires_db
async def test_unauthenticated_writes_never_count(
    app: FastAPI, client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, world: uuid.UUID
) -> None:
    limit(app, 1)
    assert (await client.post("/todos", json=TODO)).status_code == 201
    before = await counter(maker, user_id)
    del app.dependency_overrides[current_user_id]
    app.state.auth_runtime = build_runtime(
        AuthSettings(apple_bundle_id="", fernet_keys=((1, Fernet.generate_key().decode()),)),
        repository_provider=sqlalchemy_repository_provider(maker),
    )
    for headers in ({}, {"Authorization": "Bearer not-a-session-token"}):
        resp = await client.post("/todos", json=TODO, headers=headers)
        assert (resp.status_code, resp.json()["error"]["code"]) == (401, "unauthenticated")
    assert await counter(maker, user_id) == before
    assert len(await rows(maker, "SELECT 1 FROM write_rate_counters")) == 1


@requires_db
async def test_refused_write_changes_nothing(
    app: FastAPI, client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, world: uuid.UUID
) -> None:
    busy_reads: list[uuid.UUID] = []

    def spy(request: Request, maker: MakerDep) -> BusyProvider:
        busy_reads.append(uuid.uuid4())
        return get_busy_provider(request, maker)

    app.dependency_overrides[get_busy_provider] = spy
    limit(app, 1)
    await use_up(client, 1)

    async def snapshot() -> list[Any]:
        return [
            await rows(maker, "SELECT id, status FROM changesets ORDER BY id"),
            await rows(maker, "SELECT id, name, status FROM projects ORDER BY id"),
            await rows(maker, "SELECT id, title FROM todos ORDER BY id"),
            await rows(maker, "SELECT * FROM user_preferences"),
            await counter(maker, user_id),
        ]

    before = await snapshot()
    for method, path, body in (
        ("POST", "/projects", READING),
        ("PATCH", f"/projects/{WORKOUT}", {"name": "Lifting"}),
        ("DELETE", f"/projects/{WORKOUT}", None),
        ("PUT", "/preferences", PREFS),
        ("POST", "/todos", TODO),
    ):
        assert_rate_limited(await client.request(method, path, json=body))
    assert await snapshot() == before
    assert busy_reads == []  # refused before the handler's dependencies resolved


@requires_db
async def test_handler_errors_still_count(
    app: FastAPI, client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, world: uuid.UUID
) -> None:
    limit(app, 3)
    assert (await client.patch(f"/todos/{DEAD}", json={"title": "x"})).status_code == 404
    assert (await client.post("/todos", json={"kind": "nope"})).status_code == 422
    assert (await client.post(f"/changesets/{DEAD}/reject")).status_code == 404
    assert await counter(maker, user_id) == [(NOW, 3)]
    assert_rate_limited(await client.post("/todos", json=TODO))


@requires_db
async def test_reads_are_never_limited(
    app: FastAPI, client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, world: uuid.UUID
) -> None:
    limit(app, 1)
    await use_up(client, 1)
    assert_rate_limited(await client.post("/todos", json=TODO))
    for path in (
        "/projects",
        f"/projects/{WORKOUT}",
        "/preferences",
        "/exceptions",
        "/pins",
        "/todos",
        "/plans/current",
    ):
        resp = await client.get(path)
        assert resp.status_code == 200, (path, resp.text)
    assert await counter(maker, user_id) == [(NOW, 1)]


@requires_db
async def test_every_covered_route_is_limited(
    app: FastAPI, client: httpx.AsyncClient, world: uuid.UUID
) -> None:
    limit(app, 1)
    await use_up(client, 1)
    covered = [
        ("POST", "/projects"),
        ("PATCH", f"/projects/{DEAD}"),
        ("DELETE", f"/projects/{DEAD}"),
        ("PUT", "/preferences"),
        ("POST", "/exceptions"),
        ("DELETE", f"/exceptions/{DEAD}"),
        ("DELETE", f"/pins/{DEAD}"),
        ("PATCH", f"/blocks/{DEAD}"),
        ("POST", f"/changesets/{DEAD}/confirm"),
        ("POST", f"/changesets/{DEAD}/reject"),
        ("POST", "/todos"),
        ("PATCH", f"/todos/{DEAD}"),
        ("DELETE", f"/todos/{DEAD}"),
        ("PUT", "/todos/order"),
        ("PUT", f"/todos/{DEAD}/completions/2026-11-16"),
        ("DELETE", f"/todos/{DEAD}/completions/2026-11-16"),
    ]
    for method, path in covered:
        body = {} if method in ("POST", "PUT", "PATCH") else None
        assert_rate_limited(await client.request(method, path, json=body, headers=TZ_HEADERS))
