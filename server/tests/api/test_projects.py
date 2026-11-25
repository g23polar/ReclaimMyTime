"""Projects beyond the fixtures: name-only direct apply, archived conflicts, D23 visibility, O9
carry-forward of a pending creation, and request validation."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.contract import assert_conforms
from tests.db.conftest import PORTFOLIO, WORKOUT, requires_db, rows

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]
READING = {
    "name": "Reading",
    "kind": "perennial",
    "weekly_target_hours": 2.0,
    "max_block_minutes": 60,
}


async def _archive_workout(client: httpx.AsyncClient) -> None:
    cs = (await client.delete(f"/projects/{WORKOUT}")).json()["id"]
    assert (await client.post(f"/changesets/{cs}/confirm")).status_code == 200


async def test_name_only_patch_applies_directly(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID
) -> None:
    resp = await client.patch(f"/projects/{WORKOUT}", json={"name": "Gym"})
    assert resp.status_code == 200, resp.text
    assert_conforms("updateProject", 200, resp.json())
    assert resp.json()["name"] == "Gym"
    assert await rows(maker, "SELECT count(*) FROM changesets WHERE status = 'pending'") == [(0,)]
    last = "SELECT kind::text, source::text, changeset_id FROM mutations"
    trace = await rows(maker, last + " ORDER BY created_at DESC LIMIT 1")
    assert trace == [("project_updated", "manual", None)]


async def test_name_with_scheduling_fields_is_staged(
    client: httpx.AsyncClient, world: uuid.UUID
) -> None:
    resp = await client.patch(f"/projects/{WORKOUT}", json={"name": "Gym", "max_block_minutes": 45})
    assert resp.status_code == 202
    changes = resp.json()["staged"][0]["payload"]["changes"]
    assert changes == {"name": "Gym", "max_block_minutes": 45}
    assert (await client.get(f"/projects/{WORKOUT}")).json()["name"] == "Workout"


async def test_archived_project_conflicts_and_visibility(
    client: httpx.AsyncClient, world: uuid.UUID
) -> None:
    await _archive_workout(client)
    for method, body in (("PATCH", {"weekly_target_hours": 1.0}), ("DELETE", None)):
        resp = await client.request(method, f"/projects/{WORKOUT}", json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (409, "project_archived")
    ids = [p["id"] for p in (await client.get("/projects")).json()["projects"]]
    assert ids == [str(PORTFOLIO)]
    every = await client.get("/projects", params={"include_archived": "true"})
    assert {p["id"]: p["status"] for p in every.json()["projects"]}[str(WORKOUT)] == "archived"
    assert (await client.get(f"/projects/{WORKOUT}")).json()["status"] == "archived"


async def test_patch_on_a_pending_creation_carries_it_forward(
    client: httpx.AsyncClient, maker: Maker, world: uuid.UUID
) -> None:
    created = (await client.post("/projects", json=READING)).json()
    pid = created["staged"][0]["payload"]["project_id"]
    assert (await client.get(f"/projects/{pid}")).status_code == 404  # D23
    resp = await client.patch(f"/projects/{pid}", json={"weekly_target_hours": 1.0})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert [m["kind"] for m in body["staged"]] == ["project_created", "project_updated"]
    status = "SELECT status::text FROM changesets WHERE id = :c"
    assert await rows(maker, status, c=uuid.UUID(created["id"])) == [("expired",)]
    assert (await client.post(f"/changesets/{body['id']}/confirm")).status_code == 200
    project = (await client.get(f"/projects/{pid}")).json()
    assert (project["status"], project["weekly_target_hours"]) == ("active", 1.0)


async def test_unknown_project_is_404(client: httpx.AsyncClient, world: uuid.UUID) -> None:
    for method in ("PATCH", "DELETE"):
        body = {"name": "x"} if method == "PATCH" else None
        resp = await client.request(method, f"/projects/{uuid.uuid4()}", json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "not_found")


async def test_request_validation(client: httpx.AsyncClient, world: uuid.UUID) -> None:
    bad_patches = [{}, {"kind": "deadline"}, {"status": "archived"}, {"name": None}]
    for body in bad_patches:
        resp = await client.patch(f"/projects/{WORKOUT}", json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "validation_failed"), body
    inverted = READING | {"min_block_minutes": 90}
    resp = await client.post("/projects", json=inverted)
    assert resp.status_code == 422
    resp = await client.patch(f"/projects/{WORKOUT}", json={"min_block_minutes": 90})
    assert resp.status_code == 422  # merged min > max (semantic, from the overlay)
    json_header = {"Content-Type": "application/json"}
    resp = await client.post("/projects", content=b"{not json", headers=json_header)
    assert (resp.status_code, resp.json()["error"]["code"]) == (400, "bad_request")


async def test_hours_round_to_one_decimal(client: httpx.AsyncClient, world: uuid.UUID) -> None:
    resp = await client.post("/projects", json=READING | {"weekly_target_hours": 2.04})
    assert resp.status_code == 202
    assert resp.json()["staged"][0]["payload"]["project"]["weekly_target_hours"] == 2.0


async def test_new_tag_past_the_tag_cap_is_422(
    client: httpx.AsyncClient, maker: Maker, user_id: uuid.UUID, world: uuid.UUID
) -> None:
    async with maker() as s, s.begin():
        for i in range(49):  # + "gym" from the onboarding = 50
            insert = "INSERT INTO user_tags (user_id, tag) VALUES (:u, :t)"
            await s.execute(text(insert), {"u": user_id, "t": f"t{i:02}"})
    requests = [
        ("POST", "/projects", READING | {"requirements": ["gym", "music"]}),
        ("PATCH", f"/projects/{WORKOUT}", {"requirements": ["music"]}),
    ]
    for method, path, body in requests:
        resp = await client.request(method, path, json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "validation_failed")
        assert "at most 50 tags" in resp.json()["error"]["message"]
    assert await rows(maker, "SELECT count(*) FROM changesets WHERE status = 'pending'") == [(0,)]
    assert await rows(maker, "SELECT count(*) FROM user_tags") == [(50,)]
    resp = await client.post("/projects", json=READING | {"requirements": ["gym", "t48"]})
    assert resp.status_code == 202, resp.text
