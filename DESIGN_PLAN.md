# CVAI — Design Audit & Plan (Phase 0)

Files audited: `cvai-global.css`, `landing.html` (standalone), `home.html` (standalone, CSS+structure), `base_app.html`.
Not read (budget): `partials/studio_styles.html` — a 4th style source included by home.html; flagged for Phase 1.

---

## 1. Current design system as it exists

### Three competing `:root` token sets

| Token | cvai-global.css (app) | landing.html | home.html (studio) |
|---|---|---|---|
| Background | `#080c14` | `#060810` | `#070a14` |
| Panel/card | `#0f1526` (modal) | `#0c1022` | `#0d1120` |
| Text primary | `#f1f5f9` | `#e2e8f0` | `#e2e8f0` |
| Text muted | `#7c8a9e` (AA-fixed) | `#94a3b8` | `#64748b` (**fails AA ~3.5:1**) |
| `--accent` | **`#7c3aed` (purple)** | — | **`#4cc9f0` (cyan)** — same name, different color |
| Body font | **Outfit** | **DM Sans** (+ Syne headings) | **Inter** |

Shared brand colors: cyan `#4cc9f0`, pink `#f72585`. Indigo `#4361ee` is used in the main CTA gradient everywhere but is only a token on landing (`--c4`).

### Measured inconsistencies
- **Border radii: 11 distinct values** — 8, 10, 12, 14, 16, 20, 24, 32px, pills as both `50px` and `99px`, circles as `50%`.
- **Font sizes: ~26 distinct values** — .6, .62, .65, .68, .7, .72, .75, .78, .8, .82, .85, .88, .9, .92, .95, 1, 1.05, 1.1, 1.15, 1.2, 1.35, 1.4, 2.2, 3rem + clamps. No scale.
- **Transitions: 10+ durations** (.15s–.8s) with mixed easings; global tokens (`--transition-fast/base/slow`) exist but landing/studio hardcode.
- **Spacing tokens exist (`--gap-*`) but are almost never consumed** — paddings are one-off values (4→52px, ~20 distinct).
- **Shadows**: 3 tokens in global; landing uses ~8 ad-hoc shadows (`0 40px 80px`, `0 30px 60px`, `0 0 60px`…).
- **Two different golds**: upgrade button `#f59e0b→#fbbf24` vs pricing `#ffd700→#ffaa00`.
- **~8 primary-button styles**: `btn-primary-grad`, `btn-primary-glow`, `btn-nav`, `btn-upgrade`, `btn-plan-gold/elite/free`, `btn-ai-magic`, `vs-nav-btn-dl`, `btn-ai-fill`. Two unrelated `btn-ghost` definitions (global vs landing).
- **Heavy inline styles on landing**: mobile drawer, footer links, feature icons — all inline, duplicating classes that exist in global CSS.
- **`base_landing.html` exists but nothing extends it**; landing.html and home.html are standalone full documents duplicating head/CDN/font links.
- **Hero stacks 7 animated layers**: grid, 3 orbs, particles, waves, noise, typewriter, marquee — plus a (disabled) custom cursor. Busy = cheap.
- **Rule violations to fix in Phase 2**: hardcoded fake stats ("2,400 documents", "94% ATS pass rate"), 3 fake testimonials with names, "500+ GitHub stars" in a placeholder. Brand inconsistency: "cv-letter.ai" in the demo window title vs CVAI.
- Minor: mojibake emojis in home.html template fallbacks (`⏱ï¸`, `⚖ï¸`); `@keyframes blink` defined twice in landing; `shimmer` defined in both landing and global.

---

## 2. Ten highest-impact changes (ranked)

1. **One token set, consumed everywhere** — merge the 3 `:root` blocks into `cvai-global.css`; landing + studio consume it. Resolves the `--accent` collision, 3 backgrounds, 3 muted grays. *Why: consistency is the single biggest perceived-quality lever; unblocks everything else.* (Phase 1)
2. **One type system** — Syne (or Outfit 800) for display headings, Outfit for UI/body, JetBrains Mono for numbers; collapse 26 sizes into an 8-step scale. *Why: typography does the heavy lifting on premium feel.* (Phase 1–2)
3. **Rebuild hero: restraint + real product visual** — drop particles/orbs/grid/waves/noise/typewriter to at most one subtle background effect; add a framed screenshot/stylized mock of the studio above the fold; remove fake counters. *Why: value is currently invisible and the busy-ness reads as template-grade.* (Phase 2)
4. **Button hierarchy: 3 roles + 1 accent** — primary (indigo→pink gradient, glow reserved for it alone), secondary ghost, danger, gold upgrade accent (one gold). Kill the other 5 styles. *Why: coherent CTAs = product feel; also fixes gradient overuse.* (Phase 1 plumbing, applied 2–3)
5. **Normalize radius/shadow/transition to token scales** — 5 radii, 3 shadows + 1 reserved glow, 3 timings (150/200/300ms). *Why: pervasive subtle polish; the current 11-radius mix is exactly what makes UIs feel cheap.* (Phase 1)
6. **Honest social proof structure** — replace fake stats/testimonials with clearly-marked placeholder slots. *Why: rule compliance + trust; recruiters notice invented numbers.* (Phase 2)
7. **Studio: editor vs canvas separation** — flat panel for the form, visually distinct inset canvas for the PDF preview (contrast step + soft shadow), consistent 150–200ms micro-transitions, fix studio muted-text contrast (`#64748b` → AA). (Phase 4)
8. **Whitespace rhythm on landing + app screens** — consistent section padding scale, more breathing room inside cards; content max-width on large screens. (Phases 2–3, 5)
9. **De-inline the landing** — move drawer/footer/feature-icon inline styles into classes; reuse global nav/drawer components instead of the duplicated inline copy. *Why: consistency + maintainability; inline styles are why screens drift.* (Phase 2)
10. **States: empty/loading/error everywhere** — skeletons for async (90s generation must never look frozen), proper empty states with CTA, human error messages with retry. (Phase 3)

---

## 3. Proposed design tokens (one tight set → `cvai-global.css`)

```css
:root {
  /* Brand */
  --brand:        #4cc9f0;   /* cyan — links, active, focus */
  --brand-2:      #f72585;   /* pink — gradient end only */
  --indigo:       #4361ee;   /* gradient start only */
  --gold:         #f59e0b;   /* upgrade accent (single gold: #f59e0b→#fbbf24) */
  --grad-cta:     linear-gradient(135deg, var(--indigo), var(--brand-2));

  /* Surfaces (one background, stepped surfaces) */
  --bg:           #080c14;
  --surface-1:    #0d1322;               /* panels, cards (solid — no rgba stacking) */
  --surface-2:    rgba(255,255,255,.04); /* raised elements, inputs */
  --surface-3:    rgba(255,255,255,.07); /* hover */
  --border:       rgba(255,255,255,.08);
  --border-hover: rgba(255,255,255,.16);

  /* Text (all AA on --bg) */
  --text-1:       #f1f5f9;
  --text-2:       #94a3b8;
  --text-3:       #7c8a9e;   /* minimum for readable text */
  --text-dim:     #475569;   /* decorative only, never body copy */

  /* Status */
  --success:#22c55e; --warning:#eab308; --danger:#ef4444; --info:var(--brand);

  /* Type — 2 families, 8-step scale */
  --font-display: 'Syne', sans-serif;      /* 700/800, hero + section titles */
  --font-body:    'Outfit', sans-serif;    /* 400/600/800, everything else */
  --font-mono:    'JetBrains Mono', monospace; /* numbers, badges, code */
  --fs-xs:.72rem; --fs-sm:.8rem; --fs-base:.875rem; --fs-md:.95rem;
  --fs-lg:1.125rem; --fs-xl:1.5rem;
  --fs-2xl:clamp(1.75rem,3.5vw,2.75rem);        /* section titles */
  --fs-hero:clamp(2.4rem,6vw,4.5rem);           /* hero only */
  --lh-tight:1.1; --lh-base:1.55; --lh-loose:1.7;
  --track-tight:-0.02em;                        /* headings ≥ --fs-xl */

  /* Space — 4px base scale, consumed everywhere */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px;
  --sp-6:32px; --sp-7:48px; --sp-8:64px; --sp-9:96px; --sp-10:128px;
  --section-pad: clamp(72px, 10vw, 120px);      /* landing section rhythm */
  --content-max: 1200px;

  /* Radius — 5 values, nothing else */
  --r-sm:8px; --r-md:12px; --r-lg:16px; --r-xl:24px; --r-pill:999px;

  /* Elevation — borders + soft shadow, no glows except CTA */
  --shadow-1: 0 2px 8px rgba(0,0,0,.35);
  --shadow-2: 0 8px 28px -4px rgba(0,0,0,.45);
  --shadow-3: 0 24px 64px -12px rgba(0,0,0,.55);
  --glow-cta: 0 8px 32px rgba(67,97,238,.35);   /* primary CTA hover only */

  /* Motion */
  --t-fast: 150ms ease-out;                     /* hover, color, borders */
  --t-base: 200ms ease;                         /* transforms, reveals */
  --t-slide: 300ms cubic-bezier(.22,1,.36,1);   /* drawers, panels */
}
```

Migration notes (Phase 1): map old → new (`--primary`→`--brand`, `--radius-sm 10px`→`--r-sm/md`, `radius 14/20px`→`--r-lg`, `radius 24/32px`→`--r-xl`, pills→`--r-pill`, durations round to nearest token). Studio's local `--accent` gets deleted and its usages pointed at `--brand`. Legacy aliases can be kept temporarily (`--primary: var(--brand)`) so nothing breaks mid-migration.

---

**PHASE 0 DONE** — awaiting approval to start Phase 1 (token plumbing, no visual redesign).
