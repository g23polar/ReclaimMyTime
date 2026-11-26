"""Todos v1 (issue #10): CRUD per kind, the one-off and recurring rules, period validation,
no project link (issue #30), the 7-day done window, reorder, cascades, tenant isolation, and the
guarantee that no todo route writes a `mutations` row. The contract fixtures are in
test_contract.py."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Mutation, Todo, TodoCompletion, User, UserPreferences
from tests.api.conftest import SettableClock
from tests.api.contract import assert_conforms
from tests.db.conftest import requires_db
from tests.todos.story import (
    PASSPORT,
    REVIEW,
    STORY_NOW,
    STRETCH,
    add_todo,
    count,
    seed_story,
    tick,
)

pytestmark = requires_db
Maker = async_sessionmaker[AsyncSession]
TODAY = date(2026, 11, 24)  # the local date at STORY_NOW in America/Los_Angeles


@pytest.fixture
def at_story(clock: SettableClock) -> SettableClock:
    clock.current = STORY_NOW
    return clock


async def _other_user(maker: Maker) -> uuid.UUID:
    uid = uuid.uuid4()
    async with maker() as s, s.begin():
        await s.execute(insert(User).values(id=uid, created_at=STORY_NOW))
    return uid


def _ids(body: dict[str, Any]) -> set[str]:
    return {t["id"] for t in body["todos"]}


async def _create(client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post("/todos", json=body)
    assert resp.status_code == 201, resp.text
    assert_conforms("createTodo", 201, resp.json())
    return resp.json()


async def _error(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code, resp.text
    return resp.json()["error"]


# ---------------------------------------------------------------- CRUD per kind


async def test_crud_for_each_kind(client: httpx.AsyncClient, at_story: SettableClock) -> None:
    daily = await _create(client, {"kind": "daily", "title": "Water", "notes": "2 litres"})
    weekly = await _create(client, {"kind": "weekly", "title": "Plan"})
    one_off = await _create(client, {"kind": "one_off", "title": "Tax", "due_date": "2026-12-01"})
    assert (daily["kind"], weekly["kind"], one_off["kind"]) == ("daily", "weekly", "one_off")
    assert daily["notes"] == "2 litres" and daily["completed_periods"] == []

    patched = await client.patch(
        f"/todos/{weekly['id']}", json={"title": "Plan week", "notes": "x"}
    )
    assert patched.status_code == 200
    assert (patched.json()["title"], patched.json()["notes"]) == ("Plan week", "x")

    listed = (await client.get("/todos")).json()
    assert {daily["id"], weekly["id"], one_off["id"]} <= _ids(listed)
    assert listed["periods"] == {"daily": "2026-11-24", "weekly": "2026-11-23"}

    gone = await client.delete(f"/todos/{daily['id']}")
    assert gone.status_code == 204
    assert daily["id"] not in _ids((await client.get("/todos")).json())


async def test_kind_is_immutable(client: httpx.AsyncClient, at_story: SettableClock) -> None:
    todo = await _create(client, {"kind": "daily", "title": "Water"})
    resp = await client.patch(f"/todos/{todo['id']}", json={"kind": "one_off"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


# ---------------------------------------------------------------- defaults and due dates


async def test_defaults_one_off_due_today_recurring_never_due(
    client: httpx.AsyncClient, at_story: SettableClock
) -> None:
    bare = await _create(client, {"title": "Call dentist"})
    assert (bare["kind"], bare["due_date"], bare["notes"]) == ("one_off", "2026-11-24", None)

    explicit_null = await _create(client, {"title": "Someday", "due_date": None})
    assert explicit_null["due_date"] is None

    daily = await _create(client, {"kind": "daily", "title": "Stretch"})
    assert daily["due_date"] is None


async def test_default_due_date_uses_the_device_local_day(
    client: httpx.AsyncClient, clock: SettableClock
) -> None:
    """07:30 UTC on the 24th is 23:30 on the 23rd in Los Angeles: the default follows X-Timezone."""
    clock.current = datetime(2026, 11, 24, 7, 30, tzinfo=UTC)
    todo = await _create(client, {"title": "Late night"})
    assert todo["due_date"] == "2026-11-23"


async def test_recurring_with_a_due_date_is_422_on_create(
    client: httpx.AsyncClient, at_story: SettableClock
) -> None:
    resp = await client.post(
        "/todos", json={"kind": "weekly", "title": "X", "due_date": "2026-12-01"}
    )
    err = await _error(resp, 422, "validation_failed")
    assert err["details"]["fields"][0]["path"] == "due_date"


async def test_create_rejects_unknown_and_extra_fields(
    client: httpx.AsyncClient, at_story: SettableClock
) -> None:
    await _error(
        await client.post("/todos", json={"kind": "monthly", "title": "X"}),
        422,
        "validation_failed",
    )
    await _error(
        await client.post("/todos", json={"title": "X", "done": True}), 422, "validation_failed"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("title", ""), ("title", "x" * 201), ("notes", "x" * 2001)],
)
async def test_length_limits(
    client: httpx.AsyncClient, at_story: SettableClock, field: str, value: str
) -> None:
    body = {"title": "ok", field: value}
    await _error(await client.post("/todos", json=body), 422, "validation_failed")


async def test_length_limits_at_the_boundary_are_accepted(
    client: httpx.AsyncClient, at_story: SettableClock
) -> None:
    todo = await _create(client, {"title": "x" * 200, "notes": "y" * 2000})
    assert len(todo["title"]) == 200 and len(todo["notes"]) == 2000


# ---------------------------------------------------------------- recurring vs one-off rules


async def test_done_or_due_on_recurring_is_409(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    await seed_story(maker, user_id)
    await _error(
        await client.patch(f"/todos/{STRETCH}", json={"done": True}), 409, "todo_recurring"
    )
    await _error(
        await client.patch(f"/todos/{REVIEW}", json={"due_date": "2026-12-01"}),
        409,
        "todo_recurring",
    )
    # Clearing a due date that a recurring todo never has is a harmless no-op.
    resp = await client.patch(f"/todos/{REVIEW}", json={"due_date": None})
    assert resp.status_code == 200 and resp.json()["due_date"] is None


async def test_one_off_done_sets_and_clears_completed_at(
    client: httpx.AsyncClient, clock: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    clock.current = STORY_NOW
    await seed_story(maker, user_id)
    first = await client.patch(f"/todos/{PASSPORT}", json={"done": True})
    assert first.status_code == 200 and first.json()["completed_at"] is not None
    stamp = first.json()["completed_at"]

    clock.current = STORY_NOW + timedelta(hours=1)
    again = await client.patch(f"/todos/{PASSPORT}", json={"done": True})
    assert again.json()["completed_at"] == stamp  # the first completion time is kept

    undone = await client.patch(f"/todos/{PASSPORT}", json={"done": False})
    assert undone.json()["completed_at"] is None


async def test_completion_route_on_one_off_is_409(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    await seed_story(maker, user_id)
    form = await add_todo(maker, user_id, title="Sign the form", position=1)
    resp = await client.put(f"/todos/{form}/completions/{TODAY.isoformat()}")
    await _error(resp, 409, "todo_not_recurring")


async def test_null_title_and_done_are_422(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    await seed_story(maker, user_id)
    form = await add_todo(maker, user_id, title="Sign the form", position=1)
    await _error(
        await client.patch(f"/todos/{form}", json={"title": None}), 422, "validation_failed"
    )
    await _error(
        await client.patch(f"/todos/{form}", json={"done": None}), 422, "validation_failed"
    )


async def test_empty_patch_is_422(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    await seed_story(maker, user_id)
    form = await add_todo(maker, user_id, title="Sign the form", position=1)
    await _error(await client.patch(f"/todos/{form}", json={}), 422, "validation_failed")


# ---------------------------------------------------------------- period completions


@pytest.mark.parametrize(
    "period",
    [
        "2026-11-25",  # a Wednesday, not a week start
        "2026-11-09",  # two weeks back
        "2026-11-30",  # next week's start, in the future
    ],
)
async def test_weekly_invalid_periods(
    client: httpx.AsyncClient,
    at_story: SettableClock,
    maker: Maker,
    period: str,
    user_id: uuid.UUID,
) -> None:
    await seed_story(maker, user_id)
    err = await _error(
        await client.put(f"/todos/{REVIEW}/completions/{period}"), 422, "invalid_period"
    )
    assert err["details"]["allowed"] == ["2026-11-23", "2026-11-16"]


@pytest.mark.parametrize("period", ["2026-11-22", "2026-11-25"])
async def test_daily_invalid_periods(
    client: httpx.AsyncClient,
    at_story: SettableClock,
    maker: Maker,
    period: str,
    user_id: uuid.UUID,
) -> None:
    await seed_story(maker, user_id)
    await _error(await client.put(f"/todos/{STRETCH}/completions/{period}"), 422, "invalid_period")
    await _error(
        await client.delete(f"/todos/{STRETCH}/completions/{period}"), 422, "invalid_period"
    )


async def test_malformed_period_is_422(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    await seed_story(maker, user_id)
    resp = await client.put(f"/todos/{REVIEW}/completions/not-a-date")
    assert resp.status_code == 422


async def test_complete_and_uncomplete_are_idempotent(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    await seed_story(maker, user_id)
    url = f"/todos/{REVIEW}/completions/2026-11-23"
    for _ in range(2):
        resp = await client.put(url)
        assert resp.status_code == 200 and resp.json()["completed_periods"] == ["2026-11-23"]
    assert await count(maker, TodoCompletion, TodoCompletion.todo_id == REVIEW) == 1

    for _ in range(2):  # the second delete finds nothing and still succeeds
        resp = await client.delete(url)
        assert resp.status_code == 200 and resp.json()["completed_periods"] == []


async def test_completed_periods_show_only_current_and_previous(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    uid = user_id
    await seed_story(maker, uid)
    await tick(maker, REVIEW, date(2026, 11, 16))  # previous week
    await tick(maker, REVIEW, date(2026, 11, 9))  # older: stored, not reported
    listed = (await client.get("/todos")).json()["todos"]
    review = next(t for t in listed if t["id"] == str(REVIEW))
    assert review["completed_periods"] == ["2026-11-16"]


async def test_week_start_change_stops_old_weekly_ticks_counting(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    """The accepted v1 gap: completions are not re-keyed when week_start changes. A Monday tick
    stops counting once the user's week starts on Sunday (documented in app.todos.periods)."""
    uid = user_id
    await seed_story(maker, uid)
    async with maker() as s, s.begin():
        s.add(
            UserPreferences(
                user_id=uid,
                timezone="America/Los_Angeles",
                schedulable_windows={},
                week_start=1,
            )
        )
    await client.put(f"/todos/{REVIEW}/completions/2026-11-23")
    async with maker() as s, s.begin():
        await s.execute(
            update(UserPreferences).where(UserPreferences.user_id == uid).values(week_start=7)
        )
    listed = (await client.get("/todos")).json()
    review = next(t for t in listed["todos"] if t["id"] == str(REVIEW))
    assert listed["periods"]["weekly"] == "2026-11-22"
    assert review["completed_periods"] == []


# ---------------------------------------------------------------- listing window


async def test_done_one_offs_drop_out_after_seven_days_unless_asked(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    uid = user_id
    recent = await add_todo(maker, uid, title="Recent", completed_at=STORY_NOW - timedelta(days=2))
    old = await add_todo(maker, uid, title="Old", completed_at=STORY_NOW - timedelta(days=8))
    open_one = await add_todo(maker, uid, title="Open")

    default = _ids((await client.get("/todos")).json())
    assert str(recent) in default and str(open_one) in default and str(old) not in default

    everything = _ids((await client.get("/todos", params={"include_done": "true"})).json())
    assert {str(recent), str(old), str(open_one)} <= everything


# ---------------------------------------------------------------- order and positions


async def test_create_appends_within_the_kind(
    client: httpx.AsyncClient, at_story: SettableClock
) -> None:
    first = await _create(client, {"kind": "one_off", "title": "A"})
    second = await _create(client, {"kind": "one_off", "title": "B"})
    daily = await _create(client, {"kind": "daily", "title": "C"})
    assert (first["position"], second["position"], daily["position"]) == (0, 1, 0)


async def test_reorder_success_and_rejections(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    uid = user_id
    await seed_story(maker, uid)
    form = await add_todo(maker, uid, title="Sign the form", position=1)
    third = await add_todo(maker, uid, title="Third", position=2)
    url = "/todos/order"

    ok = await client.put(
        url, json={"kind": "one_off", "ids": [str(third), str(form), str(PASSPORT)]}
    )
    assert ok.status_code == 200
    one_offs = [
        t for t in ok.json()["todos"] if t["kind"] == "one_off" and t["completed_at"] is None
    ]
    assert [t["id"] for t in one_offs] == [str(third), str(form), str(PASSPORT)]
    assert [t["position"] for t in one_offs] == [0, 1, 2]

    missing = await client.put(url, json={"kind": "one_off", "ids": [str(form), str(PASSPORT)]})
    assert (await _error(missing, 422, "invalid_order"))["details"]["missing"] == [str(third)]

    extra = await client.put(
        url,
        json={"kind": "one_off", "ids": [str(form), str(PASSPORT), str(third), str(uuid.uuid4())]},
    )
    assert len((await _error(extra, 422, "invalid_order"))["details"]["extra"]) == 1

    wrong_kind = await client.put(
        url, json={"kind": "one_off", "ids": [str(form), str(PASSPORT), str(third), str(STRETCH)]}
    )
    err = await _error(wrong_kind, 422, "invalid_order")
    assert err["details"]["extra"] == [str(STRETCH)]

    duplicate = await client.put(url, json={"kind": "one_off", "ids": [str(form), str(form)]})
    await _error(duplicate, 422, "validation_failed")


# ---------------------------------------------------------------- deletes and cascades


async def test_delete_cascades_to_completions(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    uid = user_id
    await seed_story(maker, uid)
    await client.put(f"/todos/{REVIEW}/completions/2026-11-23")
    assert await count(maker, TodoCompletion, TodoCompletion.todo_id == REVIEW) == 1
    assert (await client.delete(f"/todos/{REVIEW}")).status_code == 204
    assert await count(maker, TodoCompletion, TodoCompletion.todo_id == REVIEW) == 0


# ---------------------------------------------------------------- tenant isolation


async def test_a_foreign_todo_is_404_on_every_route(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker
) -> None:
    other = await _other_user(maker)
    theirs = await add_todo(maker, other, kind="daily", title="Theirs")
    theirs_one_off = await add_todo(maker, other, title="Theirs too")
    for method, path, body in (
        ("PATCH", f"/todos/{theirs}", {"title": "mine now"}),
        ("DELETE", f"/todos/{theirs}", None),
        ("PUT", f"/todos/{theirs}/completions/{TODAY.isoformat()}", None),
        ("DELETE", f"/todos/{theirs}/completions/{TODAY.isoformat()}", None),
        ("PATCH", f"/todos/{theirs_one_off}", {"done": True}),
    ):
        resp = await client.request(method, path, json=body)
        assert resp.status_code == 404, (method, path, resp.text)
        assert resp.json()["error"]["code"] == "not_found"
    # Their rows are untouched.
    assert await count(maker, Todo, Todo.id == theirs, Todo.title == "Theirs") == 1


async def test_reorder_cannot_touch_another_users_todos(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker
) -> None:
    other = await _other_user(maker)
    theirs = await add_todo(maker, other, title="Theirs")
    await _create(client, {"title": "Mine"})
    resp = await client.put("/todos/order", json={"kind": "one_off", "ids": [str(theirs)]})
    await _error(resp, 422, "invalid_order")


# ---------------------------------------------------------------- no project link (issue #30)


@pytest.mark.parametrize("project_id", [str(uuid.uuid4()), None])
async def test_create_with_project_id_is_refused(
    client: httpx.AsyncClient,
    at_story: SettableClock,
    maker: Maker,
    user_id: uuid.UUID,
    project_id: str | None,
) -> None:
    resp = await client.post("/todos", json={"title": "x", "project_id": project_id})
    await _error(resp, 422, "validation_failed")
    assert (await client.get("/todos")).json()["todos"] == []
    assert await count(maker, Todo, Todo.user_id == user_id) == 0


@pytest.mark.parametrize("project_id", [str(uuid.uuid4()), None])
async def test_patch_with_project_id_is_refused(
    client: httpx.AsyncClient, at_story: SettableClock, project_id: str | None
) -> None:
    todo = await _create(client, {"title": "Mine"})
    resp = await client.patch(
        f"/todos/{todo['id']}", json={"title": "Changed", "project_id": project_id}
    )
    await _error(resp, 422, "validation_failed")
    listed = (await client.get("/todos")).json()["todos"]
    assert listed == [todo]


async def test_responses_carry_no_project_fields(
    client: httpx.AsyncClient, at_story: SettableClock
) -> None:
    created = await _create(client, {"title": "Mine"})
    patched = (await client.patch(f"/todos/{created['id']}", json={"title": "Mine 2"})).json()
    listed = (await client.get("/todos")).json()["todos"]
    for todo in (created, patched, *listed):
        assert not {"project_id", "project_archived"} & todo.keys()


# ---------------------------------------------------------------- writes and headers


async def test_no_todo_route_writes_a_mutations_row(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    uid = user_id
    await seed_story(maker, uid)
    form = await add_todo(maker, uid, title="Sign the form", position=1)
    before = await count(maker, Mutation, Mutation.user_id == uid)
    created = await _create(client, {"kind": "weekly", "title": "Log"})
    await client.patch(f"/todos/{created['id']}", json={"title": "Log 2"})
    await client.put(f"/todos/{created['id']}/completions/2026-11-23")
    await client.delete(f"/todos/{created['id']}/completions/2026-11-23")
    await client.put("/todos/order", json={"kind": "one_off", "ids": [str(form), str(PASSPORT)]})
    await client.patch(f"/todos/{PASSPORT}", json={"done": True})
    await client.delete(f"/todos/{created['id']}")
    assert await count(maker, Mutation, Mutation.user_id == uid) == before


async def test_missing_timezone_is_400(client: httpx.AsyncClient, at_story: SettableClock) -> None:
    resp = await client.get("/todos", headers={"X-Timezone": ""})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "timezone_invalid"


# ------------------------------------------------- open-set reorder (owner decision, 2026-11-24)


async def test_reorder_covers_only_open_items(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    """A done one-off is outside the manual order: not in the id set, position kept."""
    await seed_story(maker, user_id)
    form = await add_todo(maker, user_id, title="Sign the form", position=1)
    done = await add_todo(
        maker, user_id, title="Done", position=2, completed_at=STORY_NOW - timedelta(days=1)
    )
    resp = await client.put(
        "/todos/order", json={"kind": "one_off", "ids": [str(form), str(PASSPORT)]}
    )
    assert resp.status_code == 200, resp.text
    by_id = {t["id"]: t for t in resp.json()["todos"]}
    assert (by_id[str(form)]["position"], by_id[str(PASSPORT)]["position"]) == (0, 1)
    assert by_id[str(done)]["position"] == 2


async def test_reorder_including_a_done_one_off_is_422(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    await seed_story(maker, user_id)
    form = await add_todo(maker, user_id, title="Sign the form", position=1)
    done = await add_todo(
        maker, user_id, title="Done", position=2, completed_at=STORY_NOW - timedelta(days=1)
    )
    with_done = await client.put(
        "/todos/order",
        json={"kind": "one_off", "ids": [str(form), str(PASSPORT), str(done)]},
    )
    err = await _error(with_done, 422, "invalid_order")
    assert err["details"] == {"kind": "one_off", "missing": [], "extra": [str(done)]}

    without_open = await client.put("/todos/order", json={"kind": "one_off", "ids": [str(form)]})
    err = await _error(without_open, 422, "invalid_order")
    assert err["details"]["missing"] == [str(PASSPORT)]


async def test_undoing_a_one_off_appends_to_the_open_order(
    client: httpx.AsyncClient, at_story: SettableClock, maker: Maker, user_id: uuid.UUID
) -> None:
    """Marking done keeps the position; undoing moves the one-off to max(open position)+1.
    With nothing open, the undone one-off goes to 0."""
    await seed_story(maker, user_id)  # PASSPORT open at 0, form open at 1
    form = await add_todo(maker, user_id, title="Sign the form", position=1)
    done = await client.patch(f"/todos/{PASSPORT}", json={"done": True})
    assert done.json()["position"] == 0  # done keeps its position

    undone = await client.patch(f"/todos/{PASSPORT}", json={"done": False})
    assert undone.json()["position"] == 2  # open max is form at 1

    await client.patch(f"/todos/{form}", json={"done": True})
    await client.patch(f"/todos/{PASSPORT}", json={"done": True})
    alone = await client.patch(f"/todos/{form}", json={"done": False})
    assert alone.json()["position"] == 0  # nothing else is open
