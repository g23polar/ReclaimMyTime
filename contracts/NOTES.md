# Contract notes (T0.1)

Companion to `openapi.yaml`. The DDL in ARCHITECTURE.md §3 is the source of truth for field names, types, and enums. This file covers what the schema can't express. **Status: draft until the main session ratifies the Decisions below; frozen after that (TECH_SPEC task protocol).**

Files: `openapi.yaml` (OpenAPI 3.1), `fixtures/*.json` (one or more per operation), and `validate_fixtures.py` (round-trip check).

## Auth
- `Authorization: Bearer <token>` on every request except `POST /auth/google/start`, `POST /auth/google/complete`, `POST /auth/apple` and `GET /gcal/callback`. The token is the opaque 256-bit value returned once by `POST /auth/google/complete` (or `POST /auth/apple`). It is not a JWT; store it in the Keychain.
- **Sign-in is Google (T6.0, D26).** Sign in with Apple is optional and off unless the server is configured for it (`POST /auth/apple` → 503 `apple_unavailable`).
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
- Size limits: at most 20 tags in `requirements`/`blocks_tags` (`TagList`) and at most 10 windows per weekday (`DayWindows`) (added 2026-11-25, issue #4). They are enforced on writes: REST bodies and agent tool calls. Over a cap is 422 `validation_failed` naming the field, e.g. `path: "requirements"`, `"blocks_tags"`, `"preferred_windows.mon"` or `"schedulable_windows.wed"`; an agent tool call over a cap gets a tool error and stages nothing. Rows stored before the caps are deliberately still served as they are, so a response may exceed them.
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
| 400 | `gcal_state_invalid` | `POST /gcal/complete`: the state is undecryptable, malformed, expired, or was issued to a different user (deliberately not distinguished). Start the connect flow again (D24; 2026-11-15, T4.1 review). Also a sign-in (`login`) state sent here (T6.0). **`POST /auth/google/complete`** (T6.0, D26): the state is undecryptable, malformed, expired or a connect state, or the `nonce` does not hash to the state's `nonce_hash` (not distinguished); start sign-in again |
| 401 | `unauthenticated` | missing, invalid, expired, or revoked session |
| 401 | `apple_token_invalid` | `details.reason`: `signature`\|`expired`\|`issuer`\|`audience`\|`nonce_mismatch`\|`authorization_code` (code rejected by Apple, or its id_token's `sub` ≠ the identity token's `sub` — added 2026-11-14, T2.2) |
| 404 | `not_found` | unknown id or another user's resource; `details.resource`, `details.id` |
| 404 | `preferences_not_set` | `GET /preferences` before onboarding sets them |
| 404 | `no_live_plan` | `GET /plans/current` when no live plan covers now |
| 409 | `changeset_not_pending` | confirm/reject on a changeset in a different terminal state; `details.status` |
| 409 | `changeset_stale` | confirm found the proposal out of date (a block it moves has started, or state changed — O8), or a proposed future block now overlaps fresh calendar busy time (T4.2). Nothing applied; the server staged a fresh proposal: `details.changeset_id` = the new pending changeset to review (2026-11-15, T2.1) |
| 409 | `project_exists` \| `exception_exists` \| `pin_exists` | a staged write reuses an id that already exists (agent-staged writes; T2.3, 2026-11-15) |
| 409 | `gcal_reconnect_required` | any route that solves (staged writes returning 202 Changeset, `PATCH /blocks` edit/delete, `POST /changesets/{id}/confirm`, `POST /agent/message`): the Google connection is `revoked` (stored, or Google answered `invalid_grant` just now), or its stored credential can no longer be decrypted (reconnecting stores a fresh one; 2026-11-16), so busy time cannot be read. Nothing staged or applied; reconnect via `GET /gcal/connect`, then retry. Never sent to a user who never connected (their busy time is empty) (T4.2, 2026-11-15). Also `POST /gcal/sync` when the connection is revoked or its credential undecryptable: writes that already completed (in this drain, or an earlier pass it joined) stay written and `synced`; only the remainder stays pending (T4.3, 2026-11-16; reworded in review round 2) |
| 409 | `block_not_started` | `complete`/`skip` on a future block |
| 409 | `block_immutable` | `edit`/`delete` on a past or in-progress block (Principle 4) |
| 409 | `block_superseded` | PATCH on a block that isn't in the current live plan |
| 409 | `project_archived` | PATCH/DELETE on an archived project |
| 409 | `limit_reached` | a staged write would add a row past a per-user cap: `project_created` past 100 non-archived projects, `exception_added` past 200 exceptions that have not ended, or `pin_added` past 200 pins/forbids that have not ended. Refused at staging (`POST /projects`, `POST /exceptions`, `PATCH /blocks/{id}` edit/delete, agent staging) and again at confirm (`POST /changesets/{id}/confirm`; nothing applied, the changeset stays pending). The list is judged on its net result (removing a row and adding one at the cap passes); updates, archives and removals are never refused; Google Calendar edits are exempt. `details: {resource: "projects"\|"exceptions"\|"pins", limit}` (issue #24, 2026-11-25) |
| 404 | `gcal_not_connected` | `DELETE /gcal` with no connection |
| 409 | `project_unavailable` | Todos (2026-11-24, Todos v1): a todo's `project_id` names a project in status `staged` (or `archived`, on a new link). Only the user's own `active`, `paused` or `done` projects can be linked; an unknown or foreign project is 404 `not_found` (2026-11-24, Todos v1) |
| 409 | `todo_recurring` | Todos v1: `done` or a non-null `due_date` sent on a daily or weekly todo (2026-11-24, Todos v1) |
| 409 | `todo_not_recurring` | Todos v1: a `completions/{period_start}` route called on a one-off todo (2026-11-24, Todos v1) |
| 422 | `validation_failed` | schema or semantic failure; `details.fields: [{path, message}]` |
| 422 | `invalid_period` | Todos v1: `period_start` is not the current or previous period start for the todo's kind (2026-11-24, Todos v1). `details` names the allowed starts |
| 422 | `invalid_order` | Todos v1: `PUT /todos/order` `ids` is not exactly the open todos of that kind: all daily or weekly todos, or one-offs with `completed_at` null (2026-11-24, Todos v1; owner decision, same day). Done one-offs are excluded |
| 422 | `gcal_scope_insufficient` | `POST /gcal/complete`: the user did not grant both calendar scopes; the partial grant was discarded (D24; 2026-11-15, T4.1 review) |
| 429 | `agent_budget_exceeded` | `POST /agent/message`: the user's agent tokens in the last 24 hours (rolling; input + output + cache-write) reached the per-user daily budget. Pre-stream when the budget is already used up; mid-turn it arrives as an SSE `error` event instead, and nothing from the turn is staged. Try again later (issue #3, 2026-11-24) |
| 429 | `agent_turn_in_progress` | `POST /agent/message`: another turn for this user is still running, on any server. Not queued; retry after it ends. A turn lost with its server frees within 3 minutes (issue #3, 2026-11-24) |
| 429 | `rate_limited` | The per-user write rate limit: `POST /projects`, `PATCH`/`DELETE /projects/{id}`, `PUT /preferences`, `POST /exceptions`, `DELETE /exceptions/{id}`, `DELETE /pins/{id}`, `PATCH /blocks/{id}`, `POST /changesets/{id}/confirm` and `/reject`, and every todo write (`POST /todos`, `PATCH`/`DELETE /todos/{id}`, `PUT /todos/order`, `PUT`/`DELETE /todos/{id}/completions/{period_start}`) share one limit per user: 60 per minute by default, in fixed windows, counted across every server. Every authenticated request to them counts, whatever it returns; a refused one does not. The `Retry-After` header gives the whole seconds to wait. Nothing is staged or applied (issue #23, 2026-11-25) |
| 500 | `internal_error` | unexpected |
| 502 | `gcal_exchange_failed` | `POST /gcal/complete`: Google rejected or did not answer the code exchange, or returned no refresh token or an unusable id_token (D24; 2026-11-15, T4.1 review). **`POST /auth/google/complete`** (T6.0, D26): Google rejected or did not answer the code exchange, or returned an unusable id_token; nothing was created (a missing refresh token or calendar scope is not an error there) |
| 503 | `agent_unavailable` | LLM upstream down, before the stream starts (otherwise sent as an SSE `error` event) |
| 503 | `gcal_unavailable` | any route that solves (as for `gcal_reconnect_required`): Google Calendar freeBusy failed (5xx, timeout, per-calendar error, unexpected or malformed answer). Also the retry case: the solve's week moved (a concurrent preferences change) while busy time was being read. Fails closed: nothing staged or applied; retry (T4.2, 2026-11-15; retry cases 2026-11-16). Also `POST /gcal/sync` when Google failed during the write-back drain: events already written stay written, the rest stay pending; retry (T4.3, 2026-11-16) |
| 503 | `solver_timeout` | any route that solves (as for `gcal_reconnect_required`, plus `POST /gcal/sync`): the solve ran out of time (`SOLVER_TIMEOUT_SECONDS` on the server). Nothing staged, applied or promoted; for `POST /gcal/sync`, what the drain already wrote stays written. Retry (issue #22, 2026-11-25) |
| 503 | `apple_unavailable` | Apple JWKS or token endpoint unreachable and no cached keys usable (T2.2, 2026-11-14). Also `POST /auth/apple` on a server without Sign in with Apple configured, before the request is looked at (T6.0, D26); and `DELETE /me` for a user with a stored Apple credential when Apple is not configured (it cannot be revoked; nothing is deleted) |
| 503 | `apple_revocation_failed` | `DELETE /me` could not revoke the stored Apple credential; nothing is deleted. `details.reason`: `undecryptable_credential` (with `details.key_id`) \| `apple_error` (T2.2, 2026-11-14) |
| 503 | `gcal_revocation_failed` | `DELETE /gcal` or `DELETE /me` could not revoke the Google grant. The connection row (and account) are kept; an earlier credential replaced by a concurrent reconnect may already have been revoked. `details.reason`: `undecryptable_credential` (with `details.key_id`) \| `google_error` (T4.1, 2026-11-15; wording 2026-11-15, T4.1 review) |

Codes in the `GET /gcal/callback` redirect (`reclaimmytime://gcal/callback?result=error&code=…`, or `reclaimmytime://auth/google/callback?…` for a sign-in state (T6.0, D26); updated 2026-11-15, T4.1 review, D24):
- `gcal_access_denied`: Google `error=access_denied`
- `gcal_state_invalid`: state missing, undecryptable, or expired
- `gcal_exchange_failed`: any other Google `error` value, or no `code`

On success the redirect is `reclaimmytime://gcal/callback?result=authorized&auth_code=<URL-encoded code>&state=<URL-encoded state>`; nothing is connected until the app calls `POST /gcal/complete` (scope, exchange and state-ownership errors come back from there as JSON). For a sign-in state the same redirect goes to `reclaimmytime://auth/google/callback`, and nothing happens until the app calls `POST /auth/google/complete` (D26).

**Two outcomes are not errors.**
- **Infeasibility:** the response is still 202 (or 200 for a plan). The best partial plan comes back with `plan.infeasibility` = `{violations, relaxations}`. The UI shows the trade-off (Principle 2). Choosing a relaxation is a new staged write (e.g. `PATCH /projects/{id}` with the proposed `target_date`); it is never applied automatically.
  - An invalidated pin is a `pin_invalidated` violation. It is never silently dropped.
- **Agent refusal:** the stream is still 200, and it ends with `done` where `outcome: "refused"` and `changeset: null`. There is no state change.

## SSE: `POST /agent/message`
- **Request:** `{"messages": [{role, content, changeset_id?}, …]}`, holding the full transcript the client keeps (the server is stateless).
  - `role` is `user` or `assistant`. `content` is plain text; for assistant turns, use the previous `done.reply` verbatim.
  - The last message must be `user`; otherwise the server returns 422 before the stream starts.
  - Limits: 200 messages, 8000 characters each, and 32,000 characters in total (added 2026-11-24, issue #3; over it is 422 `validation_failed` with `path: "messages"`). Characters are Unicode scalars (code points). The client trims the oldest messages first.
  - Optional `changeset_id` on an assistant turn names the changeset that turn produced.
- **Pre-stream failures** (401/400/422/429/503) are plain JSON error responses. Once the server sends `200 text/event-stream`, every outcome arrives in the stream.
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
  - In-stream `error.code` values (additive, M3 2026-11-15): `agent_unavailable` (LLM upstream failed mid-turn, or the turn hit its model-call cap with nothing staged), `changeset_stale` (the pending changeset this turn built on was confirmed, rejected or replaced elsewhere during the turn — or, since T4.2 (2026-11-16), the preferences week changed after the turn read busy time; refetch and retry the turn), `internal_error`. Added 2026-11-15 (T4.2): `gcal_unavailable` and `gcal_reconnect_required`, same meaning as the HTTP codes; a turn reads busy time when it first solves, so these arrive in-stream (before the stream starts they would be the plain 503/409). Added 2026-11-24 (issue #3): `agent_budget_exceeded`, the daily token budget ran out between two model calls of the turn (before the stream starts it is the plain 429).
  - Added 2026-11-25 (issue #22): `solver_timeout`, same meaning as the HTTP code: the turn's solve ran out of time and nothing from the turn was staged; retry the turn.
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
  - `GoogleAuthSession.gcal_connected` (T6.0, D26)
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
  - **Drift (T4.3, 2026-11-16):** a meeting lands on Wednesday's Workout → `drift` changeset c…e11 / plan v11 against live v9, with empty `staged` (`post_gcal_sync__drift.json` at the sync that found it; `post_gcal_sync__pending_drift.json` on relaunch).
  - **Google edit (T4.3b, 2026-11-16), an alternative branch from live v9:** the user shortens Wednesday's Workout to 7–7:30 AM in Google → applied directly as live v12; the knock-on (a 30-min Sunday Workout, lineage L12) → `google_edit` changeset c…e13 / plan v13 against v12, with empty `staged` (`post_gcal_sync__google_edit.json`).
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
- **D15 `GET /gcal/connect` returns JSON `{authorization_url}`** rather than a 302, because ASWebAuthenticationSession can't send the bearer header. The callback always 302s to `reclaimmytime://gcal/callback?result=…`. That custom scheme is a placeholder until iOS picks a bundle scheme or universal link. *Updated 2026-11-15 (T4.1 review):* the callback no longer connects anything; it returns `result=authorized` with the code and state, and the app completes via `POST /gcal/complete` (D24).
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
- **D24 Google connect completes as the bearer holder (2026-11-15, T4.1 review; user decision).** *Problem:* the callback was unauthenticated and the state was bound only to the user who called `GET /gcal/connect`. Anyone could request a connect URL with their own bearer and forward it; a victim who consented would have their Google grant stored under the attacker's account (login-CSRF-style account binding). *Decision:* `GET /gcal/callback` exchanges and stores nothing; it 302s `result=authorized&auth_code=…&state=…` into the app, and the app sends both to **`POST /gcal/complete`** (bearer + X-Timezone). The server requires the state's user id to equal the bearer user, then exchanges (PKCE), checks scopes/refresh token/id_token, persists, and returns `200 GcalStatus`. Only the device that holds the session can complete, and only for its own state. Errors: 400 `gcal_state_invalid` (bad, expired, or another user's — not distinguished), 422 `gcal_scope_insufficient`, 502 `gcal_exchange_failed`. *Alternative rejected:* a server-side single-use state table bound to a cookie set at connect — ASWebAuthenticationSession cookies are not reliable and it adds a table. *Numbering:* requested as D18, which was already taken.

- **D25 Google sync entry point, write-back outbox, changeset reason (2026-11-16, T4.3; resolves O2, O11 and design gap G3).** Additive only.
  - **`POST /gcal/sync`** (bearer + X-Timezone, no body) is the single sync entry point. Its server behaviour is available now (T4.3a); calling it on app foreground and from `BGAppRefreshTask` is pending, owned by the queued iOS follow-up (the app does not call it yet; 2026-11-16, review round 2). Steps:
    1. Drain this user's write-back outbox, waiting at most ~10 s overall. Whatever is not written by then stays `pending` and the response still returns; the drain may finish in the background.
    2. Ingest edits the user made to our events inside Google, then the drift check (T4.3b, 2026-11-16; details below). Skipped when the drain answered 409/503 (that error is returned), and skipped for a never-connected user. If the drain is still running when the budget runs out, only the drift check runs this time; the next sync ingests.
    3. Answer **200 `GcalSyncResult`** = `{pending_changeset: Changeset | null, outbox: {pending, synced}}`. `pending_changeset` is the user's current pending changeset (at most one, D4) whatever its reason, so a relaunch mid-review recovers the banner (O2/G3). `outbox` counts the blocks of live plans of the current and future weeks after the drain: `pending` = not yet written to Google, `synced` = written.
  - **Never connected (or disconnected): 200** with `outbox` `{pending: 0, synced: 0}`, not 404 `gcal_not_connected`. Reason: the endpoint is also the O2 recovery path, which every user needs whether or not they connected Google, and a 404 would force the client to special-case an error to learn there is nothing to sync.
  - **Errors:** 409 `gcal_reconnect_required` (revoked, or credential undecryptable; the client shows the reconnect prompt; no `pending_changeset` in this answer), 503 `gcal_unavailable` (Google failed mid-drain, or during the edit ingest or busy read (T4.3b); what was written stays written; retry later). Plus the usual 400/401.
  - **Write-back semantics (server-side; recorded here for the client's mental model):** after every successful confirm the server drains the outbox in the background, without delaying the confirm response; `/gcal/sync` retries anything left. Each live block that has not ended becomes one event in the app-owned "ReclaimMyTime" calendar, titled with the project name, description "Scheduled by ReclaimMyTime", with `extendedProperties.private.lineage_id` and `block_id`. A moved block updates its lineage's event. A lineage that leaves the live plan has its event deleted. Ended blocks are never rewritten. `Block.gcal_status` goes `pending → synced` and `Block.gcal_event_id` is set once written. Event ids are deterministic per (lineage, plan week): 32 hex digits of the lineage id followed by the week's `YYYYMMDD` (the `rmt…` ids in the fixtures are placeholders).
  - **`Changeset.reason`** (required): `user_edit` (REST staged writes) | `agent` (agent turns) | `drift` | `google_edit` (both from part (b)). A stale re-proposal (409 `changeset_stale`) keeps the reason of the changeset it replaces. Clients decode `reason` strictly, like every other enum while contracts/ is frozen: a new value is a contract change that ships with a client update (2026-11-16, T4.3b; replaces the earlier "treat an unknown value like `user_edit`"). *Use:* background refresh posts a local notification for a pending changeset whose `reason` is `drift` or `google_edit` (2026-11-16, T4.3b; was drift only).
  - **`Changeset.staged` is now `minItems: 0`.** It is empty **only** when `reason` is `drift` (a re-solve that changes no constraint, O11) or `google_edit` (the user's pin or forbid was already applied directly and only the knock-on re-solve is proposed; 2026-11-16, T4.3b). Every other reason has at least one entry; the server enforces this when staging, and the schema does too (an `if`/`then` on `Changeset`, plus a semantic check in `validate_fixtures.py` with the negative fixture `post_exceptions__user_edit_empty_staged.json`; 2026-11-16, T4.3 review round 1, widened for `google_edit` in T4.3b). Confirm is unchanged for an empty list; reject is unchanged except for `google_edit` (below).
  - **Edits made in Google (2026-11-16, T4.3b; user decision "hybrid").** Server-side, recorded for the client's mental model. The sync lists the app calendar over the live weeks (only id, etag, status, start, end and our private extended properties are requested; titles and descriptions are never read, and nothing goes to the LLM). It compares each live block that is `synced` and has not started with its event:
    - same etag as our last write → unchanged;
    - `cancelled`, or gone (an `events.get` confirms a missing event) → **deleted**;
    - start or end changed → **moved**, but only if the new span is in the future, inside the block's week and on whole minutes; otherwise ignored (a pin cannot express it);
    - only the title or description changed → ignored; the next write of that block restores our title.

    `pending` rows (our own write in flight) and started blocks are never judged. A move becomes a `pin` on the new span, a delete a `forbid` on the old span, both with source `system` and the interpretation "Moved/Deleted in Google Calendar" (the same shapes PATCH /blocks stages). They are applied as follows:
    - **(i)** pin + re-solve moves no other block → it goes live directly (no review);
    - **(ii)** other blocks would move, but the new span overlaps no other live block → the moved block goes live directly, and the knock-on re-solve is proposed as `google_edit` with an **empty** `staged`;
    - **(iii)** the new span overlaps another live block, or the pin cannot be honoured (calendar busy time, an exception or the schedulable windows beat it), or the one-block change fails verification → nothing is applied; the pin is staged in a `google_edit` changeset, and the Google event stays where the user put it until they decide;
    - **delete** → the forbid and the block's removal go live directly; re-placing the lost minutes is proposed as `google_edit` with an empty `staged`, or nothing is proposed if the re-solve changes nothing.

    A directly applied block that already matches its event is `synced` with the event's etag, so it is not rewritten. Several edits in one sync are handled in block-start order, giving at most one combined `google_edit` changeset. If a changeset is already pending, direct applies still happen and no `google_edit` changeset is staged. When a direct apply has a knock-on (case ii, delete), the user's pending changeset is **restaged** against the new live plan, with the same `staged` list and the same `reason`, and solved against the same fresh busy time, so the knock-on shows up inside their proposal (it gets a new id; the old one is `expired`, as with any newer proposal; 2026-11-16, T4.3b review). The replacement's `created_at` is taken when it is written and is always later than the one it replaces, so a client that keeps the newest proposal adopts it. When restaging, a `pin_removed` for a pin the Google edit just removed or replaced is dropped and the rest of the list kept. If that leaves the list empty, or the rest still cannot apply, the pending changeset is **expired** (never left pending but unconfirmable), and the knock-on is then staged as the usual `google_edit` proposal (2026-11-16, T4.3b review round 2). A pending list that moves the week (a `week_start` change) is left as it was; its confirm re-checks busy time. Case (iii) pins are not staged while something is pending; the next sync re-evaluates them. **Rejecting a `google_edit` changeset that carries staged pins (case iii: nothing was applied) undoes the edit in Google:** the affected blocks go back to `pending`, the write-back puts our planned times back over the user's events, and the next sync sees our etag, so nothing is re-proposed. **Rejecting an empty `google_edit` changeset only declines the knock-on:** the user's edit is already live and pinned (or the block is forbidden) and stays that way; nothing is re-pended or written to Google (2026-11-16, T4.3b review; user decision). A 404 on the listing means the app calendar was deleted: blocks are re-pended and the calendar recreated, as in the write-back.
  - **Drift (2026-11-16, T4.3b).** After the ingest, fresh primary busy time over the current week (read once, before the lock, and shared with the edit re-solves) is compared with live blocks that have not started. Any overlap → a re-solve against that busy time is proposed as `drift` with an empty `staged`, unless a changeset is already pending (that one is returned; its confirm re-checks busy time). If the busy read fails, the sync answers 503 `gcal_unavailable` / 409 `gcal_reconnect_required` and no Google edit is applied; what the drain wrote stays written. Fixtures: `post_gcal_sync__drift.json` (drift found by this sync), `post_gcal_sync__pending_drift.json` (a relaunch with it waiting), `post_gcal_sync__google_edit.json` (case ii, empty `staged`).
  - *Alternatives rejected:* `GET /changesets?status=pending` for O2 (a second endpoint the background check would need anyway next to sync); a no-op `system` mutation for O11 (fakes a constraint change in the trace and in the diff narration).

- **D26 Google sign-in replaces Sign in with Apple (2026-11-16, T6.0; user decision: no paid Apple Developer membership, which Sign in with Apple needs).** Additive only; every existing operation keeps its shape.
  - **One combined consent.** `POST /auth/google/start` returns a Google URL asking for `openid email calendar.freebusy calendar.app.created` with `access_type=offline&prompt=consent` and PKCE: the same consent `GET /gcal/connect` asks for. One flow creates or finds the account, issues a session, and stores the calendar connection.
  - **Flow (iOS):**
    0. Both `/auth/google/*` routes are unauthenticated and do not require X-Timezone (as D11); a client that sends it on every request is fine: it is ignored, whatever its value.
    1. Generate a random nonce (≥ 16 chars) and keep it in memory. Send `POST /auth/google/start {nonce_hash}` with `nonce_hash` = base64url(SHA-256(nonce)), unpadded (43 chars). The answer is `{authorization_url}` (the `GcalConnectUrl` shape).
    2. Open it in ASWebAuthenticationSession. Google redirects to `GET /gcal/callback`, which 302s to `reclaimmytime://auth/google/callback?result=authorized&auth_code=…&state=…`, or `result=error&code=gcal_access_denied|gcal_exchange_failed|gcal_state_invalid`. The callback never exchanges the code.
    3. `POST /auth/google/complete {auth_code, state, nonce}` (both values URL-decoded, plus the raw nonce). The answer is **`GoogleAuthSession`**, exactly `AuthSession` (the `POST /auth/apple` shape) plus **`gcal_connected: boolean`**, always 200 with `is_new_user` (as D21). Store the token as before.
  - **Login-CSRF binding: the app nonce, not a bearer.** D24 binds a connect state to the bearer user; a sign-in has no bearer yet. The encrypted state (purpose `login`) carries `nonce_hash`, and the server requires base64url(SHA-256(`nonce`)) to equal it. A forwarded sign-in URL is useless without the nonce, which never leaves the app that started the flow. The nonce is not sent to Google.
  - **One redirect URI, two purposes.** Google knows one redirect URI, so `GET /gcal/callback` serves both flows. The encrypted state now names its purpose: `connect` (with the user id, `GET /gcal/connect`) or `login` (with the nonce hash). A sign-in state goes to `reclaimmytime://auth/google/callback`; a connect state, a state minted before T6.0 (treated as `connect`) or a state too broken to read goes to `reclaimmytime://gcal/callback` as before. A state presented to the endpoint of the other purpose is 400 `gcal_state_invalid` (both `POST /gcal/complete` and `POST /auth/google/complete`). Clients only check `result` and `code`; ASWebAuthenticationSession matches on the scheme, so any `result=error` that ends the web session fails the flow the app started.
  - **Identity and accounts.** The account key is Google's `sub` (`identities(provider='google', subject=sub)`), so changing the Google address keeps the account. **No account merging:** a new Google identity always gets a new user, even when an Apple user has the same email. `users.email` is the verified Google email unless another user already holds it, in which case it is null (same rule as Apple).
  - **Calendar scopes are optional on Google's screen.** If both calendar scopes were granted and a refresh token came back, the connection is stored in the same transaction as the session (`gcal_connected: true`). Otherwise the user is still signed in. No connection is stored, the partial grant is revoked best-effort (never when the user already has a connection to that same Google account, since revocation is grant-wide), and `gcal_connected` reports whether a connection already existed. With `false`, show the existing connect prompt: **`GET /gcal/connect` stays** for that case and for reconnecting after `gcal_reconnect_required`. A returning user's sign-in with calendar scopes replaces the stored connection by the T4.1 rules (same account keeps `app_calendar_id`; a revoked connection to a different account is replaced and its old grant revoked). **An active connection to a different Google account is kept** (M6 gate, user decision 2026-11-18): the new grant is discarded (revoked unless another user holds that account) and `gcal_connected` is true; switching calendars stays an explicit `GET /gcal/connect`.
  - **Errors** (existing codes, new routes): 400 `gcal_state_invalid`, 502 `gcal_exchange_failed`, 422 `validation_failed` (bad `nonce_hash` format or body). No `gcal_scope_insufficient` at sign-in.
  - **Apple is optional.** The server starts and serves everything without any `APPLE_*` variable. `POST /auth/apple` then answers 503 `apple_unavailable`. The Apple code stays, and a server configured for Apple behaves exactly as before. `DELETE /me` revokes Google, then Apple only for users with a stored Apple credential (fails closed with 503 if Apple is unconfigured and such a credential exists).
  - *Alternatives rejected:* native Google Sign-In SDK id_token + separate calendar connect (two consents and an SDK dependency; the web flow already exists from T4.1); binding the sign-in state to a cookie (ASWebAuthenticationSession cookies are unreliable, as in D24); a separate redirect URI for sign-in (a second Google registration, and the callback logic would be duplicated).

- **D27 `preferred_strict` replaces availability for its project (2026-11-17, solver 1.1.0; user decision).** No shape change. A project with `preferred_strict: true` may be scheduled anywhere inside its own `preferred_windows`, even outside the user's `schedulable_windows`; busy time, exceptions, forbid pins, the deadline and the horizon still apply. Non-strict windows remain a preference inside `schedulable_windows`. Consequences for clients: a strict project with empty or cleared `preferred_windows` gets no time (shortfall; open question in PRD §10), and block spans of strict projects can fall outside the work hours the preferences screen shows.

- **D28 Perennial projects get at most one block per day (2026-11-18, solver 1.2.0; user decision).** No shape change. The solver never places two blocks of a perennial project on the same day; when the week has too few days, the shortfall is a `demand_shortfall` with relaxations (a perennial's `raise_max_block_minutes` is capped at 2× its current `max_block_minutes`). A user placement wins: a pin (block edit, or a Google move) may share a day with another block of that habit, and the solver moves its own block instead; a Google move onto such a day comes back as a proposal. `focused` day diversity themes deadline projects only.

## Open questions (spec gaps found while writing the contract; not decided here)
- **O1 DDL FK vs staged projects — RESOLVED 2026-11-13:** option 1 chosen; `project_status` gains `'staged'`, the row is inserted at staging, confirm → `active`, reject/expire → deleted (ARCH §3 DDL + Semantics updated). Original text kept for context:** A proposed plan for a staged `project_created` holds blocks whose `project_id` has no `projects` row yet. That violates `blocks (project_id, user_id) → projects`, and `block_pins` has the same problem. T2.1 must choose an option and record it in ARCH:
  - insert the project row at staging under a non-scheduling status,
  - defer the FK,
  - or don't persist blocks for proposals that touch new projects.
- **O2 Recovering a pending changeset — RESOLVED 2026-11-16 (T4.3, D25):** `POST /gcal/sync` returns `pending_changeset`. Original text: No route fetches the pending changeset. If the app is relaunched mid-review, the proposal is lost until another write happens. Suggest `GET /changesets?status=pending` or `GET /changesets/{id}`. Not added because it isn't in ARCH §7.
- **O3 Tag vocabulary.** No route lists `user_tags` for the project or exception forms. It's also undecided whether a REST write with an unknown tag adds it on confirm (assumed here) or returns 422.
  - Limit: a user has at most 50 tags (added 2026-11-25, issue #7). A write that would add a tag past it is 422 `validation_failed` (`path: ""`, at staging and again at confirm); a write that only uses existing tags is never refused.
- **O4 Burn-down in the UI.** `remaining_minutes` (computed) isn't exposed on Project.
- **O5 Solving without a Google connection — RESOLVED 2026-11-16 (T4.2):** never connected → solve with no busy time (`busy = []`, nothing to warn about); Google transient failure → 503 `gcal_unavailable`; connection revoked, or its stored credential undecryptable → 409 `gcal_reconnect_required`. Either error stages/applies nothing (see error table). Original text: When there is none, or it is `revoked`: solve with `busy = []` and warn, or refuse to propose?
- **O6 Pin length limits — RESOLVED 2026-11-14 (M0 gate):** a manual pin is a user override. `verify()` skips `block_length` for pinned blocks; every other check (busy, exception, forbid, overlap, buffer, window, horizon) still applies to them and a failure is surfaced as `pin_invalidated` in `infeasibility` (D17 extended to those kinds), never dropped. D7 stands.
- **O7 Plan history.** No route lists earlier plan versions, so "why did my week change?" across versions has no way to discover the `against` id beyond the changeset diff.
- **O8 Stale pending proposals — RESOLVED 2026-11-15 (T2.1):** implemented as recommended; 409 `changeset_stale` with `details.changeset_id` of the freshly staged proposal (see error table). Original text: A pending proposal can outlive the start of a block it moves, and `complete`/`skip` mutate live blocks without expiring it. Confirm must define reconciliation. Recommended: confirm re-runs the solver on current state (`now`, frozen blocks, fresh state flags) under the advisory lock and promotes the re-solve; if the re-solve differs from the stored proposal beyond frozen-block copy-forward, return 409 `changeset_stale` with the fresh Changeset for re-review.
- **O11 Empty `staged` for drift re-solves — RESOLVED 2026-11-16 (T4.3, D25):** `staged` is `minItems: 0`, empty only for `reason: drift`. Original text (T2.1, 2026-11-15; owner T4.3): A drift re-solve changes no constraint, but `Changeset.staged` has `minItems: 1`. Server accepts an empty list internally; T4.3 must either relax the schema (contract change) or stage a no-op `system` mutation. Decide before T4.3.
- **O12 Rejected-plan history vs staged-project deletion (T2.1, 2026-11-15).** `blocks → projects` has no cascade, so deleting a staged project on reject/expire first deletes its blocks from rejected plans. Accepted for v1 (rejected plans are not user-visible; O7 has no history route).
- **O10 Nonce replay store is per-process (T2.2, 2026-11-14; accepted for v1).** Apple identity-token nonces are tracked in process memory, so a replay could land on another Fly machine. Mitigations in place: the authorization code is single-use at Apple and is now bound to the identity token's `sub` (reason `authorization_code`), and nonces are retained for the full acceptance window. Trigger to revisit: running more than one machine, or a sign-in replay observed in logs → add a `used_nonces(nonce_hash, expires_at)` table.
- **O9 Relaxation PATCH on a staged project (M0 gate 2026-11-14; owner T2.1/T2.3, blocks M2 not M1).** `PATCH /projects/{id}` to pick a relaxation for a project whose `project_created` is still pending would, under D4 + O1, expire the changeset that owns the creation and delete the staged row. Recommended: a write that targets a staged project of the pending changeset carries the pending `staged` list forward into the replacement changeset (same self-contained semantics agent turns already have), so creation + relaxation are confirmed together; the staged row is re-pointed, not deleted.
