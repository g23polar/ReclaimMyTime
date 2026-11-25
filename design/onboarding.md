# Onboarding (T5.2)

Flow: **Intent panels → Sign in with Apple → Google Calendar consent → Conversational intake → First week proposal** (hands off to `diff-and-infeasibility.md`).

## 1. Intent-narrative panels (PRD §4.1)

**Ownership:** the product owner builds these panels and their animation. The app ships `IntentNarrativePlaceholderView` behind a replaceable seam (T5.1). This section is direction only. Use any of it or none.

Fixed contract for whatever ships in the slot:
- Entry: first launch with no session. Never shown again after sign-in; reachable later from Settings › About.
- Exit: **Continue** on the last panel, or **Skip** (top-trailing, visible on every panel), both go to sign-in.
- Paging: horizontal `TabView(.page)` with the system page indicator. Swipe and tap both work.

Shared doodle style: single-weight black ink stroke (2 pt at 1×, round caps), slightly wobbly lines, stick figures with circle heads and no faces beyond two dots. One spot color per panel, taken from the project palette. White paper background in light mode; in dark mode the paper becomes `#1E1E1E` and ink becomes `#F2F1EE`. Doodles are decorative: `accessibilityHidden(true)`; the copy carries the meaning.

| # | Doodle brief | Headline | Body |
|---|---|---|---|
| 1 | A stick figure at a desk at sunrise. Around them, small clocks with wings drift up and out of frame. The window shows the sun already setting. Spot color: orange on the sun. | Where did today go? | You meant to work on the thing. Then email happened. And lunch. And a "quick" call. |
| 2 | A school hallway wall with a printed timetable: a grid of hours, each cell labeled ("Maths", "Lunch", "Art"). A younger stick figure points at it, unbothered. Spot color: blue on the grid. | Remember when every hour had a name? | School was rigid. It was also the last time "when do I do this?" had an answer. |
| 3 | The same grid, but in the adult's phone. A few cells filled with their own things ("Portfolio", "Gym", "Reading"); the rest is open. Spot color: green on the filled cells. | Your week, with names on it | Tell me what matters. I'll find it a place in your week, around what's already there. |
| 4 | The stick figure leaning back, feet up, a single cell glowing on the grid behind them: "Next 2h: Portfolio". Spot color: purple on the glow. | Then stop deciding | When it's time, it's time. When it's done, you're done. Shift happens? Say so and the week rearranges. |

Panel 4's button reads **Let's build your week**.

## 2. Sign in with Apple (PRD §4.2)

- One screen: `display` headline "Let's start with you", `body` "Sign in so your week syncs and stays yours.", then the system `SignInWithAppleButton` (`.signIn`, style follows appearance).
- Below the button, `textSecondary` callout: "We only use your Apple ID to sign you in. We never see your password."
- Errors: `apple_token_invalid` and `apple_unavailable` show an inline `callout` above the button ("Apple sign-in didn't go through. Try again.") with no modal alert.

## 3. Google Calendar consent (PRD §4.2, §8 privacy)

Shown before handing off to Google's OAuth page. Layout: `display` headline, then a three-row list with SF Symbols, then the primary button and a secondary "Not now".

Headline: **Connect your calendar**

| Symbol | Row title | Row copy |
|---|---|---|
| `eye.slash` | We see when you're busy, not what you're doing | ReclaimMyTime only reads free/busy times. Event titles, notes, attendees and locations never reach our server or the AI. |
| `calendar.badge.plus` | We add one calendar of our own | Your planned blocks go into a separate "ReclaimMyTime" calendar. Hide it, color it, delete it: your other calendars are never touched. |
| `arrow.uturn.backward` | You can disconnect any time | Settings › Google Calendar. Disconnecting stops all reading and writing immediately. |

- Primary: **Connect Google Calendar** (opens `GET /gcal/connect` URL in `ASWebAuthenticationSession`).
- Secondary: **Not now**. Decision: allow skipping. Rationale: the week can still be planned without busy data (O5); the app then shows a persistent "Calendar not connected" row in Settings and a one-line note on the first proposal.
- Callback outcomes map to inline callouts: `gcal_access_denied` → "No problem. You can connect later in Settings."; `gcal_scope_insufficient` → "Google didn't grant calendar access. Please try again and allow both permissions."; other codes → "Something went wrong with Google. Try again."

## 4. Conversational intake (PRD §4.3)

Lives in the **Chat** tab; onboarding simply opens it with the agent's first message.

### Layout
- `ScrollView` of bubbles, newest at bottom, anchored to bottom.
- Agent bubble: `surface`, `chat` font, leading-aligned, max 85% width.
- User bubble: `accent` fill, `onAccent` text, trailing-aligned.
- Composer: system `TextField` with `.axis(.vertical)` (1–5 lines), send button `arrow.up.circle.fill`, disabled while a turn is streaming.
- Opening agent message: "Hi! Let's figure out what you want to make time for. What's one project on your mind right now: something with a deadline, or something you want to do every week?"

### Streaming states (SSE `delta | status | done | error`)

| State | Source | Display |
|---|---|---|
| Thinking | sent, no event yet | Agent bubble with three-dot typing indicator |
| Streaming | `delta` | Text appends in place (`motion.stream`) |
| Working | `status` | Small `textSecondary` line under the bubble, e.g. "Checking your week…" with a spinner |
| Done, with proposal | `done` + `changeset` | Bubble finishes, then summary cards and/or the proposal banner appear below it |
| Refused | `done` + `outcome: refused` | Bubble shows the agent's decline text; nothing else changes |
| Error | `error` / 503 `agent_unavailable` | Inline row "Couldn't reach the assistant." with **Retry**. Retrying is a new turn (not idempotent). |
| Daily limit reached | `error` / 429 `agent_budget_exceeded` | Same row and **Retry**, copy "You've reached today's limit for the assistant. Try again later." |
| Reply still running | 429 `agent_turn_in_progress` | Same row and **Retry**, copy "I'm still writing a reply to your last message. Try again in a moment." |

### Project summary card
Shown under the agent message when a changeset stages `project_created` (or `project_updated`). One card per project.

```
┌─────────────────────────────────────────┐
│ ■ Portfolio site              Deadline  │  ← project color chip + kind badge
│ Due Fri 12 Dec · about 30 h in total    │
│ Up to 2 h per sitting · needs laptop    │
│ ─────────────────────────────────────── │
│ [ Edit ]                    [ Looks right ] │
└─────────────────────────────────────────┘
```

| Field | Deadline project | Perennial project |
|---|---|---|
| Line 1 | name + "Deadline" badge | name + "Every week" badge |
| Line 2 | "Due {target_date} · about {estimated_total_hours} h in total" | "{weekly_target_hours} h a week" |
| Line 3 | "Up to {max_block} per sitting" (+ "at least {min}" if set) · requirement tags | same |

- **Looks right** confirms the changeset (`POST /changesets/{id}/confirm`). Decision: the confirm lives on the card during onboarding rather than in the banner. Rationale: PRD §4.3 says the agent mirrors a summary card per project "for confirmation".
- **Edit** opens a sheet with a standard `Form` prefilled from the payload. Saving sends a `PATCH`, which returns a new pending changeset that supersedes the old one, and the card updates in place. Decision: edit via a form rather than another chat turn. Rationale: fixing "2 h" to "3 h" is faster as a stepper than as a sentence. The user can still correct things by typing in chat.
- Multiple projects in one changeset: cards stack; a single **Looks right** appears below the stack (one changeset, one confirm).
- Confirmed cards collapse to a one-line row with `checkmark.circle.fill` + name.

### Preferences step
After at least one project is confirmed, the agent asks the global questions. These render as interactive cards inside the chat rather than free text.

- **Schedulable hours:** "When can I schedule things?" A compact card with two time pickers per row ("Weekdays 8:00–18:00", "Weekends 10:00–16:00") and an **Add another window** link. Uses `DatePicker(.hourAndMinute)` in `.compact` style.
- **Themed days vs variety:** "Do you like themed days where you hammer one thing, or variety in every day?" Two large selectable cards side by side (stacked at AX sizes):
  - **Themed days** (`focused`): mini-illustration of a day column filled with one color. Caption "One project gets the day."
  - **Variety** (`mixed`): a day column with three colors. Caption "A bit of everything, every day."
- Submitting sends `PUT /preferences` → 202 → the first-week proposal opens in Week (see `diff-and-infeasibility.md`; every entry is `added` since `against_plan_id` is null).
