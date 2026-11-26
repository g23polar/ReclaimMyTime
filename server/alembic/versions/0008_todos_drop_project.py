"""todos drop project: remove the todo–project link (issue #30).

Drops the `fk_todos_project_id_projects` constraint and then the nullable `todos.project_id`
column. Existing links are deleted for good. The downgrade re-adds the column (nullable) and the
constraint, but every value is null: old links are not restored. Constraint names are spelled
out so the migrated schema matches `Base.metadata` (see tests/db/test_migration.py).

Revision ID: 0008_todos_drop_project
Revises: 0007_write_rate_counters
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_todos_drop_project"
down_revision: str | None = "0007_write_rate_counters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE todos DROP CONSTRAINT fk_todos_project_id_projects")
    op.execute("ALTER TABLE todos DROP COLUMN project_id")


def downgrade() -> None:
    op.execute("ALTER TABLE todos ADD COLUMN project_id uuid")
    op.execute(
        "ALTER TABLE todos ADD CONSTRAINT fk_todos_project_id_projects "
        "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL"
    )
