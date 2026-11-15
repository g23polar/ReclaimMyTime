"""Intent tables: projects, user_tags, user_preferences, availability_exceptions, block_pins.

ARCHITECTURE.md §3 DDL, column-for-column. No relationships: deletes rely on the DDL's
ON DELETE CASCADE only. Writes go through `app.db.write_paths` exclusively.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSTZRANGE, UUID, Range
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.enums import pg_enum

_UUID = UUID(as_uuid=True)

PROJECT_KIND_CHECK = (
    "(kind = 'deadline' AND target_date IS NOT NULL AND estimated_total_hours IS NOT NULL"
    " AND weekly_target_hours IS NULL) OR "
    "(kind = 'perennial' AND target_date IS NULL AND estimated_total_hours IS NULL"
    " AND weekly_target_hours IS NOT NULL)"
)


def _user_fk() -> Any:
    return ForeignKey("users.id", ondelete="CASCADE")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("max_block_minutes > 0", name="max_block_minutes"),
        CheckConstraint(
            "min_block_minutes > 0 AND min_block_minutes <= max_block_minutes",
            name="min_block_minutes",
        ),
        UniqueConstraint("id", "user_id", name="uq_projects_id_user_id"),  # composite-FK target
        CheckConstraint(PROJECT_KIND_CHECK, name="kind_fields"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(_UUID, _user_fk(), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(pg_enum("project_kind"), nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date)  # end of that local day
    estimated_total_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    weekly_target_hours: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    max_block_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # No DB default: the app sets LEAST(25, max_block_minutes).
    min_block_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    preferred_windows: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)  # Python None → SQL NULL, not JSON null
    )
    preferred_strict: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    requirements: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    status: Mapped[str] = mapped_column(
        pg_enum("project_status"), nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserTag(Base):
    __tablename__ = "user_tags"

    user_id: Mapped[uuid.UUID] = mapped_column(_UUID, _user_fk(), primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)


class UserPreferences(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (CheckConstraint("week_start BETWEEN 1 AND 7", name="week_start"),)

    user_id: Mapped[uuid.UUID] = mapped_column(_UUID, _user_fk(), primary_key=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)  # IANA; no default
    schedulable_windows: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    day_diversity: Mapped[str] = mapped_column(
        pg_enum("day_diversity"), nullable=False, server_default=text("'mixed'")
    )
    buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("15"))
    week_start: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))


class AvailabilityException(Base):
    __tablename__ = "availability_exceptions"
    __table_args__ = (
        CheckConstraint("blocks_all OR cardinality(blocks_tags) > 0", name="blocks_something"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(_UUID, _user_fk(), nullable=False)
    span: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    blocks_all: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    blocks_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BlockPin(Base):
    __tablename__ = "block_pins"
    __table_args__ = (
        CheckConstraint("kind = 'forbid' OR project_id IS NOT NULL", name="pin_has_project"),
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.user_id"],
            name="fk_block_pins_project_id_projects",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(_UUID, _user_fk(), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(_UUID)  # NULL only for a blanket forbid
    span: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    kind: Mapped[str] = mapped_column(pg_enum("pin_kind"), nullable=False)
    # Provenance only; no FK (block rows churn with versions).
    created_from_block_id: Mapped[uuid.UUID | None] = mapped_column(_UUID)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
