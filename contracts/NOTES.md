# Contract notes (T0.1)

Companion to `openapi.yaml`. The DDL in ARCHITECTURE.md §3 is the source of truth for field names, types, and enums. This file covers what the schema can't express. **Status: draft until the main session ratifies the Decisions below; frozen after that (TECH_SPEC task protocol).**

Files: `openapi.yaml` (OpenAPI 3.1), `fixtures/*.json` (one or more per operation), and `validate_fixtures.py` (round-trip check).

## Auth
- `Authorization: Bearer <token>` on every request except `POST /auth/apple` and `GET /gcal/callback`. The token is the opaque 256-bit value returned once by `POST /auth/apple`. It is not a JWT; store it in the Keychain.
- Session expiry is 30 days, sliding, with a 180-day absolute cap. Any 401 (`unauthenticated`) means: discard the token and sign in again.

## X-Timezone
- **Required on every authenticated request:** `X-Timezone: <IANA name>` (e.g. `America/Los_Angeles`). If it is missing or not a tzdata name, the server returns 400 `timezone_invalid`.
- Local fields in exception drafts are resolved with this header at staging time (see D10).
- Mismatch detection happens on the client: compare the device tz with `GET /preferences` → `timezone`. To accept a change (PRD §10 re-solve prompt), send `PUT /preferences` with the new `timezone`. That returns a changeset like any other write.

## Time and the tstzrange mapping
- A `tstzrange` column (`blocks.span`, `availability_exceptions.span`, `block_pins.span`, `week_plans.horizon`) maps to the JSON object `{"start": <date-time>, "end": <date-time>}`.
  - The range is half-open `[start, end)`, with `start < end`. This matches Postgres' default `[)` bounds, so two adjacent blocks don't overlap.
  - The server always emits RFC 3339 in UTC (`Z`). Clients may send any offset.
- Other column types:
  - `date` → `format: date` (local calendar date).
  - `timestamptz` → `format: date-time`.
  - `uuid` → `format: uuid`.
  - `numeric(p,1)` → JSON number with one decimal place.
  - `text[]` → array of strings.
- `jsonb` window columns (`schedulable_windows`, `preferred_windows`) take the shape `{"mon":[["08:00","22:00"]], …}`.
  - Keys are `mon`…`sun`. An absent key means `[]`.
  - Each window is `[start, end)` local `HH:MM`, and `"24:00"` is allowed only as an end.
  - Windows can't cross midnight (split them instead). Windows within a day are sorted and non-overlapping (checked by the server).
- Clients send absolute times in exactly one place: `PATCH /blocks/{id}` with `action: edit`. Everywhere else, drafts carry local dates and times only (ARCH §5).

## Staging model (propose → confirm)
- **Every write that changes scheduling inputs is staged:** `POST/PATCH/DELETE /projects`, `PUT /preferences`, `POST/DELETE /exceptions`, `DELETE /pins/{id}`, and `PATCH /blocks` with edit or delete. These return **`202` + a `pending` Changeset**.
  - The Changeset holds the staged mutations, the proposed plan, and a diff against the live plan.
  - Nothing is applied until `POST /changesets/{id}/confirm`.
  - Pre-assigned ids (`project_id`, `exception_id`, `pin_id`) in `staged[].payload` become the row ids on confirm.
- Within a Changeset, `plan_id`, `plan`, and `diff` are either all set or all null.
  - They are null when no solve is possible yet, i.e. during onboarding before preferences exist.
  - `diff.against_plan_id` is null when there was no live plan (first week). In that case every entry is `added`.
- **At most one `pending` changeset per user.** Creating a new one moves the previous one to `expired`, and its plan to `rejected`. The agent may build on the pending changeset: the new changeset's `staged` list is complete and self-contained.
- `PATCH /blocks` with `complete` or `skip` is not staged. It flips `state` on a started block and returns `200` + Block.
- **Confirm/reject idempotency:**
  - Confirming a `confirmed` changeset returns 200 with the same body. Rejecting a `rejected` one does the same.
  - Any other non-`pending` status returns **409 `changeset_not_pending`**, with `details.status` set to the current status. Example: confirming a rejected or expired changeset. In practice the changeset was superseded or abandoned; refetch state and re-propose.
  - No `Idempotency-Key` header in v1.
  - `POST /agent/message` is **not** idempotent. Retrying it is a new turn and expires any changeset the earlier turn produced.

## Error envelope
Every non-2xx/3xx JSON response uses this shape:
```json
{"error": {"code": "snake_case_code", "message": "human-readable", "details": {}}}
```
`details` is optional and specific to each code. `message` is not for program logic. `code` is an open string, so clients must handle codes they don't know.

| HTTP | code | when / details |
|---|---|---|
| 400 | `timezone_invalid` | X-Timezone missing or not an IANA name |
| 400 | `bad_request` | malformed JSON or wrong content type |
| 401 | `unauthenticated` | missing, invalid, expired, or revoked session |
| 401 | `apple_token_invalid` | `details.reason`: `signature`\|`expired`\|`issuer`\|`audience`\|`nonce_mismatch`\|`authorization_code` (code rejected by Apple, or its id_token's `sub` ≠ the identity token's `sub` — added 2026-11-14, T2.2) |
| 404 | `not_found` | unknown id or another user's resource; `details.resource`, `details.id` |
| 404 | `preferences_not_set` | `GET /preferences` before onboarding sets them |
| 404 | `no_live_plan` | `GET /plans/current` when no live plan covers now |
| 409 | `changeset_not_pending` | confirm/reject on a changeset in a different terminal state; `details.status` |
| 409 | `changeset_stale` | confirm found the proposal out of date (a block it moves has started, or state changed — O8). Nothing applied; the server staged a fresh proposal: `details.changeset_id` = the new pending changeset to review (2026-11-15, T2.1) |
| 409 | `block_not_started` | `complete`/`skip` on a future block |
| 409 | `block_immutable` | `edit`/`delete` on a past or in-progress block (Principle 4) |
| 409 | `block_superseded` | PATCH on a block that isn't in the current live plan |
| 409 | `project_archived` | PATCH/DELETE on an archived project |
| 404 | `gcal_not_connected` | `DELETE /gcal` with no connection |
| 422 | `validation_failed` | schema or semantic failure; `details.fields: [{path, message}]` |
| 500 | `internal_error` | unexpected |
| 503 | `agent_unavailable` | LLM upstream down, before the stream starts (otherwise sent as an SSE `error` event) |
| 503 | `apple_unavailable` | Apple JWKS or token endpoint unreachable and no cached keys usable (T2.2, 2026-11-14) |
| 503 | `apple_revocation_failed` | `DELETE /me` could not revoke the stored Apple credential; nothing is deleted. `details.reason`: `undecryptable_credential` (with `details.key_id`) \| `apple_error` (T2.2, 2026-11-14) |

Codes in the `GET /gcal/callback` redirect (`reclaimmytime://gcal/callback?result=error&code=…`):
- `gcal_access_denied`
- `gcal_state_invalid`
- `gcal_exchange_failed`
- `gcal_scope_insufficient`

**Two outcomes are not errors.**
- **Infeasibility:** the response is still 202 (or 200 for a plan). The best partial plan comes back with `plan.infeasibility` = `{violations, relaxations}`. The UI shows the trade-off (Principle 2). Choosing a relaxation is a new staged write (e.g. `PATCH /projects/{id}` with the proposed `target_date`); it is never applied automatically.
  - An invalidated pin is a `pin_invalidated` violation. It is never silently dropped.
- **Agent refusal:** the stream is still 200, and it ends with `done` where `outcome: "refused"` and `changeset: null`. There is no state change.

## SSE: `POST /agent/message`
- **Request:** `{"messages": [{role, content, changeset_id?}, …]}`, holding the full transcript the client keeps (the server is stateless).
  - `role` is `user` or `assistant`. `content` is plain text; for assistant turns, use the previous `done.reply` verbatim.
  - The last message must be `user`; otherwise the server returns 422 before the stream starts.
  - Limits: 200 messages, 8000 characters each. The client trims the oldest messages first.
  - Optional `changeset_id` on an assistant turn names the changeset that turn produced.
- **Pre-stream failures** (401/400/422/503) are plain JSON error responses. Once the server sends `200 text/event-stream`, every outcome arrives in the stream.
- **Framing:** `event: <name>\ndata: <single-line JSON>\n\n`.
  - Comment lines (`: ping`) are heartbeats, sent at least every 15 s; ignore them.
  - There are no `id:` lines and no resume. A dropped connection means the turn is lost: retry the turn.
  - The fixtures store the parsed frames as `[{"event": name, "data": {...}}, …]`. That is the `AgentStreamEvent` schema.

| event | data | terminal |
|---|---|---|
| `delta` | `{text}`: incremental reply text | no |
| `status` | `{phase: "staging"\|"solving"}`: a stage_* tool or the solver (propose_schedule) is running | no |
| `done` | `{outcome: "completed"\|"refused", reply, changeset: Changeset\|null}` | **yes** |
| `error` | `{error: {code, message, details?}}` | **yes** |

- **Exactly one terminal event** (`done` or `error`) ends every stream, and the server then closes it.
- `done.reply` is authoritative: replace the streamed delta text with it. This also covers a refusal that arrives after some text was already streamed.
- On `error`, nothing from the turn was staged. Discard the streamed text.
- The `changeset` in `done` is the only one the user can confirm right now. It may have `plan: null` during onboarding (e.g. a project summary card before preferences exist).

## Pagination
None in v1. Lists (`/projects`, `/exceptions`, `/pins`) return everything in a wrapper object such as `{"projects": [...]}`, so pagination can be added later without breaking clients. Filters:
- `include_archived` on `/projects`
- `include_past` on `/exceptions` and `/pins`

## Responses vs DDL
- Columns that are never exposed:
  - `user_id` (always the caller)
  - `week_plans.solver_input`
  - `blocks.gcal_etag`
  - `sessions.token_hash` and `sessions.last_seen_at`
  - every `*_enc` column, `key_id`, and the `identities` row
- Computed (non-column) fields:
  - `WeekPlan.blocks`
  - `Changeset.plan`, `Changeset.diff`, `Changeset.narration`
  - `AuthSession.token` and `AuthSession.is_new_user`
- Schemas set `additionalProperties: false` so the conformance tests (T2.3) stay strict. Even so, clients should ignore unknown fields so that additive changes don't break them.

## Fixtures
- **Name:** `<method>_<path segments joined by _, path params as "id">__<case>.json`, e.g. `patch_blocks_id__edit_pin.json`.
- **Shape:** each file has `operation` (operationId), `method`, `path`, `case`, `description`, an optional `request_valid: false` (a deliberately invalid request that the validator must reject), an optional `response_valid: false` (a deliberately invalid server response that the validator must reject), an optional `expected_error` (a substring the rejection must contain), `request` {`path_params`, `query`, `headers`, `body`}, and `response` {`status`, `headers`, `body`}.
- **Check:** `uvx --with pyyaml --with jsonschema --with rfc3339-validator --with rfc3986-validator python contracts/validate_fixtures.py` from the repo root.
  - It validates params, headers, bodies, and statuses with format checking on.
  - It requires every SSE stream to end in exactly one terminal event.
  - It checks invariants the schema can't express: plan, diff, and changeset ids agree; spans run start < end, including inside staged mutation payloads, with or without a plan; staged exceptions satisfy the `blocks_all`/`blocks_tags` CHECK; blocks are sorted, don't overlap, and stay inside the horizon; the diff's after side equals the plan's blocks.
  - It fails if any operation has no fixture.
  - **`fixtures/negative/`** holds only `response_valid: false` fixtures, and those fixtures must live there. They test the validator itself and are **not** mock data: the iOS mock must load only `fixtures/*.json`.
- **Story:** one user in `America/Los_Angeles`, week of Mon 2026-11-16 (`week_start` 1).
  - **Onboarding (Sat evening, 11-15 UTC):** the Reading project is staged with no plan. Then `PUT /preferences` → v1, where every lineage is `added`.
  - **Tue 10:30 local:** "no laptop today or tomorrow" → changeset c…e01 / plan v2, with lineages moved, removed (Wed Portfolio), and added (Sat Portfolio). Then it is confirmed.
  - **Later, each against live v2:** edit → pin (v3, rejected), new Grant project infeasible (v4), Workout 4.0→3.0 (v5), archive Reading (v6), traveling Friday (v7), delete laptop exception (v8), delete Sunday Reading → forbid (v9, confirmed), delete that forbid (v10, against v9).
  - Each newer pending changeset expires the one before it.

| thing | id |
|---|---|
| user | `0b6e2f1a-5c3d-4e8f-9a01-2b3c4d5e6f70` |
| Portfolio site (deadline, laptop) | `5a1f0c3e-7d2b-4b8a-9e61-0c4d2f8a1b01` |
| Workout (perennial, gym) | `…1b02` |
| Reading (perennial) | `…1b03` |
| Grant application (deadline, staged) | `…1b04` |
| laptop / travel exception | `e7c2a9d4-3f1b-4e6a-8d20-5b9c1a7e3f01` / `…3f02` |
| edit pin / forbid | `d3b8f6a1-9c2e-4a7d-b5f0-6e1c8a2d4b01` / `…4b02` |
| plan vN | `9c0d4e2b-6a1f-4c8e-a3b7-2f5d1e9c8a0N` (N two digits) |
| changeset cN | `c4a7e1f2-8b3d-4f6a-9e05-1d2c3b4a5eNN` (onboarding Reading = `…5e10`, first-week prefs = `…5e00`) |
| lineage LNN | `1a2b3c4d-5e6f-4a7b-8c9d-0000000000NN` |

Block ids are uuid5 of `(plan version, lineage)`, which gives new rows per version. The same lineage keeps its `gcal_event_id` across versions.

## Decisions (ratified by main session 2026-11-13 — contracts/ is FROZEN; changes need a main-session decision)
- **D1 tstzrange → `{start, end}`**, half-open, server emits UTC `Z`. *Alternatives:* a Postgres literal string `"[a,b)"`, or an ISO 8601 interval `"a/b"`. Both need custom parsing on iOS.
- **D2 Every scheduling-input write is staged (202 + Changeset), including plain CRUD.** *Alternative:* apply CRUD directly (201/200) and propose a re-solve separately. I rejected that: if the user rejects the proposal, constraints are applied while the live plan violates them (e.g. laptop blocks inside an applied exception). *Cost:* `POST /projects` returns a Changeset, not a Project, and a rename still goes through confirm. *Softer option:* apply fields that don't affect scheduling (only `name`) directly.
- **D3 Staged writes return `202 Accepted`**, not 200/201: the request is accepted but not applied.
- **D4 At most one pending changeset per user**; a newer one expires the older. *Alternative:* one per overlapping horizon, the most the DDL exclusion constraint allows. That is more complex for the client and gives no v1 benefit.
- **D5 `complete`/`skip` apply directly (200 + Block)**, only on started blocks, with no re-solve. *Alternative:* return `{block, changeset}` offering a make-up block for skipped deadline sessions (PRD §10 "may offer"). Deferred.
- **D6 Block delete writes a project-scoped forbid** (`project_id` = the block's project): other projects may still use the span. *Alternative:* a blanket forbid (`project_id` null), which keeps the slot empty for everything. The DDL allows both. The UI could later offer "keep this time free".
- **D7 Block `edit` takes an absolute span** that must start after now and fall inside the block's plan horizon (otherwise 422). There is no cross-week drag in v1. A pin may exceed `max_block_minutes`; see O6.
- **D8 Responses omit internal/secret columns and add computed fields** (listed above). `Changeset.narration` is the T3.3 template text for manual/system proposals and null for agent proposals.
- **D9 Projects are typed as a oneOf on `kind`**, and both the draft (request) and the response are discriminated. In responses, the other kind's fields are present as `null` so the shape stays stable. `kind` is immutable on PATCH. `archived` is reachable only via DELETE (PATCH `status` accepts active|paused|done).
- **D10 Local-time resolution:**
  - `ExceptionDraft` local dates and times are resolved once, at staging, using **X-Timezone**. The device's tz reflects where the user is when they say "tomorrow".
  - An omitted `start_time` means 00:00. An omitted `end_time` means the end of `end_date`.
  - Project `target_date` and all windows stay local and are projected at solve time with **`preferences.timezone`**.
  - *Alternative:* resolve everything with `preferences.timezone`.
- **D11 X-Timezone is required on all authenticated routes.** It isn't required on `/auth/apple`: there is no prefs row to write it into yet, and the first `PUT /preferences` carries `timezone`. Mismatch detection is done by the client.
- **D12 Error envelope:** `{error: {code, message, details?}}` with an open `code` string, not a schema enum. *Note:* the agent-usage log entry for this task mentions RFC 9457 problem+json. My brief specified this envelope, so I followed the brief. The main session should pick one.
- **D13 SSE design:**
  - Named events `delta|status|done|error`.
  - `done.reply` is authoritative.
  - A refusal is a `done` with `outcome: refused`, not a separate event or an error.
  - No resume.
- **D14 Transcript format:** text only (no tool blocks), plus an optional `changeset_id` on assistant turns. Limits are 200 × 8000 characters.
- **D15 `GET /gcal/connect` returns JSON `{authorization_url}`** rather than a 302, because ASWebAuthenticationSession can't send the bearer header. The callback always 302s to `reclaimmytime://gcal/callback?result=…`. That custom scheme is a placeholder until iOS picks a bundle scheme or universal link.
- **D16 Additions beyond ARCH §7:**
  - `GET /preferences`: the client needs the timezone and the edit form.
  - `GET /gcal/status`: shows the reconnect prompt after `invalid_grant`.
  - `DELETE /gcal`: disconnect.
  - `GET /pins` and `DELETE /pins/{pin_id}`: from TECH_SPEC T2.3, coordinator-directed. There is deliberately no `POST /pins`; pins come only from block edit/delete or the agent's `stage_pin`.
  - The list filters `include_archived` and `include_past`.
- **D17 Infeasibility shape (a proposal for T1.2 to confirm):**
  - `violations` is `demand_shortfall {project_id, required/scheduled/shortfall_minutes}` or `pin_invalidated {pin_id, pin_kind, project_id, span, conflicts_with: busy|exception|window|buffer|overlap|forbid|horizon|before_now|preferred_window, exception_id, related_lineage_ids}` (widened 2026-11-14 with O6: every verify() failure on a pinned block except block_length is reported here; `before_now` = pin starts in the past, `preferred_window` = pin outside the project's strict windows).
  - `relaxations` is one of `extend_deadline`, `reduce_estimate`, `reduce_weekly_target`, `raise_max_block_minutes`, `pause_project`, `remove_pin`, `remove_exception`. Each has typed parameters plus `recovers_minutes`.
  - Units: the minute-valued fields are integer minutes, per ARCH §4. These are `required_minutes`, `scheduled_minutes`, `shortfall_minutes`, `recovers_minutes` and `max_block_minutes`.
  - The proposed project values in a relaxation use the project's own units and types, so a picked relaxation maps 1:1 onto a `PATCH /projects` body. (The solver emits minutes — `remaining_minutes`, `weekly_target_minutes` — and the API layer converts to these hours, adding completed work; T1.2, 2026-11-14.) `estimated_total_hours` and `weekly_target_hours` are numbers in hours with one decimal (e.g. 6.7), and `target_date` is a local date.
- **D18 The diff covers every lineage in either plan, including `unchanged`**, so the client can draw the whole week from the diff. `moved` includes resizes. The `against` parameter is required.
- **D19 Hours bounds are tighter than the DDL:**
  - `estimated_total_hours` > 0 and ≤ 99999.9.
  - `weekly_target_hours` > 0 and ≤ 168.
  - The server rounds to 0.1. The schema has no `multipleOf`, because float `multipleOf` is unreliable in validators.
- **D20 `week_start` uses ISO numbering:** 1 = Monday … 7 = Sunday.
- **D21 `POST /auth/apple` always returns 200** with `is_new_user`, never 201.
- **D22 `GET /plans/current` returns 404 `no_live_plan`** when there is no plan, rather than 200 with `null`.
- **D23 Staged projects are invisible to reads (2026-11-14, M0 gate):** `ProjectStatus` gains `staged`; `GET /projects` excludes staged rows and `GET /projects/{id}` returns 404 `not_found` for one. A staged project is observable only inside its pending Changeset (`project_created` payload + the proposal's blocks). Confirm flips it to `active`; reject/expire deletes it (O1).

## Open questions (spec gaps found while writing the contract; not decided here)
- **O1 DDL FK vs staged projects — RESOLVED 2026-11-13:** option 1 chosen; `project_status` gains `'staged'`, the row is inserted at staging, confirm → `active`, reject/expire → deleted (ARCH §3 DDL + Semantics updated). Original text kept for context:** A proposed plan for a staged `project_created` holds blocks whose `project_id` has no `projects` row yet. That violates `blocks (project_id, user_id) → projects`, and `block_pins` has the same problem. T2.1 must choose an option and record it in ARCH:
  - insert the project row at staging under a non-scheduling status,
  - defer the FK,
  - or don't persist blocks for proposals that touch new projects.
- **O2 Recovering a pending changeset.** No route fetches the pending changeset. If the app is relaunched mid-review, the proposal is lost until another write happens. Suggest `GET /changesets?status=pending` or `GET /changesets/{id}`. Not added because it isn't in ARCH §7.
- **O3 Tag vocabulary.** No route lists `user_tags` for the project or exception forms. It's also undecided whether a REST write with an unknown tag adds it on confirm (assumed here) or returns 422.
- **O4 Burn-down in the UI.** `remaining_minutes` (computed) isn't exposed on Project.
- **O5 Solving without a Google connection.** When there is none, or it is `revoked`: solve with `busy = []` and warn, or refuse to propose?
- **O6 Pin length limits — RESOLVED 2026-11-14 (M0 gate):** a manual pin is a user override. `verify()` skips `block_length` for pinned blocks; every other check (busy, exception, forbid, overlap, buffer, window, horizon) still applies to them and a failure is surfaced as `pin_invalidated` in `infeasibility` (D17 extended to those kinds), never dropped. D7 stands.
- **O7 Plan history.** No route lists earlier plan versions, so "why did my week change?" across versions has no way to discover the `against` id beyond the changeset diff.
- **O8 Stale pending proposals — RESOLVED 2026-11-15 (T2.1):** implemented as recommended; 409 `changeset_stale` with `details.changeset_id` of the freshly staged proposal (see error table). Original text: A pending proposal can outlive the start of a block it moves, and `complete`/`skip` mutate live blocks without expiring it. Confirm must define reconciliation. Recommended: confirm re-runs the solver on current state (`now`, frozen blocks, fresh state flags) under the advisory lock and promotes the re-solve; if the re-solve differs from the stored proposal beyond frozen-block copy-forward, return 409 `changeset_stale` with the fresh Changeset for re-review.
- **O11 Empty `staged` for drift re-solves (T2.1, 2026-11-15; owner T4.3).** A drift re-solve changes no constraint, but `Changeset.staged` has `minItems: 1`. Server accepts an empty list internally; T4.3 must either relax the schema (contract change) or stage a no-op `system` mutation. Decide before T4.3.
- **O12 Rejected-plan history vs staged-project deletion (T2.1, 2026-11-15).** `blocks → projects` has no cascade, so deleting a staged project on reject/expire first deletes its blocks from rejected plans. Accepted for v1 (rejected plans are not user-visible; O7 has no history route).
- **O10 Nonce replay store is per-process (T2.2, 2026-11-14; accepted for v1).** Apple identity-token nonces are tracked in process memory, so a replay could land on another Fly machine. Mitigations in place: the authorization code is single-use at Apple and is now bound to the identity token's `sub` (reason `authorization_code`), and nonces are retained for the full acceptance window. Trigger to revisit: running more than one machine, or a sign-in replay observed in logs → add a `used_nonces(nonce_hash, expires_at)` table.
- **O9 Relaxation PATCH on a staged project (M0 gate 2026-11-14; owner T2.1/T2.3, blocks M2 not M1).** `PATCH /projects/{id}` to pick a relaxation for a project whose `project_created` is still pending would, under D4 + O1, expire the changeset that owns the creation and delete the staged row. Recommended: a write that targets a staged project of the pending changeset carries the pending `staged` list forward into the replacement changeset (same self-contained semantics agent turns already have), so creation + relaxation are confirmed together; the staged row is re-pointed, not deleted.
