# Notifications and prompts (T5.4)

Local notifications only (no APNs in v1, ARCH §3). The app keeps a rolling 48-hour window registered, rebuilt on every plan change and on foreground.

## 1. Block start

Fires at `span.start`. Title is the project; body is the time budget, phrased so the end is the point.

| Condition | Title | Body |
|---|---|---|
| Default | {project} | Next {duration}: {project}. That's all the time it gets today. |
| Last block of the project this week | {project} | Last {project} session this week: {duration}. |
| Deadline project, ≤ 3 days to target | {project} | {duration} on {project}. {days} days to go. |
| Pinned block | {project} | Your {duration} for {project}, as you set it. |
| Perennial, same time as usual | {project} | {duration} of {project}, same as always. |

Duration format: "1 hour", "2 hours", "90 minutes" (minutes under 2 h when not whole hours, else "2 h 30 m" spelled "2 hours 30 minutes").

Decision: one variant per block, picked by the first matching row top to bottom. Rationale: predictable copy beats clever rotation.

Actions (notification category `BLOCK_START`):
- **Skip this** → `PATCH /blocks/{id}` `skip` (allowed once the block has started).
- **Open** → Day view, scrolled to the block.

## 2. Ten-minute warning (optional, off by default)

Settings › Notifications › "Heads-up 10 minutes before" (toggle).

| Title | Body |
|---|---|
| In 10 minutes: {project} | {duration} starting at {time}. Wrap up what you're doing. |

Category `BLOCK_SOON`, action **Open** only.

## 3. Timezone change (PRD §10)

Trigger: the device timezone differs from the one the plan was built in (detected on foreground; the client sends `X-Timezone` on every request). In-app prompt, not a notification.

- Presented as a sheet on the Week tab:
  - Title: "You're in a new time zone"
  - Body: "Your week was planned for {old tz city}. Want me to re-plan the rest of it for {new tz city}? Times already done stay as they are."
  - Buttons: **Re-plan my week** (starts the re-solve flow → proposal review) and **Keep it as is**.
- "Keep it as is" suppresses the prompt until the timezone changes again.

## 4. Calendar drift (PRD §10)

Trigger: `BGAppRefreshTask` or foreground detects live blocks overlapping fresh busy time; the server returns a proposed changeset (system source, template `narration`).

- **App closed:** local notification
  - Title: "A meeting landed on your plan"
  - Body: "{project} at {time} clashes with something new in your calendar. Tap to see a fix."
  - Opens proposal review for the drift changeset.
- **App open:** the pending-changeset banner appears with "Your calendar changed · Review".
- Decision: never auto-apply a drift fix. Rationale: PRD §10 leaves auto-re-solve aggressiveness open, and rule 5 makes every change a proposal.

## 5. Google disconnected

Trigger: `GET /gcal/status` returns `revoked`.
- Settings row turns `warning` with "Reconnect".
- One-time in-app banner: "Google Calendar disconnected. Your plan no longer accounts for new meetings. Reconnect"
- No push notification. Rationale: not time-critical, and nagging users who disconnected on purpose is the wrong tone.

## Permission request

Ask for notification permission after the first week is confirmed, not at launch. Pre-prompt card: "Want a nudge when each block starts? That's the whole point of a timetable." → **Turn on** (system prompt) / **Not now**.
