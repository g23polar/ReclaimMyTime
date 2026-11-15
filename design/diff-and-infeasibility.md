# Proposal review and trade-offs (T5.3)

Data: the pending `Changeset` (`staged`, `plan`, `diff`, `narration`). Diff entries are keyed by `lineage_id` (D18: every lineage in either plan is listed, including `unchanged`).

## 1. Proposal review screen

Presented as a full-height sheet (`.presentationDetents([.large])`) from the banner, after a drag, or automatically when an agent turn ends with a changeset.

Structure, top to bottom:
1. **Narration.** `narration` text for manual/system proposals; for agent proposals the agent's reply is already in chat, so show a one-line summary from diff counts instead. `callout`, `textSecondary`.
2. **Infeasibility strip** (only if `plan.infeasibility`): see §3. Placed above the grid so it can't be missed.
3. **Week grid in diff mode** (same geometry as Week view; tap a day header to zoom to Day).
4. **Confirm/reject bar** (§2), pinned to the bottom safe area.

## 2. Diff rendering

| `change` | Treatment | VoiceOver |
|---|---|---|
| `unchanged` | Normal block at 60% opacity, so changes stand out | "{project}, {time}, unchanged" |
| `added` | Normal block + 1.5 pt `proposal` outline + `sparkles` badge + "New" micro-label | "New: {project}, {time}" |
| `removed` | Ghost at `before` span: no fill, 1 pt dashed outline in project ink, title struck through | "Removed: {project}, {before time}" |
| `moved` | Block at `after` span with `proposal` outline; ghost at `before` span (dashed, 40%); a thin curved connector from ghost to block, drawn only when both are in the visible day range. On open, the block animates from `before` to `after` (`motion.move`; crossfade under Reduce Motion). | "Moved: {project}, from {before time} to {after time}" |

- Resizes count as `moved` (D18). If only the length changed, the label reads "Now {duration}" instead of showing a connector.
- **First week** (`against_plan_id` null): everything is `added`; suppress the per-block "New" labels and badge, since the whole week is new. Title: "Your first week".
- **List alternative:** a toolbar toggle switches the grid to a list of changes only (moved/added/removed, grouped by day). This is also the VoiceOver rotor default. Decision: the grid is primary per PRD §6 ("the timetable with moved blocks highlighted, not a wall of text"), the list is for accessibility and small changes.
- `plan_id` null (onboarding, no solve yet): no grid; show the staged project summary cards instead.

## 3. Confirm / reject bar

- Two buttons, full width split: **Keep my week** (secondary, bordered) and **Confirm changes** (`borderedProminent`, `accent`). Decision: label reject as "Keep my week", not "Reject". Rationale: it says what happens.
- If infeasibility is present, the confirm button reads **Confirm what fits**, still enabled. Rationale: the partial plan is valid; confirming it is a legitimate choice (rule 3 means the user sees the trade-off, not that they're blocked).
- Confirm → `POST /changesets/{id}/confirm` → success haptic, sheet dismisses, banner clears, Week shows the new live plan with moved blocks briefly pulsing (`motion.standard`, once).
- Keep my week → `POST /changesets/{id}/reject` → sheet dismisses, banner clears, nothing else changes.

## 4. Superseded and expired proposals

There is no TTL (gap G4); a changeset ends `expired` only when a newer one replaces it, or the server reports it via 409.
- **Superseded locally** (a new 202 arrives while reviewing): the sheet swaps content to the new changeset with a `callout` at top: "Updated with your latest change." No modal.
- **409 `changeset_not_pending`** on confirm/reject: dismiss the sheet, refetch the plan, show a toast: "That proposal is out of date: your week changed since. Nothing was applied." The banner clears.
- Pending changeset when the user switches tabs or backgrounds the app: preserved in memory; banner remains.

## 5. Trade-off sheet (infeasibility)

Entry: the infeasibility strip in the review screen ("6 h doesn't fit this week · See options"), `warning` text + `exclamationmark.triangle`. Tapping opens a medium/large sheet.

### Violations, in plain language
One row per violation, `headline` title + `body` explanation.

| Violation | Title | Explanation template |
|---|---|---|
| `demand_shortfall` | "{project}: {shortfall} short" | "Needs {required} this week; {scheduled} fits." Durations formatted "2 h 30 m". |
| `pin_invalidated`, `conflicts_with: busy` | "Your {time} {project} block clashes with a meeting" | "Your calendar comes first, so this block can't stay there." |
| `… exception` | "… is inside time you marked unavailable" | "You said you're unavailable then, which takes priority over a pinned block." |
| `… window` | "… is outside your schedulable hours" | "It's outside the hours you gave me." |
| `… preferred_window` | "… is outside {project}'s set times" | "This project only runs at certain times." |
| `… overlap` / `buffer` | "… is too close to another block" | "It overlaps or leaves no breathing room before {other project}." |
| `… forbid` | "… is in time you kept free" | "You removed a block from this slot earlier." |
| `… horizon` | "… is outside this week" | "It falls outside the week being planned." |
| `… before_now` | "… is in the past" | "That time has already gone." |

Pin violations always end with: "Your pin is kept on record until you remove or move it." (rule 5: reported, never dropped).

### Relaxations as choices
Below the violations: "Here's what would help". Each relaxation is a tappable card, sorted by `recovers_minutes` descending.

| `kind` | Card title | Detail line |
|---|---|---|
| `extend_deadline` | "Move {project}'s deadline to {target_date}" | "Frees up {recovers}" |
| `reduce_estimate` | "Plan {project} as {estimated_total_hours} h total" | "Frees up {recovers}" |
| `reduce_weekly_target` | "{project}: {weekly_target_hours} h a week" | "Frees up {recovers}" |
| `raise_max_block_minutes` | "Let {project} run up to {max_block}" | "Fits {recovers} more" |
| `pause_project` | "Pause {project} for now" | "Frees up {recovers}" |
| `remove_pin` | "Unpin your {time} {project} block" | "Frees up {recovers}" |
| `remove_exception` | "Drop the '{exception}' unavailability" | "Frees up {recovers}" |

- Tapping a card shows a confirmation within the sheet: "This will create a new proposal. Nothing changes until you confirm it." with **Try it** / **Cancel**.
- **Try it** sends the matching staged write (e.g. `PATCH /projects/{id}` with the proposed value) → 202 → the review screen swaps to the new proposal (§4 "Superseded locally"). Never auto-applied (NOTES "Infeasibility").
- A final, always-present option: **Leave it as is** (dismisses the sheet; the user can still "Confirm what fits").
- No red, no countdowns, no guilt copy (principle 3).
