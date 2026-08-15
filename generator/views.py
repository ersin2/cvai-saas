import httpx
import json
import logging
import re
from urllib.parse import urljoin

from asgiref.sync import sync_to_async          # wrap sync ORM/render calls for use in async views
from django.db.models import Count, Q           # aggregate dashboard stats in one query
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from users.models import Profile
from .models import Generation, JobApplication, AIResult
from .pdf_engine import build_pdf, build_cover_letter_pdf, TEMPLATES

# ── Extracted modules ────────────────────────────────────────────────────────
# views.py was 1,793 lines because it also held the request guards, the resume
# schema and its recovery logic, and the AI transport — none of which are views.
# Only names this module actually calls are imported here; anything else lives
# in, and is imported from, the module that owns it.
from .ai import _call_ai_service, _language_rule, AI_MODEL_PROSE
from .guards import (
    json_login_required,
    _check_rate_limit,
    _url_points_to_public_host,
    _validate_pdf_upload,
    _validate_photo_upload,
    _SCRAPE_MAX_REDIRECTS,
    PREVIEW_RATE_LIMIT,
)
from .resume_schema import (
    _extract_resume_text,
    _parse_and_validate_resume,
    _truncate_on_boundary,
    RESUME_JSON_SCHEMA,
    RESUME_TEXT_BUDGET,
)

logger = logging.getLogger(__name__)


# Template rendering hits the DB via lazy FK access (e.g. user.profile);
# calling it without this wrapper raises SynchronousOnlyOperation.
arender = sync_to_async(render)


# === LANDING PAGE (unauthenticated) ===
def landing(request):
    if request.user.is_authenticated:
        return redirect('home')
    return render(request, 'generator/landing.html')


# === PRICING PAGE — needs full request context for dynamic plan buttons ===
def pricing(request):
    return render(request, 'generator/pricing.html')


# === STATIC LEGAL PAGES ===
def terms(request):
    return render(request, 'terms.html')


def privacy(request):
    return render(request, 'privacy.html')



# === HOME PAGE — Unified Resume Studio ===
@login_required
def home(request):
    # The gallery renders from the engine registry rather than hardcoded markup,
    # so template names/descriptions can never drift from what build_pdf ships.
    allowed_limit = 2  # anonymous preview falls back to the free-plan allowance
    context = {}
    if request.user.is_authenticated:
        from users.models import Profile
        profile, _ = Profile.objects.get_or_create(user=request.user)
        context['profile'] = profile
        allowed_limit = profile.get_pdf_template_limit()

    # Mirror download_pdf's gating so locked templates are visible up front,
    # instead of silently falling back to template 1 at download time.
    context['templates'] = [
        {**tpl, 'locked': idx >= allowed_limit}
        for idx, tpl in enumerate(TEMPLATES)
    ]
    return render(request, 'generator/home.html', context)



# === AI COVER LETTER GENERATION ===
@json_login_required
@require_POST
async def generate_letter(request):
    """
    Async cover letter generation.
    Awaits _call_ai_service() without blocking the Gunicorn/Uvicorn worker.
    """
    error_message = None
    result = None
    # Safe async profile fetch — auto-creates if the post_save signal missed
    profile, _ = await Profile.objects.aget_or_create(user=request.user)

    # Quota before throttle — see the note in ats_score. The quota check further
    # down still stands as a guard against a concurrent request spending the
    # last generation between here and there; this one exists so the *message*
    # is right, because the throttle would otherwise answer first and tell a
    # user who is out of generations to wait a minute.
    if not await sync_to_async(profile.has_generations_left)():
        return JsonResponse({'result': None, 'error': profile.quota_message()})

    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled

    resume_text  = request.POST.get('resume', '')[:10000]
    job_desc     = request.POST.get('job_description', '')[:10000]
    company_name = request.POST.get('company_name', 'Target Company')[:200]
    job_title    = request.POST.get('job_title', 'Professional')[:200]
    tone         = request.POST.get('tone', 'Professional')[:200]
    language     = request.POST.get('language', 'English')[:200]
    # Fall back to the account's real name — never the literal "the candidate",
    # which used to leak verbatim into the generated letter and History.
    full_name    = (request.POST.get('full_name', '').strip()[:400]
                    or request.user.get_full_name().strip()
                    or request.user.username)

    if not profile.has_generations_left():
        error_message = profile.quota_message()
    else:
        # ── CRITICAL LANGUAGE LOCK ──────────────────────────────────────────
        # Every sentence of the output MUST be in the requested language.
        # We use tagged section markers so the parser works regardless of language.
        # NOTE ON THIS PROMPT
        # MAIN_LETTER used to be specified as "full ATS-optimized resume summary
        # and experience bullet points" — resume content, in the section that
        # holds the cover letter. It was a copy-paste from the resume prompt, and
        # the model wrote what it was asked for.
        #
        # The rest of the prompt was pure format: five tags and no instruction on
        # what makes a letter good, so the model defaulted to the same template
        # regardless of which job description it was handed. The HOW TO WRITE
        # block below is what makes two different postings produce two different
        # letters.
        system_prompt = f"""
You are a career writer. You write cover letters that a hiring manager reads to
the end, because they are specific about this candidate and this job.

{_language_rule(language)}HOW TO WRITE THE LETTER

- Open with the specific reason this candidate fits this role. Never open with
  "I am writing to express my interest in" or "I am excited to apply for".
- Name at least two concrete requirements from the job description, and answer
  each with specific evidence from the CV — a project, a number, a system they
  built. If the CV cannot answer a requirement, leave it out rather than
  bluffing.
- Use only facts present in the CV. Do not invent employers, dates, metrics,
  degrees or seniority. If the CV is thin, write a shorter letter.
- Prefer plain, direct sentences. No "leverage", "synergy", "passionate about",
  "proven track record", "fast-paced environment".
- 250-350 words. Four paragraphs at most.

OUTPUT STRUCTURE — use these EXACT section tags (the tags themselves stay in
English, but ALL content between tags must be in {language}):

[SECTION: MAIN_LETTER]
... the complete cover letter, ready to send, in {language} ...
[END_SECTION]

[SECTION: VERSION_A]
... the same letter rewritten in a more formal, corporate register, in {language} ...
[END_SECTION]

[SECTION: VERSION_B]
... the same letter rewritten with a bolder, more direct opening, in {language} ...
[END_SECTION]

[SECTION: ATS_ANALYSIS]
... ATS score 0-100 for the CV against this job description, then the specific
    keywords from the posting that are missing from the CV — all in {language} ...
[END_SECTION]

[SECTION: RISK_ANALYSIS]
... 3 things a recruiter would question in this application (gaps, jumps, missing
    skills) and a concrete fix for each — all in {language} ...
[END_SECTION]

Do not write anything outside these tags.
"""

        user_prompt = f"""
========================================
CANDIDATE DATA
========================================
Full Name: {full_name}
Candidate CV:
{resume_text}

Job Description:
{job_desc}

Target Company: {company_name}
Job Title: {job_title}
Preferred Tone: {tone}
Output Language: {language}

⚠️  REMINDER: Every word of your entire response must be in {language}.
Include the candidate's full name: {full_name}.

Generate the elite career response now.
"""
        # Async handoff — Django yields its thread here.
        # This is the flagship generative feature and it ran on
        # llama-3.1-8b-instant, which is a large part of why letters read as
        # template-filling regardless of the posting. max_tokens has to cover
        # five tagged sections plus thinking.
        result, error_message = await _call_ai_service(
            system_prompt, user_prompt,
            model=AI_MODEL_PROSE, max_tokens=8192,
        )

        if result:
            # Django 5 async ORM: acreate() is non-blocking
            await Generation.objects.acreate(
                user=request.user,
                resume_text=resume_text,
                job_description=job_desc,
                company_name=company_name,
                job_title=job_title,
                tone=tone,
                language=language,
                result=result,
            )
            # use_generation() calls self.save() — wrap with sync_to_async
            await sync_to_async(profile.use_generation)()

    # Return JSON — the old template no longer renders cover letter results inline.
    # This endpoint remains available as an API for future use.
    return JsonResponse({
        'result': result,
        'error': error_message,
    })


# === AI RESUME GENERATION (Structured JSON for Visual Studio) ===
@json_login_required
@require_POST
async def generate_resume(request):
    """
    Async AI resume generation — returns STRUCTURED JSON for the
    A4 Visual Resume Studio canvas.

    The AI is prompted to output a JSON object with fields:
    full_name, target_role, email, phone, location, linkedin,
    summary, experience[], skills[], education[], languages[].

    Accepts an optional uploaded PDF (field name 'resume_pdf').
    If present, extracts text via pdfminer and combines it with
    any typed text the user also entered.
    """
    error_message = None
    warning = None
    result = None
    profile, _ = await Profile.objects.aget_or_create(user=request.user)

    # Quota before throttle — see the note in ats_score. The quota check further
    # down still stands as a guard against a concurrent request spending the
    # last generation between here and there; this one exists so the *message*
    # is right, because the throttle would otherwise answer first and tell a
    # user who is out of generations to wait a minute.
    if not await sync_to_async(profile.has_generations_left)():
        return JsonResponse({
            'resume': None,
            'error': profile.quota_message(),
            'warning': None,
        })

    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled

    typed_text   = request.POST.get('resume', '')
    language     = request.POST.get('language', 'English')[:200]
    full_name    = (request.POST.get('full_name', '').strip()[:400]
                    or request.user.get_full_name().strip()
                    or request.user.username)

    # ── PDF upload: extract text and combine with any typed text ───────
    # Each source gets its own budget so a long PDF can never evict what the
    # user typed (the old code concatenated then re-sliced the whole thing).
    pdf_text = ''
    pdf_file = request.FILES.get('resume_pdf')
    if pdf_file:
        # Same server-side guard as parse_resume_pdf — client checks are bypassable.
        ok, pdf_error = _validate_pdf_upload(pdf_file)
        if not ok:
            logger.warning(
                "Rejected resume_pdf upload from user=%s: %s (filename=%r)",
                request.user.id, pdf_error, pdf_file.name,
            )
            return JsonResponse({'resume': None, 'error': pdf_error})
        pdf_text, extract_error = await sync_to_async(_extract_resume_text)(pdf_file)
        if extract_error and not typed_text.strip():
            # Nothing typed to fall back on — tell the user instead of sending
            # an empty prompt to the model and billing them for the round trip.
            return JsonResponse({'resume': None, 'error': extract_error})

    typed_budget = min(len(typed_text), RESUME_TEXT_BUDGET // 3)
    pdf_budget   = RESUME_TEXT_BUDGET - typed_budget
    parts = []
    if pdf_text:
        parts.append(_truncate_on_boundary(pdf_text, pdf_budget, 'uploaded resume'))
    if typed_text.strip():
        parts.append(_truncate_on_boundary(typed_text.strip(), typed_budget, 'pasted text'))
    resume_text = '\n\n'.join(parts).strip()

    if not resume_text:
        return JsonResponse(
            {'resume': None, 'error': 'No resume content provided. Paste your resume or upload a PDF.'}
        )

    if not profile.has_generations_left():
        error_message = profile.quota_message()
    else:
        # ── STRUCTURED JSON RESUME PROMPT ──────────────────────────────────
        # Kept deliberately terse. This was compressed to fit a 6000 tokens/min
        # budget on a provider that has since been dropped, so the hard limit is
        # gone — but every rule below is load-bearing and the brevity costs
        # nothing. Shorten only by removing words, never rules.
        system_prompt = f"""You are an expert resume parsing engine. Convert unstructured candidate text into one clean, structured JSON object.

LANGUAGE: Detect the input's language. ALL content values MUST be in it. User selected {language} — fallback only.

NEVER INVENT: Use only facts explicitly in the source. Never invent, guess or hallucinate metrics, percentages, figures, skills, dates or achievements. Missing field -> empty string ""; section with no data -> empty array []. Never emit placeholders like "Not specified", "Unknown" or "N/A".

PRESERVE VOICE: Parse and structure only. No corporate fluff, no rewriting the candidate's descriptions in invented language, no achievements they did not claim. Light grammar/formatting cleanup of existing bullets is allowed.

EXTRACT EVERYTHING: This is a transcription task, not a summary. Every role, every project, every bullet point and every skill in the source MUST appear in the output. Never merge two bullets into one, never drop the weakest item to keep a list tidy, never stop early because a section is long. If the source lists nine bullets under a job, the output has nine.

Schema (all keys required, nesting exactly as shown):
{{
  "full_name": "string", "target_role": "string",
  "email": "string", "phone": "string", "location": "string",
  "linkedin": "string", "github": "string",
  "summary": "2-3 sentences, strictly from provided data",
  "experience": [{{"title": "string", "company": "string", "location": "string", "dates": "string", "bullets": ["string"]}}],
  "projects": [{{"title": "string", "tech_stack": "string", "bullets": ["string"]}}],
  "skills": [{{"category": "string", "items": ["string"]}}],
  "education": [{{"degree": "string", "school": "string", "dates": "string"}}],
  "languages": ["string"]
}}

RULES:
- Extract skills exactly as named; do not fabricate skill names or levels
- Group skills under the source's OWN headings, e.g. {{"category": "AI & LLM", "items": ["Claude", "RAG"]}}. Source has no headings -> ONE group with "category": ""
- Preserve job titles, company names and date ranges exactly as given
- Projects are a first-class section: extract every one, with its tech stack and all of its bullets
- No projects in the input -> "projects": []
- No GitHub URL found -> "github": ""
- Output ONLY the JSON object — no markdown, code fences, commentary or surrounding text."""

        user_prompt = f"""Candidate Name: {full_name}

Raw Career Information:
{resume_text}

Output Language: {language}

Generate the structured JSON resume now."""

        # This call reserves 8192 tokens. That reservation is why the previous
        # provider could never serve it: it counted the reservation against a
        # 6000 tokens/minute budget, so the call was 137% of the whole budget on
        # its own and failed regardless of how short the prompt was.
        #
        # Extraction is a fidelity task, not a creative one: the schema is
        # enforced by the provider rather than requested in prose, and effort
        # is held low because thinking shares the max_tokens budget with the
        # response and would otherwise crowd out a long resume.
        raw_result, error_message = await _call_ai_service(
            system_prompt, user_prompt,
            temperature=0.1, json_mode=True, max_tokens=8192,
            json_schema=RESUME_JSON_SCHEMA, effort="low",
        )

        problems = ['No response from the AI service.'] if not raw_result else None
        if raw_result:
            result, problems = _parse_and_validate_resume(raw_result)

            # ── One repair attempt when the first response is unusable ──────
            # Previously any parseable JSON was accepted as final, so a partial
            # extraction was billed and shown as a complete resume.
            if problems:
                logger.info(
                    "Resume JSON incomplete for user=%s (%d problem(s)) — retrying: %s",
                    request.user.id, len(problems), '; '.join(problems[:3]),
                )
                repair_prompt = (
                    f"{user_prompt}\n\n"
                    "━━━ REPAIR PASS ━━━\n"
                    "Your previous attempt was rejected by the schema validator:\n"
                    + '\n'.join(f'- {p}' for p in problems[:10])
                    + "\n\nRe-read the candidate text above and emit the COMPLETE JSON object "
                      "conforming exactly to the schema. Extract every experience, education "
                      "and skill entry present in the source. Output ONLY the JSON object."
                )
                retry_raw, retry_error = await _call_ai_service(
                    system_prompt, repair_prompt,
                    temperature=0.0, json_mode=True, max_tokens=8192,
                    json_schema=RESUME_JSON_SCHEMA, effort="low",
                )
                if retry_raw:
                    retry_result, retry_problems = _parse_and_validate_resume(retry_raw)
                    # Keep the retry only if it is genuinely better.
                    if retry_result is not None and len(retry_problems) < len(problems):
                        result, problems = retry_result, retry_problems
                        raw_result = retry_raw
                elif retry_error:
                    logger.warning("Resume repair pass failed: %s", retry_error)

        # A usable result has a name and at least one populated section.
        usable = result is not None and not any(
            p.startswith('Response was not') or p.startswith('No summary') for p in problems
        )

        if usable:
            await Generation.objects.acreate(
                user=request.user,
                resume_text=resume_text,
                job_description='[AI Resume Studio]',
                company_name='',
                job_title='',
                tone='Professional',
                language=language,
                result=json.dumps(result, ensure_ascii=False),
            )
            # Only bill a generation once we actually have something to show.
            await sync_to_async(profile.use_generation)()
            if problems:
                warning = (
                    'Some details could not be read from your resume — '
                    'please check the fields before exporting.'
                )
        else:
            result = None
            if not error_message:
                error_message = (
                    "The AI couldn't read a complete resume from that input. "
                    "Try pasting the text directly, or upload a text-based PDF."
                )
            logger.warning(
                "Resume extraction failed for user=%s: %s",
                request.user.id, '; '.join(problems[:3]) if problems else 'no result',
            )

    return JsonResponse({
        'resume': result,
        'error': error_message,
        'warning': warning,
    })




# === SECTION REWRITER (Visual Resume Studio — micro-AI per field) ===
@json_login_required
@require_POST
async def rewrite_section(request):
    """
    Async view — rewrites a single resume section with AI.

    POST fields
    -----------
    text          : str   The raw section text to rewrite (max 5 000 chars).
    section_type  : str   'summary' | 'experience'
    language      : str   Output language (fallback if auto-detect is ambiguous).

    Returns
    -------
    200  { "rewritten_text": "..." }
    400  { "error": "..." }         — bad input
    429  { "error": "..." }         — rate-limited
    402  { "error": "..." }         — out of generations
    """
    profile, _ = await Profile.objects.aget_or_create(user=request.user)

    # Quota before throttle, deliberately.
    #
    # Both gates can reject the same click, and they give opposite advice. Being
    # out of generations is permanent until you upgrade; being rate-limited
    # clears in a minute. Checked the other way round, a user who had spent
    # their last generation and clicked again was told "Too many requests,
    # please wait a minute" — advice that never comes true, because after the
    # minute they are still out of generations.
    has_left = await sync_to_async(profile.has_generations_left)()
    if not has_left:
        return JsonResponse(
            {'error': profile.quota_message()},
            status=402,
        )

    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled

    # ── Input validation ──────────────────────────────────────────────────────
    raw_text     = (request.POST.get('text') or '').strip()[:5000]
    section_type = (request.POST.get('section_type') or '').strip().lower()
    language     = (request.POST.get('language') or 'English').strip()[:50]

    if not raw_text:
        return JsonResponse({'error': 'No text provided to rewrite.'}, status=400)

    if section_type not in ('summary', 'experience'):
        return JsonResponse(
            {'error': f"Unknown section_type '{section_type}'. Must be 'summary' or 'experience'."},
            status=400,
        )

    # ── Build prompts ─────────────────────────────────────────────────────────
    LANG_RULE = _language_rule(language)

    if section_type == 'summary':
        system_prompt = (
            "You are an elite executive career coach and personal branding expert.\n\n"
            + LANG_RULE
            + "Your task: Transform the candidate's draft professional summary into a "
            "sharp, ATS-optimised 2–3 sentence professional summary.\n\n"
            "RULES:\n"
            "- Open with the candidate's seniority level and core identity (e.g. 'Senior Backend Engineer').\n"
            "- NEVER INVENT FACTS. Use only what the draft actually says. Do not add "
            "numbers, percentages, team sizes, timeframes, employers, or technologies "
            "that are not in the source. If the draft contains no metrics, write a "
            "specific summary without them — a concrete claim with no number beats an "
            "impressive number that is not true.\n"
            "- Where the draft DOES give a number, keep it exactly and put it to work.\n"
            "- Use active voice. Zero filler words ('passionate', 'hard-working', 'dynamic').\n"
            "- Maximum 60 words. Output ONLY the summary — no labels, no explanations."
        )
        user_prompt = f"Candidate's draft summary:\n\n{raw_text}"

    else:  # experience
        system_prompt = (
            "You are a senior technical recruiter who specialises in ATS optimisation "
            "and achievement-based resume writing.\n\n"
            + LANG_RULE
            + "Your task: Rewrite the candidate's work experience section using Google's XYZ formula:\n"
            "  'Accomplished [X] as measured by [Y] by doing [Z].'\n\n"
            "RULES:\n"
            "- Start every bullet with a strong past-tense action verb "
            "(Engineered, Reduced, Scaled, Delivered, Architected…).\n"
            "- NEVER INVENT FACTS. This resume goes to real employers and gets checked "
            "in interviews. Do not add numbers, percentages, team sizes, revenue, "
            "user counts, timeframes or technologies that are not in the source text. "
            "A made-up metric is worse than no metric.\n"
            "- Where the source gives a number, keep it exactly and make it the [Y] of "
            "the formula. Where it does not, write the strongest true version — name "
            "the system, the scope, or the outcome in words — and leave [Y] out rather "
            "than inventing one.\n"
            "- Ruthlessly remove vague duties. Replace with achievements the source supports.\n"
            "- Preserve the original job titles, company names, and date ranges exactly.\n"
            "- Keep the same plain-text structure as the input "
            "(title line, company|dates line, then bullet lines starting with '- ').\n"
            "- Output ONLY the rewritten experience block — no labels, no preamble."
        )
        user_prompt = f"Candidate's experience text:\n\n{raw_text}"

    # ── Call AI service ───────────────────────────────────────────────────────
    rewritten, error = await _call_ai_service(
        system_prompt, user_prompt,
        # The Studio's per-field rewriter — the feature demoed on the landing
        # page. Rewriting a vague bullet into a specific, quantified one is a
        # writing-judgement task, not a throughput task.
        # temperature is ignored on the Anthropic path (see _call_ai_service).
        temperature=0.55,
        model=AI_MODEL_PROSE,
    )

    if error or not rewritten:
        return JsonResponse(
            {'error': error or 'AI returned an empty response. Please try again.'},
            status=503,
        )

    # ── Deduct generation ─────────────────────────────────────────────────────
    await sync_to_async(profile.use_generation)()

    # ── Log the rewrite (reuse Generation model; store concisely) ────────────
    await Generation.objects.acreate(
        user=request.user,
        resume_text=raw_text[:500],
        job_description=f'[Section Rewrite — {section_type}]',
        company_name='',
        job_title='',
        tone='Professional',
        language=language,
        result=rewritten,
    )

    return JsonResponse({'rewritten_text': rewritten.strip()})


# === GENERATION HISTORY ===
def _classify_generation(gen):
    """
    Tag a Generation with the feature that produced it so History can label it
    correctly. We distinguish by the job_description sentinel written at save time:
      - '[AI Resume Studio]'      → resume  (from generate_resume)
      - '[Section Rewrite — …]'   → draft   (from rewrite_section)
      - otherwise (real company/title) → cover  (from generate_letter)
    """
    jd = gen.job_description or ''
    if jd == '[AI Resume Studio]':
        return 'resume'
    if jd.startswith('[Section Rewrite'):
        return 'draft'
    if gen.company_name or gen.job_title:
        return 'cover'
    return 'resume'


@login_required
def history(request):
    generations = list(Generation.objects.filter(user=request.user)[:20])
    for gen in generations:
        gen.kind = _classify_generation(gen)
    return render(request, 'generator/history.html', {
        'generations': generations,
    })


# === DELETE GENERATION (AJAX) ===
@json_login_required
@require_POST
async def delete_generation(request, pk):
    """
    Async AJAX view to delete a single generation.
    Returns JSON {"ok": true} on success, {"error": "..."} on failure.
    Ownership is enforced — users can only delete their own records.
    """
    gen = await sync_to_async(get_object_or_404)(
        Generation, pk=pk, user=request.user
    )
    await gen.adelete()
    return JsonResponse({'ok': True})


# === PDF GENERATOR (multi-template via pdf_engine.py) ===
@login_required
@require_POST
def generate_pdf(request):
    """
    Routes the request to the correct ReportLab template via pdf_engine.build_pdf().
    Validates the requested template against the user's plan limits.

    Query-string modifier:
      ?mode=preview  → Content-Disposition: inline  (for the live-preview <iframe>)
      (default)      → Content-Disposition: attachment  (triggers browser download)
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)

    # ── Rate-limit: PDF rendering is CPU-intensive ────────────────────────
    # The Studio live preview re-renders on every debounced edit, so it uses a
    # separate, generous bucket — otherwise editing would trip the per-plan
    # generation limit (Free = 3/min) and the preview would 429 mid-typing.
    is_preview = request.GET.get('mode') == 'preview'
    if is_preview:
        throttled = _check_rate_limit(
            request.user, profile.plan,
            limit=PREVIEW_RATE_LIMIT, key_prefix='rlprev',
        )
    else:
        throttled = _check_rate_limit(request.user, profile.plan)
    if throttled:
        return throttled

    # Reject an unusable photo up front with a specific reason. Without this the
    # file reaches PIL, and a large enough one takes the worker down with it —
    # the browser then shows a generic failure that blames nothing in particular.
    photo_ok, photo_error = _validate_photo_upload(request.FILES.get('photo'))
    if not photo_ok:
        return JsonResponse({'error': photo_error}, status=400)

    template_slug = request.POST.get('template_name', 'classic_navy')
    allowed_limit = profile.get_pdf_template_limit()
    allowed_slugs = [t['slug'] for t in TEMPLATES[:allowed_limit]]

    if template_slug not in allowed_slugs:
        # Silently fall back to the first allowed template
        template_slug = allowed_slugs[0]

    buffer   = build_pdf(template_slug, request)
    filename = f'CVAI_{template_slug}.pdf'

    # Inline mode (is_preview, resolved above): used by the live-preview iframe in
    # the Visual Resume Studio. The frontend appends ?mode=preview so the browser can
    # render the PDF directly inside the <iframe> without forcing a download.
    response = FileResponse(
        buffer,
        content_type='application/pdf',
        as_attachment=not is_preview,
        filename=filename,
    )
    if is_preview:
        # Allow the iframe to display the PDF; still keep it same-origin secure.
        response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response




# (export_resume_pdf removed — replaced by client-side html2pdf.js in Resume Studio)


# =====================================================
# === NEW FEATURES ===
# =====================================================


# === COVER LETTER PDF ===
@login_required
@require_POST
def download_cover_letter(request):
    """
    Render the generated cover letter as a PDF.

    Separate from download_pdf: that one dispatches to the resume layout
    builders, none of which lay out a business letter. Only the letter prose is
    sent here — the ATS and red-flag sections of the generated result are
    analysis for the candidate, not part of what gets sent to an employer.

    Rate-limited on the PDF bucket rather than the generation bucket: nothing
    is generated, an already-saved letter is just typeset.
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)
    throttled = _check_rate_limit(
        request.user, profile.plan,
        limit=PREVIEW_RATE_LIMIT, key_prefix='rlprev',
    )
    if throttled:
        return throttled

    if not (request.POST.get('body') or '').strip():
        return JsonResponse({'error': 'There is no letter to export yet.'}, status=400)

    buffer = build_cover_letter_pdf(request)
    company = (request.POST.get('company_name') or 'cover-letter').strip()
    slug = re.sub(r'[^A-Za-z0-9]+', '_', company).strip('_') or 'cover_letter'
    return FileResponse(
        buffer,
        content_type='application/pdf',
        as_attachment=True,
        filename=f'CVAI_Cover_Letter_{slug}.pdf',
    )


# === JOB URL SCRAPER ===
@login_required
@require_POST
def scrape_job_url(request):
    """Scrape a job posting URL and return the extracted text.

    Security:
    - Rate-limited per user/plan to prevent abuse.
    - SSRF-protected: only public http/https URLs are allowed.
      Requests to localhost, loopback, link-local, and RFC-1918
      private ranges are rejected with a 400 before any network call.
    """
    # ── Rate-limit check ─────────────────────────────────────────────────
    profile, _ = Profile.objects.get_or_create(user=request.user)
    throttled = _check_rate_limit(request.user, profile.plan)
    if throttled:
        return throttled

    url = request.POST.get('url', '').strip()
    if not url:
        return JsonResponse({'error': 'No URL provided'}, status=400)

    # ── SSRF-safe fetch ───────────────────────────────────────────────
    # We follow redirects MANUALLY so every hop is re-validated against the
    # public-host allow-list. Auto-following would let a public URL bounce us
    # into a private/internal address.
    try:
        from bs4 import BeautifulSoup

        current_url = url
        resp = None
        for _ in range(_SCRAPE_MAX_REDIRECTS + 1):
            ok, reason = _url_points_to_public_host(current_url)
            if not ok:
                logger.warning(
                    "SSRF attempt blocked for URL: %s (user=%s): %s",
                    current_url, request.user.id, reason,
                )
                return JsonResponse({'error': reason}, status=400)

            resp = httpx.get(
                current_url, timeout=15, follow_redirects=False,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
            )
            if resp.is_redirect:
                # httpx resolves the (possibly relative) Location into next_request;
                # fall back to urljoin if it is unavailable for any reason.
                if resp.next_request is not None:
                    current_url = str(resp.next_request.url)
                else:
                    current_url = urljoin(current_url, resp.headers.get('location', ''))
                continue
            break
        else:
            return JsonResponse({'error': 'Too many redirects.'}, status=400)

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Remove scripts, styles, nav, footer
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()

        text = soup.get_text(separator='\n', strip=True)
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        cleaned = '\n'.join(lines[:150])  # Max 150 lines

        return JsonResponse({'text': cleaned[:5000]})  # Max 5000 chars
    except Exception as e:
        logger.warning("Scrape error: %s", e)
        return JsonResponse({'error': 'Could not fetch that URL. Try pasting the text instead.'}, status=400)


# === RESUME PDF PARSER ===
@login_required
@require_POST
def parse_resume_pdf(request):
    """Extract text from an uploaded PDF resume.

    Security:
    - Rate-limited per user/plan.
    - Magic-bytes validated: only true PDF streams (starting with b'%PDF')
      are accepted, preventing disguised malware uploads.
    """
    # ── Rate-limit: pdfminer extraction can be expensive ────────────────────
    profile, _ = Profile.objects.get_or_create(user=request.user)
    throttled = _check_rate_limit(request.user, profile.plan)
    if throttled:
        return throttled

    pdf_file = request.FILES.get('pdf_file')
    if not pdf_file:
        return JsonResponse({'error': 'No file uploaded.'}, status=400)

    # ── Size + magic-bytes validation (shared helper) ────────────────────────
    # Catches oversized uploads and renamed executables/scripts regardless of
    # the file extension. The helper resets the pointer for pdfminer.
    ok, pdf_error = _validate_pdf_upload(pdf_file)
    if not ok:
        logger.warning(
            "Rejected non-PDF upload from user=%s: %s (filename=%r)",
            request.user.id, pdf_error, pdf_file.name,
        )
        return JsonResponse({'error': pdf_error}, status=400)

    # Shared extractor: column-aware layout params + structure-preserving cleanup,
    # so this path and generate_resume see identical text.
    text, extract_error = _extract_resume_text(pdf_file)
    if extract_error:
        return JsonResponse({'error': extract_error}, status=400)
    return JsonResponse({'text': _truncate_on_boundary(text, RESUME_TEXT_BUDGET)})


# === DASHBOARD ===
@login_required
def dashboard(request):
    """Unified dashboard with stats, recent activity, and analytics."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    generations = Generation.objects.filter(user=request.user)
    applications = JobApplication.objects.filter(user=request.user)
    ai_results = AIResult.objects.filter(user=request.user)

    # One query for all application counts instead of six separate .count() calls.
    app_counts = applications.aggregate(
        total=Count('id'),
        saved=Count('id', filter=Q(status='saved')),
        applied=Count('id', filter=Q(status='applied')),
        interviews=Count('id', filter=Q(status='interview')),
        offers=Count('id', filter=Q(status='offer')),
        rejected=Count('id', filter=Q(status='rejected')),
    )

    stats = {
        'total_generations': generations.count(),
        'total_applications': app_counts['total'],
        'total_ai_results': ai_results.count(),
        'saved': app_counts['saved'],
        'applied': app_counts['applied'],
        'interviews': app_counts['interviews'],
        'offers': app_counts['offers'],
        'rejected': app_counts['rejected'],
        'generations_left': profile.generations_count if profile.plan == 'free' else '∞',
        'plan': profile.get_plan_display(),
    }

    recent_generations = generations.order_by('-created_at')[:5]
    recent_apps = applications.order_by('-created_at')[:5]
    recent_ai = ai_results.order_by('-created_at')[:5]

    return render(request, 'generator/dashboard.html', {
        'stats': stats,
        'recent_generations': recent_generations,
        'recent_apps': recent_apps,
        'recent_ai': recent_ai,
    })


# === AI TOOLS PAGE ===
@login_required
def tools(request):
    """Render the AI tools page with interview prep, follow-up, and ATS tools."""
    from users.models import Profile
    profile, _ = Profile.objects.get_or_create(user=request.user)
    recent_results = AIResult.objects.filter(user=request.user)[:10]
    return render(request, 'generator/tools.html', {
        'results': recent_results,
        'profile': profile,
    })


# === INTERVIEW PREP AI ===
@json_login_required
@require_POST
async def interview_prep(request):
    """Async interview question generation."""
    profile, _ = await Profile.objects.aget_or_create(user=request.user)
    # Quota before throttle, deliberately.
    #
    # Both gates can reject the same click, and they give opposite advice. Being
    # out of generations is permanent until you upgrade; being rate-limited
    # clears in a minute. Checked the other way round, a user who had spent
    # their last generation and clicked again was told "Too many requests,
    # please wait a minute" — advice that never comes true, because after the
    # minute they are still out of generations.
    if not profile.has_generations_left():
        recent_results = await sync_to_async(list)(
            AIResult.objects.filter(user=request.user)[:10]
        )
        return await arender(request, 'generator/tools.html', {
            'results': recent_results,
            'active_tool': 'interview',
            'tool_error': profile.quota_message(),
        })

    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled

    resume  = request.POST.get('resume', '')[:10000]
    job_desc = request.POST.get('job_description', '')[:10000]
    company  = request.POST.get('company_name', '')[:200]

    system_prompt = "You are a senior technical interviewer and career coach.\n\n" + _language_rule() + """Generate exactly 10 likely interview questions for this candidate based on their resume
and the job description. For each question, provide:
- The question
- Why they'll ask it
- A strong sample answer (2-3 sentences)
- A tip for delivery

Format each as:
### Q1: [Question]
**Why they ask:** ...
**Strong answer:** ...
**Tip:** ...

Be specific to the role and company. No generic questions."""

    user_prompt = f"Resume:\n{resume}\n\nJob Description:\n{job_desc}\n\nCompany: {company}"

    result, error = await _call_ai_service(
        system_prompt, user_prompt,
        # Analysis a user reads and acts on, not a throughput task — worth a
        # stronger model than the cheap one this used to run on.
        model=AI_MODEL_PROSE,
    )

    if result:
        await AIResult.objects.acreate(
            user=request.user,
            result_type='interview',
            input_summary=f"{company} — Interview Prep",
            result=result,
        )
        await sync_to_async(profile.use_generation)()

    recent_results = await sync_to_async(list)(
        AIResult.objects.filter(user=request.user)[:10]
    )
    return await arender(request, 'generator/tools.html', {
        'results': recent_results,
        'active_tool': 'interview',
        'tool_result': result,
        'tool_error': error,
    })


# === FOLLOW-UP EMAIL GENERATOR ===
@json_login_required
@require_POST
async def followup_email(request):
    """Async follow-up email generation."""
    profile, _ = await Profile.objects.aget_or_create(user=request.user)
    # Quota before throttle, deliberately.
    #
    # Both gates can reject the same click, and they give opposite advice. Being
    # out of generations is permanent until you upgrade; being rate-limited
    # clears in a minute. Checked the other way round, a user who had spent
    # their last generation and clicked again was told "Too many requests,
    # please wait a minute" — advice that never comes true, because after the
    # minute they are still out of generations.
    if not profile.has_generations_left():
        recent_results = await sync_to_async(list)(
            AIResult.objects.filter(user=request.user)[:10]
        )
        return await arender(request, 'generator/tools.html', {
            'results': recent_results,
            'active_tool': 'followup',
            'tool_error': profile.quota_message(),
        })

    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled

    company   = request.POST.get('company_name', '')[:200]
    job_title = request.POST.get('job_title', '')[:200]
    context   = request.POST.get('context', '')[:10000]

    system_prompt = "You are an expert career communication strategist.\n\n" + _language_rule() + """Generate 3 professional follow-up emails for a job application:

1. **3-Day Follow-Up** — Short, polite check-in after applying
2. **7-Day Follow-Up** — Slightly more assertive, restate value
3. **14-Day Follow-Up** — Final follow-up, express continued interest

For each email provide:
- Subject line
- Full email body
- A note on best timing/approach

Keep emails concise (under 150 words each), professional, and personalized.
Do NOT be generic — reference the specific role and company."""

    user_prompt = f"Company: {company}\nJob Title: {job_title}\nAdditional Context: {context}"

    result, error = await _call_ai_service(
        system_prompt, user_prompt,
        # Analysis a user reads and acts on, not a throughput task — worth a
        # stronger model than the cheap one this used to run on.
        model=AI_MODEL_PROSE,
    )

    if result:
        await AIResult.objects.acreate(
            user=request.user,
            result_type='followup',
            input_summary=f"{company} — Follow-Up Emails",
            result=result,
        )
        await sync_to_async(profile.use_generation)()

    recent_results = await sync_to_async(list)(
        AIResult.objects.filter(user=request.user)[:10]
    )
    return await arender(request, 'generator/tools.html', {
        'results': recent_results,
        'active_tool': 'followup',
        'tool_result': result,
        'tool_error': error,
    })


# === ATS SCORE CHECKER ===
@json_login_required
@require_POST
async def ats_score(request):
    """Async ATS score generation."""
    profile, _ = await Profile.objects.aget_or_create(user=request.user)
    # Quota before throttle, deliberately.
    #
    # Both gates can reject the same click, and they give opposite advice. Being
    # out of generations is permanent until you upgrade; being rate-limited
    # clears in a minute. Checked the other way round, a user who had spent
    # their last generation and clicked again was told "Too many requests,
    # please wait a minute" — advice that never comes true, because after the
    # minute they are still out of generations.
    if not profile.has_generations_left():
        recent_results = await sync_to_async(list)(
            AIResult.objects.filter(user=request.user)[:10]
        )
        return await arender(request, 'generator/tools.html', {
            'results': recent_results,
            'active_tool': 'ats',
            'tool_error': profile.quota_message(),
        })

    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled

    resume   = request.POST.get('resume', '')[:10000]
    job_desc = request.POST.get('job_description', '')[:10000]

    system_prompt = "You are an ATS (Applicant Tracking System) expert.\n\n" + _language_rule() + """Analyze the resume against the job description and provide:

1. **ATS COMPATIBILITY SCORE: XX/100**

2. **KEYWORD MATCH** — List matched and missing keywords in two columns

3. **SECTION-BY-SECTION ANALYSIS**
   - Contact Info (present/missing)
   - Work Experience (relevance score)
   - Skills (match percentage)
   - Education (relevance)
   - Format Issues (bullet points, headers, etc.)

4. **TOP 5 IMPROVEMENTS** — Specific, actionable fixes ranked by impact

5. **OPTIMIZED SUMMARY** — Rewrite the resume summary/objective to score higher

Be brutally honest. Give specific keyword suggestions. Start with the score on the very first line."""

    user_prompt = f"Resume:\n{resume}\n\nJob Description:\n{job_desc}"

    result, error = await _call_ai_service(
        system_prompt, user_prompt,
        # Analysis a user reads and acts on, not a throughput task — worth a
        # stronger model than the cheap one this used to run on.
        model=AI_MODEL_PROSE,
    )

    score = None
    if result:
        import re
        score_match = re.search(r'(\d{1,3})\s*/\s*100', result)
        if score_match:
            score = int(score_match.group(1))

        await AIResult.objects.acreate(
            user=request.user,
            result_type='ats',
            input_summary="ATS Score Check",
            result=result,
            score=score,
        )
        await sync_to_async(profile.use_generation)()

    recent_results = await sync_to_async(list)(
        AIResult.objects.filter(user=request.user)[:10]
    )
    return await arender(request, 'generator/tools.html', {
        'results': recent_results,
        'active_tool': 'ats',
        'tool_result': result,
        'tool_error': error,
        'ats_score': score,
    })


# === APPLICATION TRACKER ===
# Kanban columns (status, label, top-border color) — presentational data for the board.
KANBAN_COLUMNS = [
    ('saved',     '📌 Saved',     '#64748b'),
    ('applied',   '📤 Applied',   '#3b82f6'),
    ('interview', '🎤 Interview', '#f59e0b'),
    ('offer',     '🎉 Offer',     '#10b981'),
    ('rejected',  '❌ Rejected',  '#ef4444'),
]


@login_required
def tracker(request):
    """Job application tracker dashboard."""
    if request.method == 'POST':
        # ── Plan limit guard ─────────────────────────────────────────────────
        profile, _ = Profile.objects.get_or_create(user=request.user)
        current_count = JobApplication.objects.filter(user=request.user).count()
        max_jobs = profile.get_max_tracked_jobs()

        if current_count >= max_jobs:
            messages.error(
                request,
                f"You've reached the limit of {max_jobs} tracked jobs for your "
                f"{profile.get_plan_display()} plan. Upgrade to track more!"
            )
            return redirect('pricing')
        # ── End limit guard ──────────────────────────────────────────────────

        JobApplication.objects.create(
            user=request.user,
            company_name=request.POST.get('company_name', ''),
            job_title=request.POST.get('job_title', ''),
            job_url=request.POST.get('job_url', ''),
            job_description=request.POST.get('job_description', ''),
            status=request.POST.get('status', 'saved'),
            salary_range=request.POST.get('salary_range', ''),
            notes=request.POST.get('notes', ''),
        )
        messages.success(request, 'Application added!')
        return redirect('tracker')

    applications = JobApplication.objects.filter(user=request.user)

    # Stats
    stats = {
        'total': applications.count(),
        'applied': applications.filter(status='applied').count(),
        'interviews': applications.filter(status='interview').count(),
        'offers': applications.filter(status='offer').count(),
        'rejected': applications.filter(status='rejected').count(),
    }

    return render(request, 'generator/tracker.html', {
        'applications': applications,
        'stats': stats,
        'kanban_cols': KANBAN_COLUMNS,
    })


@login_required
@require_POST
def tracker_update(request, pk):
    """Update a job application's status."""
    app = get_object_or_404(JobApplication, pk=pk, user=request.user)
    new_status = request.POST.get('status')
    if new_status in dict(JobApplication.STATUS_CHOICES):
        app.status = new_status
        app.save()
        return JsonResponse({'status': 'success', 'new_status': new_status})
    return JsonResponse({'error': 'Invalid status'}, status=400)


@login_required
@require_POST
def tracker_delete(request, pk):
    """Delete a job application."""
    app = get_object_or_404(JobApplication, pk=pk, user=request.user)
    app.delete()
    return JsonResponse({'status': 'success'})
