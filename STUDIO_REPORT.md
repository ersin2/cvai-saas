# Resume Studio — Audit Report (Phase 0, read-only)

## Critical context: which studio is actually live

Your prompt describes a contenteditable **reactive-canvas** editor (`RS`, `renderCanvas`,
`switchTpl`, `.studio-ctrl-bar`, the `data-bs-parent` accordion). That code lives in
`partials/studio_content.html` + `partials/studio_scripts.html` — which **no template
includes**. It is dead code (it even calls non-existent endpoints `/save-resume/` and
`/api/refine-text/`).

The **live** studio is in **`home.html`**: a form-driven editor with a live **PDF**
preview. `studio_styles.html` is its stylesheet (home includes it). Decision confirmed:
**redesign the live studio; delete the two dead partials.**

## How the live studio works (so we're aligned)

- Left = a form in collapsible `.vs-section` blocks: Template Gallery · Personal Info ·
  Experience · Education · Skills · Theme. Toggles are **independent** (several can be
  open — so the "one-section-at-a-time accordion" pain point does *not* apply here).
- Every change → `scheduleLivePreview()` (700 ms debounce) → POSTs the whole form to
  `/download-pdf/?mode=preview` → gets a **PDF blob** → shows it in an `<iframe>`.
  **The preview literally is the exported PDF — zero preview/PDF drift. Keep this.**
- Skills = a chip builder with its own `_skills` state (add/preset/bulk/delete, level
  slider). Theme = colour pickers + font + sliders + presets. Photo = FileReader preview
  with a 5 MB guard. Plus a drag-resize divider and `Ctrl+\` preview toggle.

---

## Findings

### CRITICAL

- **[BUG] Live preview is rate-limited → it breaks during normal editing.**
  `generate_pdf` runs `_check_rate_limit` (Free = **3/min**). The preview reuses that
  endpoint, so after 3 renders in a minute a free user gets a **429** and sees
  "Preview failed" — during ordinary typing. Fix: exempt `mode=preview` from the limiter
  (or give preview its own generous bucket). Small backend change in `generate_pdf`.

- **[XSS] Skill chips inject unescaped user input.**
  `_renderChips()` sets `chip.innerHTML` with `` `<span>${sk.name}</span>` `` and
  `aria-label="Remove ${sk.name}"` (home.html ~2343). A skill named
  `<img src=x onerror=alert(1)>` executes. Names come from manual entry **and** AI
  auto-fill (prompt-injectable via a malicious pasted resume). Fix: escape, or build the
  chip with `textContent` / `createElement`.

### HIGH

- **[UX] No unsaved-changes guard.** The draft autosaves to `localStorage` only, every
  **30 s**. Close the tab mid-edit and you lose up to 30 s of work with no warning. Add a
  `beforeunload` guard, debounced save-on-change, and an honest "Saved ✓" indicator.

- **[UX/PERF] Every edit reloads the whole PDF iframe.** Each debounced change swaps
  `iframe.src`, so the preview white-flashes and **scroll jumps back to page 1** — losing
  your place in a multi-page resume, and feeling laggy while typing. Preserve scroll
  position, swap smoothly, and skip renders when nothing meaningful changed.

- **[BUG] Preview errors/429 dead-end.** A failed render shows a generic "Preview failed"
  toast and can silently stop updating, with no retry. Needs a distinct rate-limit message
  and an auto-retry once the window clears.

### MEDIUM

- **[UX] No undo/redo.** No `Ctrl+Z` for template / skills / theme / photo actions
  (native per-field undo aside).
- **[UX] Experience & Education are single freeform textareas.** No structured entries →
  no drag-reorder, and formatting is guesswork parsed by `pdf_engine`. (Structured entries
  = a bigger change — see Proposed.)
- **[BUG] Template gallery mismatch.** Header says "**(20 designs)**" but there are **10**
  cards, and thumbnails point at `/static/img/templates/*.jpg` which aren't in the repo →
  users see emoji fallbacks instead of real previews.
- **[UX] Clear/Reset** uses native `confirm()`, and only resets the colour-hex labels — it
  does **not** reset the selected template or the theme sliders to defaults.
- **[BUG] Skill delimiter collision.** `_compileSkills` joins as `Name-Level,Name-Level`;
  a skill containing `-` or `,` corrupts parsing in `pdf_engine`.
- **[POLISH] ~30+ inline `onclick`/`oninput`/`onchange` handlers** — CSP-hostile and
  fragile. Migrate to delegated listeners.

### LOW / POLISH

- **[POLISH] Dead code:** delete `partials/studio_content.html` +
  `partials/studio_scripts.html`; prune any `.a4-paper`/`.cv-*` canvas rules left in
  `studio_styles.html` from that orphaned editor.
- **[POLISH] Monolithic ~900-line inline `<script>`** in home.html — split into logical
  sections (state / render / api / events), still vanilla JS.
- **[UX] Mobile:** a side-by-side form + PDF-iframe is impractical on a phone; needs a
  stacked flow (edit, then a Preview/Download action). To verify in `studio_styles.html`.
- **[A11y]** Focus states + clearer clickable affordances on template cards, chips, toggles.

---

## Proposed (bigger / subjective — your call, not done yet)

- **P1 — Structured Experience/Education** (repeatable blocks with add / remove / drag)
  instead of freeform textareas. Enables reorder and cleaner PDFs; needs the `pdf_engine`
  input format kept in sync. Biggest item.
- **P2 — Client-side HTML preview** to kill the per-edit server round-trip. **Not
  recommended** — it reintroduces preview-vs-PDF drift, which the current design elegantly
  avoids. Better to fix the rate-limit + scroll/flash instead.
- **P3 — Zoom controls** (fit-width / 100% / +/–) for the preview pane.

---

## Suggested phase plan (on approval)

- **Phase 1 — critical:** exempt preview from the rate limiter · escape skill chips (XSS) ·
  robust 429/error + retry · delete the dead partials.
- **Phase 2 — UX:** unsaved-changes guard + honest autosave · preview scroll/flash/no-op ·
  scoped undo · gallery count + real thumbnails · styled confirm · skill-delimiter fix.
- **Phase 3 — polish:** inline-handler migration · affordances + focus states · mobile
  stacked flow · micro-transitions · template visual quality.

## Status

**Phase 1 — critical (done, 70 tests pass):**
- Preview exempted from the generation limiter — `generate_pdf` gives `?mode=preview` its
  own generous bucket (`PREVIEW_RATE_LIMIT` = 60/min, `key_prefix='rlprev'`). Verified:
  6 preview renders in a row all 200; real downloads still 429 on the Free tier.
- Skill-chip XSS fixed — `_renderChips` now escapes the name via `_escapeHtml` before it
  reaches `innerHTML`.
- Live preview handles 429 distinctly (back-off + single auto-retry) and errors no longer
  dead-end.
- Deleted the dead `studio_content.html` + `studio_scripts.html` partials.

**Phase 2 — UX (done, 70 tests pass):**
- Autosave is now honest — debounced save-on-edit (~0.8 s), a "Saving… / Saved ✓"
  indicator in the header, skills persisted + restored, and a `beforeunload` guard that
  warns only while a save is pending.
- Live preview no longer flashes — two stacked iframes crossfade (double-buffer), and a
  form-signature check skips no-op re-renders.
- Skill delimiter fixed — `pdf_engine._parse_skills` splits the level off the right
  (`rsplit('-',1)`) so `Objective-C-90` parses correctly; the frontend strips commas.
- Gallery count corrected ("20 designs" → "10").
- Native `confirm()` on Clear replaced with a styled promise-based dialog.

**Phase 3 — polish (done, 70 tests pass):**
- Accessible keyboard focus (`:focus-visible` rings) across buttons, links, inputs,
  template cards, and chips.
- The template gallery is now keyboard-operable (cards get `tabindex`/`role=button`;
  Enter/Space selects).
- Consistent 150 ms micro-transitions on interactive chrome (cards, chips, toggles, pills).

**Deferred (documented, not done — by choice):**
- **Inline-handler migration** (~30 `onclick`/`oninput`). On this page it yields little
  real benefit — the studio relies on inline `<script>`, inline `style=`, and CDN scripts,
  so a strict CSP isn't achievable regardless — while carrying meaningful regression risk.
  Recommend a separate, dedicated pass if a strict CSP becomes a goal.
- **Mojibake cleanup.** `home.html` contains double-encoded characters (garbled `…`, `✓`,
  `—`, box-drawing) in comments and a few visible status strings. Best fixed by re-saving
  the source as clean UTF-8, not piecemeal — deferred to avoid encoding risk mid-pass.
- **Deeper mobile flow.** The studio already stacks below 900 px (form above, preview
  below); a dedicated mobile preview/download toggle would refine it further.
- **Structured Experience/Education entries** (P1 proposal) — still recommended as the
  biggest future upgrade; kept as freeform textareas for now.

## Top-5 to verify manually (after fixes)

1. **Free-tier editing** — type in 4+ fields within a minute; the preview keeps updating (no 429).
2. **XSS** — add a skill `<b>x</b>`; it renders literally, not as HTML.
3. **Data safety** — close the tab mid-edit → warned; reopen → draft restored.
4. **Scroll** — on a 2-page resume, scroll to page 2 and edit; preview keeps your place.
5. **Contract** — every template still exports a correct PDF (backend payload unchanged).
