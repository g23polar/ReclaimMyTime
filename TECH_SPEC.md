# ReclaimMyTime — Technical Spec & Task Breakdown

**Purpose:** executable task list for coding agents. `PRD.md` and `ARCHITECTURE.md` are the source of truth — tasks cite sections instead of restating them; a task that contradicts them is wrong. Ownership follows the routing policy in `CLAUDE.md`.

**Task protocol (applies to every task):**
- The owner reads `PRD.md` + the cited `ARCHITECTURE.md` sections before writing code.
- Done means: acceptance criteria met, tests green (`uv run pytest` / `xcodebuild test`), `tessl code review` clean on the task diff (fallback `/code-review`), `.claude/agent-usage.md` updated.
- A task blocked by a spec conflict stops and reports; it does not improvise around the contract.
- `architect-reviewer` runs at every milestone boundary before the next milestone starts.
- `contracts/` is frozen after T0.1; changes need a main-session decision.

**Dependency graph (what runs in parallel):**

```
T0.2 scaffold ──► M1 solver (backend-developer) ──► M2 persistence/API ──► M3 agent layer ──► M4 gcal ─┐
T0.1 contracts (api-designer) ──► M5 iOS vs mock (ios-developer, parallel with M1–M4) ────────────────┼─► M6 integration
                                  T5.0 design spec (ui-designer) ──► T5.2/T5.3                        ┘
```

---

## M0 — Foundations

**T0.1 — API contracts** · owner: `api-designer` · deps: none
- In: ARCH §3 (DDL), §7 (route sketch), §8 (agent message/SSE shape), PRD §5–6 (flows the API must carry).
- Out: `contracts/openapi.yaml` + `contracts/NOTES.md` (error envelope, auth header, SSE event format for `/agent/message`, pagination: none in v1) + JSON fixtures per route for the iOS mock.
- Accept: every §7 route fully schematized incl. changeset confirm/reject, block PATCH actions (`complete|skip|edit→pin|delete→forbid`), diff response keyed by `lineage_id`; field names/types match the DDL exactly; fixtures round-trip against the schemas.

**T0.2 — Repo scaffold** · owner: main session · deps: none · *also the `tessl agent` pilot: attempt via Tessl agent run first; judge output against acceptance criteria before any other task uses Tessl runs*
- Out: `server/` uv project (FastAPI, SQLAlchemy, Alembic, pytest, hypothesis, ruff, import-linter with the §2 scheduler-purity contract), empty `ios/` Xcode project (iOS 17, strict concurrency), both building green.
- Accept: `uv run pytest` and `lint-imports` pass on empty tree; initial commit made.

## M1 — Solver (ARCH §4 is the whole brief)

**T1.1 — Solver models + `verify()`** · owner: `backend-developer` · deps: T0.2
- In: ARCH §4, §3 Semantics (conflict order, pins/forbids).
- Out: `scheduler/models.py` (SolverInput/Output, versioned), `scheduler/verify.py` (pure `verify(plan, input) -> list[Violation]` covering overlap, busy, windows, exceptions, pins, forbids, buffers, max/min block length).
- Accept: verify catches every constraint class (one failing fixture each); integer minutes throughout; no clock/tz/env reads (enforced by test).

**T1.2 — Packing heuristic** · owner: `backend-developer` · deps: T1.1
- In: ARCH §4; PRD §5.2 (spacing, deadline pressure, diversity, perennial rhythm).
- Out: `scheduler/pack.py` — greedy/scored packer; returns best partial plan + structured `Infeasibility` (violations + candidate relaxations) when constraints can't be met; reuses `lineage_id` for moved-not-recreated blocks.
- Accept: `verify()` returns `[]` on every produced plan across 10k randomized inputs; PRD's canonical cases pass as fixtures (2h-max project never gets >2h; "no laptop Tue–Wed" displaces only laptop projects; `focused` vs `mixed` produce observably different day composition); infeasible fixtures return partial plans, never raise.

**T1.3 — Determinism + golden harness** · owner: `backend-developer` · deps: T1.2
- Out: hypothesis property suite (verify as oracle); two-`PYTHONHASHSEED` subprocess test comparing byte-identical outputs; golden replay runner loading stored `solver_input` JSON snapshots.
- Accept: all green in CI-style run; a seeded corpus of ≥20 goldens committed.

## M2 — Persistence & API core

**T2.1 — Migrations + models + write paths** · owner: `backend-developer` · deps: T1.1 (SolverOutput), T0.1
- In: ARCH §3 complete (DDL verbatim — it is already validated against Postgres 18, plus the `staged` project_status added 2026-11-13), §3 Semantics incl. the "Staged writes" bullet; `contracts/NOTES.md` Decisions D1–D22 (ratified).
- Out: Alembic initial migration = §3 DDL; SQLAlchemy models; `db/write_paths.py`: `persist_plan(tx, SolverOutput)` (sole creator of blocks) and `apply_mutation(tx, ...)` (sole state mutator, writes the trace row in-transaction); changeset confirm/reject transaction with `pg_advisory_xact_lock`, supersede-before-promote.
- Accept: migration applies to scratch Postgres; the 7 invariant smoke tests from the spec sessions re-expressed as pytest; concurrent confirm test (two tasks, one week) serializes correctly.

**T2.2 — Auth** · owner: `backend-developer` · deps: T0.2
- In: ARCH §6.
- Out: Apple identity-token verification (JWKS cached, nonce, iss/aud), `identities` upsert + email-linking rules, opaque sessions (sliding + absolute expiry, hourly `last_seen_at`), `DELETE /me` with Apple token revocation, auth dependency for FastAPI.
- Accept: unit tests with fixture JWKS/tokens (valid, expired, wrong aud, replayed nonce, private-relay email); no plaintext secrets in DB rows.

**T2.3 — CRUD + plan endpoints** · owner: `backend-developer` · deps: T2.1, T2.2, T0.1 frozen
- Out: routers for projects/preferences/exceptions/pins/blocks PATCH/plans/changesets per `contracts/openapi.yaml`, mounted under the auth dependency.
- Accept: contract tests — every fixture from T0.1 validates against the live route (schemathesis or equivalent); block PATCH semantics produce pins/forbids + re-solve, never in-place edits.

## M3 — LLM agent layer (main session only — see routing policy)

**T3.1 — Extraction schemas + tools** · owner: main session · deps: T2.1
- In: ARCH §5, §8; `claude-api` skill loaded fresh.
- Out: `agent/schemas.py` (drafts, local-date/time fields only), tool definitions (strict, per-request closed tag enum from `user_tags`), changeset assembly.
**T3.2 — `/agent/message` handler** · owner: main session · deps: T3.1, T1.2
- Out: context assembly in §8 cache order (prefix breakpoint; client transcript; volatile snapshot as mid-conversation system message), streaming SSE per contract, `propose_schedule` tool running the solver on hypothetical state, refusal handling (decline, zero state change), per-turn Pydantic validation of tool inputs.
**T3.3 — Narration templates** · owner: main session · deps: T2.1
- Out: deterministic diff/infeasibility text for manual-edit and drift proposals (no LLM call).
- Accept (M3 overall): recorded-fixture tests for the full turn loop (mock Anthropic transport); a turn can stage a changeset + proposal end-to-end; no code path invokes the LLM without a user message.

## M4 — Google Calendar

**T4.1 — OAuth + connection lifecycle** · owner: `backend-developer` · deps: T2.2
- In: ARCH §3 (google_connections), Semantics (tokens), review S9/S14 notes in `reviews/`.
- Out: connect/callback with PKCE + signed state bound to session user, narrow scopes (free/busy + app-created calendar), MultiFernet encryption with `key_id`, `invalid_grant` → revoked status.
**T4.2 — freeBusy + app calendar** · owner: `backend-developer` · deps: T4.1
- Out: freeBusy reads over the horizon excluding the app calendar; app-calendar create + 404-recreate.
**T4.3 — Write-back outbox + drift** · owner: `backend-developer` · deps: T4.2, T2.1
- Out: idempotent worker driving `gcal_status pending→synced` with `lineage_id` in extendedProperties; external-edit→pin rule; drift check (live blocks ∩ fresh busy) producing a template-text proposal.
- Accept (M4 overall): recorded-HTTP tests (no live Google in CI); partial-failure test leaves outbox retryable; drift fixture yields a proposed changeset.

## M5 — iOS app (parallel with M1–M4 after T0.1)

**T5.0 — Visual design spec** · owner: `ui-designer` · deps: none
- In: PRD §4.1 (intent narrative, doodle direction), §5–6 (timetable, diff review).
- Out: `design/` markdown spec: onboarding panel narrative + art direction, timetable week/day visual system, block/diff/infeasibility presentation, type/color tokens. Spec only — no code.

**T5.1 — App skeleton + API client + mock** · owner: `ios-developer` · deps: T0.1
- Out: project structure, typed API client matching `contracts/`, mock transport serving T0.1 fixtures behind a flag, Sign in with Apple flow (nonce), Keychain session storage.
- Accept: app runs fully against mock; decoding tests for every contract fixture.

**T5.2 — Onboarding journey** · owner: `ios-developer` · deps: T5.1, T5.0
- Out: agent chat UI with SSE streaming + confirmation cards, project summary cards → changeset confirm. **Intent-narrative panels/animation (PRD §4.1) are owned by the product owner (decided 2026-11-14);** the app ships a placeholder view behind a replaceable seam.
**T5.3 — Timetable + editing** · owner: `ios-developer` · deps: T5.1, T5.0
- Out: week/day views, block drag/resize/delete emitting PATCH semantics (edit→pin, delete→forbid), proposal diff view highlighting moved blocks by `lineage_id`, infeasibility trade-off sheet.
**T5.4 — Notifications + background** · owner: `ios-developer` · deps: T5.3
- Out: rolling 48h local notification window, re-registration on plan change/foreground, BGAppRefreshTask drift check, device-timezone reporting + re-solve prompt.
- Accept (M5 overall): all three PRD journeys walkable end-to-end against the mock; view-model and scheduling-logic tests green.

## M6 — Integration & hardening

**T6.1 — Live integration** · owner: main session · deps: M2–M5
- Kill the mock flag path by path against the real server; fix seams; `gcal` connect flow on a real test Google account.
**T6.2 — Journey walkthrough + guards** · owner: main session · deps: T6.1
- E2E of the three PRD journeys; eval-capture plumbing (persisted triples per ARCH §8); per-user daily token budget; `architect-reviewer` final pass.

---

*Milestone exit = all tasks accepted + architect-reviewer pass logged in `reviews/`.*

---

## Progress checklist

*Maintained by the main session; a task is checked only when committed to `main` after local checks + review. Last updated 2026-11-15.*

**M0 — Foundations**
- [x] T0.1 API contracts — `9b94804`, frozen `86024f2`
- [x] T0.2 Repo scaffold — `7444dc5`
- [x] M0 architect gate — pass with conditions (`reviews/m0-gate-2026-11-14.md`); fixes `9a3ea30`
- [x] CI: server tests on Postgres 16 — `2158d99`

**M1 — Solver**
- [x] T1.1 Solver models + `verify()` — `03dc641`
- [x] T1.2 Packing heuristic — `28ea65a`
- [x] T1.3 Determinism + golden harness — `216c474` (2 minors logged)
- [x] M1 architect gate — pass with conditions (`reviews/m1-m2-gate-2026-11-15.md`, local); fixes `fccffbf`

**M2 — Persistence & API core**
- [x] T2.1 Migrations + models + write paths — `a9c22e2`
- [x] T2.2 Auth (Sign in with Apple, sessions) — `c8e9501` (2 minors logged)
- [x] T2.3 CRUD + plan endpoints — `3089a3a`
- [x] M2 architect gate — combined with M1; fixes `fccffbf`

**M3 — LLM agent layer** (main session only)
- [ ] M3 tasks

**M4 — Google Calendar**
- [ ] T4.1 OAuth + connection lifecycle
- [ ] T4.2 freeBusy + app calendar
- [ ] T4.3 Write-back outbox + drift

**M5 — iOS**
- [x] T5.0 Visual design spec — `927bb08` (intent panels: product owner)
- [x] T5.1 Skeleton + API client + mock — `a301a16`
- [x] T5.2 Onboarding journey (sign-in, consent, chat intake, summary cards, preferences) — `265fba3`
- [x] T5.3 Timetable + editing + diff + trade-off sheet — `8cd9aaa`
- [x] T5.4 Notifications + background — `f208bcb` (drift detection stubbed until T4.3)
- [ ] Intent-narrative panels (product owner)

**M6 — Integration & hardening**
- [ ] T6.1 Live integration
- [ ] T6.2 Journey walkthrough + guards
