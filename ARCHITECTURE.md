# ReclaimMyTime — Architecture & Data Schema

**Status:** Draft v2 · companion to `PRD.md`. Revised 2026-11-13 after design review (`reviews/architecture-review-2026-11-13.md`); v1 of this doc is in git history.

---

## 1. Stack

| Layer | Choice | Why |
|---|---|---|
| iOS app | SwiftUI, iOS 17+ | Native polish for a consumer app; decided in PRD. |
| API | Python 3.12+, FastAPI, Pydantic v2 | Pydantic doubles as the LLM tool-use extraction contract; mature Anthropic ecosystem. |
| Solver | Pure Python (`server/app/scheduler/`) | Deterministic packing + separate `verify()`; property-tested with `hypothesis`. Contract in §4. |
| DB | Postgres 16 (Neon; dev branching) | Interval math is native: `tstzrange` + exclusion constraints enforce PRD Principle 1 physically. `pgvector` exists if semantic memory is ever needed — **no vector DB otherwise; there is no retrieval problem** (per-user state is a few KB, passed whole into prompts). |
| DB access | SQLAlchemy 2.0 async (Core-leaning) + Alembic + asyncpg | Boring, typed, migration-tracked. |
| Auth | **Hand-rolled, Google sign-in (v1; Apple optional — T6.0, D26, 2026-11-16)** | One Google consent creates the account, session and calendar connection (no paid Apple Developer membership). Sign in with Apple is kept but off unless configured (503). Originally: iOS-only v1 means every user has an Apple ID. Email magic links deferred until a second platform exists — drops Resend and an entire brute-force/scanner/rate-limit hardening surface. |
| LLM | Anthropic API via backend proxy | Model id in config, not code. Conversation/extraction on Haiku 5.5 (§8). API key never ships in the app. |
| Google Calendar | `httpx` against Calendar REST (OAuth by hand; id_token claims checked with PyJWT — no `google-auth`, T4.1) | **Narrow scopes from day one:** free/busy read + app-created-calendar write. Makes the PRD §8 privacy promise ("titles never sent") true by construction, and is near-irreversible in the other direction once users grant broad scope. |
| Hosting | Fly.io (API) + Neon (DB) | Swappable. Consequence: machines restart/auto-stop, so the server holds **no in-memory session state** — the iOS client carries the chat transcript each turn. |
| Tooling | `uv`, `pytest`, `ruff`, `import-linter` | Layer contract enforced from the first commit. |

## 2. Repo Layout

```
ios/              # SwiftUI app (Xcode project)
server/
  app/
    api/          # FastAPI routers: auth, projects, preferences, exceptions, plans, changesets, agent, gcal
    auth/         # sessions, account deletion, Google sign-in identity (T6.0), optional Apple JWT verification
    agent/        # LLM layer: prompts, Pydantic extraction schemas, changeset assembly
    scheduler/    # deterministic solver + verify() — pure functions, zero I/O
    gcal/         # Google OAuth, freeBusy reads, write-back outbox
    db/           # SQLAlchemy models, Alembic migrations, persist_plan/apply_mutation write paths
  tests/
PRD.md
ARCHITECTURE.md
reviews/
```

**Load-bearing boundary (PRD §7):** `scheduler/` imports nothing from `agent/`, `api/`, `gcal/`, or `db/` — enforced by an `import-linter` contract **in the first commit**. The LLM never emits blocks; it assembles changesets and requests a re-solve. Structurally: future block rows are writable through exactly one function, `db.persist_plan(tx, SolverOutput)`, and constraint changes through `db.apply_mutation(tx, ...)` — so only solver output can ever create blocks, and every state change commits together with its trace row.

## 3. Schema (DDL)

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;

-- ---------- identity ----------

CREATE TABLE users (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email      citext UNIQUE,          -- nullable: Apple may provide none, or a private-relay address
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE identities (
  provider                text NOT NULL CHECK (provider IN ('apple', 'google')),  -- 0003 added google (T6.0)
  subject                 text NOT NULL,     -- provider `sub`
  user_id                 uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  email                   citext,
  email_verified          boolean NOT NULL DEFAULT false,
  is_private_email        boolean NOT NULL DEFAULT false,
  apple_refresh_token_enc bytea,             -- from authz-code exchange; required to revoke at account deletion
  created_at              timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, subject)
);
CREATE INDEX identities_user_id_idx ON identities (user_id);   -- account-scoped reads + cascade (T2.2 review, 2026-11-14)

CREATE TABLE sessions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   text NOT NULL UNIQUE,         -- sha256 of opaque bearer token
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,         -- 30-day sliding + 180-day absolute cap
  last_seen_at timestamptz                   -- updated at most hourly
);
CREATE INDEX sessions_user_id_idx ON sessions (user_id);   -- user-scoped revoke + cascade (T2.2 review, 2026-11-14)

-- ---------- google ----------

CREATE TYPE connection_status AS ENUM ('active','revoked');

CREATE TABLE google_connections (
  user_id           uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  google_email      citext NOT NULL,
  refresh_token_enc bytea NOT NULL,          -- MultiFernet-encrypted; access tokens never stored
  key_id            smallint NOT NULL DEFAULT 1,   -- encryption key rotation
  scopes            text[] NOT NULL,
  app_calendar_id   text,                    -- the app-owned "ReclaimMyTime" calendar
  status            connection_status NOT NULL DEFAULT 'active',
  revoked_at        timestamptz,             -- set on invalid_grant; UI prompts reconnect
  created_at        timestamptz NOT NULL DEFAULT now()
);
-- No sync-state table in v1: busy time comes from freeBusy per solve + app foreground,
-- with the app's own calendar explicitly excluded. Event mirror + events.watch come later.

-- ---------- intent ----------

CREATE TYPE project_kind   AS ENUM ('deadline', 'perennial');
CREATE TYPE project_status AS ENUM ('staged', 'active', 'paused', 'done', 'archived');
-- 'staged': row exists only so a proposed plan's blocks/pins can reference it (FK) while its
-- changeset is pending; never scheduled from live state. confirm → 'active', reject/expire → row deleted.

CREATE TABLE projects (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name                  text NOT NULL,
  kind                  project_kind NOT NULL,
  target_date           date,                -- end of that local day
  estimated_total_hours numeric(6,1),
  weekly_target_hours   numeric(5,1),
  max_block_minutes     int NOT NULL CHECK (max_block_minutes > 0),
  min_block_minutes     int NOT NULL CHECK (min_block_minutes > 0 AND min_block_minutes <= max_block_minutes),
  preferred_windows     jsonb,               -- {"mon":[["06:00","09:00"]],...} local; "workout mornings"
  preferred_strict      boolean NOT NULL DEFAULT false,   -- hard constraint vs scoring bias
  requirements          text[] NOT NULL DEFAULT '{}',     -- values drawn from user_tags
  status                project_status NOT NULL DEFAULT 'active',
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, user_id),                      -- composite-FK target
  CHECK (
    (kind = 'deadline'  AND target_date IS NOT NULL AND estimated_total_hours IS NOT NULL AND weekly_target_hours IS NULL) OR
    (kind = 'perennial' AND target_date IS NULL     AND estimated_total_hours IS NULL     AND weekly_target_hours IS NOT NULL)
  )
);
-- min_block_minutes has no DB default: the app sets LEAST(25, max_block_minutes).
-- remaining effort is computed, never stored: estimated_total_hours*60 − Σ completed block minutes.

CREATE TABLE user_tags (
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tag     text NOT NULL,
  PRIMARY KEY (user_id, tag)
);
-- The controlled vocabulary for requirements/blocks_tags. The extraction tool schema is built
-- per-request with these as a JSON-schema enum; a new tag requires explicit user confirmation.

CREATE TYPE day_diversity AS ENUM ('focused', 'mixed');

CREATE TABLE user_preferences (
  user_id             uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  timezone            text NOT NULL,         -- IANA; NO default — set from TimeZone.current at signup
  schedulable_windows jsonb NOT NULL,        -- {"mon":[["08:00","22:00"]], ...} local times
  day_diversity       day_diversity NOT NULL DEFAULT 'mixed',
  buffer_minutes      int NOT NULL DEFAULT 15,
  week_start          smallint NOT NULL DEFAULT 1 CHECK (week_start BETWEEN 1 AND 7)
);

CREATE TABLE availability_exceptions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  span        tstzrange NOT NULL,
  blocks_all  boolean NOT NULL DEFAULT false,
  blocks_tags text[] NOT NULL DEFAULT '{}',
  reason      text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (blocks_all OR cardinality(blocks_tags) > 0)   -- a forgotten tag list is an error, not a blackout
);

CREATE TYPE pin_kind AS ENUM ('pin','forbid');

-- Manual edits as CONSTRAINTS, not solver output: a drag/resize writes a 'pin'
-- (this project occupies exactly this span); a deletion writes a 'forbid'
-- (negative pin — never schedule this project, or anything, in this span).
CREATE TABLE block_pins (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id            uuid,                -- NULL only for a blanket forbid
  span                  tstzrange NOT NULL,
  kind                  pin_kind NOT NULL,
  created_from_block_id uuid,                -- provenance only; no FK (block rows churn with versions)
  created_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (kind = 'forbid' OR project_id IS NOT NULL),
  FOREIGN KEY (project_id, user_id) REFERENCES projects (id, user_id)
);

-- ---------- plans ----------

CREATE TYPE plan_status AS ENUM ('proposed','live','superseded','rejected');

CREATE TABLE week_plans (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  horizon       tstzrange NOT NULL,          -- absolute span this plan governs
  week_start    date NOT NULL,               -- descriptive (display, versioning key)
  timezone      text NOT NULL,               -- tz the horizon was derived in (descriptive)
  version       int  NOT NULL,
  status        plan_status NOT NULL DEFAULT 'proposed',
  solver_input  jsonb NOT NULL,              -- the serialized SolverInput (§4) — a solve is replayable
  infeasibility jsonb,                       -- violations + candidate relaxations (best partial plan is still stored)
  created_at    timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,
  UNIQUE (id, user_id),
  UNIQUE (user_id, week_start, version),
  -- liveness + proposal invariants, keyed on ABSOLUTE time so overlapping horizons
  -- across timezone changes or future rolling windows are caught:
  EXCLUDE USING gist (user_id WITH =, horizon WITH &&) WHERE (status = 'live'),
  EXCLUDE USING gist (user_id WITH =, horizon WITH &&) WHERE (status = 'proposed')
);

CREATE TYPE changeset_status AS ENUM ('pending','confirmed','rejected','expired');

-- The confirm/reject unit: staged constraint mutations + the proposed plan they produced.
CREATE TABLE changesets (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  plan_id     uuid REFERENCES week_plans(id) ON DELETE SET NULL,   -- the proposed plan
  staged      jsonb NOT NULL,                -- the mutations, unapplied until confirm
  status      changeset_status NOT NULL DEFAULT 'pending',
  created_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);
CREATE INDEX changesets_user_id_pending_idx ON changesets (user_id) WHERE status = 'pending';   -- pending lookup under the user lock (T2.1 review, 2026-11-15)

CREATE TYPE block_state AS ENUM ('planned','completed','skipped');
CREATE TYPE gcal_sync   AS ENUM ('pending','synced','failed');

CREATE TABLE blocks (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id       uuid NOT NULL,
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL,
  lineage_id    uuid NOT NULL,               -- stable across plan versions; keys diffs + gcal reconciliation
  span          tstzrange NOT NULL,
  state         block_state NOT NULL DEFAULT 'planned',
  gcal_status   gcal_sync NOT NULL DEFAULT 'pending',   -- the write-back outbox
  gcal_event_id text,
  gcal_etag     text,
  FOREIGN KEY (plan_id, user_id)    REFERENCES week_plans (id, user_id) ON DELETE CASCADE,
  FOREIGN KEY (project_id, user_id) REFERENCES projects   (id, user_id),
  -- PRD Principle 1, enforced physically: no overlapping blocks within a plan version
  EXCLUDE USING gist (plan_id WITH =, span WITH &&)
);
-- No 'active' state (it's just now() <@ span) and no 'displaced' state
-- (a superseded version + lineage_id already says that).

-- ---------- agent trace ----------

CREATE TYPE mutation_source AS ENUM ('agent','manual','system');
CREATE TYPE mutation_kind AS ENUM (
  'project_created','project_updated','project_archived',
  'preferences_updated','exception_added','exception_removed',
  'pin_added','pin_removed',
  'plan_proposed','plan_confirmed','plan_rejected'
);

-- Durable record of every state change (chat itself is ephemeral by decision).
CREATE TABLE mutations (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  changeset_id   uuid REFERENCES changesets(id) ON DELETE SET NULL,
  plan_id        uuid REFERENCES week_plans(id) ON DELETE SET NULL,
  kind           mutation_kind NOT NULL,
  payload        jsonb NOT NULL,
  before         jsonb,                      -- prior state of the touched entity
  interpretation text,                       -- the agent's one-line confirmation the user agreed to
  source         mutation_source NOT NULL DEFAULT 'agent',
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- One row per POST /agent/message turn: eval capture + the §8 per-user daily token budget
-- (migration 0004, T6.2, user decision 2026-11-17). Text columns are NULL unless AGENT_CAPTURE_TURNS is on.
-- 30-day retention: each insert purges EVERY user's expired rows (app.db.agent_turns; a row
-- exactly 30 days old is kept), so inactive users' captured text still ages out.
CREATE TABLE agent_turns (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at         timestamptz NOT NULL DEFAULT now(),   -- app passes `now`
  model              text NOT NULL,
  outcome            text NOT NULL CHECK (outcome IN ('completed','refused','error')),
  error_code         text,
  stop_reason        text,
  model_calls        int NOT NULL CHECK (model_calls >= 0),
  input_tokens       int NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens      int NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  cache_read_tokens  int NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
  cache_write_tokens int NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
  transcript_len     int NOT NULL,
  staged             jsonb NOT NULL DEFAULT '[]',          -- same shape as changesets.staged
  changeset_id       uuid REFERENCES changesets(id) ON DELETE SET NULL,
  plan_id            uuid REFERENCES week_plans(id) ON DELETE SET NULL,
  user_message       text,                                 -- only with AGENT_CAPTURE_TURNS
  snapshot           text                                  -- rendered state snapshot, ditto
);
CREATE INDEX agent_turns_user_id_created_at_idx ON agent_turns (user_id, created_at);
CREATE INDEX agent_turns_created_at_idx ON agent_turns (created_at);   -- global retention purge

-- One agent turn in flight per user, across every server process (migration 0006, issue #3).
-- A lease, not a lock: POST /agent/message inserts it before any work and deletes it on every
-- exit, holding no connection or transaction in between. TTL = open timeout + turn timeout +
-- 30 s (180 s by default), judged against the request `now`, so a holder that died blocks its
-- user for 3 minutes at most; the next acquire overwrites an expired row. Release deletes by
-- `token`, so a holder whose lease was taken over never deletes the new holder's row.
CREATE TABLE agent_turn_leases (
  user_id    uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  token      uuid NOT NULL,          -- identifies the holder; release matches on it
  expires_at timestamptz NOT NULL
);
```

### Semantics

- **Propose → confirm lifecycle (Journeys 2 & 3):** an agent turn or manual edit assembles a `changeset` (staged mutations, unapplied) and runs the solver against the *hypothetical* state, storing the result as a `proposed` plan. `POST /changesets/{id}/confirm` runs one transaction under `pg_advisory_xact_lock(hash(user_id))`: apply staged mutations via `apply_mutation` → set the old live plan `superseded` → promote the proposal to `live` (**supersede strictly before promote** — exclusion constraints are checked per statement and cannot be deferred) → copy forward past/in-progress block rows verbatim with their lineage → mark changed blocks' `gcal_status='pending'`. Reject/abandon leaves constraint state untouched; the plan row becomes `rejected`. The advisory lock serializes concurrent re-solve triggers (agent message, manual edit, drift detection). **Lock key, fixed:** `pg_advisory_xact_lock(hashtext(user_id::text))` — auth (sign-in for an existing identity, account deletion; T2.2) uses the same key, so changeset work must too.
- **Staged writes (ratified 2026-11-13, contracts D2/O1):** every write that changes scheduling inputs — project/exception/preferences CRUD included — is staged into a changeset and returns `202 + Changeset`; nothing is applied until confirm. A `project_created` staging inserts the project row immediately with `status='staged'` so the proposal's blocks and pins satisfy their FKs; `confirm` flips it to `active` inside the same transaction, `reject`/`expire` deletes it. Only `name`-only edits may apply directly. At most one pending changeset per user; a newer one expires the older.
- **Conflict order, fixed:** calendar busy time > availability exceptions > pins. A pin invalidated by something above it is **reported in `infeasibility`**, never silently dropped (PRD Principle 2).
- **Block deletion** is `PATCH`-as-forbid: write a `forbid` pin, re-solve. The solver cannot resurrect what the user removed.
- **Lazy sweep, no job:** on read, past `planned` blocks become `completed` (the default — defaulting to "missed" would poison the remaining-effort math that drives deadline pressure). "I skipped this" flips to `skipped`.
- **Write-back outbox:** after confirm, an idempotent worker drives `gcal_status` `pending → synced`, writing `extendedProperties.private.lineage_id` on each event so either side can be reconciled. An edit made to our event inside Google Calendar becomes a **pin**, applied by the **hybrid rule** (T4.3b, 2026-11-16): applied directly when the re-solve moves no other future block; otherwise the moved block applies directly if it overlaps nothing and the knock-on re-solve is staged as a `google_edit` proposal; a move onto another block stages everything. A Google delete applies a `forbid` directly and stages any re-placement. Rejecting a `google_edit` proposal restores our planned time on the event. Detection is by etag on `POST /gcal/sync` (contracts D25). A 404 on `app_calendar_id` re-pends, then recreates the calendar.
- **Busy time & drift:** `freeBusy` over the horizon on each solve and app-foreground, **app calendar excluded**. Drift = live blocks overlapping fresh busy time → proposed re-solve surfaced on foreground / `BGAppRefreshTask`. No APNs in v1 (trigger to add: wanting drift alerts while the app is closed → `device_tokens` table + push).
- **Chat is ephemeral and the server stateless:** the iOS client holds the transcript and sends it each turn (Fly machines restart/auto-stop). Exception (T6.2, 2026-11-17): with `AGENT_CAPTURE_TURNS` on, each turn's last user message and state snapshot are kept in `agent_turns` for evals for 30 days; off by default. Each project is persisted the moment its summary card is confirmed, so a dropped conversation costs at most the project in progress.
- **Tokens:** only refresh tokens stored, MultiFernet-encrypted (`key_id` enables rotation); access tokens minted on demand, in memory. Google OAuth uses PKCE + signed `state` bound to the session's user id, `access_type=offline&prompt=consent`.
- **Time:** windows and preferences are local wall-clock + IANA tz; blocks, exceptions, pins, and horizons are absolute (`tstzrange`). Projection local→absolute happens **before** the solver boundary; nothing inside the solver does timezone math. The device sends its timezone on every request — a change triggers the PRD §10 re-solve prompt.

## 4. Solver Contract (M1)

```python
class SolverInput(BaseModel):        # versioned: input_schema_version, solver_version
    now: AwareDatetime               # injected — the solver never reads a clock
    horizon: Interval                # absolute
    busy: list[Interval]             # absolute, app calendar already excluded
    schedulable: list[Interval]      # absolute — windows already projected through the user's tz
    frozen: list[FrozenBlock]        # past/in-progress blocks, copied verbatim (lineage_id, project_id, span, state)
    pins: list[Pin]                  # kind pin|forbid, absolute spans
    projects: list[ProjectInput]     # remaining_minutes / weekly_target_minutes (INTEGER MINUTES),
                                     # max/min block minutes, preferred windows (absolute) + strict flag, requirement tags
    exceptions: list[ExceptionInput] # absolute spans + tags / blocks_all
    day_diversity: Literal['focused','mixed']
    buffer_minutes: int

class SolverOutput(BaseModel):
    blocks: list[PlannedBlock]       # NEW blocks only — frozen blocks are not echoed (confirm copies them forward, §3);
                                     # lineage_id reused when a block is "the same session, moved" → feeds the diff UI
    infeasibility: Infeasibility | None   # violations (DemandShortfall | PinInvalidated) + candidate relaxations;
                                          # best partial plan is still returned; verify_violations keeps the raw list
```

Decisions from T1.2 (2026-11-14): `ProjectInput.target_date` (optional local date) feeds deadline pressure and `extend_deadline`; relaxation values are in **solver units (minutes)** — the API layer converts to D17's hours and adds completed work when building a `PATCH /projects` body; `verify()` reports `pin_missing` only for a pin that could actually be placed — an unplaceable pin (busy/exception/forbid/window/horizon) is reported by `pack()` as `PinInvalidated`, so the best partial plan still verifies clean.

Rules:
- **Strict preferred windows replace availability** (2026-11-17): a strict project may be placed anywhere in its own preferred windows, even outside `schedulable` (empty strict windows = no time); busy, exceptions, forbid pins, `now`, horizon and deadline still cut it. Non-strict windows are only a scoring bias inside `schedulable`. `verify()` skips the `window` check for strict projects (`preferred_window` covers them); the slack rule measures load inside `schedulable` only.
- **One perennial block per day — hard** (solver 1.2.0, 2026-11-18): a perennial never gets a second block on a day (24 h from `horizon.start`) where it has a frozen, pinned or placed one; a week without enough days is a `DemandShortfall` with relaxations. The rule binds the solver, not the user: pinned blocks may share a day (pin+pin, pin+frozen) and an ordinary block on a pinned/frozen day moves or becomes shortfall; `verify()` reports `perennial_daily` only for an ordinary planned block sharing a day with another block of the project. A perennial short on days is offered `raise_max_block_minutes` = ceil(owed ÷ usable days). `focused` theming applies to deadline projects only; greedy pressure is measured before the daily cut.
- **Integer minutes everywhere.** No floats, no `datetime.now()`, no environment reads, no tz math (inputs are absolute).
- **Deterministic:** stable sort keys only — never iterate a `set` into an ordering. Tested by running identical input under two `PYTHONHASHSEED` values in subprocesses and comparing outputs byte-for-byte.
- **`verify(plan, input) -> list[Violation]`** is a separate pure module from the packer: checks overlap, busy/window/exception/pin/buffer violations. Called before every `persist_plan`; doubles as the `hypothesis` property-test oracle. The DB exclusion constraint only covers block-vs-block — `verify()` covers everything else.
- **Golden replay tests:** stored `solver_input` snapshots from real plans re-solve to identical output across releases (or the change is a deliberate, versioned solver bump).

## 5. Pydantic Extraction Contracts

`server/app/agent/schemas.py` defines `ProjectDraft`, `PreferencesDraft`, `ExceptionDraft`, `PinDraft` — the Anthropic tool-use schemas *and* the API request bodies. The deadline/perennial XOR is a model validator matching the DB CHECK. Two rules from the review:
- **The LLM never writes an absolute timestamp.** Extraction fields are local dates/times; the server applies the user's timezone. `now` and the timezone are always in the prompt; the agent's one-line confirmation (stored as `mutations.interpretation`) is the safety net for "tomorrow"-near-midnight ambiguity.
- **Tags are a closed enum per request,** built from `user_tags`. A tag outside the vocabulary requires explicit user confirmation, which inserts into `user_tags` first. A user has at most 50 tags (`MAX_USER_TAGS`): a write that would add a tag past that is refused with 422 `validation_failed` (a tool error when the agent stages it), and a write that only uses existing tags is never refused (issue #7, 2026-11-25).
- **Per-user row caps:** at most 100 projects that are not archived (`MAX_USER_PROJECTS`), 200 exceptions and 200 pins/forbids whose span has not ended (`MAX_USER_EXCEPTIONS`, `MAX_USER_PINS`); a write that would add a row past a cap is refused with 409 `limit_reached` at staging and again at confirm (a tool error when the agent stages it), judged on the changeset's net result, so updates, archives and removals are never refused, and Google Calendar edits (`system` pins) are exempt (`check_row_caps`, issue #24, 2026-11-25).

## 6. Auth Design (Google sign-in, v1; Apple optional)

- **Google sign-in (T6.0, decided 2026-11-16; contract D26):** one combined consent (openid email + calendar.freebusy + calendar.app.created). `POST /auth/google/start {nonce_hash}` → authorization URL; `/gcal/callback` (state purpose=login) redirects `auth_code`+`state` into the app; `POST /auth/google/complete {auth_code, state, nonce}` → session. Login CSRF is bound by the app nonce whose hash sits in the encrypted state. Identity `(google, sub)`, no account merging; calendar scopes unticked → account without a connection. **A sign-in never replaces an active calendar connection to a different Google account** (M6 gate, decision 2026-11-18): the new grant is discarded and switching calendars stays an explicit reconnect.
- **Sign in with Apple (optional, original v1 design below):** enabled only when configured; otherwise `/auth/apple` returns 503.

- **Flow:** app requests Apple sign-in with a SHA-256 **nonce** → server verifies the identity token against Apple's cached JWKS, checks signature, `exp`, `nonce`, `iss = https://appleid.apple.com`, `aud =` bundle id → upserts `identities(provider='apple', subject=sub)`.
- **Email linking:** `users.email` set only when Apple reports it verified and **not** private-relay; otherwise null. (Prevents duplicate-account hell when email auth arrives later.)
- **Account deletion (App Store requirement):** the authorization code is exchanged at sign-in and Apple's refresh token stored encrypted — `DELETE /me` revokes it, then cascades the user row. Subscribe to Apple's server-to-server notifications (consent revoked, email changed).
- **Sessions:** opaque 256-bit bearer tokens, sha256-stored; 30-day sliding expiry with `last_seen_at` written at most hourly; 180-day absolute cap; `DELETE /sessions` revokes all.
- **Deferred:** email magic links (trigger: a second platform, or App Review demanding a demo login). The v1 door stays open via the `identities` table.

## 7. API Surface (sketch — schemas come with implementation)

```
POST   /auth/google/start|complete   POST /auth/apple (optional)   GET /me        DELETE /me        DELETE /sessions[/current]
CRUD   /projects                 PUT /preferences                 CRUD /exceptions
GET    /plans/current            GET /plans/{id}/diff?against={id}
PATCH  /blocks/{id}              (complete | skip | edit→pin | delete→forbid; edits trigger re-solve)
POST   /agent/message            (client sends transcript; returns reply + changeset when actionable)
POST   /changesets/{id}/confirm  POST /changesets/{id}/reject
GET    /gcal/connect             GET /gcal/callback
```

## 8. Agent Runtime

**Compute & hosting:** the agent is not a resident process — it is the Anthropic Messages API (first-party), called from FastAPI handlers. Inference runs on Anthropic's infrastructure; our Fly machines only assemble context, execute tool calls against Postgres, and stream tokens to iOS. Managed Agents was evaluated and rejected: our loop is short request/response, tools are DB-local staging functions, no sandbox is needed. The API key lives in Fly secrets.

**Invocation map (v1):** exactly one entry point, `POST /agent/message` — onboarding intake and mid-week utterances share it (same tool surface, different conversation state). When a turn produces a proposal, the solver runs *inside* the turn as a tool and its diff/infeasibility comes back as the tool result, which the model narrates in the same reply — never a second call. **The LLM is never invoked without a user action.** Proposals arising from manual edits or drift detection get deterministic template text; the agent's voice appears only in chat.

**Write discipline:** the model's tools (decided 2026-11-15, M3) are one per mutation kind — `stage_project_create`, `stage_project_update`, `stage_project_archive`, `stage_preferences`, `stage_exception_add`, `stage_exception_remove`, `stage_pin_add`, `stage_pin_remove` — plus `discard_staged` and `propose_schedule`. Every agent turn builds on the pending changeset; staging happens in memory, `propose_schedule` solves on a rolled-back transaction so the model can narrate, and the one real write (`propose_changeset`) happens right before `done` — so an `error` never leaves anything staged, and no lock is held while the model generates. If the model stages but never proposes, the server proposes at turn end. The commit re-checks, under the user lock, that the pending changeset the turn built on is still the pending one; otherwise the turn ends with `error` `changeset_stale` and writes nothing. A turn that only re-proposes the unchanged pending list returns that changeset's card without writing. A dry run counts as narrated only once a later model call has received its result; whenever the persisted solve (fingerprint of staged list, blocks, diff and infeasibility) is not the narrated one — staged without proposing, propose on the last allowed call, or state moved — `done.reply` is **replaced** by "Here's the proposal:" + the T3.3 template narration, so it always describes `done.changeset` (M3 gate, user decision 2026-11-15). Cap: 8 model calls per turn (hitting it ends the turn the same way; with nothing staged it is an `error`). The model refers to rows by per-turn labels (`P1`, `E1`, `N1`, `S1`) listed in the snapshot, never UUIDs. Tool schemas are **static** (byte-identical for all users and turns: a schema change mid-turn would invalidate replayed thinking blocks, and an empty enum is invalid), so label and tag membership is checked by the server and returned as an error `tool_result`. New tags go in `new_tags`, only after explicit user agreement; near-duplicates of existing tags (case, space, `-`, `_`) are refused. All tools are `strict: true` except `stage_project_create`, `stage_project_update` and `stage_preferences`, which run non-strict because the week-window shape pushes the strict grammar over the API's size cap (c8dcafd, 2026-11-17); for them the server's draft validation is the guard, and a missing `*_set` flag means unset. Schemas are generated from the agent's Pydantic drafts (local dates/times only); `eager_input_streaming` stays off, because it disables the server-side validation strict mode provides. Forced `tool_choice` (`any`/`tool`) returns a 400 on current models — extraction is steered by the system prompt plus strict schemas; do not reintroduce it. Tool inputs are validated against the Pydantic schema before execution regardless of `strict`.

**Context assembly, per turn (ordered for prompt caching — tools → system → messages):**
1. *Cached prefix* (one breakpoint): tool schemas + static system prompt (purpose, extraction rules, out-of-scope refusals).
2. *Transcript*: supplied by the iOS client (server stateless, §3 Semantics).
3. *Volatile state snapshot*, after the cache boundary, injected as a mid-conversation system message (cache-safe on Haiku 5.5) and built once per turn: labelled projects + constraints, preferences, exceptions, pins, staged-but-unconfirmed changes, a compact live-plan rendering, remaining effort per deadline project, busy intervals (**intervals only — never event details**, §1 privacy), `now` + IANA timezone, and `user_tags` (listed here, not as a schema enum — tool schemas are static). ~1–3K tokens: the whole state, no retrieval.

**Model & mechanics:** `claude-haiku-5-5` ($0.10/$0.50 per MTok → ≈$0.0005 per typical turn), id in config — an upgrade is a config change (note: prompt caches are per-model). Adaptive thinking at default effort; streaming (SSE passthrough to the app); no prefill (removed on current models). **Haiku has no server-side refusal fallback:** the handler checks `stop_reason == "refusal"` and replies with a polite decline, zero state change.

**Quality & cost guardrails:** the `(state snapshot, staged mutations, solver_input)` triples we already persist double as eval raw material — build an extraction eval before TestFlight so any model upgrade (Haiku → Sonnet/Opus) is measured, not vibes. Haiku's extraction risk degrades safely by design: strict schemas, closed tag enums, confirmation cards, and `verify()` turn weakness into clarifying questions, not corrupted state. Every turn is captured in `agent_turns` (§3): token counts, outcome, staged result and ids always; the user's text and the state snapshot only when `AGENT_CAPTURE_TURNS` is on; 30-day retention. `POST /agent/message` enforces three per-user limits (issue #3, 2026-11-24): (1) a **daily token budget**, 1,000,000 tokens per rolling 24 h by default (`AGENT_DAILY_TOKEN_BUDGET`), summing `agent_turns` (input + output + cache-write tokens, `tokens_used_since`; there is no separate `llm_usage` table) — checked before the first model call (429 `agent_budget_exceeded`) and by the loop before each later call against that baseline plus the turn's own tokens (in-stream `error` `agent_budget_exceeded`, nothing staged, the turn still recorded); (2) a **32,000-character transcript** total (code points; 422 `validation_failed`), which the client trims to; (3) **one turn in flight**, a cross-process DB lease in `agent_turn_leases` (§3) taken before any other work and released on every exit — a concurrent request gets 429 `agent_turn_in_progress`, not queued. The loop stays DB-free: the router reads the baseline and passes it in.

## 9. Deferred, With Triggers

| Deferred | Add when |
|---|---|
| APNs + `device_tokens` | drift alerts needed while app is closed |
| Event mirror + `events.watch` channels | freeBusy polling proves insufficient |
| Email magic links | second platform, or App Review requires demo access |
| Plan-version GC job | `blocks` approaches ~1M rows (keep live + solver_input after week end; drop rejected after 7 days) |
| — (not deferred, noting here) iOS caps pending local notifications at 64 | v1 schedules a rolling 48h window, topped up on foreground/background refresh |
