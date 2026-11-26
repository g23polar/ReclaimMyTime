"""Issue #28: each agent turn lists the user's open todos, read-only, in the state snapshot —
bounded, ordered by due date, scoped to the user, with no project reference and no writes."""

from __future__ import annotations

from datetime import date

import pytest

from app.agent import context
from app.agent.prompt import _TODO_NOTES_SHOWN, SYSTEM_PROMPT
from tests.agent.fake_anthropic import Call, Text
from tests.api.conftest import new_user
from tests.api.test_agent_message import turn
from tests.api.test_busy_agent import agent_app, fake  # noqa: F401  (pytest fixtures)
from tests.db.conftest import requires_db, rows
from tests.todos.story import add_todo

pytestmark = [requires_db, pytest.mark.usefixtures("world")]

HEADER = "Todos (read-only; chat can't change them):"


async def todo_lines(client, fake) -> list[str]:  # noqa: F811
    fake.script = [Call([Text("Here's what I see.")])]
    await turn(client, ("user", "What's on my list?"))
    text = fake.requests[0]["messages"][-1]["content"]
    lines = text.splitlines()
    start = lines.index(HEADER)
    end = lines.index("Busy (calendar):")
    assert "Live schedule from now on:" in lines[:start]
    return lines[start + 1 : end]


async def test_no_todos_renders_none(agent_app, client, fake):  # noqa: F811
    assert await todo_lines(client, fake) == ["  (none)"]


async def test_each_kind_listed_one_off_first(agent_app, client, fake, maker, who):  # noqa: F811
    await add_todo(maker, who.user_id, kind="weekly", title="Plan week")
    await add_todo(maker, who.user_id, kind="daily", title="Stretch")
    await add_todo(maker, who.user_id, kind="one_off", title="File taxes")
    assert await todo_lines(client, fake) == [
        "  one_off 'File taxes'",
        "  daily 'Stretch'",
        "  weekly 'Plan week'",
    ]


async def test_due_date_rendered(agent_app, client, fake, maker, who):  # noqa: F811
    await add_todo(maker, who.user_id, title="Tax return", due_date=date(2026, 11, 20))
    assert await todo_lines(client, fake) == ["  one_off 'Tax return' due 2026-11-20 (Fri)"]


async def test_completed_one_off_excluded(agent_app, client, fake):  # noqa: F811
    done = (await client.post("/todos", json={"kind": "one_off", "title": "Passport"})).json()
    await client.post("/todos", json={"kind": "one_off", "title": "Visa"})
    res = await client.patch(f"/todos/{done['id']}", json={"done": True})
    assert res.status_code == 200, res.text
    # Created through the API, a one-off is due today by default.
    assert await todo_lines(client, fake) == ["  one_off 'Visa' due 2026-11-17 (Tue)"]


async def test_long_notes_collapsed_and_cut(agent_app, client, fake, maker, who):  # noqa: F811
    notes = ("line one\nline two\n" * 200)[:2000]
    await add_todo(maker, who.user_id, kind="daily", title="Journal", notes=notes)
    [line] = await todo_lines(client, fake)
    prefix = "  daily 'Journal' — notes: "
    assert line.startswith(prefix)
    note = line.removeprefix(prefix)
    assert note.endswith("…") and len(note) == _TODO_NOTES_SHOWN
    assert note.startswith("line one line two line one")


async def test_short_notes_whole(agent_app, client, fake, maker, who):  # noqa: F811
    await add_todo(maker, who.user_id, title="Call bank", notes="ask about\nthe fee")
    assert await todo_lines(client, fake) == ["  one_off 'Call bank' — notes: ask about the fee"]


async def test_cap_keeps_nearest_due_dates(
    agent_app,  # noqa: F811
    client,
    fake,  # noqa: F811
    maker,
    who,
    monkeypatch,
):
    monkeypatch.setattr(context, "_TODOS_SHOWN", 3)
    await add_todo(maker, who.user_id, kind="daily", title="Stretch")
    await add_todo(maker, who.user_id, kind="weekly", title="Plan week")
    await add_todo(maker, who.user_id, title="Someday")
    for day in (28, 19, 25, 21, 30):
        await add_todo(maker, who.user_id, title=f"Due {day}", due_date=date(2026, 11, day))
    assert await todo_lines(client, fake) == [
        "  one_off 'Due 19' due 2026-11-19 (Thu)",
        "  one_off 'Due 21' due 2026-11-21 (Sat)",
        "  one_off 'Due 25' due 2026-11-25 (Wed)",
        "  … and 5 more not shown",
    ]


async def test_order_ties_by_position(agent_app, client, fake, maker, who):  # noqa: F811
    await add_todo(maker, who.user_id, kind="daily", title="Second", position=1)
    await add_todo(maker, who.user_id, kind="daily", title="First", position=0)
    await add_todo(maker, who.user_id, title="Undated")
    await add_todo(maker, who.user_id, title="Dated", due_date=date(2026, 12, 31))
    assert await todo_lines(client, fake) == [
        "  one_off 'Dated' due 2026-12-31 (Thu)",
        "  one_off 'Undated'",
        "  daily 'First'",
        "  daily 'Second'",
    ]


async def test_other_users_todos_never_shown(agent_app, client, fake, maker, who):  # noqa: F811
    other = await new_user(maker)
    await add_todo(maker, other, kind="daily", title="Not mine")
    await add_todo(maker, who.user_id, kind="daily", title="Mine")
    assert await todo_lines(client, fake) == ["  daily 'Mine'"]


async def test_turn_writes_no_todos(agent_app, client, fake, maker, who):  # noqa: F811
    await add_todo(maker, who.user_id, kind="daily", title="Stretch")
    await add_todo(maker, who.user_id, title="Tax", due_date=date(2026, 11, 20))
    await client.post("/todos", json={"kind": "one_off", "title": "Old"})

    async def state():
        todos = await rows(maker, "SELECT id, updated_at, completed_at FROM todos ORDER BY id")
        done = await rows(maker, "SELECT todo_id, period_start, completed_at FROM todo_completions")
        return todos, sorted(done)

    before = await state()
    await todo_lines(client, fake)
    assert await state() == before
    assert not any("todo" in t["name"] for t in fake.requests[0]["tools"])


def test_system_prompt_todo_guidance():
    assert "todos as context only" in SYSTEM_PROMPT
    assert "use its title and due date instead of asking for them again" in SYSTEM_PROMPT
    assert "You can't add, tick, edit or delete todos" in SYSTEM_PROMPT
    assert "point the user to the Todos tab" in SYSTEM_PROMPT
    assert "Scope: only the user's schedule, projects, availability and todos." in SYSTEM_PROMPT
    assert "linked project" not in SYSTEM_PROMPT and "project link" not in SYSTEM_PROMPT
