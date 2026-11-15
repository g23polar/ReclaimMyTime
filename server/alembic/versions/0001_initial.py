"""Initial schema: ARCHITECTURE.md §3 DDL verbatim.

The SQL below is the §3 DDL with one mechanical change: every constraint carries an explicit
`CONSTRAINT <name>` matching `app.db.base.NAMING_CONVENTION` (and the explicit names in
`app.db.models`), so the migrated schema and `Base.metadata` agree name-for-name. Auth relies
on this (`uq_users_email`, app/auth/repository.py). gen_random_uuid() is core since
Postgres 13, so pgcrypto is not needed. One addition beyond the §3 text as of 2026-11-15:
`changesets_user_id_pending_idx` (T2.1 review; the main session adds it to ARCH §3).

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = """
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;
"""

ENUMS = """
CREATE TYPE connection_status AS ENUM ('active','revoked');
CREATE TYPE project_kind   AS ENUM ('deadline', 'perennial');
CREATE TYPE project_status AS ENUM ('staged', 'active', 'paused', 'done', 'archived');
CREATE TYPE day_diversity AS ENUM ('focused', 'mixed');
CREATE TYPE pin_kind AS ENUM ('pin','forbid');
CREATE TYPE plan_status AS ENUM ('proposed','live','superseded','rejected');
CREATE TYPE changeset_status AS ENUM ('pending','confirmed','rejected','expired');
CREATE TYPE block_state AS ENUM ('planned','completed','skipped');
CREATE TYPE gcal_sync   AS ENUM ('pending','synced','failed');
CREATE TYPE mutation_source AS ENUM ('agent','manual','system');
CREATE TYPE mutation_kind AS ENUM (
  'project_created','project_updated','project_archived',
  'preferences_updated','exception_added','exception_removed',
  'pin_added','pin_removed',
  'plan_proposed','plan_confirmed','plan_rejected'
);
"""

IDENTITY = """
CREATE TABLE users (
  id         uuid NOT NULL DEFAULT gen_random_uuid(),
  email      citext,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_users PRIMARY KEY (id),
  CONSTRAINT uq_users_email UNIQUE (email)
);

CREATE TABLE identities (
  provider                text NOT NULL,
  subject                 text NOT NULL,
  user_id                 uuid NOT NULL,
  email                   citext,
  email_verified          boolean NOT NULL DEFAULT false,
  is_private_email        boolean NOT NULL DEFAULT false,
  apple_refresh_token_enc bytea,
  created_at              timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_identities PRIMARY KEY (provider, subject),
  CONSTRAINT ck_identities_provider CHECK (provider IN ('apple')),
  CONSTRAINT fk_identities_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX identities_user_id_idx ON identities (user_id);

CREATE TABLE sessions (
  id           uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL,
  token_hash   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  last_seen_at timestamptz,
  CONSTRAINT pk_sessions PRIMARY KEY (id),
  CONSTRAINT uq_sessions_token_hash UNIQUE (token_hash),
  CONSTRAINT fk_sessions_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX sessions_user_id_idx ON sessions (user_id);
"""

GOOGLE = """
CREATE TABLE google_connections (
  user_id           uuid NOT NULL,
  google_email      citext NOT NULL,
  refresh_token_enc bytea NOT NULL,
  key_id            smallint NOT NULL DEFAULT 1,
  scopes            text[] NOT NULL,
  app_calendar_id   text,
  status            connection_status NOT NULL DEFAULT 'active',
  revoked_at        timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_google_connections PRIMARY KEY (user_id),
  CONSTRAINT fk_google_connections_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""

INTENT = """
CREATE TABLE projects (
  id                    uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id               uuid NOT NULL,
  name                  text NOT NULL,
  kind                  project_kind NOT NULL,
  target_date           date,
  estimated_total_hours numeric(6,1),
  weekly_target_hours   numeric(5,1),
  max_block_minutes     int NOT NULL,
  min_block_minutes     int NOT NULL,
  preferred_windows     jsonb,
  preferred_strict      boolean NOT NULL DEFAULT false,
  requirements          text[] NOT NULL DEFAULT '{}',
  status                project_status NOT NULL DEFAULT 'active',
  created_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_projects PRIMARY KEY (id),
  CONSTRAINT fk_projects_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT ck_projects_max_block_minutes CHECK (max_block_minutes > 0),
  CONSTRAINT ck_projects_min_block_minutes
    CHECK (min_block_minutes > 0 AND min_block_minutes <= max_block_minutes),
  CONSTRAINT uq_projects_id_user_id UNIQUE (id, user_id),
  CONSTRAINT ck_projects_kind_fields CHECK (
    (kind = 'deadline'  AND target_date IS NOT NULL AND estimated_total_hours IS NOT NULL
      AND weekly_target_hours IS NULL) OR
    (kind = 'perennial' AND target_date IS NULL     AND estimated_total_hours IS NULL
      AND weekly_target_hours IS NOT NULL)
  )
);

CREATE TABLE user_tags (
  user_id uuid NOT NULL,
  tag     text NOT NULL,
  CONSTRAINT pk_user_tags PRIMARY KEY (user_id, tag),
  CONSTRAINT fk_user_tags_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE user_preferences (
  user_id             uuid NOT NULL,
  timezone            text NOT NULL,
  schedulable_windows jsonb NOT NULL,
  day_diversity       day_diversity NOT NULL DEFAULT 'mixed',
  buffer_minutes      int NOT NULL DEFAULT 15,
  week_start          smallint NOT NULL DEFAULT 1,
  CONSTRAINT pk_user_preferences PRIMARY KEY (user_id),
  CONSTRAINT fk_user_preferences_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT ck_user_preferences_week_start CHECK (week_start BETWEEN 1 AND 7)
);

CREATE TABLE availability_exceptions (
  id          uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL,
  span        tstzrange NOT NULL,
  blocks_all  boolean NOT NULL DEFAULT false,
  blocks_tags text[] NOT NULL DEFAULT '{}',
  reason      text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_availability_exceptions PRIMARY KEY (id),
  CONSTRAINT fk_availability_exceptions_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT ck_availability_exceptions_blocks_something
    CHECK (blocks_all OR cardinality(blocks_tags) > 0)
);

CREATE TABLE block_pins (
  id                    uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id               uuid NOT NULL,
  project_id            uuid,
  span                  tstzrange NOT NULL,
  kind                  pin_kind NOT NULL,
  created_from_block_id uuid,
  created_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_block_pins PRIMARY KEY (id),
  CONSTRAINT fk_block_pins_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT ck_block_pins_pin_has_project CHECK (kind = 'forbid' OR project_id IS NOT NULL),
  CONSTRAINT fk_block_pins_project_id_projects
    FOREIGN KEY (project_id, user_id) REFERENCES projects (id, user_id)
);
"""

PLANS = """
CREATE TABLE week_plans (
  id            uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL,
  horizon       tstzrange NOT NULL,
  week_start    date NOT NULL,
  timezone      text NOT NULL,
  version       int  NOT NULL,
  status        plan_status NOT NULL DEFAULT 'proposed',
  solver_input  jsonb NOT NULL,
  infeasibility jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,
  CONSTRAINT pk_week_plans PRIMARY KEY (id),
  CONSTRAINT fk_week_plans_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT uq_week_plans_id_user_id UNIQUE (id, user_id),
  CONSTRAINT uq_week_plans_user_id_week_start_version UNIQUE (user_id, week_start, version),
  CONSTRAINT week_plans_one_live_excl
    EXCLUDE USING gist (user_id WITH =, horizon WITH &&) WHERE (status = 'live'),
  CONSTRAINT week_plans_one_proposed_excl
    EXCLUDE USING gist (user_id WITH =, horizon WITH &&) WHERE (status = 'proposed')
);

CREATE TABLE changesets (
  id          uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL,
  plan_id     uuid,
  staged      jsonb NOT NULL,
  status      changeset_status NOT NULL DEFAULT 'pending',
  created_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  CONSTRAINT pk_changesets PRIMARY KEY (id),
  CONSTRAINT fk_changesets_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_changesets_plan_id_week_plans
    FOREIGN KEY (plan_id) REFERENCES week_plans(id) ON DELETE SET NULL
);
CREATE INDEX changesets_user_id_pending_idx ON changesets (user_id) WHERE status = 'pending';

CREATE TABLE blocks (
  id            uuid NOT NULL DEFAULT gen_random_uuid(),
  plan_id       uuid NOT NULL,
  user_id       uuid NOT NULL,
  project_id    uuid NOT NULL,
  lineage_id    uuid NOT NULL,
  span          tstzrange NOT NULL,
  state         block_state NOT NULL DEFAULT 'planned',
  gcal_status   gcal_sync NOT NULL DEFAULT 'pending',
  gcal_event_id text,
  gcal_etag     text,
  CONSTRAINT pk_blocks PRIMARY KEY (id),
  CONSTRAINT fk_blocks_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_blocks_plan_id_week_plans
    FOREIGN KEY (plan_id, user_id) REFERENCES week_plans (id, user_id) ON DELETE CASCADE,
  CONSTRAINT fk_blocks_project_id_projects
    FOREIGN KEY (project_id, user_id) REFERENCES projects (id, user_id),
  CONSTRAINT blocks_no_overlap_excl EXCLUDE USING gist (plan_id WITH =, span WITH &&)
);

CREATE TABLE mutations (
  id             uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL,
  changeset_id   uuid,
  plan_id        uuid,
  kind           mutation_kind NOT NULL,
  payload        jsonb NOT NULL,
  before         jsonb,
  interpretation text,
  source         mutation_source NOT NULL DEFAULT 'agent',
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_mutations PRIMARY KEY (id),
  CONSTRAINT fk_mutations_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_mutations_changeset_id_changesets
    FOREIGN KEY (changeset_id) REFERENCES changesets(id) ON DELETE SET NULL,
  CONSTRAINT fk_mutations_plan_id_week_plans
    FOREIGN KEY (plan_id) REFERENCES week_plans(id) ON DELETE SET NULL
);
"""

# Reverse dependency order. Extensions are left installed: other schemas in the same
# database may use them, and `CREATE EXTENSION IF NOT EXISTS` makes re-upgrade safe.
TABLES_DOWN = (
    "mutations",
    "blocks",
    "changesets",
    "week_plans",
    "block_pins",
    "availability_exceptions",
    "user_preferences",
    "user_tags",
    "projects",
    "google_connections",
    "sessions",
    "identities",
    "users",
)
ENUMS_DOWN = (
    "mutation_kind",
    "mutation_source",
    "gcal_sync",
    "block_state",
    "changeset_status",
    "plan_status",
    "pin_kind",
    "day_diversity",
    "project_status",
    "project_kind",
    "connection_status",
)


def _run(sql: str) -> None:
    # asyncpg prepares each execute, and a prepared statement holds one command: split.
    # (No statement in this file contains a literal ';'.)
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)


def upgrade() -> None:
    for section in (EXTENSIONS, ENUMS, IDENTITY, GOOGLE, INTENT, PLANS):
        _run(section)


def downgrade() -> None:
    for table in TABLES_DOWN:
        op.execute(f"DROP TABLE {table}")
    for enum in ENUMS_DOWN:
        op.execute(f"DROP TYPE {enum}")
