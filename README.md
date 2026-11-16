# ReclaimMyTime

An iOS app that gives you back a weekly timetable: describe your projects in a conversation,
and a deterministic scheduler blocks out your week around your Google Calendar.

> **Status:** in active development. Not deployed, not on the App Store. See [Status](#status).

## The idea

A lot of time in a week goes unaccounted for. The high-school timetable worked because every
hour had a name: you knew what you were doing, when, and for how long. ReclaimMyTime brings
that framework to adult life: "for the next two hours I'm working on this, and that's all the
time it gets today." The block gives you focus while it runs, and urgency because it ends.

The AI is plumbing, not the product. The product is a visible, trustworthy week you can hold
in your head. The conversational agent exists so that building and maintaining it takes a
sentence rather than a form or a drag-and-drop session.

## How it works

1. **Capture.** You chat with the agent about what you're working on. It extracts structured
   projects: deadline-bound ("portfolio site, ~30 hours, 3 weeks") or perennial ("gym, 4 h a
   week"), maximum focus length per sitting, and requirements such as `laptop` or `gym`. It
   also captures preferences: schedulable hours, buffers between blocks, and whether you like
   one-project days or variety within a day. Each project is confirmed on a summary card.
2. **Solve.** A deterministic solver packs project blocks into the free time between your
   calendar events. It respects block-length caps, spreads deadline work across the week,
   paces against remaining effort, places perennial habits as a rhythm, and honours buffers
   and wake/sleep windows.
3. **Propose, then confirm.** Every change, whether from the agent, a manual drag in the
   timetable, or an edit to a project, is staged as a *changeset*: the proposed plan plus a
   before/after diff. Nothing is applied until you confirm it.
4. **Adjust mid-week.** "I don't have my laptop today or tomorrow" becomes a temporary
   availability exception. Only the rest of the week is re-solved: laptop work moves later,
   other work may move earlier to fill the gap, and you review the diff before accepting.
5. **Write back and remind** *(in progress)*. Confirmed blocks go to a dedicated
   "ReclaimMyTime" calendar in Google Calendar, and local notifications fire at block start.

## Design principles

- **The LLM never produces a schedule.** The system has two layers. The LLM layer handles
  conversation, extracts constraints into schema-validated tool calls, and explains results.
  The solver layer is pure, deterministic code and produces every block. The model can only
  stage constraint changes and ask for a re-solve. A valid schedule is guaranteed by
  construction, not by trusting model output.
- **The schedule is always valid.** Blocks never overlap each other or calendar events, and
  never break a stated constraint. A separate `verify()` checks every plan before it is
  stored, and Postgres exclusion constraints back it up at the storage layer.
- **Past blocks are immutable.** Re-scheduling only touches the remainder of the week.
- **Infeasibility is a choice, not a failure.** When the constraints can't all be met, the
  solver returns its best partial plan plus a structured explanation of what conflicts, by how
  much, and with candidate relaxations (extend the deadline, raise a cap, drop something).
  You pick one.
- **Your calendar is ground truth.** Calendar busy time beats temporary exceptions, which beat
  manual pins. A pin that can no longer be honoured is reported, never silently dropped.
- **Free/busy only.** The Google integration requests free/busy read access plus write access
  to the app's own calendar. Event titles and details are never read, so they can never reach
  the LLM.
- **Deterministic and replayable.** The same input always produces the same schedule. Every
  stored plan keeps the exact solver input that produced it.

## Architecture

```mermaid
flowchart LR
    App["iOS app<br/>(SwiftUI)"] -- "REST + SSE" --> API["FastAPI"]
    API --> Agent["Agent layer<br/>(LLM: talk + extract)"]
    Agent -- "stage mutations,<br/>request re-solve" --> API
    API --> Solver["Solver<br/>(pure, deterministic)"]
    API --> DB[("Postgres 16")]
    API -. "free/busy, write-back" .-> GCal["Google Calendar"]
    Agent -. "Messages API" .-> LLM["Anthropic API"]
```

| Layer | Choice |
|---|---|
| iOS app | SwiftUI, iOS 17+, Swift 6 with strict concurrency |
| API | Python 3.12, FastAPI, Pydantic v2 |
| Solver | Pure Python: greedy, scored packing plus an independent `verify()`; property-tested with Hypothesis |
| Database | Postgres 16; SQLAlchemy 2.0 (async) + asyncpg; Alembic migrations |
| Auth | Sign in with Apple; opaque, hashed bearer sessions |
| LLM | Anthropic Messages API, called only from the server (the API key never ships in the app); model id is configuration |
| Tooling | uv, pytest, Ruff, import-linter, GitHub Actions |

A few details that shape the code:

- **Enforced layering.** `server/app/scheduler/` imports nothing from the API, agent,
  database or calendar packages, and only the agent route can reach the LLM client. Both
  rules are `import-linter` contracts that run in CI.
- **Single write paths.** Future blocks can only be created by persisting solver output, and
  constraint state only changes through one mutation function. That function writes a durable
  trace row in the same transaction.
- **Stateless chat.** The client keeps the transcript and sends it with each turn. The server
  streams the reply over Server-Sent Events. The LLM is only ever invoked in response to a
  user message. Proposals from manual edits get deterministic template text instead.
- **Time handling.** Preferences are local wall-clock times plus an IANA timezone. Everything
  the solver sees is an absolute instant, so the solver does no timezone math. The client
  sends its timezone on every request.

### Repository layout

```
contracts/      OpenAPI 3.1 spec, conventions, JSON fixtures shared by server tests and the iOS mock
design/         Visual design spec (tokens, onboarding, timetable, diff and trade-off views)
ios/            SwiftUI app (ReclaimMyTime.xcodeproj) and unit tests
server/
  app/
    scheduler/  Solver models, pack(), verify(): no I/O, no clock, no environment reads
    agent/      Tool schemas, prompt, per-turn staging, narration, /agent/message SSE loop
    api/        REST routers (projects, preferences, exceptions, pins, blocks, plans, changesets)
    auth/       Sign in with Apple verification, sessions, account deletion
    db/         SQLAlchemy models, solver I/O, write paths
    gcal/       Google Calendar integration (in progress)
  alembic/      Migrations
  tests/        Unit, property, golden-replay, contract and Postgres-backed tests
```

## Status

| Area | State |
|---|---|
| Solver: packing, `verify()`, typed infeasibility, determinism and golden-replay suites | Done |
| Persistence and REST API: migrations, propose/confirm changesets, CRUD and plan endpoints | Done |
| Auth: Sign in with Apple, sessions, account deletion with Apple token revocation | Done |
| Conversational agent: strict tool schemas, staging, streaming turn loop, refusal handling | Done (tested against a recorded/fake model transport) |
| iOS app: onboarding chat, timetable week/day views, drag/resize/delete as staged edits, diff review, trade-off sheet, notifications | Done, running against a fixture-backed mock transport |
| Google Calendar: OAuth connect (PKCE, bearer-bound completion), status, disconnect with revocation | Done |
| Google Calendar: free/busy reads on every solve (primary calendar; fails closed when Google is unreachable or the grant is revoked) | Done |
| Google Calendar: write-back of confirmed blocks to a dedicated "ReclaimMyTime" calendar (idempotent outbox, retried on sync) | Done |
| Google Calendar: edits made in Google flow back as pins, drift detection | In progress |
| Live end-to-end integration (iOS app against the real server) | Not started |
| Onboarding intent-narrative illustrations | Placeholder in the app |

Explicitly out of scope for v1: Android or web clients, calendar providers other than
Google, shared or team schedules, time tracking, analytics, widgets, and watch apps.

## Local development

### Server

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). All commands run from `server/`.

```bash
cd server
uv sync                     # creates server/.venv with runtime + dev dependencies
source .venv/bin/activate   # or prefix the commands below with `uv run`

pytest                      # full suite (Postgres-backed tests skip without a database)
ruff check . && ruff format --check .
lint-imports                # layering contracts (solver purity, LLM behind the agent route)
pytest -m slow              # long Hypothesis property runs (~15 min; nightly in CI)
```

**Postgres-backed tests.** Database tests are skipped unless `TEST_DATABASE_URL` points at a
Postgres 16 instance. Each test migrates its own throwaway schema with the real Alembic
migration. For example, with the same settings CI uses:

```bash
docker run -d --name rmt-pg -p 5432:5432 \
  -e POSTGRES_USER=rmt -e POSTGRES_PASSWORD=rmt -e POSTGRES_DB=rmt_test postgres:16
export TEST_DATABASE_URL=postgresql+asyncpg://rmt:rmt@localhost:5432/rmt_test
pytest -rs                  # -rs lists any skipped tests and why
```

**Contract fixtures.** Check that every fixture in `contracts/fixtures/` still validates
against the OpenAPI spec:

```bash
uv run --with openapi-spec-validator --with 'jsonschema[format]' --with rfc3339-validator \
  python ../contracts/validate_fixtures.py
```

**Running the API.** `uvicorn app.main:app --reload` starts the server on port 8000.
`GET /health` needs no configuration. Settings are read from the environment on first use,
not at import:

| Variable | Used for |
|---|---|
| `DATABASE_URL` | App and Alembic (`postgresql+asyncpg://...`); apply migrations with `alembic upgrade head` |
| `APPLE_BUNDLE_ID` | Expected audience of Sign in with Apple identity tokens |
| `AUTH_FERNET_KEYS` | Encryption keys for stored tokens, as `<key_id>:<fernet-key>[,...]` (first is primary) |
| `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY` | Apple REST client (authorization-code exchange, revocation) |
| `ANTHROPIC_API_KEY` | Read by the Anthropic SDK for `/agent/message` |
| `AGENT_MODEL` | Optional model id override |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Google OAuth web client; the redirect URI points at `/gcal/callback` |
| `GCAL_STATE_KEY` | Fernet key that encrypts the OAuth `state` (must differ from every `AUTH_FERNET_KEYS` entry) |
| `GCAL_APP_CALLBACK_URL` | Optional; app redirect target, default `reclaimmytime://gcal/callback` |

Authenticated routes need real Sign in with Apple credentials, so running the full API
locally is currently only practical for the authors. The test suite needs none of these
variables because it injects fakes.

### iOS

Requires Xcode 26 (the project targets iOS 17 and Swift 6). Open
`ios/ReclaimMyTime.xcodeproj` and run the **ReclaimMyTime** scheme.

- The shared scheme launches with `-UseMockAPI YES`, so the app runs entirely against the
  JSON fixtures in `contracts/fixtures/`. No server or Apple account is needed. The fixtures
  are referenced from the repository, so build from a full checkout.
- In Debug builds, `-DemoScenario week|day|review|tradeoff` opens the app on a canned mock
  state (useful for screenshots).
- Live mode (`-UseMockAPI NO`, base URL from the `APIBaseURL` user default, default
  `http://localhost:8000`) exists but has not been exercised end to end yet.

Run the unit tests with **Product > Test** (Cmd-U), or from the command line:

```bash
xcodebuild test -project ios/ReclaimMyTime.xcodeproj -scheme ReclaimMyTime \
  -destination 'platform=iOS Simulator,name=<an installed simulator>'
```

The tests cover decoding of every contract fixture, the API client and SSE parser, the chat
and changeset flows, notification planning, and design tokens.

## API contract

The server and the iOS client share a single contract:

- [`contracts/openapi.yaml`](contracts/openapi.yaml): OpenAPI 3.1 spec for every route.
- [`contracts/NOTES.md`](contracts/NOTES.md): conventions the schema can't express (auth
  header, required `X-Timezone`, error envelope, time-range encoding, the staging model, and
  the SSE event format for `POST /agent/message`).
- [`contracts/fixtures/`](contracts/fixtures/): example responses. The server's contract tests
  and the iOS mock transport both use them.

The visual design spec is in [`design/README.md`](design/README.md).

## Continuous integration

[`.github/workflows/server.yml`](.github/workflows/server.yml) runs on pushes and pull requests
that touch `server/` or `contracts/`. It runs Ruff (lint and format check), the import-linter
contracts, and the full test suite against a Postgres 16 service, and fails if any
database-backed test was skipped. It then round-trips the contract fixtures. The slow property
suite runs nightly. There is no iOS CI yet.
