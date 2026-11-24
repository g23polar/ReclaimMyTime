# ReclaimMyTime — Product Requirements Document

**Platform:** iOS (SwiftUI) · **Status:** Draft v1 · **Owner:** Gautam

---

## 1. Vision & Thesis

There is a lot of unaccounted-for time in a day, and it slips away silently. In high school, the image of the weekly timetable was seared into memory: you knew exactly where you were supposed to be, when, and what you were supposed to be working on. That degree of chunking doesn't transfer wholesale to adult life — but the *framework* does. The mental model of **"for the next 3 hours I'm working on this, and that's all the time this gets"** is motivating in two directions: it creates focus during the block, and urgency because the block ends.

ReclaimMyTime is **not another AI time-management agent**. The AI is plumbing, not the point. The point is giving users back the timetable: a visible, trustworthy structure for their week that they can hold in their head. The agent exists only to make building and maintaining that structure effortless — a conversation instead of a form, a sentence instead of a drag-and-drop rescheduling session.

## 2. Product Principles

1. **The schedule is always valid.** Blocks never overlap each other or calendar events, and never violate a user's stated constraints. This is guaranteed by construction (see §7), not by hoping the AI gets it right.
2. **The agent never silently fails.** When constraints are infeasible ("finish X in 3 weeks, but only 2h/day and your calendar is full"), the agent surfaces the trade-off explicitly: *extend the deadline, raise the daily cap, or drop something else.* The user decides.
3. **The user's calendar is ground truth.** Existing Google Calendar events are immovable facts the schedule forms around. ReclaimMyTime claims the gaps; it never fights the calendar.
4. **Past blocks are immutable.** Rescheduling only ever touches the remainder of the week. History is a record, not a draft.
5. **Conversation in, structure out.** Every user utterance resolves to a structured change (new project, constraint update, availability change). Nothing lives only in chat history.

## 3. Core Concepts & Data Model

### Project
The unit of intent. Two kinds:
- **Deadline-bound:** has a target completion date and an estimated total effort (e.g., "ship portfolio site, ~30 hours, 3 weeks").
- **Perennial:** never completes (working out, meal prep). Has a recurring weekly effort target instead of a deadline.

```
Project {
  id, name, kind: deadline | perennial
  targetDate?            // deadline-bound only
  estimatedTotalHours?   // deadline-bound only
  weeklyTargetHours?     // perennial only
  maxBlockMinutes        // hard cap per sitting (e.g., 120)
  minBlockMinutes        // below this a block isn't worth starting
  requirements: [tag]    // e.g., "laptop", "gym", "daylight" — used for rescheduling
  status: active | paused | done
}
```

### UserPreferences
```
UserPreferences {
  wakeWindow / sleepWindow        // schedulable hours per weekday
  dayDiversity: focused | mixed   // one-project days vs. variety within a day
  bufferMinutes                   // gap enforced between consecutive blocks
  weekStart                       // when a planning week rolls over
}
```

### Block
A scheduled commitment of a time range to a project. Carries `projectId`, `start`, `end`, `calendarEventId` (write-back link), and `state: planned | completed | skipped`.

### WeekPlan
The set of blocks for one week plus the constraint snapshot that produced it. Re-solving produces a new WeekPlan version; prior versions are retained for "why did my week change?" explanations.

### AvailabilityException
Temporary constraint overlaying a window of time: "no laptop Tue–Wed", "traveling Friday". Tagged so the solver knows which projects it blocks (via `Project.requirements`).

### Todo
A standalone checklist item, separate from projects and the solver. Three kinds:
- **Daily:** a recurring checklist item that resets every local day. Ticked per day.
- **Weekly:** a recurring checklist item that resets at the start of each week (`UserPreferences.weekStart`). Ticked per week.
- **One-off:** a single item, completed once and optionally undone. May carry a due date and an optional project link.

Recurring todos keep one completion per period, so "done today" and "done this week" are independent of earlier periods. A todo can link to one of the user's active, paused or done projects; the link survives the project being archived. Todos are direct writes: they don't stage a changeset, re-solve the week, or touch the agent. **Tracking only in v1; calendar integration is later.**

## 4. User Journey 1 — Onboarding

**Goal:** the user leaves onboarding with (a) an emotional understanding of *why* this app exists and (b) a fully structured project list, without ever seeing a form.

**4.1 Intent narrative.** 3–5 swipeable panels with hand-drawn, stick-figure-style doodles. Arc: *a day evaporating* → *the high-school timetable, where every hour had a name* → *"your week, reclaimed."* Tone is warm and a little wry, not productivity-guilt. This section is skippable but shown by default.

**4.2 Sign-up.** Account creation (Google sign-in, one consent that also connects the calendar — T6.0, 2026-11-16; Sign in with Apple optional; email auth deferred until a second platform exists), then Google Calendar OAuth with a plain-language explanation of exactly what is read and written.

**4.3 Conversational intake.** A fluid chat with the agent that extracts, per project:
- what it is
- deadline-bound or perennial
- target date + rough total effort (deadline) or weekly hours (perennial)
- max block length — "how long can you actually focus on this in one sitting?"
- physical requirements (laptop? a specific place?)

Plus global preferences: schedulable hours, and the diversity question — *"do you like themed days where you hammer one thing, or variety in every day?"*

The agent asks follow-ups until each project's schema is complete, mirrors back a structured summary card per project for confirmation, and only then proceeds. **Extraction contract:** the conversation must terminate in valid `Project` and `UserPreferences` records; anything the user said that didn't fit the schema is either clarified or explicitly discarded, never silently kept as prose.

## 5. User Journey 2 — Initial Week Blocking

**Goal:** a believable, constraint-respecting first week, on screen within seconds of finishing intake.

1. Backend pulls Google Calendar busy intervals for the planning horizon.
2. Solver (§7) packs project blocks into free time, honoring:
   - `maxBlockMinutes` — a "2 hours at a time" project **never** gets a 3-hour block
   - spacing — deadline project sessions spread across the week rather than clumping, so pace is sustainable and slack remains for rescheduling
   - deadline pressure — weekly hours per project derived from remaining effort ÷ remaining weeks, flagged (Principle 2) when that exceeds available time
   - perennial targets — weekly hours placed as recurring rhythm (e.g., workout mornings)
   - `dayDiversity` — `focused` biases toward one-project days; `mixed` biases toward variety
   - buffers, min block length, wake/sleep windows
3. User reviews the proposed week in the timetable view. Blocks can be dragged/resized/deleted; manual edits become pinned constraints the solver must respect on future re-solves.
4. On confirm, blocks are written back to Google Calendar as events (dedicated "ReclaimMyTime" calendar, so users can toggle visibility and we can distinguish our events from theirs).
5. Notifications are scheduled for each block start: *"Next 2 hours: Portfolio site. That's all the time it gets today."*

## 6. User Journey 3 — Dynamic Rescheduling

**Goal:** mid-week reality changes are absorbed in one sentence.

Canonical example: Tuesday, the user says *"I don't have my laptop today or tomorrow."*

1. Agent resolves the utterance to a structured `AvailabilityException` (window: now → Wed 23:59, blocks tag: `laptop`) and confirms its interpretation in one line.
2. Solver re-solves **only the remainder of the week**: displaced laptop-project blocks move into later free slots; non-laptop projects (workout, reading) may move *earlier* to backfill the freed time. Past and in-progress blocks are untouched (Principle 4).
3. Agent presents a before/after diff of the week — not a wall of text, the timetable with moved blocks highlighted — and explains consequences honestly: *"Portfolio site loses 2h this week; I've added a Saturday block to stay on pace. OK?"*
4. If displaced hours cannot fit, Principle 2: present the trade-off, user picks.
5. On confirm: calendar events updated, notifications rescheduled.

Other utterances handled by the same pipeline: "double my gym time", "pause project X", "my mornings are gone next week", "I finished the deck early".

## 7. Agent & Scheduler Architecture

Two layers, strict boundary:

**LLM layer (interface).** Runs server-side via backend proxy. Responsibilities: onboarding conversation, parsing utterances into structured mutations (projects, preferences, exceptions), explaining solver output in human terms, narrating trade-offs. Every turn produces schema-validated JSON (tool-use style); invalid extractions are retried/clarified, never guessed around.

**Solver layer (engine).** Pure, deterministic, unit-testable code. Input: busy intervals + projects + preferences + exceptions + pinned blocks. Output: a valid `WeekPlan` or a structured `Infeasibility` (which constraints conflict, by how much, with candidate relaxations for the LLM to narrate). Greedy/heuristic packing with scoring (spacing, diversity, deadline pressure) is sufficient for v1; this is not an optimality contest, it's a believability contest. Deterministic: same input → same schedule.

**The LLM never emits blocks, times, or schedules directly.** It may only mutate constraint state and request a re-solve. This is the load-bearing rule of the architecture: it's what makes Principle 1 a guarantee instead of a hope.

## 8. Integrations & Platform Requirements

| Area | Requirement |
|---|---|
| App | SwiftUI, iOS 17+. Core screens: timetable week/day view, agent chat, project list, settings. |
| Google Calendar | OAuth with narrow scopes (free/busy read + app-created-calendar write); read busy intervals across the user's calendars *excluding the app's own*; write blocks to a dedicated app-owned calendar; handle token refresh/revocation. |
| Notifications | Local notifications at block start (and optional 10-min warning). Re-registered on every plan change. |
| Auth | Google sign-in (T6.0); Sign in with Apple optional; email magic links deferred past v1. |
| Backend | Required in v1: accounts, Google OAuth token storage (encrypted at rest), LLM proxy (API key never ships in the app), solver execution, plan storage. Stack: FastAPI + Postgres — see `ARCHITECTURE.md`. |
| Privacy | Calendar event titles/details never sent to the LLM — only busy intervals. State this in onboarding. |

## 9. V1 Scope

**In:** the three journeys above · Google Calendar read + write-back · accounts · notifications · timetable UI with manual block editing (pinning) · infeasibility trade-off flow.

**Out (explicitly):** Android/web · multiple calendar providers (Outlook, CalDAV) · team/shared schedules · analytics dashboards & streaks · time *tracking* (what you actually did vs. planned) · Apple Calendar sync · widgets/watch (fast-follow candidates).

## 10. Edge Cases & Open Questions

- **Missed blocks.** **Resolved 2026-11-13:** a past block defaults to `completed`; a one-tap "I skipped this" flips it to `skipped`. (Defaulting to missed/lost would poison the remaining-effort math that drives deadline pressure.) Deadline-bound projects may offer a make-up block when something is skipped.
- **Calendar drift.** A new meeting lands on top of a planned block. Backend detects on sync → treat as implicit availability change → propose a re-solve via notification. How aggressive should auto-re-solve be? — OPEN
- **Scarcity contention.** Deadline project needs the exact hours a perennial occupies. Deadline pressure should win, but perennials shouldn't silently vanish for weeks (that breaks habit formation). Floor per perennial? — OPEN
- **Week rollover.** When does next week get planned — fixed day/time, or rolling horizon? *Lean: plan on `weekStart` with a Sunday-evening review nudge.* — OPEN
- **Strict project with no windows.** Since solver 1.1.0, strict preferred windows replace availability for their project, so a strict project whose windows are empty (cleared, or never set) gets no time at all. Today that surfaces only as a full-target shortfall, and the relaxations offered (reduce target, pause) are the wrong fixes. Reject the combination at staging, auto-clear `preferred_strict` when windows are cleared, or add a "turn off strict / add windows" relaxation? — OPEN (logged 2026-11-17)
- **Timezones/travel.** Blocks store absolute instants; re-solve prompt on timezone change.
- **Estimation error.** Users will under-estimate `estimatedTotalHours`. Burn-down check: when remaining effort ÷ remaining time exceeds the daily cap, trigger the Principle-2 conversation *early*, not in the final week.
