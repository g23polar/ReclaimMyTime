"""Todos tables (migration 0005, Todos v1): `todos` and `todo_completions`.

Column-for-column with migration 0005. Direct-write tables, written only by `app.db.todos`
(see the note in app.db.models.intent).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.enums import pg_enum

_UUID = UUID(as_uuid=True)


def _user_fk() -> Any:
    return ForeignKey("users.id", ondelete="CASCADE")


class Todo(Base):
    __tablename__ = "todos"
    __table_args__ = (
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint("char_length(notes) <= 2000", name="notes_length"),
        CheckConstraint(
            "kind = 'one_off' OR (due_date IS NULL AND completed_at IS NULL)",
            name="recurring_fields",
        ),
        Index("ix_todos_user_id_kind_position", "user_id", "kind", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(_UUID, _user_fk(), nullable=False)
    kind: Mapped[str] = mapped_column(pg_enum("todo_kind"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date)  # one_off only; local date
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("projects.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # one_off only
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TodoCompletion(Base):
    __tablename__ = "todo_completions"

    todo_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("todos.id", ondelete="CASCADE"), primary_key=True
    )
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
