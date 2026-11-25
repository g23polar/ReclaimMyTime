"""Write-side size caps (issue #4): at most MAX_TAGS tags in `requirements`/`blocks_tags` and
MAX_WINDOWS_PER_DAY windows per weekday. At the cap → 202; one over → 422 naming the field,
with no new changeset. Rows stored over a cap before it existed still read, confirm and carry
forward."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.staged import MAX_TAGS, MAX_WINDOWS_PER_DAY, parse_staged
from tests.api.conftest import NOW
from tests.db.conftest import WORKOUT, prefs, propose, requires_db, rows

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]
READING = {
    "name": "Reading",
    "kind": "perennial",
    "weekly_target_hours": 2.0,
    "max_block_minutes": 60,
}
WEEK = {d: [["08:00", "20:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
PREFS = {
    "timezone": "America/Los_Angeles",
    "schedulable_windows": WEEK,
    "day_diversity": "mixed",
    "buffer_minutes": 15,
    "week_start": 1,
}
FRIDAY_NO_TAGS = {"start_date": "2026-11-20", "end_date": "2026-11-20"}


def tags(n: int) -> list[str]:
    return [f"tag-{i:02d}" for i in range(n)]


def windows(n: int) -> list[list[str]]:
    """`n` sorted, non-overlapping 30-minute windows from 08:00."""
    return [[f"{8 + i:02d}:00", f"{8 + i:02d}:30"] for i in range(n)]


async def changeset_count(maker: Maker) -> int:
    return (await rows(maker, "SELECT count(*) FROM changesets"))[0][0]


async def assert_cap(
    client: httpx.AsyncClient,
    maker: Maker,
    method: str,
    path: str,
    at_cap: dict[str, Any],
    over_cap: dict[str, Any],
    field: str,
) -> None:
    before = await changeset_count(maker)
    resp = await client.request(method, path, json=over_cap)
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "validation_failed"
    assert [f["path"] for f in error["details"]["fields"]] == [field]
    assert await changeset_count(maker) == before  # nothing staged
    resp = await client.request(method, path, json=at_cap)
    assert resp.status_code == 202, resp.text


async def test_create_requirements_cap(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID
) -> None:
    await assert_cap(
        client,
        maker,
        "POST",
        "/projects",
        READING | {"requirements": tags(MAX_TAGS)},
        READING | {"requirements": tags(MAX_TAGS + 1)},
        "requirements",
    )


async def test_patch_requirements_cap(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID
) -> None:
    await assert_cap(
        client,
        maker,
        "PATCH",
        f"/projects/{WORKOUT}",
        {"requirements": tags(MAX_TAGS)},
        {"requirements": tags(MAX_TAGS + 1)},
        "requirements",
    )


async def test_create_preferred_windows_cap(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID
) -> None:
    await assert_cap(
        client,
        maker,
        "POST",
        "/projects",
        READING | {"preferred_windows": {"mon": windows(MAX_WINDOWS_PER_DAY)}},
        READING | {"preferred_windows": {"mon": windows(MAX_WINDOWS_PER_DAY + 1)}},
        "preferred_windows.mon",
    )


async def test_patch_preferred_windows_cap(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID
) -> None:
    await assert_cap(
        client,
        maker,
        "PATCH",
        f"/projects/{WORKOUT}",
        {"preferred_windows": {"mon": windows(MAX_WINDOWS_PER_DAY)}},
        {"preferred_windows": {"mon": windows(MAX_WINDOWS_PER_DAY + 1)}},
        "preferred_windows.mon",
    )


async def test_schedulable_windows_cap(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID
) -> None:
    await assert_cap(
        client,
        maker,
        "PUT",
        "/preferences",
        PREFS | {"schedulable_windows": WEEK | {"thu": windows(MAX_WINDOWS_PER_DAY)}},
        PREFS | {"schedulable_windows": WEEK | {"thu": windows(MAX_WINDOWS_PER_DAY + 1)}},
        "schedulable_windows.thu",
    )


async def test_blocks_tags_cap(client: httpx.AsyncClient, maker: Maker, world: uuid.UUID) -> None:
    await assert_cap(
        client,
        maker,
        "POST",
        "/exceptions",
        FRIDAY_NO_TAGS | {"blocks_tags": tags(MAX_TAGS)},
        FRIDAY_NO_TAGS | {"blocks_tags": tags(MAX_TAGS + 1)},
        "blocks_tags",
    )


async def test_existing_validation_paths_unchanged(
    client: httpx.AsyncClient, world: uuid.UUID
) -> None:
    cases: list[tuple[str, str, dict[str, Any], str]] = [
        ("POST", "/projects", READING | {"requirements": ["a", "a"]}, "requirements"),
        ("POST", "/projects", READING | {"preferred_windows": {"mon": [["09:00", "08:00"]]}},
         "preferred_windows"),
        ("PATCH", f"/projects/{WORKOUT}", {"requirements": ["a", "a"]}, "requirements"),
        ("PUT", "/preferences", PREFS | {"schedulable_windows": {"xyz": []}},
         "schedulable_windows"),
        ("POST", "/exceptions", FRIDAY_NO_TAGS | {"blocks_tags": ["a", "a"]}, "blocks_tags"),
    ]  # fmt: skip
    for method, path, body, field in cases:
        resp = await client.request(method, path, json=body)
        assert resp.status_code == 422, (path, body)
        assert [f["path"] for f in resp.json()["error"]["details"]["fields"]] == [field]
    deadline = {"name": "G", "kind": "deadline", "estimated_total_hours": 2.0,
                "max_block_minutes": 60, "requirements": tags(MAX_TAGS)}  # fmt: skip
    resp = await client.post("/projects", json=deadline)
    assert "target_date" in {f["path"] for f in resp.json()["error"]["details"]["fields"]}


# ---------------------------------------------------------------- rows stored over a cap


def over_cap_items(pid: uuid.UUID) -> list[dict[str, Any]]:
    """A 21-tag project creation and preferences with 11 windows on Wednesday, as stored
    before the caps (the staged models don't cap them)."""
    over = prefs(schedulable_windows=WEEK | {"wed": windows(MAX_WINDOWS_PER_DAY + 1)})
    project = {
        "kind": "project_created",
        "payload": {
            "project_id": str(pid),
            "project": READING | {"requirements": tags(MAX_TAGS + 1)},
        },
    }
    return [over, project]


async def stored_staged(maker: Maker, cs: uuid.UUID) -> Any:
    return (await rows(maker, "SELECT staged FROM changesets WHERE id = :c", c=cs))[0][0]


async def test_stored_over_cap_changeset_confirms(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, world: uuid.UUID
) -> None:
    pid = uuid.uuid4()
    cs = await propose(maker, user_id, over_cap_items(pid), NOW)
    staged = parse_staged(await stored_staged(maker, cs))
    assert [m.kind for m in staged] == ["preferences_updated", "project_created"]
    resp = await client.post(f"/changesets/{cs}/confirm")
    assert resp.status_code == 200, resp.text
    project = (await client.get(f"/projects/{pid}")).json()
    assert project["requirements"] == tags(MAX_TAGS + 1)
    windows_wed = (await client.get("/preferences")).json()["schedulable_windows"]["wed"]
    assert len(windows_wed) == MAX_WINDOWS_PER_DAY + 1


async def test_stored_over_cap_changeset_is_carried_forward(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, world: uuid.UUID
) -> None:
    pid = uuid.uuid4()
    await propose(maker, user_id, over_cap_items(pid), NOW)
    resp = await client.patch(f"/projects/{pid}", json={"weekly_target_hours": 1.0})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    kinds = [m["kind"] for m in body["staged"]]
    assert kinds == ["preferences_updated", "project_created", "project_updated"]
    assert body["staged"][1]["payload"]["project"]["requirements"] == tags(MAX_TAGS + 1)
    assert (await client.post(f"/changesets/{body['id']}/confirm")).status_code == 200


async def test_stored_over_cap_changeset_then_valid_create(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, world: uuid.UUID
) -> None:
    old = await propose(maker, user_id, over_cap_items(uuid.uuid4()), NOW)
    resp = await client.post("/projects", json=READING)
    assert resp.status_code == 202, resp.text
    status = "SELECT status::text FROM changesets WHERE id = :c"
    assert await rows(maker, status, c=old) == [("expired",)]
