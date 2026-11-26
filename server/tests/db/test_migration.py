"""Alembic migrations: up/down/up on a scratch schema, migrated schema == Base.metadata, and the
per-revision data checks (0002 reason backfill, 0003 google identities, 0004 agent_turns,
0005 todos, 0006 agent_turn_leases,
0007 write_rate_counters)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base
from app.db.models import enums
from tests.db.conftest import make_engine, requires_db, run_alembic

pytestmark = requires_db

TODO_TABLES = ("todos", "todo_completions")  # migration 0005 (Todos v1, issue #10)

TABLES = {
    "users",
    "identities",
    "sessions",
    "google_connections",
    "projects",
    "user_tags",
    "user_preferences",
    "availability_exceptions",
    "block_pins",
    "week_plans",
    "changesets",
    "blocks",
    "mutations",
    "agent_turns",
    *TODO_TABLES,
    "agent_turn_leases",  # migration 0006 (issue #3)
    "write_rate_counters",  # migration 0007 (issue #23)
}


def _tables(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names()) - {"alembic_version"}


def _enums(conn: Connection, schema: str) -> dict[str, list[str]]:
    rows = conn.execute(
        text(
            "SELECT t.typname, e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid"
            " JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = :s"
            " ORDER BY t.typname, e.enumsortorder"
        ),
        {"s": schema},
    ).all()
    out: dict[str, list[str]] = {}
    for name, label in rows:
        out.setdefault(name, []).append(label)
    return out


def _constraints(conn: Connection, schema: str) -> set[tuple[str, str, str, str]]:
    """(table, name, type, normalized definition) for every constraint, EXCLUDE included."""
    rows = conn.execute(
        text(
            "SELECT c.conrelid::regclass::text, c.conname, c.contype::text,"
            " pg_get_constraintdef(c.oid) FROM pg_constraint c"
            " JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = :s"
            " AND c.conrelid <> 0"
        ),
        {"s": schema},
    ).all()
    return {(t.split(".")[-1], n, ty, d) for t, n, ty, d in rows if "alembic_version" not in t}


def _shape(conn: Connection, schema: str) -> dict[str, Any]:
    insp = inspect(conn)
    out: dict[str, Any] = {}
    for table in sorted(_tables(conn)):
        cols = {
            c["name"]: (str(c["type"]), c["nullable"], str(c.get("default")))
            for c in insp.get_columns(table)
        }
        idx = {
            i["name"]: (tuple(i["column_names"]), bool(i["unique"]))
            for i in insp.get_indexes(table)
        }
        out[table] = {"columns": cols, "indexes": idx}
    out["__constraints__"] = _constraints(conn, schema)
    out["__indexdefs__"] = {
        (t, n, d.replace(f"{schema}.", ""))
        for t, n, d in conn.execute(
            text("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = :s"),
            {"s": schema},
        ).all()
        if t != "alembic_version"
    }
    out["__enums__"] = _enums(conn, schema)
    return out


async def test_upgrade_downgrade_upgrade(schema: str) -> None:
    engine = await make_engine(schema)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(run_alembic, "upgrade", "head")
            assert await conn.run_sync(_tables) == TABLES
            await conn.run_sync(run_alembic, "downgrade", "base")
            assert await conn.run_sync(_tables) == set()
            assert await conn.run_sync(_enums, schema) == {}
            await conn.run_sync(run_alembic, "upgrade", "head")
            assert await conn.run_sync(_tables) == TABLES
    finally:
        await engine.dispose()


async def test_schema_matches_metadata(engine: AsyncEngine, schema: str) -> None:
    """Every table: columns, types, nullability, defaults, indexes, constraints and enums
    of the migrated schema equal those of `Base.metadata` (create_all in a sibling schema)."""
    meta_schema = f"{schema}_meta"
    meta_engine = await make_engine(meta_schema)
    try:
        async with meta_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{meta_schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            migrated = await conn.run_sync(_shape, schema)
        async with meta_engine.connect() as conn:
            from_meta = await conn.run_sync(_shape, meta_schema)
        assert set(migrated) - {"__constraints__", "__enums__", "__indexdefs__"} == TABLES
        for table in sorted(TABLES):
            assert migrated[table] == from_meta[table], table
        assert migrated["__constraints__"] == from_meta["__constraints__"]
        # Full CREATE INDEX text: catches a partial-index predicate drifting between the two.
        assert migrated["__indexdefs__"] == {
            (t, n, d.replace(f"{meta_schema}.", "")) for t, n, d in from_meta["__indexdefs__"]
        }
        assert (
            migrated["__enums__"]
            == from_meta["__enums__"]
            == {k: list(v) for k, v in enums.ENUMS.items()}
        )
    finally:
        async with meta_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{meta_schema}" CASCADE'))
        await meta_engine.dispose()


async def test_metadata_columns_and_nullability_direct(engine: AsyncEngine) -> None:
    """Belt and braces: compare the migrated columns/nullability/index names straight
    against the Table objects, independent of create_all."""

    def check(conn: Connection) -> None:
        insp = inspect(conn)
        for name, table in Base.metadata.tables.items():
            cols = {c["name"]: c["nullable"] for c in insp.get_columns(name)}
            assert cols == {c.name: c.nullable for c in table.columns}, name
            idx = {i["name"] for i in insp.get_indexes(name) if not i.get("duplicates_constraint")}
            assert idx == {i.name for i in table.indexes}, name
            uqs = {u["name"] for u in insp.get_unique_constraints(name)}
            meta_uqs = {
                c.name for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"
            } | {f"uq_{name}_{c.name}" for c in table.columns if c.unique}
            assert uqs == meta_uqs, name
            fks = {f["name"] for f in insp.get_foreign_keys(name)}
            assert fks == {f.name for f in table.foreign_key_constraints}, name

    async with engine.connect() as conn:
        await conn.run_sync(check)


async def test_0002_changeset_reason_backfills_and_round_trips(schema: str) -> None:
    """Migration 0002 (T4.3): existing changesets are backfilled `user_edit`; down drops the
    column and its enum; up again restores both."""

    def columns(conn: Connection) -> set[str]:
        return {c["name"] for c in inspect(conn).get_columns("changesets")}

    engine = await make_engine(schema)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(run_alembic, "upgrade", "0001_initial")
            assert "reason" not in await conn.run_sync(columns)
            await conn.execute(
                text("INSERT INTO users (id) VALUES ('00000000-0000-4000-8000-000000000001')")
            )
            await conn.execute(
                text(
                    "INSERT INTO changesets (user_id, staged) VALUES"
                    " ('00000000-0000-4000-8000-000000000001', '[]')"
                )
            )
            await conn.run_sync(run_alembic, "upgrade", "head")
            got = (await conn.execute(text("SELECT reason::text FROM changesets"))).all()
            assert got == [("user_edit",)]
            assert (await conn.run_sync(_enums, schema))["changeset_reason"] == [
                "user_edit",
                "agent",
                "drift",
                "google_edit",
            ]
            await conn.run_sync(run_alembic, "downgrade", "0001_initial")
            assert "reason" not in await conn.run_sync(columns)
            assert "changeset_reason" not in await conn.run_sync(_enums, schema)
            await conn.run_sync(run_alembic, "upgrade", "head")
            got = (await conn.execute(text("SELECT reason::text FROM changesets"))).all()
            assert got == [("user_edit",)]
    finally:
        await engine.dispose()


async def test_0003_google_identity_widens_provider_and_round_trips(schema: str) -> None:
    """Migration 0003 (T6.0): 'google' identities are rejected before it and accepted after;
    downgrade refuses while one exists (changing nothing), and otherwise narrows the CHECK
    back; up again restores it. Apple rows survive every step."""
    uid = "00000000-0000-4000-8000-000000000003"
    insert_identity = (
        "INSERT INTO identities (provider, subject, user_id) VALUES (:p, :s, '" + uid + "')"
    )

    def provider_check(conn: Connection) -> str:
        return conn.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c"
                " JOIN pg_namespace n ON n.oid = c.connamespace"
                " WHERE n.nspname = :s AND c.conname = 'ck_identities_provider'"
            ),
            {"s": schema},
        ).scalar_one()

    engine = await make_engine(schema)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(run_alembic, "upgrade", "0002_changeset_reason")
            await conn.execute(text(f"INSERT INTO users (id) VALUES ('{uid}')"))
            await conn.execute(text(insert_identity), {"p": "apple", "s": "apple-sub"})
            nested = await conn.begin_nested()
            with pytest.raises(IntegrityError):
                await conn.execute(text(insert_identity), {"p": "google", "s": "g-sub"})
            await nested.rollback()

            await conn.run_sync(run_alembic, "upgrade", "head")
            assert "'google'" in await conn.run_sync(provider_check)
            nested = await conn.begin_nested()
            await conn.execute(text(insert_identity), {"p": "google", "s": "g-sub"})
            with pytest.raises(DBAPIError, match="google identities exist"):
                await conn.run_sync(run_alembic, "downgrade", "0002_changeset_reason")
            await nested.rollback()  # discards the google row and the failed downgrade
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text(insert_identity), {"p": "github", "s": "x"})

            await conn.run_sync(run_alembic, "downgrade", "0002_changeset_reason")
            assert "'google'" not in await conn.run_sync(provider_check)
            await conn.run_sync(run_alembic, "upgrade", "head")
            assert "'google'" in await conn.run_sync(provider_check)
            await conn.execute(text(insert_identity), {"p": "google", "s": "g-sub-2"})
            got = (
                await conn.execute(text("SELECT provider, subject FROM identities ORDER BY 1, 2"))
            ).all()
            assert got == [("apple", "apple-sub"), ("google", "g-sub-2")]
    finally:
        await engine.dispose()


async def test_0004_agent_turns_round_trips(schema: str) -> None:
    """Migration 0004 (T6.2): creates agent_turns with its two indexes; downgrade drops only that
    table (existing rows elsewhere survive); up again recreates it empty."""
    uid = "00000000-0000-4000-8000-000000000004"

    def indexes(conn: Connection) -> set[str]:
        return {i["name"] for i in inspect(conn).get_indexes("agent_turns")}

    engine = await make_engine(schema)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(run_alembic, "upgrade", "0003_google_identity")
            assert "agent_turns" not in await conn.run_sync(_tables)
            await conn.execute(text(f"INSERT INTO users (id) VALUES ('{uid}')"))

            await conn.run_sync(run_alembic, "upgrade", "head")
            assert await conn.run_sync(_tables) == TABLES
            assert await conn.run_sync(indexes) == {
                "agent_turns_user_id_created_at_idx",
                "agent_turns_created_at_idx",
            }
            await conn.execute(
                text(
                    "INSERT INTO agent_turns (user_id, model, outcome, model_calls,"
                    f" transcript_len) VALUES ('{uid}', 'm', 'completed', 1, 1)"
                )
            )
            got = (
                await conn.execute(
                    text(
                        "SELECT input_tokens, output_tokens, cache_read_tokens,"
                        " cache_write_tokens, staged::text, user_message, snapshot"
                        " FROM agent_turns"
                    )
                )
            ).all()
            assert got == [(0, 0, 0, 0, "[]", None, None)]

            await conn.run_sync(run_alembic, "downgrade", "0003_google_identity")
            # Head includes 0005 to 0007, so downgrading past 0004 drops their tables too.
            assert await conn.run_sync(_tables) == TABLES - {
                "agent_turns",
                *TODO_TABLES,
                "agent_turn_leases",
                "write_rate_counters",
            }
            assert (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
            await conn.run_sync(run_alembic, "upgrade", "head")
            assert await conn.run_sync(_tables) == TABLES
            n = (await conn.execute(text("SELECT count(*) FROM agent_turns"))).scalar_one()
            assert n == 0
    finally:
        await engine.dispose()


async def test_0005_todos_round_trips(schema: str) -> None:
    """Migration 0005 (Todos v1): creates todo_kind and both tables; the recurring-field CHECK
    holds; a completion cascades with its todo; downgrade removes everything it created."""
    uid = "00000000-0000-4000-8000-000000000005"
    todo = "00000000-0000-4000-8000-0000000000a1"
    engine = await make_engine(schema)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(run_alembic, "upgrade", "0004_agent_turns")
            assert not (set(TODO_TABLES) & await conn.run_sync(_tables))
            await conn.execute(text(f"INSERT INTO users (id) VALUES ('{uid}')"))

            await conn.run_sync(run_alembic, "upgrade", "head")
            assert set(TODO_TABLES) <= await conn.run_sync(_tables)
            assert (await conn.run_sync(_enums, schema))["todo_kind"] == [
                "daily",
                "weekly",
                "one_off",
            ]
            await conn.execute(
                text(
                    "INSERT INTO todos (id, user_id, kind, title, position, due_date)"
                    f" VALUES ('{todo}', '{uid}', 'one_off', 'Renew', 0, '2026-12-10')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO todo_completions (todo_id, period_start)"
                    f" VALUES ('{todo}', '2026-11-23')"
                )
            )
            # A recurring todo may not carry a due date (ck_todos_recurring_fields).
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "INSERT INTO todos (user_id, kind, title, position, due_date)"
                            f" VALUES ('{uid}', 'daily', 'Bad', 0, '2026-12-10')"
                        )
                    )

            await conn.execute(text(f"DELETE FROM todos WHERE id = '{todo}'"))
            left = (await conn.execute(text("SELECT count(*) FROM todo_completions"))).scalar_one()
            assert left == 0  # cascade

            await conn.run_sync(run_alembic, "downgrade", "0004_agent_turns")
            assert not ({*TODO_TABLES, "agent_turn_leases"} & await conn.run_sync(_tables))
            assert (await conn.run_sync(_enums, schema)).get("todo_kind") is None
            assert (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_0006_agent_turn_leases_round_trips(schema: str) -> None:
    """Migration 0006 (issue #3): one lease row per user (primary key), cascading with the
    user; downgrade drops only that table; up again recreates it empty."""
    uid = "00000000-0000-4000-8000-000000000006"
    lease = (
        "INSERT INTO agent_turn_leases (user_id, token, expires_at)"
        f" VALUES ('{uid}', gen_random_uuid(), '2026-11-17T18:33:00Z')"
    )
    engine = await make_engine(schema)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(run_alembic, "upgrade", "0005_todos")
            assert "agent_turn_leases" not in await conn.run_sync(_tables)
            await conn.execute(text(f"INSERT INTO users (id) VALUES ('{uid}')"))

            await conn.run_sync(run_alembic, "upgrade", "head")
            assert await conn.run_sync(_tables) == TABLES
            await conn.execute(text(lease))
            with pytest.raises(IntegrityError):  # pk_agent_turn_leases: one lease per user
                async with conn.begin_nested():
                    await conn.execute(text(lease))

            await conn.execute(text(f"DELETE FROM users WHERE id = '{uid}'"))
            n = (await conn.execute(text("SELECT count(*) FROM agent_turn_leases"))).scalar_one()
            assert n == 0  # cascade

            await conn.execute(text(f"INSERT INTO users (id) VALUES ('{uid}')"))
            await conn.execute(text(lease))
            await conn.run_sync(run_alembic, "downgrade", "0005_todos")
            assert await conn.run_sync(_tables) == TABLES - {
                "agent_turn_leases",
                "write_rate_counters",
            }
            assert (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
            await conn.run_sync(run_alembic, "upgrade", "head")
            n = (await conn.execute(text("SELECT count(*) FROM agent_turn_leases"))).scalar_one()
            assert n == 0
    finally:
        await engine.dispose()


async def test_0007_write_rate_counters_round_trips(schema: str) -> None:
    """Migration 0007 (issue #23): one counter row per user (primary key), cascading with the
    user; downgrade drops only that table; up again recreates it empty."""
    uid = "00000000-0000-4000-8000-000000000007"
    counter = (
        "INSERT INTO write_rate_counters (user_id, window_start, count)"
        f" VALUES ('{uid}', '2026-11-17T18:30:00Z', 1)"
    )
    engine = await make_engine(schema)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(run_alembic, "upgrade", "0006_agent_turn_leases")
            assert "write_rate_counters" not in await conn.run_sync(_tables)
            await conn.execute(text(f"INSERT INTO users (id) VALUES ('{uid}')"))

            await conn.run_sync(run_alembic, "upgrade", "head")
            assert await conn.run_sync(_tables) == TABLES
            await conn.execute(text(counter))
            with pytest.raises(IntegrityError):  # pk_write_rate_counters: one row per user
                async with conn.begin_nested():
                    await conn.execute(text(counter))

            await conn.execute(text(f"DELETE FROM users WHERE id = '{uid}'"))
            n = (await conn.execute(text("SELECT count(*) FROM write_rate_counters"))).scalar_one()
            assert n == 0  # cascade

            await conn.execute(text(f"INSERT INTO users (id) VALUES ('{uid}')"))
            await conn.execute(text(counter))
            await conn.run_sync(run_alembic, "downgrade", "0006_agent_turn_leases")
            assert await conn.run_sync(_tables) == TABLES - {"write_rate_counters"}
            assert (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
            await conn.run_sync(run_alembic, "upgrade", "head")
            n = (await conn.execute(text("SELECT count(*) FROM write_rate_counters"))).scalar_one()
            assert n == 0
    finally:
        await engine.dispose()


def _todo_columns(conn: Connection) -> dict[str, bool]:
    return {c["name"]: c["nullable"] for c in inspect(conn).get_columns("todos")}


def _todo_fks(conn: Connection) -> set[str | None]:
    return {fk["name"] for fk in inspect(conn).get_foreign_keys("todos")}


async def test_0008_todos_drop_project_round_trips(schema: str) -> None:
    """Migration 0008 (issue #30): drops todos.project_id and its FK, keeping the todo row;
    downgrade restores a nullable column (all null, links not restored) and the FK."""
    uid = "00000000-0000-4000-8000-000000000008"
    project = "00000000-0000-4000-8000-0000000000b1"
    todo = "00000000-0000-4000-8000-0000000000b2"
    engine = await make_engine(schema)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(run_alembic, "upgrade", "0007_write_rate_counters")
            await conn.execute(text(f"INSERT INTO users (id) VALUES ('{uid}')"))
            await conn.execute(
                text(
                    "INSERT INTO projects (id, user_id, name, kind, weekly_target_hours,"
                    " max_block_minutes, min_block_minutes)"
                    f" VALUES ('{project}', '{uid}', 'Portfolio', 'perennial', 1, 60, 25)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO todos (id, user_id, kind, title, position, project_id)"
                    f" VALUES ('{todo}', '{uid}', 'one_off', 'Renew', 0, '{project}')"
                )
            )
            assert "fk_todos_project_id_projects" in await conn.run_sync(_todo_fks)

            await conn.run_sync(run_alembic, "upgrade", "head")
            assert "project_id" not in await conn.run_sync(_todo_columns)
            assert "fk_todos_project_id_projects" not in await conn.run_sync(_todo_fks)
            ids = (await conn.execute(text("SELECT id::text FROM todos"))).scalars().all()
            assert ids == [todo]

            await conn.run_sync(run_alembic, "downgrade", "0007_write_rate_counters")
            assert (await conn.run_sync(_todo_columns))["project_id"] is True
            assert "fk_todos_project_id_projects" in await conn.run_sync(_todo_fks)
            links = (await conn.execute(text("SELECT id::text, project_id FROM todos"))).all()
            assert [tuple(r) for r in links] == [(todo, None)]

            await conn.run_sync(run_alembic, "upgrade", "head")
            assert "project_id" not in await conn.run_sync(_todo_columns)
    finally:
        await engine.dispose()
