# ReclaimMyTime: Visual Design Spec (T5.0)

**Status:** v1 draft, 2026-11-14 · **Owner:** main session (spec only) · **Implemented by:** `ios-developer` in T5.2–T5.4
**Sources:** PRD §2, §4–6, §8, §10 · TECH_SPEC M5 · `contracts/NOTES.md` (Staging model, D1–D23, O1–O10) · `contracts/openapi.yaml`

This spec covers how things look and behave. Data comes only from the frozen contract. Each UI state names the contract field that drives it. If a design needs data the contract doesn't provide, it's flagged **[contract gap]** and left for a main-session decision. This spec does not change the contract.

## Design principles

1. **The timetable is the product.** Every screen either builds the week, shows it, or changes it. Chat is how you get to the grid, and the grid is where you end up.
2. **Nothing changes until you say so.** Every staged write (`202` + pending Changeset) appears as a *proposal*: ghosted, badged, with an explicit confirm and reject. A proposal is never styled like the live plan.
3. **Honest about limits.** When a week doesn't fit, say so plainly, in hours, with real choices (CLAUDE.md rule 3). No red-alert styling, no guilt copy. The tone is a friend who's good at calendars.
4. **Meaning never rides on color alone.** Every state has at least two carriers out of color, glyph, text, pattern and outline. Project color helps you scan. The label is what identifies the block.
5. **Native first.** Standard SwiftUI navigation (TabView, NavigationStack, sheets, context menus, swipe actions), system type, Dynamic Type through AX5, full dark mode. Custom visuals live only inside the timetable canvas and the onboarding doodles.

## Files

| File | Covers | Feeds |
|---|---|---|
| `tokens.md` | Type scale and Dynamic Type behaviour, semantic colors (light/dark hex, contrast ratios), project palette and assignment rule, spacing, radius, motion, SF Symbols | all |
| `onboarding.md` | Intent-narrative panels: doodle briefs + copy as *direction* for the product owner, who builds them (app ships a placeholder seam), Sign in with Apple, Google Calendar consent (privacy copy), conversational intake (bubbles, streaming states, project summary cards, preferences step) | T5.2 |
| `timetable.md` | App shell, week/day views, hour grid, now-line, layer treatments (busy / block / past / in-progress / pinned / unplaced pin), block anatomy, drag/resize/delete, empty states, pending-changeset banner | T5.3 |
| `diff-and-infeasibility.md` | Proposal review (added/removed/moved/unchanged by `lineage_id`), confirm/reject bar, superseded/expired handling, infeasibility trade-off sheet (violations in plain language, relaxations as choices) | T5.3 |
| `notifications.md` | Block-start and 10-minute copy templates, notification actions, timezone-change re-solve prompt, drift prompt, Google reconnect prompt | T5.4 |

## App shell (applies everywhere)

Decision: **5-tab `TabView`: Week · Todos · Chat · Projects · Settings.** These are the PRD §8 core screens, one tab each, with no custom navigation pattern. Day view is pushed from Week (it isn't a tab), because it's the same data at a different zoom.

### Todos screen

Standalone tracking, not part of the timetable: nothing here touches the solver or the calendar. Sections top to bottom:

- **Today** (daily) and **This week** (weekly): recurring checklists. The check ticks the current period. Each section has its own empty state.
- **One-off**: open items. The due date shows on the row, tinted `warning` when overdue. A linked project shows its name, or an "Archived" chip when it has been archived.
- **Done**: a collapsed disclosure with the count. Checking a row here undoes it.

Reorder is through Edit (drag handles), within a section. Swipe to delete. Notes show as a one-line preview in secondary text. A failed tick rolls back, with a caption under that row in `warning`. The editor's kind is fixed after create. The due date is optional, and when left out on create the server uses today.

## Contract gaps raised by this spec (for the main session)

| # | Gap | Design impact | Interim design |
|---|---|---|---|
| G1 | No route exposes busy intervals to the client (ARCH §4 `busy` stays server-side). | The timetable can't draw calendar busy time, and drag can't pre-check calendar collisions. | Specify the busy layer fully. Render it only when data exists. Until then, calendar collisions surface after the drop as `pin_invalidated` (`conflicts_with: busy`). Suggest adding `busy: [Span]` to `WeekPlan`. It's intervals only, so the privacy rule still holds. |
| G2 | `Block` has no pinned flag. | Pin glyph needs a join. | Client joins `GET /pins` (`kind: pin`) to blocks on `project_id` + exact span equality. |
| G3 | ~~No way to fetch the pending changeset (O2).~~ Resolved 2026-11-16 (T4.3, D25). | — | `POST /gcal/sync` returns `pending_changeset`; the app calls it on foreground and relaunch, so the banner is restored. |
| G4 | Changesets have no TTL. "Expiry" only happens through supersession (D4) or a 409. | No countdown UI. | Expiry is handled reactively (see `diff-and-infeasibility.md` §4). |
| G5 | `remaining_minutes` isn't exposed (O4). | No burn-down on project rows. | Project rows show targets, not progress. |
