"""write_rate_counters: the per-user write rate limit, across every server process (issue #23).

Every scheduling write route takes a slot here before it does any work. One row per user holds
the start of the user's current fixed window (aligned to the Unix epoch) and the writes
accepted in it; one atomic upsert either takes a slot or refuses, so the limit holds across
machines with no advisory lock. Written only by `app.db.write_rate`. Constraint names are
spelled out so the migrated schema matches `Base.metadata` (see tests/db/test_migration.py).

Revision ID: 0007_write_rate_counters
Revises: 0006_agent_turn_leases
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_write_rate_counters"
down_revision: str | None = "0006_agent_turn_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WRITE_RATE_COUNTERS = """
CREATE TABLE write_rate_counters (
  user_id      uuid NOT NULL,
  window_start timestamptz NOT NULL,
  count        integer NOT NULL,
  CONSTRAINT pk_write_rate_counters PRIMARY KEY (user_id),
  CONSTRAINT fk_write_rate_counters_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
)
"""


def upgrade() -> None:
    op.execute(WRITE_RATE_COUNTERS)


def downgrade() -> None:
    op.execute("DROP TABLE write_rate_counters")
