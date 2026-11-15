"""Plan tables: week_plans, changesets, blocks; agent trace: mutations.

ARCHITECTURE.md §3 DDL, column-for-column. No relationships and no ORM cascades. Block
rows are created only by `app.db.write_paths.persist_plan`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    DDL,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, UUID, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.enums import pg_enum

_UUID = UUID(as_uuid=True)

# Exclusion-constraint names (no naming-convention key exists for EXCLUDE).
WEEK_PLANS_LIVE_EXCL = "week_plans_one_live_excl"
WEEK_PLANS_PROPOSED_EXCL = "week_plans_one_proposed_excl"
BLOCKS_NO_OVERLAP_EXCL = "blocks_no_overlap_excl"

# `Base.metadata.create_all` (used by tests that skip Alembic) needs the same extensions the
# migration creates: btree_gist for `uuid WITH =` in the exclusion constraints, citext for
# email columns. gen_random_uuid() is core since Postgres 13.
for _ext in ("btree_gist", "citext"):
    event.listen(Base.metadata, "before_create", DDL(f"CREATE EXTENSION IF NOT EXISTS {_ext}"))


class WeekPlan(Base):
    __tablename__ = "week_plans"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_week_plans_id_user_id"),
        UniqueConstraint(
            "user_id", "week_start", "version", name="uq_week_plans_user_id_week_start_version"
        ),
        # Liveness + proposal invariants on ABSOLUTE time (ARCH §3).
        ExcludeConstraint(
            ("user_id", "="),
            ("horizon", "&&"),
            where=text("status = 'live'"),
            using="gist",
            name=WEEK_PLANS_LIVE_EXCL,
        ),
        ExcludeConstraint(
            ("user_id", "="),
            ("horizon", "&&"),
            where=text("status = 'proposed'"),
            using="gist",
            name=WEEK_PLANS_PROPOSED_EXCL,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    horizon: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("plan_status"), nullable=False, server_default=text("'proposed'")
    )
    solver_input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    infeasibility: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)  # Python None → SQL NULL, not JSON null
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Changeset(Base):
    __tablename__ = "changesets"
    # D4 "at most one pending per user": the per-user pending lookup runs under the user lock
    # on every staging/confirm; resolved history must not be scanned (T2.1 review).
    __table_args__ = (
        Index(
            "changesets_user_id_pending_idx",
            "user_id",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("week_plans.id", ondelete="SET NULL")
    )
    staged: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("changeset_status"), nullable=False, server_default=text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "user_id"],
            ["week_plans.id", "week_plans.user_id"],
            ondelete="CASCADE",
            name="fk_blocks_plan_id_week_plans",
        ),
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.user_id"],
            name="fk_blocks_project_id_projects",
        ),
        # PRD Principle 1, enforced physically: no overlapping blocks within a plan version.
        ExcludeConstraint(
            ("plan_id", "="), ("span", "&&"), using="gist", name=BLOCKS_NO_OVERLAP_EXCL
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, server_default=text("gen_random_uuid()")
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(_UUID, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(_UUID, nullable=False)
    lineage_id: Mapped[uuid.UUID] = mapped_column(_UUID, nullable=False)
    span: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    state: Mapped[str] = mapped_column(
        pg_enum("block_state"), nullable=False, server_default=text("'planned'")
    )
    gcal_status: Mapped[str] = mapped_column(
        pg_enum("gcal_sync"), nullable=False, server_default=text("'pending'")
    )
    gcal_event_id: Mapped[str | None] = mapped_column(Text)
    gcal_etag: Mapped[str | None] = mapped_column(Text)


class Mutation(Base):
    __tablename__ = "mutations"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    changeset_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("changesets.id", ondelete="SET NULL")
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("week_plans.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(pg_enum("mutation_kind"), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)  # Python None → SQL NULL, not JSON null
    )
    interpretation: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        pg_enum("mutation_source"), nullable=False, server_default=text("'agent'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
