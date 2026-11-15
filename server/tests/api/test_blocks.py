"""PATCH /blocks semantics: edit → staged pin + re-solve (never in place), delete → forbid,
complete/skip only on started blocks, immutability of past/in-progress blocks, D7 checks."""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.mappers import span as span_json
from tests.api.conftest import NOW, Who, future_block, live_blocks, new_user, started_block
from tests.db.conftest import requires_db, rows

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]


async def _live_snapshot(maker: Maker, uid: uuid.UUID) -> list[tuple[str, str, str]]:
    return [(str(b.id), str(b.span), b.state) for b in await live_blocks(maker, uid)]


async def test_edit_stages_a_pin_and_never_edits_in_place(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID, user_id: uuid.UUID
) -> None:
    block = await future_block(maker, user_id)
    before = await _live_snapshot(maker, user_id)
    new_start = block.span.lower + timedelta(hours=1)
    target = {"start": new_start, "end": new_start + (block.span.upper - block.span.lower)}
    edit = {k: v.isoformat() for k, v in target.items()}
    resp = await client.patch(f"/blocks/{block.id}", json={"action": "edit", "span": edit})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    pin = body["staged"][0]["payload"]
    assert (pin["kind"], pin["created_from_block_id"]) == ("pin", str(block.id))
    # Proposal only: live blocks and pins untouched until confirm.
    assert await _live_snapshot(maker, user_id) == before
    assert await rows(maker, "SELECT count(*) FROM block_pins") == [(0,)]
    assert body["plan"]["status"] == "proposed" and body["plan_id"] != str(world)
    moved = {e["change"] for e in body["diff"]["entries"]}
    assert "moved" in moved or "added" in moved

    assert (await client.post(f"/changesets/{body['id']}/confirm")).status_code == 200
    assert await rows(maker, "SELECT kind::text FROM block_pins") == [("pin",)]
    live = await live_blocks(maker, user_id)
    assert any(b.span.lower == target["start"] and b.project_id == block.project_id for b in live)
    assert str(block.id) not in {str(b.id) for b in live}  # new rows per version


async def test_delete_stages_a_project_scoped_forbid(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID, user_id: uuid.UUID
) -> None:
    block = await future_block(maker, user_id)
    resp = await client.patch(f"/blocks/{block.id}", json={"action": "delete"})
    assert resp.status_code == 202, resp.text
    pin = resp.json()["staged"][0]["payload"]
    assert (pin["kind"], pin["project_id"], pin["span"]) == (
        "forbid",
        str(block.project_id),
        span_json(block.span),
    )
    blocks = resp.json()["plan"]["blocks"]
    assert not any(
        b["project_id"] == str(block.project_id) and b["span"] == pin["span"] for b in blocks
    )


async def test_complete_and_skip_apply_directly_and_are_idempotent(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID, user_id: uuid.UUID
) -> None:
    block = await started_block(maker, user_id)
    for action, state in (("skip", "skipped"), ("skip", "skipped"), ("complete", "completed")):
        resp = await client.patch(f"/blocks/{block.id}", json={"action": action})
        assert (resp.status_code, resp.json()["state"]) == (200, state)
    assert await rows(maker, "SELECT count(*) FROM changesets WHERE status = 'pending'") == [(0,)]


async def test_complete_on_a_future_block_is_block_not_started(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID, user_id: uuid.UUID
) -> None:
    block = await future_block(maker, user_id)
    resp = await client.patch(f"/blocks/{block.id}", json={"action": "complete"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "block_not_started"


async def test_edit_or_delete_of_a_started_block_is_immutable(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID, user_id: uuid.UUID, clock
) -> None:
    block = await future_block(maker, user_id)
    clock.current = block.span.lower + timedelta(minutes=5)  # in progress
    future = {"start": "2026-11-21T18:00:00Z", "end": "2026-11-21T19:00:00Z"}
    for body in ({"action": "edit", "span": future}, {"action": "delete"}):
        resp = await client.patch(f"/blocks/{block.id}", json=body)
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "block_immutable"
    assert await rows(maker, "SELECT count(*) FROM changesets WHERE status = 'pending'") == [(0,)]


async def test_edit_span_must_start_after_now_inside_the_horizon(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID, user_id: uuid.UUID
) -> None:
    block = await future_block(maker, user_id)
    cases = {
        "span.start": {"start": "2026-11-17T17:00:00Z", "end": "2026-11-17T19:00:00Z"},  # past
        "span": {"start": "2026-11-23T07:00:00Z", "end": "2026-11-23T09:00:00Z"},  # past horizon
    }
    for path, span in cases.items():
        resp = await client.patch(f"/blocks/{block.id}", json={"action": "edit", "span": span})
        assert resp.status_code == 422, resp.text
        assert path in {f["path"] for f in resp.json()["error"]["details"]["fields"]}
    seconds = {"start": "2026-11-20T02:00:30Z", "end": "2026-11-20T03:00:00Z"}
    inverted = {"start": "2026-11-20T03:00:00Z", "end": "2026-11-20T02:00:00Z"}
    bad = [
        {"action": "edit", "span": seconds},
        {"action": "edit", "span": inverted},
        {"action": "edit"},
        {"action": "move"},
        {"action": "delete", "extra": 1},
    ]  # fmt: skip
    for body in bad:
        resp = await client.patch(f"/blocks/{block.id}", json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "validation_failed"), body


async def test_superseded_and_foreign_blocks(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID, user_id: uuid.UUID, who: Who
) -> None:
    old = await future_block(maker, user_id)
    saturday = {"start_date": "2026-11-21", "end_date": "2026-11-21", "blocks_all": True}
    staged = (await client.post("/exceptions", json=saturday)).json()
    assert (await client.post(f"/changesets/{staged['id']}/confirm")).status_code == 200
    resp = await client.patch(f"/blocks/{old.id}", json={"action": "delete"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "block_superseded")

    current = await future_block(maker, user_id, NOW)
    who.user_id = await new_user(maker)
    for body in ({"action": "delete"}, {"action": "skip"}):
        resp = await client.patch(f"/blocks/{current.id}", json=body)
        assert resp.status_code == 404
        assert resp.json()["error"]["details"]["resource"] == "block"
