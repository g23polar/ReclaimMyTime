"""Alembic 0001_initial: up/down/up on a scratch schema, and migrated schema == Base.metadata."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base
from app.db.models import enums
from tests.db.conftest import make_engine, requires_db, run_alembic

pytestmark = requires_db

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
