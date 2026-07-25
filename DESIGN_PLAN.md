# CVAI — Design Audit v2

Re-audit of the **current** codebase (post-overhaul, after PR #3). Supersedes the
v1 audit, which described the pre-overhaul state and is now historical.

Measured, not remembered: every number below comes from a grep of live source
(`staticfiles/` artifacts excluded).

---

## 1. Current design system as it actually exists

### What v1 fixed and is holding

One canonical `:root` in `cvai-global.css`, consumed by every screen. Colour,
surface, border, radius, shadow and motion tokens are real and adopted — the
five competing token sets, the `--accent` name collision and the two golds are
gone, and legacy aliases were fully migrated out (434 renames). Empty/loading/
error states exist on every app screen. No fake metrics or testimonials remain.

### What is actually still broken

**1. The type and space scales are dead tokens.** This is the headline finding.
`--fs-*`, `--sp-*`, `--lh-*`, `--font-display`, `--content-max` were defined in
the v1 token block and have **zero consumers in live source**. The only hit
anywhere is `--track-tight`, used exactly once (`landing.html:224`).

> This is precisely the failure v1 diagnosed about the old `--gap-*` tokens —
> "spacing tokens exist but are almost never consumed" — and the overhaul
> reproduced it one layer up. A scale nothing consumes is not a design system,
> it is a comment.

**2. Three body typefaces are still shipping.**

| Screen | Loaded webfonts | Body renders in |
|---|---|---|
| landing | Syne, DM Sans, JetBrains Mono | **DM Sans** |
| studio (`home.html`) | Outfit, **Inter**, JetBrains Mono | **Inter** |
| every other screen | Outfit, JetBrains Mono | **Outfit** |

Landing and studio each pull a font nobody else uses. Moving between landing →
app → studio changes the letterforms twice.

**3. A font that is used but never loaded.** `dashboard.html:211` sets
`font-family: 'Inter'` on `.modal-result-text`, but dashboard extends
`base_app.html`, which loads only Outfit + JetBrains Mono. Inter is never
downloaded, so that text falls through to generic `sans-serif` — a *third*
face inside a single screen. Silent, and visible only side-by-side.

**4. 366 hardcoded radius / shadow / font-size literals remain**, concentrated in
the files that were never fully migrated:

| File | Literals |
|---|---|
| `home.html` | 97 |
| `landing.html` | 89 |
| `dashboard.html` | 66 |
| `tracker.html` | 38 |
| `pricing.html` | 28 |
| `tools.html` | 23 |
| `history.html` | 17 |
| `base_app.html` | 8 |

Radii and shadows were migrated where they sat on a named token; the long tail
of one-off `font-size: 0.78rem` / `0.82rem` / `0.88rem` was not touched at all.
The type scale is therefore still ~20 ad-hoc sizes wide in practice.

**5. Structural duplication, unchanged.** `landing.html`, `pricing.html` and
`users/profile.html` are standalone documents that each re-declare `<head>`,
CDN links, fonts, navbar and mobile drawer markup. Root `templates/` (404, 500,
terms, privacy) each carry their own mini `:root`; terms and privacy do not link
`cvai-global.css` at all. This is why screens drift.

**6. Minor.** Landing keeps a legacy purple `--c3: #7209b7` outside the token
set. `pricing.html` and `profile.html` still hand-roll their nav instead of
reusing the global components.

---

## 2. Ten highest-impact changes, ranked

1. **Make the type scale real.** Replace the ~20 ad-hoc `font-size` literals with
   the 8 `--fs-*` steps, starting with the three heaviest files. *Highest impact:
   inconsistent type size is the most legible "template-grade" tell, and the
   tokens already exist — this is adoption, not design.*
2. **One body typeface.** Standardise on Outfit; drop DM Sans (landing) and Inter
   (studio) from both the font links and the CSS. Keep Syne for landing display
   headings and JetBrains Mono for numerics. *Removes two font downloads and the
   letterform shift between landing, app and studio.*
3. **Fix the phantom Inter** in `dashboard.html:211` — it is a rendering bug, not
   a preference.
4. **Adopt the spacing scale** on section/card padding, replacing one-off values.
   Generous, *consistent* whitespace is the second-biggest premium lever.
5. **Extract a shared base template** for the standalone pages (landing, pricing,
   profile) carrying `<head>`, fonts, navbar and drawer. *Structural fix for the
   drift that produced findings 2–4 in the first place.*
6. **Bring root `templates/`** (404, 500, terms, privacy) onto `cvai-global.css`
   and delete their mini token sets.
7. **Normalise the remaining radius/shadow literals** in `home.html` and
   `landing.html` to `--r-*` / `--shadow-*`.
8. **Tighten heading hierarchy** — apply `--track-tight` and `--lh-tight`
   consistently to every heading ≥ `--fs-xl`; currently applied once.
9. **Retire landing's `--c3` purple** into the token set or remove it.
10. **Content max-width via `--content-max`** instead of the per-screen literals
    (860 / 920 / 1200 / 1300px currently in use).

---

## 3. Proposed tokens

**No new token set is proposed.** The v1 set is correct and does not need
revising; items 1, 4 and 8 above are about *consuming* what already exists in
`cvai-global.css`. Two edits only:

```css
/* remove — nothing loads Inter or DM Sans after this pass */
--font-body: 'Outfit', sans-serif;        /* unchanged, now genuinely universal */

/* add — landing's last untokenised colour */
--brand-3: #7209b7;                       /* deep purple, landing accents only */
```

Everything else — surfaces, text ramp, status, radii, shadows, motion — stays
exactly as shipped.

---

## Scope note

This is adoption and consolidation work, not a redesign. The visual language
that shipped in PR #3 is sound; what it lacks is *enforcement* in the two layers
(type, space) where tokens were written but never wired up, plus the structural
deduplication that stops the drift recurring. Expect the diff to be large but
visually near-invisible on most screens — with the exception of landing and the
studio, which will change typeface.

**Awaiting approval before any visual edits.**
