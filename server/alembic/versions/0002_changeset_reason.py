"""changesets.reason (T4.3 contract, 2026-11-16).

Why a changeset was proposed: `user_edit` (REST staging), `agent` (POST /agent/message),
`drift` (calendar busy time now overlaps live blocks) and `google_edit` (our event was edited
inside Google Calendar). The last two are produced by T4.3 part (b). Existing rows are
backfilled with `user_edit` through the column default; the default stays so the shape of the
migrated schema equals `Base.metadata`, but every write path passes `reason` explicitly.

Revision ID: 0002_changeset_reason
Revises: 0001_initial
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_changeset_reason"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE changeset_reason AS ENUM ('user_edit','agent','drift','google_edit')")
    op.execute(
        "ALTER TABLE changesets ADD COLUMN reason changeset_reason NOT NULL DEFAULT 'user_edit'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE changesets DROP COLUMN reason")
    op.execute("DROP TYPE changeset_reason")
