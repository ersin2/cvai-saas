# CVAI SaaS — Audit & Repair Report

Full senior-level pass over the Django + FastAPI resume/cover-letter SaaS.
Order: **Security → Architecture/Stability → Bugs → UI/UX.** All 70 tests in
`tests/test_suite.py` pass after the changes; `manage.py check` is clean and no new
migrations are required.

Product decision applied (per owner): **resume builder AND cover letters are both
first-class features** — the UI now distinguishes them rather than collapsing everything
into one term.

---

## Fixed

### Security
- **F1 — SSRF redirect bypass** (`generator/views.py`, `scrape_job_url`).
  The guard validated only the first hostname, then followed redirects automatically —
  a public URL could 302 into the internal network (cloud metadata `169.254.169.254`,
  the private `ai-worker`, Redis, Postgres). Now redirects are followed **manually** and
  **every hop is re-validated** by `_url_points_to_public_host()`, which resolves all
  IPs (v4+v6) via `getaddrinfo` and blocks private/loopback/link-local/multicast/
  reserved/unspecified ranges. Capped at 5 redirects. Verified against metadata IP,
  loopback, private ranges, and bad schemes.
- **F2 — Unbounded/untyped PDF upload** (`generate_resume`).
  The inline `resume_pdf` path fed any file to pdfminer with no server-side checks.
  Added a shared `_validate_pdf_upload()` (5 MB cap + `%PDF` magic bytes) now used by
  **both** `generate_resume` and `parse_resume_pdf` (dedupes the old inline logic).

### Architecture / stability
- **F3 — Rate limiter crash + notes** (`_check_rate_limit`).
  On a Redis outage (`IGNORE_EXCEPTIONS=True` returns `None`) the old code did
  `None > limit` → `TypeError` → 500 on every generation. Now **fails open** on a
  degraded cache. Documented the per-process LocMem caveat (prod uses shared Redis).
- **F4 — Stripe webhook idempotency** (`users/views.py`).
  Added an atomic `cache.add('stripe_evt:<id>')` guard so redelivered events
  short-circuit instead of re-applying upgrades. Fails open (processes) if cache is down.
- **F5 — Free-tier quota race** (`users/models.py`, `use_generation`).
  Replaced the read-modify-write decrement with an atomic guarded `F()` update
  (`generations_count__gt=0`), so concurrent requests can't lose updates or drive the
  counter negative. Returns whether a generation was consumed.
- **F6 — Redundant Profile saves** (`users/signals.py`).
  Collapsed the two-signal pattern into one `ensure_profile` that creates the profile
  only on user creation — eliminating an extra `profile.save()` on **every** `User.save()`
  (including each login's `last_login` write).
- **Dead code**: removed unused imports in `generator/views.py`
  (`os`, `io`, `tempfile`, `Path`, `get_templates_for_plan`).

### Bugs / correctness
- **F7 — History mislabeled resumes vs cover letters.**
  A cover letter (has company/title) was badged **"Resume"**, and a real resume was
  badged **"Resume Draft"**. Added `_classify_generation()` (keys off the
  `job_description` sentinel) so History now shows **Resume / Draft / Cover Letter**
  correctly. Also fixed the `meta_desc` typo ("resumes and resumes") and the empty-state
  copy mismatch.
- **F11 — Cover-letter generator had no UI + broken tools template.**
  `generate_letter` worked but nothing called it. Added a **Cover Letter tool** to the AI
  Tools page (form → `fetch` → client-rendered section tabs: Cover Letter / Version A /
  Version B / ATS Score / Red Flags; endpoint unchanged). **Bonus:** the AI Tools page
  itself was un-renderable — the sidebar opened a `{% with %}` inside `{% if/elif %}`
  branches (invalid Django, 500 on any load). Rewrote it with inline conditionals.
- **"the candidate" name leak** (`generate_letter`, `generate_resume`).
  Empty name fell back to the literal string `"the candidate"`, which the AI wrote into
  the output. Now falls back to the account's real name (`get_full_name()` → `username`).
- **F8 — Provider names leaked in marketing.**
  Landing "…powered by Groq/Anthropic on Groq's infrastructure" → "…powered by Elite AI".
  `privacy.html` still correctly names **Groq** (the actual data processor — Django
  hardcodes `provider="groq"`), which is the accurate legal disclosure.
- **F12 — Dashboard query fan-out.**
  Collapsed six per-status `.count()` calls into a single `aggregate(Count/Q)`.

### UI / UX
- **F9 — Mobile landing hero** filled the viewport (100vh + 6.5rem title), pushing the CTA
  below the fold. Added mobile overrides (auto height, smaller title/padding).
- **F10 — Contrast (WCAG AA).** Landing `--muted` `#64748b→#94a3b8` (~4:1→~7:1) and the
  shared app token `--text-muted` `#475569→#7c8a9e` (~2.5:1→~4.6:1 on `#080c14`), still
  dimmer than `--text-secondary` so hierarchy holds. One token fix lifts every app page.
- **Terminology / markets-both.** Landing hero now says "resumes and cover letters",
  a neutral "Get Started Free" CTA, "Documents Generated" stat, and the typewriter leads
  with "Perfect Resume." Dashboard empty-state "letter awaits" → "resume awaits".

---

## Intentionally left (with reason)

- **Orphaned studio partials** (`partials/studio_content.html`, `studio_scripts.html`) —
  contain dead calls to `/save-resume/` and `/api/refine-text/` (no such routes), but no
  live page includes them (`home.html` is the real studio). Left in place per "don't
  delete work-in-progress." Safe to delete later; zero runtime impact today.
- **`generator/templates/generator/pdf_template.html`** ("Cover Letter" `<h1>`) — not
  referenced by any code (PDFs come from `pdf_engine.py`/ReportLab). Left untouched.
- **Prompt injection (F14)** — user resume/scraped text is interpolated into prompts and
  can steer the model, but impact is limited to the user's own output (no cross-tenant or
  system escalation). Optional hardening later.
- **`--text-dim: #334155`** — intentionally near-invisible decorative accents, not body
  text; left as-is.

---

## Top 5 to verify / decide before deploy

1. **SSRF (F1)** — confirm in staging that a public URL 302-redirecting to
   `http://127.0.0.1` / `http://169.254.169.254` is rejected (unit-tested; worth a live check).
2. **Cover Letter tool (F11)** — generate one end-to-end; confirm it renders the section
   tabs and appears in History badged **"Cover Letter"**.
3. **Stripe webhook (F4)** — replay a `checkout.session.completed` in test mode; confirm
   the duplicate is ignored (idempotent).
4. **Rate limiter (F3)** — confirm prod `REDIS_URL` is set (shared limit) and that a Redis
   outage no longer 500s generation (fails open).
5. **Static rebuild** — the `--text-muted` change is in source CSS; `build.sh` runs
   `collectstatic` on deploy, so production picks it up automatically. No action if you
   deploy via the normal build.

---

## Verification performed

- `python manage.py check` — clean.
- `python manage.py makemigrations --check` — no changes.
- `python manage.py test tests.test_suite` — **70 passed** (SSRF, PDF guard, rate limiter,
  quota, PDF render, auth guards, tracker).
- Template compile of all edited templates — OK.
- Throwaway integration test (removed): AI Tools page renders with the new Cover Letter
  tool + previously-broken sidebar; `generate_letter` (AI mocked) saves a Generation
  classified as "cover"; name fallback resolves to the account name, not "the candidate".
