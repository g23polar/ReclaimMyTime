"""Contract tests (TECH_SPEC T2.3 acceptance): every contracts/fixtures/*.json of an in-scope
operation is driven against the live route on Postgres, with state seeded so the call is
meaningful. Asserted: the fixture's status code, the response body validates against the
openapi schema (formats on), the fixture's error code, and key semantics per case. Exact
equality with the fixture is not expected (ids and times differ)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.conftest import NOW, SettableClock, future_block, started_block
from tests.api.contract import assert_conforms, in_scope_fixtures, load_fixture
from tests.db.conftest import (
    PORTFOLIO,
    WORKOUT,
    at,
    bootstrap,
    confirm,
    exception_added,
    propose,
    reject,
    requires_db,
    rows,
)
from tests.gcal.calendar_fakes import FakeCalendar, install, new_cipher, seed_connection
from tests.gcal.fakes import FakeGoogle, Reply
from tests.todos.story import (
    LIBRARY,
    PASSPORT,
    REVIEW,
    STAGED,
    STORY_NOW,
    STRETCH,
    seed_story,
)

DEAD = "00000000-0000-4000-8000-00000000dead"
LAPTOP_EXC = uuid.UUID("e7c2a9d4-3f1b-4e6a-8d20-5b9c1a7e3f01")


@dataclass
class Ctx:
    client: httpx.AsyncClient
    maker: async_sessionmaker[AsyncSession]
    uid: uuid.UUID
    clock: SettableClock
    fixture: dict[str, Any]
    app: FastAPI | None = None
    checked: list[int] = field(default_factory=list)

    @property
    def body(self) -> Any:
        return self.fixture["request"].get("body")

    async def call(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        resp = await self.client.request(method, path, json=body, params=params or None)
        return self.expect(resp)

    def expect(self, resp: httpx.Response) -> Any:
        want = self.fixture["response"]
        assert resp.status_code == want["status"], resp.text
        body = resp.json()
        assert_conforms(self.fixture["operation"], resp.status_code, body)
        if "error" in (want.get("body") or {}):
            assert body["error"]["code"] == want["body"]["error"]["code"]
        self.checked.append(resp.status_code)
        return body


async def onboarded(ctx: Ctx) -> None:
    await bootstrap(ctx.maker, ctx.uid)
    ctx.clock.current = NOW


async def with_laptop_exception(ctx: Ctx) -> tuple[uuid.UUID, uuid.UUID]:
    """Onboarded, then 'no laptop until Thu 00:00 local' confirmed at NOW. Returns
    (old live plan id, new live plan id)."""
    old = await bootstrap(ctx.maker, ctx.uid)
    ctx.clock.current = NOW
    exc = exception_added(NOW, at(3, "00:00"), LAPTOP_EXC)
    exc["payload"] |= {"blocks_all": False, "blocks_tags": ["laptop"]}
    result = await confirm(ctx.maker, ctx.uid, await propose(ctx.maker, ctx.uid, [exc], NOW), NOW)
    return old, result.plan_id


async def pending(ctx: Ctx) -> str:
    """A pending manual changeset (archive Workout) staged through the API."""
    resp = await ctx.client.delete(f"/projects/{WORKOUT}")
    assert resp.status_code == 202, resp.text
    return resp.json()["id"]


def assert_staged(body: dict[str, Any], kind: str) -> dict[str, Any]:
    assert body["status"] == "pending"
    assert [m["kind"] for m in body["staged"]] == [kind]
    assert body["staged"][0]["source"] == "manual"
    if body["plan"] is not None:
        assert body["plan"]["status"] == "proposed"
        assert body["plan_id"] == body["plan"]["id"] == body["diff"]["plan_id"]
        after = {e["lineage_id"]: e["after"] for e in body["diff"]["entries"] if e["after"]}
        assert after == {b["lineage_id"]: b["span"] for b in body["plan"]["blocks"]}
    return body["staged"][0]["payload"]


Case = Callable[[Ctx], Awaitable[None]]
CASES: dict[str, Case] = {}


def case(name: str) -> Callable[[Case], Case]:
    def register(fn: Case) -> Case:
        CASES[name] = fn
        return fn

    return register


# ---------------------------------------------------------------- projects


@case("get_projects__ok.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    await pending(ctx)  # nothing staged is visible
    body = await ctx.call("GET", "/projects")
    assert {p["id"] for p in body["projects"]} == {str(WORKOUT), str(PORTFOLIO)}


@case("get_projects_id__deadline.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("GET", f"/projects/{PORTFOLIO}")
    assert (body["kind"], body["weekly_target_hours"]) == ("deadline", None)
    assert body["estimated_total_hours"] == 6.0 and body["target_date"] == "2026-11-30"


@case("get_projects_id__perennial.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("GET", f"/projects/{WORKOUT}")
    assert (body["kind"], body["target_date"], body["weekly_target_hours"]) == (
        "perennial",
        None,
        3.0,
    )


@case("get_projects_id__not_found.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("GET", f"/projects/{DEAD}")
    assert body["error"]["details"] == {"resource": "project", "id": DEAD}


@case("post_projects__perennial_onboarding.json")
async def _(ctx: Ctx) -> None:
    body = await ctx.call("POST", "/projects", ctx.body)  # no preferences yet
    payload = assert_staged(body, "project_created")
    assert body["plan_id"] is None and body["plan"] is None and body["diff"] is None
    assert payload["project"]["name"] == "Reading"
    # D23: the staged project is invisible to reads.
    listed = await ctx.client.get("/projects")
    assert listed.json() == {"projects": []}
    assert (await ctx.client.get(f"/projects/{payload['project_id']}")).status_code == 404


@case("post_projects__deadline_infeasible.json")
async def _(ctx: Ctx) -> None:
    await with_laptop_exception(ctx)
    # Laptop work is now possible only Thu–Fri (by the Fri deadline): 20h cannot fit.
    exc = exception_added(at(3, "00:00"), at(4, "00:00"), uuid.uuid4())
    exc["payload"] |= {"blocks_all": False, "blocks_tags": ["laptop"]}
    await confirm(ctx.maker, ctx.uid, await propose(ctx.maker, ctx.uid, [exc], NOW), NOW)
    body = await ctx.call("POST", "/projects", ctx.body)
    payload = assert_staged(body, "project_created")
    infeasible = body["plan"]["infeasibility"]
    assert infeasible is not None
    shortfall = [v for v in infeasible["violations"] if v["kind"] == "demand_shortfall"]
    assert any(v["project_id"] == payload["project_id"] for v in shortfall)
    assert infeasible["relaxations"], "a trade-off must be offered (Principle 2)"


@case("post_projects__validation_failed.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("POST", "/projects", ctx.body)
    assert "target_date" in {f["path"] for f in body["error"]["details"]["fields"]}


@case("patch_projects_id__weekly_target.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("PATCH", f"/projects/{WORKOUT}", {"weekly_target_hours": 2.0})
    payload = assert_staged(body, "project_updated")
    assert payload == {"project_id": str(WORKOUT), "changes": {"weekly_target_hours": 2.0}}
    assert body["diff"]["against_plan_id"] is not None


@case("patch_projects_id__kind_mismatch.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("PATCH", f"/projects/{PORTFOLIO}", ctx.body)
    assert [f["path"] for f in body["error"]["details"]["fields"]] == ["weekly_target_hours"]


@case("delete_projects_id__archive.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("DELETE", f"/projects/{WORKOUT}")
    assert assert_staged(body, "project_archived") == {"project_id": str(WORKOUT)}
    future = [b for b in body["plan"]["blocks"] if b["span"]["start"] > "2026-11-17T18:30:00Z"]
    assert future and all(b["project_id"] != str(WORKOUT) for b in future)


async def connected_to_google(ctx: Ctx, google: FakeGoogle, cal: FakeCalendar) -> None:
    """T4.2: the user has a Google connection; Google answers per the fakes."""
    assert ctx.app is not None
    cipher = new_cipher()
    install(ctx.app, google, cal, cipher, ctx.clock)
    await seed_connection(ctx.maker, ctx.uid, cipher, now=NOW)


async def nothing_staged(ctx: Ctx) -> None:
    pending = await rows(ctx.maker, "SELECT count(*) FROM changesets WHERE status = 'pending'")
    staged_projects = await rows(ctx.maker, "SELECT count(*) FROM projects WHERE status = 'staged'")
    assert (pending, staged_projects) == ([(0,)], [(0,)])


@case("post_projects__gcal_unavailable.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    cal = FakeCalendar(freebusy=Reply(raise_exc=httpx.ReadTimeout("slow")))
    await connected_to_google(ctx, FakeGoogle(now=NOW), cal)
    await ctx.call("POST", "/projects", ctx.body)
    await nothing_staged(ctx)


@case("post_projects__gcal_reconnect_required.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    google = FakeGoogle(now=NOW, refresh=Reply(status=400, json={"error": "invalid_grant"}))
    await connected_to_google(ctx, google, FakeCalendar())
    await ctx.call("POST", "/projects", ctx.body)
    await nothing_staged(ctx)


# ---------------------------------------------------------------- preferences


@case("get_preferences__ok.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("GET", "/preferences")
    assert (body["timezone"], body["week_start"]) == ("America/Los_Angeles", 1)


@case("get_preferences__not_set.json")
async def _(ctx: Ctx) -> None:
    await ctx.call("GET", "/preferences")


@case("put_preferences__first_week.json")
async def _(ctx: Ctx) -> None:
    # Onboarding: a project confirmed without a plan, then preferences → the first solve.
    ctx.clock.current = NOW
    reading = await ctx.client.post(
        "/projects",
        json={"name": "Reading", "kind": "perennial", "weekly_target_hours": 2.0,
              "max_block_minutes": 60},
    )  # fmt: skip
    assert (await ctx.client.post(f"/changesets/{reading.json()['id']}/confirm")).status_code == 200
    body = await ctx.call("PUT", "/preferences", ctx.body)
    assert_staged(body, "preferences_updated")
    assert body["diff"]["against_plan_id"] is None
    assert body["diff"]["entries"] and {e["change"] for e in body["diff"]["entries"]} == {"added"}


# ---------------------------------------------------------------- exceptions


@case("get_exceptions__ok.json")
async def _(ctx: Ctx) -> None:
    await with_laptop_exception(ctx)
    body = await ctx.call("GET", "/exceptions")
    assert [e["id"] for e in body["exceptions"]] == [str(LAPTOP_EXC)]
    ctx.clock.current = at(4, "00:00")  # after it ended: hidden unless include_past
    assert (await ctx.client.get("/exceptions")).json() == {"exceptions": []}
    past = (await ctx.client.get("/exceptions", params={"include_past": "true"})).json()
    assert len(past["exceptions"]) == 1


@case("get_exceptions_id__laptop.json")
async def _(ctx: Ctx) -> None:
    await with_laptop_exception(ctx)
    body = await ctx.call("GET", f"/exceptions/{LAPTOP_EXC}")
    assert (body["blocks_all"], body["blocks_tags"]) == (False, ["laptop"])
    assert body["span"] == {"start": "2026-11-17T18:30:00Z", "end": "2026-11-19T08:00:00Z"}


@case("post_exceptions__traveling_friday.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("POST", "/exceptions", ctx.body)
    payload = assert_staged(body, "exception_added")
    # D10: local dates resolved with X-Timezone; omitted times = the whole local day.
    assert payload["span"] == {"start": "2026-11-20T08:00:00Z", "end": "2026-11-21T08:00:00Z"}
    friday = [
        b for b in body["plan"]["blocks"] if "2026-11-20T08" <= b["span"]["start"] < "2026-11-21T08"
    ]
    assert friday == []


@case("post_exceptions__no_tags.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("POST", "/exceptions", ctx.body)
    assert [f["path"] for f in body["error"]["details"]["fields"]] == ["blocks_tags"]


@case("delete_exceptions_id__laptop.json")
async def _(ctx: Ctx) -> None:
    await with_laptop_exception(ctx)
    body = await ctx.call("DELETE", f"/exceptions/{LAPTOP_EXC}")
    assert assert_staged(body, "exception_removed") == {"exception_id": str(LAPTOP_EXC)}


# ---------------------------------------------------------------- pins


async def confirmed_forbid(ctx: Ctx) -> str:
    """Delete a future block (→ forbid) and confirm it. Returns the pin id."""
    await onboarded(ctx)
    block = await future_block(ctx.maker, ctx.uid)
    staged = (await ctx.client.patch(f"/blocks/{block.id}", json={"action": "delete"})).json()
    assert (await ctx.client.post(f"/changesets/{staged['id']}/confirm")).status_code == 200
    return staged["staged"][0]["payload"]["pin_id"]


@case("get_pins__ok.json")
async def _(ctx: Ctx) -> None:
    pin_id = await confirmed_forbid(ctx)
    body = await ctx.call("GET", "/pins")
    assert [(p["id"], p["kind"]) for p in body["pins"]] == [(pin_id, "forbid")]


@case("delete_pins_id__forbid.json")
async def _(ctx: Ctx) -> None:
    pin_id = await confirmed_forbid(ctx)
    body = await ctx.call("DELETE", f"/pins/{pin_id}")
    assert assert_staged(body, "pin_removed") == {"pin_id": pin_id}


@case("delete_pins_id__not_found.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("DELETE", f"/pins/{DEAD}")
    assert body["error"]["details"] == {"resource": "pin", "id": DEAD}


# ---------------------------------------------------------------- plans


@case("get_plans_current__ok.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    body = await ctx.call("GET", "/plans/current")
    assert body["status"] == "live" and body["blocks"]
    # Lazy sweep on read: every block that ended by now is `completed`.
    ended = [b for b in body["blocks"] if b["span"]["end"] <= "2026-11-17T18:30:00Z"]
    assert ended and {b["state"] for b in ended} == {"completed"}


@case("get_plans_current__no_live_plan.json")
async def _(ctx: Ctx) -> None:
    await ctx.call("GET", "/plans/current")


@case("get_plans_id_diff__laptop.json")
async def _(ctx: Ctx) -> None:
    old, new = await with_laptop_exception(ctx)
    body = await ctx.call("GET", f"/plans/{new}/diff", against=str(old))
    assert (body["plan_id"], body["against_plan_id"]) == (str(new), str(old))
    assert "unchanged" in {e["change"] for e in body["entries"]}


@case("get_plans_id_diff__not_found.json")
async def _(ctx: Ctx) -> None:
    plan = await bootstrap(ctx.maker, ctx.uid)
    body = await ctx.call("GET", f"/plans/{plan}/diff", against=DEAD)
    assert body["error"]["details"] == {"resource": "plan", "id": DEAD}


# ---------------------------------------------------------------- blocks


@case("patch_blocks_id__complete.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    block = await started_block(ctx.maker, ctx.uid)
    body = await ctx.call("PATCH", f"/blocks/{block.id}", ctx.body)
    assert (body["id"], body["state"]) == (str(block.id), "completed")


@case("patch_blocks_id__skip.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    block = await started_block(ctx.maker, ctx.uid)
    body = await ctx.call("PATCH", f"/blocks/{block.id}", ctx.body)
    assert (body["id"], body["state"]) == (str(block.id), "skipped")


@case("patch_blocks_id__skip_not_started.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    block = await future_block(ctx.maker, ctx.uid)
    await ctx.call("PATCH", f"/blocks/{block.id}", ctx.body)


@case("patch_blocks_id__edit_immutable.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    block = await started_block(ctx.maker, ctx.uid)
    await ctx.call("PATCH", f"/blocks/{block.id}", ctx.body)


@case("patch_blocks_id__edit_pin.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    block = await future_block(ctx.maker, ctx.uid)
    body = await ctx.call("PATCH", f"/blocks/{block.id}", ctx.body)
    payload = assert_staged(body, "pin_added")
    assert payload["kind"] == "pin" and payload["span"] == ctx.body["span"]
    assert payload["project_id"] == str(block.project_id)
    assert payload["created_from_block_id"] == str(block.id)
    # Re-solved around the pin: the block's project sits exactly on the pinned span.
    assert any(
        b["span"] == ctx.body["span"] and b["project_id"] == str(block.project_id)
        for b in body["plan"]["blocks"]
    )


@case("patch_blocks_id__delete_forbid.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    block = await future_block(ctx.maker, ctx.uid)
    body = await ctx.call("PATCH", f"/blocks/{block.id}", ctx.body)
    payload = assert_staged(body, "pin_added")
    assert (payload["kind"], payload["project_id"]) == ("forbid", str(block.project_id))
    assert payload["span"] == {
        "start": block.span.lower.isoformat().replace("+00:00", "Z"),
        "end": block.span.upper.isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------- changesets


@case("post_changesets_id_confirm__ok.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    cs = await pending(ctx)
    body = await ctx.call("POST", f"/changesets/{cs}/confirm")
    assert body["status"] == "confirmed" and body["plan"]["status"] == "live"
    assert body["resolved_at"] is not None and body["diff"]["against_plan_id"] is not None
    replay = await ctx.call("POST", f"/changesets/{cs}/confirm")
    assert replay == body  # idempotent: same body


@case("post_changesets_id_confirm__not_pending.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    cs = await pending(ctx)
    await reject(ctx.maker, ctx.uid, uuid.UUID(cs), NOW)
    body = await ctx.call("POST", f"/changesets/{cs}/confirm")
    assert body["error"]["details"] == {"status": "rejected"}


@case("post_changesets_id_reject__ok.json")
async def _(ctx: Ctx) -> None:
    await onboarded(ctx)
    cs = await pending(ctx)
    body = await ctx.call("POST", f"/changesets/{cs}/reject")
    assert body["status"] == "rejected" and body["plan"]["status"] == "rejected"
    assert await ctx.call("POST", f"/changesets/{cs}/reject") == body


# ---------------------------------------------------------------- todos (Todos v1, #10)


async def todo_story(ctx: Ctx) -> None:
    """The fixture story, at its clock (Tue 2026-11-24, LA). No preferences row: the week
    starts Monday by default, which is what the fixtures assume."""
    await seed_story(ctx.maker, ctx.uid)
    ctx.clock.current = STORY_NOW


@case("get_todos__ok.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("GET", "/todos")
    assert body["periods"] == {"daily": "2026-11-24", "weekly": "2026-11-23"}
    stretch = next(t for t in body["todos"] if t["id"] == str(STRETCH))
    assert stretch["completed_periods"] == ["2026-11-24"]


@case("post_todos__ok.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call(
        "POST",
        "/todos",
        {"title": "Renew passport", "due_date": "2026-12-10", "project_id": str(PORTFOLIO)},
    )
    assert (body["kind"], body["due_date"], body["position"]) == ("one_off", "2026-12-10", 2)


@case("post_todos__defaults.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("POST", "/todos", {"title": "Call dentist"})
    assert (body["kind"], body["due_date"], body["notes"]) == ("one_off", "2026-11-24", None)


@case("post_todos__daily_no_due_date.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("POST", "/todos", {"kind": "daily", "title": "Stretch before bed"})
    assert (body["due_date"], body["position"]) == (None, 1)


@case("post_todos__project_unavailable.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call(
        "POST",
        "/todos",
        {"kind": "daily", "title": "Draft grant intro", "project_id": str(STAGED)},
    )
    assert body["error"]["details"]["status"] == "staged"


@case("patch_todos_id__ok.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("PATCH", f"/todos/{PASSPORT}", {"done": True})
    assert body["completed_at"] is not None


@case("patch_todos_id__recurring.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("PATCH", f"/todos/{STRETCH}", {"done": True})
    assert body["error"]["details"]["kind"] == "daily"


@case("patch_todos_id__not_found.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("PATCH", f"/todos/{DEAD}", {"title": "Nope"})
    assert body["error"]["details"] == {"resource": "todo", "id": DEAD}


@case("delete_todos_id__ok.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    resp = await ctx.client.delete(f"/todos/{REVIEW}")  # 204: no body, so not ctx.call
    assert resp.status_code == 204 and resp.content == b""
    ctx.checked.append(204)
    listed = await ctx.client.get("/todos")  # ctx.call would compare against the 204 status
    assert len(listed.json()["todos"]) == 3


@case("delete_todos_id_completions_period_start__ok.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("DELETE", f"/todos/{STRETCH}/completions/2026-11-24")
    assert body["completed_periods"] == []


@case("put_todos_id_completions_period_start__ok.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("PUT", f"/todos/{REVIEW}/completions/2026-11-23")
    assert body["completed_periods"] == ["2026-11-23"]


@case("put_todos_id_completions_period_start__invalid_period.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("PUT", f"/todos/{REVIEW}/completions/2026-10-01")
    assert body["error"]["details"]["allowed"] == ["2026-11-23", "2026-11-16"]


@case("put_todos_id_completions_period_start__not_recurring.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call("PUT", f"/todos/{PASSPORT}/completions/2026-11-24")
    assert body["error"]["details"]["kind"] == "one_off"


@case("put_todos_order__ok.json")
async def _(ctx: Ctx) -> None:
    """ids are the open one-offs only; the done one-off keeps its position (1)."""
    await todo_story(ctx)
    body = await ctx.call("PUT", "/todos/order", {"kind": "one_off", "ids": [str(PASSPORT)]})
    by_id = {t["id"]: t for t in body["todos"]}
    assert by_id[str(PASSPORT)]["position"] == 0
    assert by_id[str(LIBRARY)]["position"] == 1 and by_id[str(LIBRARY)]["completed_at"]


@case("put_todos_order__invalid_order.json")
async def _(ctx: Ctx) -> None:
    await todo_story(ctx)
    body = await ctx.call(
        "PUT", "/todos/order", {"kind": "one_off", "ids": [str(PASSPORT), str(LIBRARY)]}
    )
    assert body["error"]["details"]["extra"] == [str(LIBRARY)]


# ---------------------------------------------------------------- driver


def test_every_in_scope_fixture_has_a_case() -> None:
    assert sorted(CASES) == in_scope_fixtures()


@requires_db
@pytest.mark.parametrize("name", in_scope_fixtures())
async def test_fixture(
    name: str,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    clock: SettableClock,
    app: FastAPI,
) -> None:
    ctx = Ctx(client, maker, user_id, clock, load_fixture(name), app)
    await CASES[name](ctx)
    assert ctx.checked, "the case never checked a response against the fixture"
