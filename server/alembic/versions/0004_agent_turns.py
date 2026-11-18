"""agent_turns: one row per POST /agent/message turn (T6.2 eval capture, user 2026-11-17).

Token counts, outcome, staged result and ids are always stored; `user_message` and the
rendered state `snapshot` only when AGENT_CAPTURE_TURNS is on (the caller passes NULL
otherwise). Retention is 30 days, enforced by `app.db.agent_turns.record_agent_turn`, which
purges EVERY user's expired rows in the same transaction as each insert (so an inactive user's
captured text still ages out); `agent_turns_created_at_idx` keeps that global delete cheap,
and `(user_id, created_at)` serves the per-user token window. The ARCH §9 per-user
token budget will sum this table (no separate `llm_usage` table).

FKs: users ON DELETE CASCADE (account deletion); changesets / week_plans ON DELETE SET NULL,
same as `mutations`, so a future plan-version GC can delete plans without touching this
table. As of this revision no app path deletes changesets or week_plans except the users
cascade.

Revision ID: 0004_agent_turns
Revises: 0003_google_identity
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_agent_turns"
down_revision: str | None = "0003_google_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENT_TURNS = """
CREATE TABLE agent_turns (
  id                 uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id            uuid NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  model              text NOT NULL,
  outcome            text NOT NULL,
  error_code         text,
  stop_reason        text,
  model_calls        int NOT NULL,
  input_tokens       int NOT NULL DEFAULT 0,
  output_tokens      int NOT NULL DEFAULT 0,
  cache_read_tokens  int NOT NULL DEFAULT 0,
  cache_write_tokens int NOT NULL DEFAULT 0,
  transcript_len     int NOT NULL,
  staged             jsonb NOT NULL DEFAULT '[]'::jsonb,
  changeset_id       uuid,
  plan_id            uuid,
  user_message       text,
  snapshot           text,
  CONSTRAINT pk_agent_turns PRIMARY KEY (id),
  CONSTRAINT fk_agent_turns_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_agent_turns_changeset_id_changesets
    FOREIGN KEY (changeset_id) REFERENCES changesets(id) ON DELETE SET NULL,
  CONSTRAINT fk_agent_turns_plan_id_week_plans
    FOREIGN KEY (plan_id) REFERENCES week_plans(id) ON DELETE SET NULL,
  -- Frozen copy of app.db.models.agent.AGENT_TURN_OUTCOMES (a later change needs a migration).
  CONSTRAINT ck_agent_turns_outcome CHECK (outcome IN ('completed', 'refused', 'error')),
  CONSTRAINT ck_agent_turns_model_calls CHECK (model_calls >= 0),
  CONSTRAINT ck_agent_turns_input_tokens CHECK (input_tokens >= 0),
  CONSTRAINT ck_agent_turns_output_tokens CHECK (output_tokens >= 0),
  CONSTRAINT ck_agent_turns_cache_read_tokens CHECK (cache_read_tokens >= 0),
  CONSTRAINT ck_agent_turns_cache_write_tokens CHECK (cache_write_tokens >= 0)
)
"""
# Separate executes: asyncpg prepares each statement, and a prepared statement holds one command.
AGENT_TURNS_INDEXES = (
    "CREATE INDEX agent_turns_user_id_created_at_idx ON agent_turns (user_id, created_at)",
    "CREATE INDEX agent_turns_created_at_idx ON agent_turns (created_at)",
)


def upgrade() -> None:
    op.execute(AGENT_TURNS)
    for index in AGENT_TURNS_INDEXES:
        op.execute(index)


def downgrade() -> None:
    op.execute("DROP TABLE agent_turns")
