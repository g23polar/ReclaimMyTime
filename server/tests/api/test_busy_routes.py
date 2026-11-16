"""T4.2 busy time on every REST route that solves: never connected → busy=[]; connected → the
solver sees primary-calendar freeBusy; transient → 503 `gcal_unavailable`; revoked (stored or
invalid_grant while minting) → 409 `gcal_reconnect_required`; failures stage nothing.
Postgres + recorded Google HTTP (MockTransport)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.api.busy import BusyWindow, get_busy_provider
from app.auth.crypto import TokenCipher
from app.gcal.busy import Busy
from app.scheduler import Interval
from tests.api.conftest import NOW, future_block
from tests.api.contract import assert_conforms, load_fixture
from tests.db.conftest import WORKOUT, at, requires_db, rows
from tests.gcal.calendar_fakes import (
    busy_body,
    seed_connection,
    z,
)
from tests.gcal.fakes import ACCESS_TOKEN, REFRESH_TOKEN, Reply

pytestmark = [requires_db, pytest.mark.usefixtures("world")]

# All of Wednesday and Thursday (local) are busy in Google: the week's horizon is Mon–Sun LA.
BUSY = [(at(2, "00:00"), at(4, "00:00"))]
HORIZON_Z = ("2026-11-16T08:00:00Z", "2026-11-23T08:00:00Z")
WEDNESDAY_OFF = {"start_date": "2026-11-18", "end_date": "2026-11-18", "blocks_all": True}


@dataclass
class Env:
    client: httpx.AsyncClient
    maker: Any
    uid: uuid.UUID


async def ok(resp: Awaitable[httpx.Response], status: int = 202) -> Any:
    r = await resp
    assert r.status_code == status, r.text
    return r.json()


async def confirmed(env: Env, resp: Awaitable[httpx.Response]) -> None:
    cs = (await ok(resp))["id"]
    await ok(env.client.post(f"/changesets/{cs}/confirm"), 200)


# ------------------------------------------------- the solving routes (setup runs unconnected)

Call = tuple[str, str, Any]
Route = Callable[[Env], Awaitable[Call]]


async def create_project(env: Env) -> Call:
    return (
        "POST",
        "/projects",
        load_fixture("post_projects__perennial_onboarding.json")["request"]["body"],
    )


async def update_project(env: Env) -> Call:
    return "PATCH", f"/projects/{WORKOUT}", {"weekly_target_hours": 2.0}


async def archive_project(env: Env) -> Call:
    return "DELETE", f"/projects/{WORKOUT}", None


async def put_preferences(env: Env) -> Call:
    prefs = await ok(env.client.get("/preferences"), 200)
    return "PUT", "/preferences", prefs | {"buffer_minutes": 10}


async def create_exception(env: Env) -> Call:
    return "POST", "/exceptions", WEDNESDAY_OFF


async def delete_exception(env: Env) -> Call:
    await confirmed(
        env, env.client.post("/exceptions", json=WEDNESDAY_OFF | {"end_date": "2026-11-19"})
    )
    [exc] = (await ok(env.client.get("/exceptions"), 200))["exceptions"]
    return "DELETE", f"/exceptions/{exc['id']}", None


async def delete_pin(env: Env) -> Call:
    block = await future_block(env.maker, env.uid)
    await confirmed(env, env.client.patch(f"/blocks/{block.id}", json={"action": "delete"}))
    [pin] = (await ok(env.client.get("/pins"), 200))["pins"]
    return "DELETE", f"/pins/{pin['id']}", None


async def edit_block(env: Env) -> Call:
    block = await future_block(env.maker, env.uid)
    span = {"start": z(at(4, "14:00")), "end": z(at(4, "15:00"))}  # Friday
    return "PATCH", f"/blocks/{block.id}", {"action": "edit", "span": span}


async def delete_block(env: Env) -> Call:
    block = await future_block(env.maker, env.uid)
    return "PATCH", f"/blocks/{block.id}", {"action": "delete"}


ROUTES: dict[str, tuple[str, Route]] = {
    "create_project": ("createProject", create_project),
    "update_project": ("updateProject", update_project),
    "archive_project": ("archiveProject", archive_project),
    "put_preferences": ("putPreferences", put_preferences),
    "create_exception": ("createException", create_exception),
    "delete_exception": ("deleteException", delete_exception),
    "delete_pin": ("deletePin", delete_pin),
    "edit_block": ("patchBlock", edit_block),
    "delete_block": ("patchBlock", delete_block),
}


async def prepared(client: httpx.AsyncClient, maker: Any, uid: uuid.UUID, name: str) -> Call:
    return await ROUTES[name][1](Env(client, maker, uid))


async def send(client: httpx.AsyncClient, call: Call) -> httpx.Response:
    method, path, body = call
    return await client.request(method, path, json=body)


async def footprint(maker: Any, uid: uuid.UUID) -> list[Any]:
    """Everything a staged write could leave behind."""
    q = "SELECT (SELECT count(*) FROM {t} WHERE user_id = :u)"
    tables = ("changesets", "week_plans", "projects", "mutations", "block_pins")
    return [(await rows(maker, q.format(t=t), u=uid))[0][0] for t in tables]


async def solver_busy(maker: Any, plan_id: str) -> list[Any]:
    got = await rows(
        maker, "SELECT solver_input->'busy' FROM week_plans WHERE id = :p", p=uuid.UUID(plan_id)
    )
    return got[0][0]


def overlaps(span: dict[str, str], a: datetime, b: datetime) -> bool:
    start, end = datetime.fromisoformat(span["start"]), datetime.fromisoformat(span["end"])
    return start < b and a < end


ALL = pytest.mark.parametrize("name", sorted(ROUTES))


# ---------------------------------------------------------------- tests


@ALL
async def test_never_connected_solves_with_empty_busy(name, app, client, maker, who):
    assert getattr(app.state, "gcal_runtime", None) is None  # building one would raise
    body = await ok(send(client, await prepared(client, maker, who.user_id, name)))
    assert body["plan"] is not None
    assert await solver_busy(maker, body["plan_id"]) == []


@ALL
async def test_connected_busy_reaches_the_solver(name, gcal, cal, cipher, client, maker, who):
    call = await prepared(client, maker, who.user_id, name)
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    cal.busy = list(BUSY)
    body = await ok(send(client, call))
    assert_conforms(ROUTES[name][0], 202, body)
    for b in body["plan"]["blocks"]:
        assert not overlaps(b["span"], *BUSY[0]), b
    assert await solver_busy(maker, body["plan_id"]) == [
        {"start": z(BUSY[0][0]), "end": z(BUSY[0][1])}
    ]
    [fb] = cal.freebusy_bodies
    assert fb == {"timeMin": HORIZON_Z[0], "timeMax": HORIZON_Z[1], "items": [{"id": "primary"}]}


@ALL
async def test_transient_google_failure_is_503_and_stages_nothing(
    name, gcal, cal, cipher, client, maker, who
):
    call = await prepared(client, maker, who.user_id, name)
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    cal.freebusy = Reply(status=503, json={})
    before = await footprint(maker, who.user_id)
    resp = await send(client, call)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "gcal_unavailable"
    assert_conforms(ROUTES[name][0], 503, resp.json())
    assert await footprint(maker, who.user_id) == before


@ALL
async def test_stored_revoked_is_409_without_calling_google(
    name, gcal, google, cal, cipher, client, maker, who
):
    call = await prepared(client, maker, who.user_id, name)
    await seed_connection(maker, who.user_id, cipher, status="revoked", now=NOW)
    before = await footprint(maker, who.user_id)
    resp = await send(client, call)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "gcal_reconnect_required"
    assert_conforms(ROUTES[name][0], 409, resp.json())
    assert await footprint(maker, who.user_id) == before
    assert google.requests == [] and cal.requests == []


@ALL
async def test_invalid_grant_while_minting_is_409_and_marks_revoked(
    name, gcal, google, cal, cipher, client, maker, who
):
    call = await prepared(client, maker, who.user_id, name)
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    google.refresh = Reply(status=400, json={"error": "invalid_grant"})
    before = await footprint(maker, who.user_id)
    resp = await send(client, call)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "gcal_reconnect_required"
    assert await footprint(maker, who.user_id) == before
    status = await rows(
        maker, "SELECT status::text FROM google_connections WHERE user_id = :u", u=who.user_id
    )
    assert status == [("revoked",)]
    assert cal.requests == []


async def test_undecryptable_credential_is_409_and_logged_with_key_id(
    gcal, cal, client, maker, who, caplog
):
    """Review Q2: reconnecting stores a fresh token under the current key, so the user can
    self-heal; the operator sees the key id at ERROR."""
    stranger = TokenCipher([(7, Fernet.generate_key().decode())])  # a key we do not hold
    await seed_connection(maker, who.user_id, stranger, now=NOW)
    before = await footprint(maker, who.user_id)
    resp = await send(client, await create_project(Env(client, maker, who.user_id)))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "gcal_reconnect_required"
    assert await footprint(maker, who.user_id) == before
    assert cal.requests == []
    assert any(r.levelno == logging.ERROR and "key_id=7" in r.getMessage() for r in caplog.records)


async def test_tokens_and_google_bodies_are_never_logged(
    gcal, google, cal, cipher, client, maker, who, caplog
):
    caplog.set_level(logging.DEBUG)
    await seed_connection(maker, who.user_id, cipher, now=NOW)
    cal.busy = list(BUSY)
    await ok(send(client, await create_project(Env(client, maker, who.user_id))))
    cal.freebusy = Reply(json=busy_body(errors=[{"domain": "x", "reason": "secretReason"}]))
    resp = await send(client, await update_project(Env(client, maker, who.user_id)))
    assert resp.status_code == 503
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, "secretReason", z(BUSY[0][0])):
        assert secret not in caplog.text


async def test_horizon_moving_between_busy_read_and_lock_is_503(app, client, maker, who):
    """A concurrent preferences change (week start) lands after the busy read: the window no
    longer covers the solve, so nothing is staged rather than solving with the wrong busy."""

    class MovesTheWeek:
        async def busy(self, user_id: uuid.UUID, horizon: Any) -> Busy:
            async with maker() as s, s.begin():
                await s.execute(text("UPDATE user_preferences SET week_start = 3"))
            return Busy(intervals=(), connected=True)

    app.dependency_overrides[get_busy_provider] = MovesTheWeek
    before = await footprint(maker, who.user_id)
    resp = await send(client, await create_project(Env(client, maker, who.user_id)))
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "gcal_unavailable"
    assert "settings changed" in resp.json()["error"]["message"]
    assert await footprint(maker, who.user_id) == before


def test_window_coverage_rules():
    week = Interval(start=at(0, "00:00"), end=at(7, "00:00"))
    other = Interval(start=at(2, "00:00"), end=at(9, "00:00"))
    w = BusyWindow(week)
    assert w.covers(week) and w.covers(None)  # None: no solve
    assert not w.covers(other)  # horizon moved
    assert not BusyWindow().covers(week)  # nothing fetched (no preferences), now a solve
    assert BusyWindow(week, never_connected=True).covers(other)  # empty busy for any horizon
