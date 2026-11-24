"""todos: standalone recurring checklists and one-off tasks (Todos v1, issue #10).

Direct-write tables: the API writes them without a changeset, a solve or a `mutations` row
(the deliberate exception noted in app.db.models.intent). Constraint names are spelled out
here so the migrated schema matches `Base.metadata` (see tests/db/test_migration.py).

Known gap, accepted for v1: `todo_completions.period_start` is keyed to the week start in force
when the tick was made. Changing `preferences.week_start` leaves older weekly rows that no
longer match the current or previous period; they are not re-keyed and simply stop counting.

Revision ID: 0005_todos
Revises: 0004_agent_turns
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_todos"
down_revision: str | None = "0004_agent_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TODO_KIND = "CREATE TYPE todo_kind AS ENUM ('daily', 'weekly', 'one_off')"

TODOS = """
CREATE TABLE todos (
  id           uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL,
  kind         todo_kind NOT NULL,
  title        text NOT NULL,
  notes        text,
  due_date     date,
  project_id   uuid,
  position     integer NOT NULL,
  completed_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_todos PRIMARY KEY (id),
  CONSTRAINT fk_todos_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_todos_project_id_projects
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
  CONSTRAINT ck_todos_title_length CHECK (char_length(title) BETWEEN 1 AND 200),
  CONSTRAINT ck_todos_notes_length CHECK (char_length(notes) <= 2000),
  CONSTRAINT ck_todos_recurring_fields CHECK (
    kind = 'one_off' OR (due_date IS NULL AND completed_at IS NULL)
  )
)
"""
TODOS_INDEX = "CREATE INDEX ix_todos_user_id_kind_position ON todos (user_id, kind, position)"

TODO_COMPLETIONS = """
CREATE TABLE todo_completions (
  todo_id      uuid NOT NULL,
  period_start date NOT NULL,
  completed_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_todo_completions PRIMARY KEY (todo_id, period_start),
  CONSTRAINT fk_todo_completions_todo_id_todos
    FOREIGN KEY (todo_id) REFERENCES todos(id) ON DELETE CASCADE
)
"""


def upgrade() -> None:
    # Separate executes: asyncpg prepares each statement, and a prepared statement holds one
    # command.
    op.execute(TODO_KIND)
    op.execute(TODOS)
    op.execute(TODOS_INDEX)
    op.execute(TODO_COMPLETIONS)


def downgrade() -> None:
    op.execute("DROP TABLE todo_completions")
    op.execute("DROP TABLE todos")
    op.execute("DROP TYPE todo_kind")
