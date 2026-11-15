# Timetable (T5.3)

Data: `GET /plans/current` (live `WeekPlan` with `blocks`), `GET /pins`, `GET /projects`. The client never computes placement (CLAUDE.md rule 1); it draws what the plan says and sends edits as staged writes.

## 1. Week view (Week tab root)

- **Header:** `NavigationStack` title shows the week range ("10–16 Nov"). Toolbar: previous/next week chevrons and a **Today** button. Decision: v1 shows only weeks that have a plan; other weeks show the empty state. Rationale: there's no plan-history route (O7).
- **Grid:** 7 day columns (week starts on `preferences.week_start`), time on the vertical axis, rows from the earliest schedulable window start to the latest end, rounded out to the hour (fallback 07:00–22:00). Outside-window time is shaded with `bg` at 50% so "not schedulable" reads at a glance.
- **Scale:** 1 hour = 44 pt in week view. A 30-minute block is 22 pt tall, enough for the project name. Vertical scroll; the grid opens scrolled to the now-line (or the first block of the week).
- **Day headers:** weekday + date in `gridLabel`; today's header uses `accent` text and a filled circle behind the date.
- **Tap a day header** pushes Day view for that day.
- **AX sizes (`.accessibility1`+):** the grid is replaced by a vertical list grouped by day: one row per block ("Mon 9:00–11:00 · Portfolio site · 2 h"). Decision: a list beats a grid nobody can read at AX5.

## 2. Day view (pushed from Week)

- Same visual system, single column, 1 hour = 64 pt. Blocks show full anatomy (below).
- Swipe left/right changes day within the week.
- This is the editing surface. Decision: drag/resize/delete live in Day view only; Week view supports long-press context menus but not dragging. Rationale: 7 columns on a phone leave blocks too narrow for reliable drag handles.

## 3. Layers, back to front

| Layer | Source | Treatment (≥2 carriers each) |
|---|---|---|
| Outside schedulable windows | preferences | `bg` 50% shade, no label |
| Calendar busy | **[contract gap G1]** | `busy` fill + 45° hatch (1 pt lines, 6 pt spacing) + `calendar` glyph when ≥ 30 min. VoiceOver: "Busy, 2 to 3 PM". Rendered only when data exists. |
| Forbidden span (forbid pin) | `GET /pins` `kind: forbid` | No fill; 1 pt dashed outline in `textSecondary` + `nosign` glyph. VoiceOver: "Kept free, 3 to 4 PM". |
| Planned block (future) | `blocks[]`, `state: planned`, start > now | Project `fill`, leading color bar, `ink` text |
| In-progress block | `planned`, start ≤ now < end | As planned + 2 pt `nowLine` outline + elapsed portion darkened 10% |
| Past completed | `state: completed` | Fill at 55% opacity + `checkmark.circle.fill` |
| Past skipped | `state: skipped` | No fill, 1 pt outline in project ink at 50%, title struck through + `arrow.uturn.forward.circle` |
| Pinned block | block exactly matching a `kind: pin` span (G2 join) | As planned + `pin.fill` glyph top-trailing + 1.5 pt solid outline in project ink |
| Now-line | device clock | 1.5 pt `nowLine` across today's column, 6 pt dot at the leading edge, time label in `gridLabel` on the gutter |

Past and in-progress blocks are frozen: no drag handles, context menu offers only **I skipped this** / **Mark done** (Principle 4, PRD §10 missed blocks).

## 4. Block anatomy

```
▌Portfolio site              📌
▌9:00–11:00 · 2 h          ⟳
```
- Leading 3 pt color bar (project slot).
- Line 1: project name, `blockTitle`, `lineLimit(1)`; pin glyph trailing if pinned.
- Line 2 (blocks ≥ 45 min): time range + duration, `blockMeta`. Sync glyph trailing:
  - `synced`: nothing.
  - `pending`: `arrow.triangle.2.circlepath` in `textSecondary`.
  - `failed`: `exclamationmark.arrow.triangle.2.circlepath` in `warning`; tap shows "This block didn't reach Google Calendar yet. We'll keep trying."
- Blocks under 30 min: name only, truncating.
- VoiceOver label (one element per block): "{project}, {weekday} {start} to {end}, {duration}. {Pinned.} {Completed|Skipped|Planned}. {Not yet in Google Calendar.}" Actions: Edit time, Delete, Mark done / I skipped this (as applicable).

## 5. Editing (Day view)

All edits are staged writes: `PATCH /blocks/{id}` with `edit` (→ pin) or `delete` (→ forbid) returns 202 + Changeset. Nothing changes in the live plan until confirm (principle 2).

| Gesture | Feedback while dragging | On release |
|---|---|---|
| Long-press (0.3 s) then drag body | Block lifts (shadow, scale 1.03), snaps to 5-minute increments, a time tooltip follows ("10:15–12:15"). Light haptic on each 15-minute tick. | Sends `edit`. Block stays at its new position drawn as a **proposal** (outline in `proposal`, `sparkles` badge) until the changeset resolves. |
| Drag top/bottom handle (appears after long-press) | Same tooltip; duration shown ("2 h 30 m") | Sends `edit` (resize). A pin may exceed the project's max block length (D7/O6); no warning. |
| Context menu › Delete, or drag onto the trash target that appears at the bottom | Trash target highlights in `destructive` | Sends `delete`. The block is replaced by a forbid outline drawn as a proposal. |

Client-side guards (no solver logic, just contract rules):
- Dropping any part of a block before now snaps back with a rigid haptic and callout "You can't move things into the past." (server would 422).
- Past/in-progress blocks can't be picked up (D7, `block_immutable`).
- If the server returns `block_superseded` (409), refetch the plan and show "Your week changed. Try that again."

Decision: after a drag, the app **opens proposal review immediately** (see `diff-and-infeasibility.md`) rather than letting the user stack several drags. Rationale: one pending changeset per user (D4); a second drag would silently expire the first.

## 6. Pending-changeset banner

Shown whenever the app holds a `pending` changeset (from chat, an edit, or onboarding), pinned above the tab bar on every tab.

```
┌────────────────────────────────────────────┐
│ ✨ 3 blocks moved · 1 added      [Review] │
└────────────────────────────────────────────┘
```
- `surfaceRaised` background, 1 pt `proposal` border, `callout` text, `sparkles` glyph.
- Summary text is computed from the diff counts: "{n} moved · {n} added · {n} removed" (omit zeros). If `plan.infeasibility` is set, append "· doesn't all fit" in `warning` with `exclamationmark.triangle`.
- With no plan yet (`plan_id` null, onboarding): "Ready to confirm your projects".
- **Review** opens the proposal review screen. The banner is not dismissible: the user must confirm or reject (principle 2).
- In-memory only; lost on relaunch (contract gap G3).

## 7. Empty and edge states

| State | Trigger | Display |
|---|---|---|
| No plan yet | `404 no_live_plan` | Empty grid at 40% + centered card: "No week planned yet." Button **Plan my week** opens Chat. |
| Plan, but nothing scheduled | live plan, zero blocks | Grid visible + card: "Nothing scheduled this week. Add a project in Chat to get started." |
| Calendar not connected | `GET /gcal/status` not connected/revoked | Thin row under the header: "Calendar not connected: planned without your meetings." Tap opens Settings › Google Calendar. |
| Loading | first fetch | Grid skeleton with redacted (`.redacted(reason: .placeholder)`) block shapes |
| Offline / error | transport error | Last plan stays visible + row "Offline. Showing your last saved week." |
