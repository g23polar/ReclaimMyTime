# Tokens

All tokens are semantic. Views reference the token name, never a raw value. Implement colors as an asset catalog (Any/Dark appearances) and type as `Font` extensions over system text styles so Dynamic Type works for free.

## Type

System font (SF Pro). SF Pro Rounded is used only where the voice is warm and personal: chat and onboarding headlines.

| Token | Text style | Weight | Design | Used for |
|---|---|---|---|---|
| `display` | `.largeTitle` | bold | rounded | Onboarding headlines, empty-state titles |
| `title` | `.title2` | semibold | default | Screen titles inside sheets |
| `headline` | `.headline` | semibold | default | Card titles, trade-off sheet rows |
| `body` | `.body` | regular | default | Default text |
| `chat` | `.body` | regular | rounded | Chat bubbles (both sides) |
| `callout` | `.callout` | regular | default | Banner text, helper copy |
| `blockTitle` | `.footnote` | semibold | default | Project name inside a block |
| `blockMeta` | `.caption2` | regular, `monospacedDigit()` | default | Duration and times inside a block |
| `gridLabel` | `.caption2` | medium, `monospacedDigit()` | default | Hour labels, weekday headers |
| `numeral` | `.title3` | semibold, `monospacedDigit()` | default | Hours in the trade-off sheet ("2h 30m short") |

Dynamic Type behaviour:
- Every style scales through AX5. Nothing is fixed-size.
- **Timetable canvas:** block text scales but the hour grid does not. At sizes `.accessibility1` and above, blocks show only the project name with `lineLimit(1)`, and the full detail moves to the VoiceOver label and the block's context menu. Decision: keep the grid geometry fixed, because a scaling grid would make a 30-minute block taller than a phone screen.
- Week view at AX sizes falls back to a list-of-days layout (see `timetable.md`).

## Color: semantic tokens

Contrast ratios are against the background each token sits on. Text tokens meet WCAG AA (4.5:1 body, 3:1 large); non-text UI meets 3:1.

| Token | Light | Dark | Contrast | Role |
|---|---|---|---|---|
| `bg` | `#FBFAF7` | `#121212` | — | App background (warm off-white; true-ish black) |
| `surface` | `#FFFFFF` | `#1E1E1E` | — | Cards, sheets, chat bubbles (agent) |
| `surfaceRaised` | `#F2F0EB` | `#2A2A2A` | — | Banners, grouped rows |
| `textPrimary` | `#1C1B19` | `#F2F1EE` | 16.4 / 15.9 | Body |
| `textSecondary` | `#5F5C55` | `#A9A6A0` | 6.3 / 7.4 | Meta, helper copy |
| `gridLine` | `#E4E1DA` | `#2E2E2E` | (decorative) | Hour lines |
| `gridLineHalf` | `#EFECE6` | `#252525` | (decorative) | Half-hour lines |
| `nowLine` | `#D9480F` | `#FF7A45` | 3.9 / 6.0 | Now marker (line + dot + time label) |
| `busy` | `#D8D5CE` | `#3A3A3A` | 3.1 on bg | Calendar busy intervals (with diagonal hatch) |
| `accent` | `#2F5DDB` | `#7EA2FF` | 5.6 / 7.1 | Primary buttons, links, user chat bubble |
| `onAccent` | `#FFFFFF` | `#0D1530` | 5.6 / 7.1 | Text on accent |
| `proposal` | `#7A4FD6` | `#B79CFF` | 5.2 / 7.6 | Pending-changeset banner, proposal outlines, "new" badges |
| `warning` | `#B4560A` | `#FFB066` | 4.7 / 9.0 | Infeasibility (calm amber, never red) |
| `positive` | `#2E7D4F` | `#6FD39A` | 5.1 / 9.4 | Completed state, confirm success |
| `destructive` | `#C0352B` | `#FF6B5E` | 5.0 / 6.1 | Delete actions only |

Decision: infeasibility uses amber `warning`, not red. Rationale: PRD Principle 2 frames it as a choice, not an error (principle 3, "honest about limits").

## Project palette

Projects are colored from a fixed 10-slot palette. Each slot has a **fill** (block background) and an **ink** (text on that fill), tuned per appearance so `ink` on `fill` is ≥ 4.5:1.

| Slot | Name | Light fill / ink | Dark fill / ink |
|---|---|---|---|
| 0 | Blue | `#DCE6FF` / `#1A3A8F` | `#25345C` / `#DCE6FF` |
| 1 | Green | `#D7F2E1` / `#1C5634` | `#1F3D2C` / `#C9F0D6` |
| 2 | Orange | `#FFE3CC` / `#7A3A06` | `#4A2C14` / `#FFDCC0` |
| 3 | Purple | `#EADFFF` / `#4B2A92` | `#382A5C` / `#E6DAFF` |
| 4 | Teal | `#D2F1F1` / `#14555A` | `#1C3F42` / `#C8EEEE` |
| 5 | Pink | `#FFDDEB` / `#86204F` | `#4D2235` / `#FFD6E7` |
| 6 | Yellow | `#FFF1BF` / `#6B5200` | `#45391A` / `#FFEDB3` |
| 7 | Slate | `#E2E6EC` / `#2D3A4E` | `#2C3440` / `#DEE4EC` |
| 8 | Brown | `#F0E2D4` / `#5C3A1C` | `#3F3024` / `#EEDCCB` |
| 9 | Lime | `#E6F5C8` / `#3E5A0E` | `#33401C` / `#E0F2BE` |

Assignment rule:
- A project gets the lowest free slot at creation, stored client-side by `project_id` (no contract field needed).
- After 10 active projects, slots reuse, and **each block always carries its project name**, so color is never the identifier (principle 4).
- Each block also has a 3 pt leading edge bar in a saturated version of its slot (the slot's `ink` at 70%), which survives grayscale and color-vision deficiency better than the fill alone.
- Decision: pastel fills with dark ink rather than saturated fills with white text. Rationale: up to 10 projects stay distinguishable without the grid becoming loud, and contrast holds in both modes.

## Spacing and radius

| Token | Value | Use |
|---|---|---|
| `space.xs` | 4 | Inside blocks |
| `space.s` | 8 | Between related items |
| `space.m` | 12 | Card padding (compact) |
| `space.l` | 16 | Screen margins, card padding |
| `space.xl` | 24 | Section gaps |
| `space.xxl` | 40 | Onboarding vertical rhythm |
| `radius.block` | 6 | Timetable blocks |
| `radius.card` | 14 | Cards, banners, bubbles |
| `radius.sheet` | system | Sheets use the system default |
| `hit.min` | 44 × 44 | Every tappable target, including block resize handles (invisible hit area extends beyond the drawn handle) |

## Motion

| Token | Duration / curve | Use |
|---|---|---|
| `motion.quick` | 0.15 s, easeOut | Press states, toggles |
| `motion.standard` | 0.25 s, `.snappy` | Banners in/out, sheet content changes |
| `motion.move` | 0.45 s, `.smooth` | Moved blocks gliding from old to new position in diff review |
| `motion.stream` | per token, no easing | Chat text streaming (append only, no fade) |

Reduce Motion: `motion.move` becomes a 0.2 s crossfade; nothing slides. All motion is decorative; state is always also shown statically.

## SF Symbols

| Meaning | Symbol |
|---|---|
| Pinned block | `pin.fill` |
| Forbidden span | `nosign` |
| Completed | `checkmark.circle.fill` |
| Skipped | `arrow.uturn.forward.circle` |
| Calendar sync pending | `arrow.triangle.2.circlepath` |
| Calendar sync failed | `exclamationmark.arrow.triangle.2.circlepath` |
| Proposal / new | `sparkles` |
| Infeasibility | `exclamationmark.triangle` |
| Busy (calendar) | `calendar` |
