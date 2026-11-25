# ReclaimMyTime — rules for every agent

iOS (SwiftUI) app for personal time blocking. An AI agent converses with the user to capture
projects and constraints; a deterministic solver turns those constraints into a weekly timetable
around their Google Calendar. Full spec: **PRD.md** (read it before any feature work). Stack, DDL,
solver contract and agent runtime: **ARCHITECTURE.md**. Task breakdown: **TECH_SPEC.md**.

## Standing architectural rules

1. **Two-layer agent design (load-bearing, PRD §7):**
   - LLM layer = interface only: conversation, constraint extraction to schema-validated JSON,
     explaining solver output.
   - Solver layer = pure, deterministic, unit-testable code that produces every schedule.
   - **The LLM never emits blocks, times, or schedules directly.** It mutates constraint state and
     requests a re-solve. Any change that violates this boundary is wrong by definition.
2. Past blocks are immutable; rescheduling touches only the remainder of the week.
3. Infeasible constraints surface as explicit user trade-offs, never silent failures.
4. Calendar event titles/details never go to the LLM — busy intervals only (structural:
   free/busy-only Google scope).
5. Solver results are proposals until the user confirms a changeset; only
   `persist_plan`/`apply_mutation` write plans/state. Conflict order: calendar > exceptions > pins;
   invalidated pins are reported, never dropped.
6. Agent runtime (ARCHITECTURE.md §8): Anthropic Messages API, `claude-haiku-5-5` from config;
   single entry point `POST /agent/message`; the LLM is never invoked without a user action;
   `stop_reason == "refusal"` → polite decline, no state change; forced `tool_choice` is a 400 on
   current models — never reintroduce it.

## Conventions

- **Layout:** SwiftUI app (iOS 17+) in `ios/`; Python backend in `server/` (deps and tooling in
  `server/pyproject.toml`); API contract in `contracts/` (frozen — changes need a human decision);
  design specs in `design/` (`tokens.md` for design tokens). Prod: Fly.io + Postgres 16 on Neon.
- `server/app/scheduler/` is pure: it imports nothing from `agent/`, `api/`, `gcal/`, or `db/`
  (enforced by `lint-imports`). No vector DB.
- Own auth (Google sign-in, one consent that also connects the calendar; opaque sessions) —
  ARCHITECTURE.md §6. Chat is ephemeral and the server stateless (client carries the transcript);
  the `mutations` table is the durable agent trace.
- Solver output changes on the golden corpus need a written justification and a `SOLVER_VERSION`
  bump (`server/app/scheduler/pack.py`).
- Tests for all new features, covering happy path and edge cases. Match surrounding code; don't
  reformat files you didn't otherwise change.
- **Never** deploy, run migrations against Neon, or point anything at production. The iOS app
  defaults to prod, so always launch it with `-UseMockAPI YES` (below). Sole exception: the
  user-only `/launch-prod` skill (`disable-model-invocation`). No agent or subagent may invoke it,
  run its commands, or launch the app without `-UseMockAPI YES`.
- Never run `rm`, `rmdir`, `unlink`, `find -delete` or `rmtree`. Leave stray files and say so.

## Fresh checkout setup

- **Server:** `cd server && uv sync --locked && source .venv/bin/activate`. Run tools from the
  activated venv (`pytest`, `ruff`, `lint-imports`), not `uv run`. Tests need no `.env`.
- **Test database:** a local Postgres 18 cluster at `localhost:54329` (trust auth), data in
  `~/.local/share/reclaimmytime/pgdata`; `factory/checks.sh` starts it if it's down. Tests use
  `TEST_DATABASE_URL=postgresql+asyncpg://postgres@localhost:54329/reclaim_test3` and never
  any other database. Never DROP a database; if `reclaim_test3` gets polluted, ask a human.
- **iOS:** Xcode at `/Applications/Xcode.app`; no setup beyond the project file.

## Validation commands (what `factory/checks.sh` runs)

From `server/` with the venv active:

```bash
ruff check . && ruff format --check .
lint-imports
TEST_DATABASE_URL=postgresql+asyncpg://postgres@localhost:54329/reclaim_test3 \
  ../factory/bin/locked --max 900 pg pytest -q -rs -o faulthandler_timeout=60   # ~4 min; must not skip
uv run --with openapi-spec-validator --with 'jsonschema[format]' --with rfc3339-validator \
  python ../contracts/validate_fixtures.py
```

When `ios/` or `contracts/` changed, from the repo root:

```bash
factory/bin/locked --max 600 sim env DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild test \
  -project ios/ReclaimMyTime.xcodeproj -scheme ReclaimMyTime \
  -destination 'platform=iOS Simulator,name=iPhone 17'        # ~3 min
```

Every checkout on this machine (the main one, factory worktrees, Orca terminals) shares one test
database and one simulator. Run pytest only as `factory/bin/locked pg …`, and xcodebuild or any
`simctl` sequence only as `factory/bin/locked sim …` (`checks.sh` already does). It waits up to
10 min for the resource, then exits 75 ("busy", naming the holder): try again later, never bypass it.

pytest and xcodebuild have each hung for over an hour here. `locked --max <seconds>` is the kill
guard (default 900): past it the whole run is killed and `locked` exits 124. Keep `--max` near 2×
the normal run time (900 for pytest, 600 for xcodebuild) and report any run it had to kill.

## iOS screenshots

For visible iOS changes, screenshots come from the simulator against the **mock API**, never prod.
Hold the simulator lock for the whole sequence: save it as a script and run
`factory/bin/locked sim bash <script>`.

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
xcodebuild build -quiet -project ios/ReclaimMyTime.xcodeproj -scheme ReclaimMyTime \
  -destination 'platform=iOS Simulator,name=iPhone 17' -derivedDataPath ios/DerivedData
xcrun simctl boot "iPhone 17" 2>/dev/null || true
xcrun simctl install "iPhone 17" ios/DerivedData/Build/Products/Debug-iphonesimulator/ReclaimMyTime.app
xcrun simctl ui "iPhone 17" appearance light          # then `dark` for the second set
xcrun simctl launch --terminate-running-process "iPhone 17" com.reclaimmytime.app \
  -UseMockAPI YES -DemoScenario week                  # cards | preferences | week | day | review | tradeoff
sleep 4 && xcrun simctl io "iPhone 17" screenshot <path>.png
```

`-DemoScenario` (see `AppModel.runDemoScenarioIfRequested`) signs in with the mock account and
opens a fixed state; `-MockGcalSync <case>` serves a sync variant. If a new state needs a new
demo scenario, adding one under `#if DEBUG` is in scope. `ios/DerivedData/` is gitignored.

## Software factory

`/factory run` builds every open GitHub issue labeled `factory` through
intake → spec → build → gate → review → approve, one PR per issue. State is in
`factory/jobs/` (gitignored); `factory/bin/job` is its only writer. Checks:
`factory/checks.sh`. Dashboard: `cd factory && python3 -m http.server`, then
open http://localhost:8000/dashboard.html.

Each job works in its own git worktree, a full checkout of the repo at
`.worktrees/<job-id>` (gitignored). A tool run from the repo root that doesn't
read `.gitignore` sees those copies too; exclude `.worktrees/` in its config.

Each job's worktree is an Orca tab nested under the main checkout (stage and verdicts in its status
and comment, a `factory` pane running `factory/bin/job-watch`). Every factory agent runs there as
its own Claude Code session (`claude --agent <name>`), dispatched by `factory/bin/job spawn` as an
Orca orchestration worker: the user may open any agent's tab and talk to it. An agent that needs
the user asks in its own tab and waits; it reports to the orchestrator only with `worker_done`.
Nothing outside the factory builds code or PRs here.

Local policy (`factory/config.json` `auto_merge`, enforced by `factory/bin/job merge`): the
factory merges a PR by itself only when intake rated it `low` risk, it has no `security` surface,
and its diff doesn't touch `server/app/agent/**`. Everything else waits for a human, labeled
`factory:needs-human`. `factory/bin/`, `factory/tests/`, `factory/checks.sh` and
`factory/config.json` are protected: no job may change them.
