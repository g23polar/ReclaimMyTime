"""agent_turn_leases: one turn in flight per user, across every server process (issue #3).

`POST /agent/message` takes a row here before it does any work and deletes it when the turn
ends. The row is a lease, not a lock: nothing holds a connection or transaction while the
turn runs, and `expires_at` (request time + open timeout + turn timeout + 30 s) lets the next
acquire take over from a holder that died mid-turn. Release deletes by `token`, so a holder
whose lease expired and was taken over never deletes the new holder's row. Written only by
`app.db.turn_leases`. Constraint names are spelled out so the migrated schema matches
`Base.metadata` (see tests/db/test_migration.py).

Revision ID: 0006_agent_turn_leases
Revises: 0005_todos
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_agent_turn_leases"
down_revision: str | None = "0005_todos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENT_TURN_LEASES = """
CREATE TABLE agent_turn_leases (
  user_id    uuid NOT NULL,
  token      uuid NOT NULL,
  expires_at timestamptz NOT NULL,
  CONSTRAINT pk_agent_turn_leases PRIMARY KEY (user_id),
  CONSTRAINT fk_agent_turn_leases_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
)
"""


def upgrade() -> None:
    op.execute(AGENT_TURN_LEASES)


def downgrade() -> None:
    op.execute("DROP TABLE agent_turn_leases")
