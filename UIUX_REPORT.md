# UI/UX Audit & Polish Report — CVAI

Frontend design/UX pass (Bootstrap 5 + custom CSS, dark glassmorphism). Backend logic
untouched. Tags: `[BUG]` broken/misleading · `[INCONSISTENT]` differs between screens ·
`[UX]` confusing/annoying · `[POLISH]` works but looks unfinished.

The design system (`generator/static/generator/css/cvai-global.css`) is solid — tokens for
color/spacing/radii/shadow/transition + shared components. Most issues are screens that
**bypass** it (inline styles, redefined components, standalone pages).

**Terminology: keep both resume and cover letters** (per owner). Only real inconsistencies
are fixed, e.g. the pricing "Claude 3.5 Sonnet" leak → "Elite AI".

---

## Cross-cutting (design system)

- **DS-1 [INCONSISTENT]** `landing.html`, `pricing.html`, `users/profile.html` are standalone
  HTML docs with their own head/nav/`:root` tokens instead of extending `base_app.html`.
  Pricing even uses a different `--dark-bg` (`#0a0e17` vs `#080c14`).
- **DS-2 [INCONSISTENT]** One primary gradient button under four names: `.btn-primary-grad`,
  `.btn-tool`, `.btn-add`, pricing `.btn-*`.
- **DS-3 [INCONSISTENT]** Three empty-state components (`.empty-state-v2`, `.dash-empty`,
  inline); `.empty-state-v2`/`.empty-*` defined twice in the CSS.
- **DS-4 [BUG]** Two toast systems: global `.toast-item` (base_app) vs `.toast-notification`
  (tools.html's own `showToast`). Different look on AI Tools.
- **DS-5 [A11y]** No `:focus-visible` on custom buttons/links/cards — keyboard focus invisible.
- **DS-6 [UX]** Native `alert()`/`confirm()` in tracker + dashboard instead of toasts/styled confirm.
- **DS-7 [POLISH]** Mixed transition durations vs the `--transition-*` tokens.
- **DS-8 [INCONSISTENT]** Pervasive inline styles bypass tokens (dashboard/profile/tracker).

## Per screen

- **Landing** — good post-recent-fixes. `[UX]` custom `cursor:none` hides pointer; `[POLISH]`
  `@keyframes blink` defined twice.
- **Studio (home)** — loading/error/success states all present. Good.
- **Dashboard** — good empty states; `[INCONSISTENT]` `.dash-empty`; `[UX]` `alert()` on copy.
- **Tracker** — `[BUG]` fake "AI Match Score" (`id+70`); `[BUG]` fake timeline chart;
  `[BUG]` dead `kanban_columns` loop + blank count badge; `[BUG]` Status select missing
  "Rejected"; `[BUG]` messages double-render (alert + base toast); `[UX]` alert/confirm;
  `[PERF]` per-row external logo calls (clearbit/ui-avatars).
- **History** — good; native `confirm()` on delete (minor).
- **AI Tools** — `[BUG]` local toast override; `[INCONSISTENT]` `document.write` submit vs the
  new fetch-based Cover Letter tool.
- **Profile** — `[INCONSISTENT]` standalone; `[POLISH]` duplicate `<style>` block.
- **Pricing** — `[INCONSISTENT]` standalone; nav missing AI Tools/Tracker; `[BUG]` downgrade
  buttons are no-ops (reload); `[INCONSISTENT]` "Claude 3.5 Sonnet" vendor leak.

## Decisions
- Remove tracker fake Match Score + fake timeline chart.
- Replace external company logos with local initials avatars.
- Keep both resume + cover-letter terminology.

## Status — all phases complete (70 tests pass, check clean)

**Phase 1 — bugs & UX (done)**
- Added a global styled `confirmDialog()` (base_app) + confirm-dialog CSS.
- Unified toasts: removed tools.html's local `showToast`/`toggleMobileNav` so AI Tools
  uses the global `.toast-item` system.
- Tracker: removed the fake "AI Match Score" and the fake "Activity Over Time" chart
  (kept the real funnel); replaced clearbit/ui-avatars with a local `.company-avatar`
  (initials); `alert()`/`confirm()` → toasts + `confirmDialog`; removed the duplicate
  message block; added the missing "Rejected" status option; fixed the dead kanban loop
  by passing `KANBAN_COLUMNS` from the view; deleted the now-dead `.match-score`/
  `.company-logo` CSS.
- Dashboard: copy-fail `alert()` → toast.
- Pricing: "Downgrade" no-ops now link to the Stripe portal ("Manage Subscription");
  "Claude 3.5 Sonnet" → "Max AI speed & priority queue".
- Profile: removed the duplicated `<style>` block.

**Phase 2 — consistency (done)**
- Deleted dead CSS: the duplicate empty-state block and the unused `.toast-notification`
  system (+ repointed its mobile rule to `.toast-container`/`.toast-item`).
- Pricing nav aligned to the app (added AI Tools + Tracker, matched labels) and its
  low-contrast grays lifted to the accessible palette.
- Added a shared `.company-avatar` component to the design system.
- Note: `.btn-tool`/`.btn-add` share the identical primary gradient — visually consistent;
  left as sanctioned aliases rather than churn every template for zero visual change.

**Phase 3 — polish (done + proposals)**
- Accessible `:focus-visible` outlines added globally (app) and on the standalone
  landing + pricing pages — keyboard focus is now visible everywhere.
- Removed the landing custom `cursor:none` (native pointer restored).
- **Proposed, not applied** (subjective/big — your call): gradient restraint (reserve the
  accent gradient for primary CTAs only), a sharper type scale, and swapping heavy glows
  for consistent 1px-border + soft-shadow depth. These change the look meaningfully, so
  they're left as proposals per the "propose big subjective changes" rule.
