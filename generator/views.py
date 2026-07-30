import httpx
import io
import re
import json
import logging
from urllib.parse import urlparse, urljoin
import socket
import ipaddress

from asgiref.sync import sync_to_async          # wrap sync ORM/render calls for use in async views
from django.conf import settings                # reads AI_SERVICE_URL
from django.core.cache import cache             # lightweight rate-limiting store
from django.db.models import Count, Q           # aggregate dashboard stats in one query
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from pdfminer.high_level import extract_text as pdf_extract_text
from pdfminer.layout import LAParams
from users.models import Profile
from .models import Generation, JobApplication, AIResult
from .pdf_engine import build_pdf, TEMPLATES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RATE LIMITER  (uses Django's default cache — LocMemCache or Redis)
# Free: 3 req/min  |  Pro: 20 req/min  |  Elite: 50 req/min
# ---------------------------------------------------------------------------
RATE_LIMITS = {'free': 3, 'pro': 20, 'elite': 50}
RATE_WINDOW = 60  # seconds

# Live preview (Studio) re-renders the PDF on every debounced edit, so it needs a
# generous per-user bucket separate from the AI-generation limit — otherwise a free
# user's preview 429s after 3 edits/min. This is a render, not an AI generation.
PREVIEW_RATE_LIMIT = 60  # renders/min per user


def _check_rate_limit(user, plan: str, *, limit=None, key_prefix='rl'):
    """
    Returns None if the user is within limits, or a JsonResponse(429) if throttled.
    Uses a simple per-user counter stored in Django's cache.

    Deployment note: in production the cache is Redis (shared across the 4 ASGI
    workers), so the limit is global. With the LocMem fallback (local dev) the
    counter is per-process, so multi-worker dev servers see a looser effective
    limit — acceptable for local use.

    Fails OPEN: if the cache backend is degraded (django-redis is configured with
    IGNORE_EXCEPTIONS=True and returns None on connection errors), we allow the
    request rather than 500 every generation.
    """
    if limit is None:
        limit = RATE_LIMITS.get(plan, 3)
    cache_key = f"{key_prefix}:{user.id}"

    # First request in this window — add() returns True when the key was created.
    if cache.add(cache_key, 1, RATE_WINDOW):
        return None

    try:
        count = cache.incr(cache_key)
    except ValueError:
        # Key expired between add() and incr() — treat as a fresh window.
        cache.set(cache_key, 1, RATE_WINDOW)
        return None

    # Degraded cache backend returned None instead of an int — fail open.
    if count is None:
        return None

    if count > limit:
        return JsonResponse(
            {'error': 'Too many requests. Please wait a minute and try again.'},
            status=429,
        )
    return None


# ---------------------------------------------------------------------------
# SSRF PROTECTION
# Validate that a user-supplied URL points at a PUBLIC host before we fetch it.
# The check runs on every redirect hop (see scrape_job_url) so a public URL
# cannot 302-redirect us into the internal network — cloud metadata
# (169.254.169.254), the private ai-worker, Redis, or Postgres.
# Note: a determined attacker could still DNS-rebind between this resolve and
# httpx's connect (TOCTOU); blocking every private range on each hop keeps the
# residual risk low without a custom pinned-IP transport.
# ---------------------------------------------------------------------------
_SCRAPE_MAX_REDIRECTS = 5


def _url_points_to_public_host(url: str):
    """
    Return (True, None) if `url` is an http(s) URL whose hostname resolves
    ONLY to public IP addresses; otherwise (False, reason).
    Every resolved address (IPv4 + IPv6) must be public.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, 'Invalid URL.'

    if parsed.scheme not in ('http', 'https'):
        return False, 'Only http and https URLs are allowed.'

    hostname = parsed.hostname
    if not hostname:
        return False, 'Invalid URL.'

    try:
        addrinfo = socket.getaddrinfo(
            hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)
        )
    except socket.gaierror:
        return False, 'Could not resolve hostname.'

    for *_head, sockaddr in addrinfo:
        try:
            ip_obj = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False, 'Invalid IP address resolved.'
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified):
            return False, 'Requests to internal or private network addresses are not allowed.'

    return True, None


# ---------------------------------------------------------------------------
# PDF UPLOAD VALIDATION  (shared by parse_resume_pdf and generate_resume)
# Rejects oversized files and anything whose leading bytes are not a real PDF
# signature, so pdfminer never parses a disguised or huge payload.
# ---------------------------------------------------------------------------
PDF_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def _validate_pdf_upload(pdf_file):
    """
    Return (True, None) if the upload is a real PDF within the size cap,
    else (False, error_message). Leaves the file pointer reset to 0 so the
    caller can hand the file straight to pdfminer.
    """
    if pdf_file.size > PDF_MAX_BYTES:
        return False, 'File too large. Maximum size is 5MB.'
    magic = pdf_file.read(4)
    pdf_file.seek(0)
    if magic != b'%PDF':
        return False, 'Invalid file format. Only real PDF files are accepted.'
    return True, None


# ---------------------------------------------------------------------------
# RESUME TEXT EXTRACTION
# ---------------------------------------------------------------------------
# pdfminer's default layout analysis (boxes_flow=0.5) tries to reconstruct
# natural reading flow, which interleaves the columns of a two-column resume —
# "Skills" ends up spliced line-by-line into "Experience" and the model parses
# garbage. boxes_flow=None disables flow ordering and emits each text box whole,
# top-to-bottom / left-to-right, which is what a sectioned resume actually wants.
_RESUME_LAPARAMS = LAParams(
    boxes_flow=None,
    line_margin=0.4,   # tighter: keeps bullet lines inside their own block
    char_margin=1.5,
    word_margin=0.1,
)

# Total characters of resume context handed to the model. The previous 10k cap
# silently amputated 3-page CVs; Llama 3.1 has ample context for this.
RESUME_TEXT_BUDGET = 24000


def _truncate_on_boundary(text, limit, label='resume'):
    """
    Trim `text` to `limit` chars on a line boundary rather than mid-word, and
    mark the cut explicitly so the model knows the input was incomplete instead
    of silently treating a severed resume as the whole document.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind('\n')
    if nl > limit * 0.6:      # only back up to a newline if it isn't far back
        cut = cut[:nl]
    logger.info("Truncated %s text from %d to %d chars", label, len(text), len(cut))
    return cut.rstrip() + f"\n\n[...{label} truncated — later sections omitted]"


def _extract_resume_text(pdf_file):
    """
    Extract resume text from an uploaded PDF, preserving section structure.

    Returns (text, error_message). Shared by parse_resume_pdf and
    generate_resume so both paths get identical layout handling.

    The upload is copied into a BytesIO first: pdfminer.six only accepts
    io.IOBase, and Django hands us an InMemoryUploadedFile/TemporaryUploadedFile,
    which it rejects with "Unsupported input type". Passing the upload straight
    through made every PDF resume upload fail as if the file were unreadable.
    """
    try:
        pdf_file.seek(0)
        stream = io.BytesIO(pdf_file.read())
        pdf_file.seek(0)
        text = pdf_extract_text(stream, laparams=_RESUME_LAPARAMS) or ''
    except Exception as exc:
        logger.warning("PDF extraction failed: %s", exc)
        return '', 'Failed to parse PDF. Try pasting your resume text instead.'

    # Collapse runs of blank lines (pdfminer emits many) without destroying the
    # single blank line that separates one section from the next.
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if not text:
        return '', 'Could not extract text from this PDF. It may be image-based.'
    return text, None


# ---------------------------------------------------------------------------
# AI RESUME JSON — SCHEMA VALIDATION
# ---------------------------------------------------------------------------
# The parser used to accept any JSON that json.loads() survived, so a response
# containing only {"full_name": "..."} was billed and rendered as a complete
# resume with every other field silently blank. These helpers make an incomplete
# response detectable so it can be retried or flagged.
_RESUME_STR_FIELDS = (
    'full_name', 'target_role', 'email', 'phone',
    'location', 'linkedin', 'github', 'summary',
)
_RESUME_LIST_FIELDS = ('experience', 'projects', 'skills', 'education', 'languages')


def _repair_truncated_json(raw):
    """
    Best-effort recovery of a JSON object cut off mid-write (the usual shape of
    a max_tokens truncation): drop the dangling tail and close open brackets.
    Returns a parsed dict or None.
    """
    start = raw.find('{')
    if start == -1:
        return None
    candidate = raw[start:]
    # Walk back to the last plausible value end, then balance the brackets.
    for end in range(len(candidate) - 1, 0, -1):
        if candidate[end] not in '}]"0123456789truefalsnl':
            continue
        stack, in_str, esc = [], False, False
        for ch in candidate[:end + 1]:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str and ch in '{[':
                stack.append(ch)
            elif not in_str and ch in '}]':
                if stack:
                    stack.pop()
        if in_str:
            continue
        closed = candidate[:end + 1].rstrip().rstrip(',')
        closed += ''.join('}' if b == '{' else ']' for b in reversed(stack))
        try:
            parsed = json.loads(closed)
            if isinstance(parsed, dict):
                logger.info("Recovered truncated resume JSON (%d chars salvaged)", end)
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _validate_resume_json(parsed):
    """
    Normalize an AI resume object and report what's wrong with it.

    Returns (normalized_dict, problems) where `problems` is a list of
    human-readable strings. An empty list means the response is usable.
    Missing keys are filled with ""/[] so the frontend never sees undefined.
    """
    problems = []
    if not isinstance(parsed, dict):
        return None, ['Response was not a JSON object.']

    out = {}
    for key in _RESUME_STR_FIELDS:
        val = parsed.get(key, '')
        if val is None:
            val = ''
        if not isinstance(val, str):
            problems.append(f'"{key}" must be a string, got {type(val).__name__}.')
            val = str(val)
        out[key] = val.strip()

    for key in _RESUME_LIST_FIELDS:
        val = parsed.get(key, [])
        if val is None:
            val = []
        if not isinstance(val, list):
            problems.append(f'"{key}" must be an array, got {type(val).__name__}.')
            val = []
        out[key] = val

    # Structural checks on the repeating sections.
    for i, job in enumerate(out['experience']):
        if not isinstance(job, dict):
            problems.append(f'experience[{i}] must be an object.')
            continue
        if not str(job.get('title', '')).strip() and not str(job.get('company', '')).strip():
            problems.append(f'experience[{i}] has neither a title nor a company.')
        bullets = job.get('bullets', [])
        if bullets is not None and not isinstance(bullets, list):
            problems.append(f'experience[{i}].bullets must be an array.')

    for i, edu in enumerate(out['education']):
        if not isinstance(edu, dict):
            problems.append(f'education[{i}] must be an object.')
        elif not str(edu.get('degree', '')).strip() and not str(edu.get('school', '')).strip():
            problems.append(f'education[{i}] has neither a degree nor a school.')

    # Completeness: a resume with a name but no content whatsoever means the
    # model gave up partway. This is the signal the old code never had.
    if not out['full_name']:
        problems.append('"full_name" is empty — the candidate name was not extracted.')
    if not (out['summary'] or out['experience'] or out['education']):
        problems.append(
            'No summary, experience or education was extracted — '
            'the response is effectively empty.'
        )
    return out, problems


# ---------------------------------------------------------------------------
# ASYNC AI MICROSERVICE CLIENT
# Django forwards prompts to the FastAPI ai_worker service.
# Uses a global httpx.AsyncClient to ensure connection pooling across requests.
# ---------------------------------------------------------------------------
_ai_client = httpx.AsyncClient(timeout=90.0)

async def _call_ai_service(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7,
    json_mode: bool = False,
    max_tokens: int = 4096,
):
    """
    Async POST to the FastAPI /generate endpoint.
    Returns (result_text | None, error_message | None).

    json_mode  — ask the provider for constrained JSON decoding (resume parser).
    max_tokens — structured resumes need more headroom than prose responses.
    """
    ai_url = getattr(settings, "AI_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")
    payload = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "provider": "groq",
        "temperature": temperature,
        "json_mode": json_mode,
        "max_tokens": max_tokens,
    }
    try:
        resp = await _ai_client.post(f"{ai_url}/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return None, data["error"]
        return data.get("result"), None

    except httpx.ConnectError:
        logger.error("Cannot reach AI service at %s — is ai_worker running?", ai_url)
        return None, ("AI service is not running. Start it with run_all.bat "
                      "(uvicorn ai_service.main:app --port 8001) and try again.")
    except httpx.TimeoutException:
        return None, "AI took too long to respond. Please try again."
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:200]
        logger.error("AI service HTTP error %s: %s", exc.response.status_code, body)
        # A 404, or an HTML body, means something other than the AI worker is
        # answering on AI_SERVICE_URL — most often a second Django dev server
        # started on port 8001, which occupies the port the worker needs.
        if exc.response.status_code == 404 or body.lstrip()[:9].lower() in ('<!doctype', '<html'):
            return None, (f"{ai_url} is not the AI service — another app is running on "
                          "that port. Stop it and start the AI worker (run_all.bat).")
        return None, f"AI service error ({exc.response.status_code}). Please try again."
    except Exception as exc:
        logger.exception("AI service unexpected error: %s", exc)
        return None, "Something went wrong with AI generation."



# Async-safe render — wraps Django's sync render() for use inside async views.
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
@login_required
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

    # ── Rate-limit check ──
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
        error_message = "You've used all free generations! Upgrade to Pro for more."
    else:
        # ── CRITICAL LANGUAGE LOCK ──────────────────────────────────────────
        # Every sentence of the output MUST be in the requested language.
        # We use tagged section markers so the parser works regardless of language.
        system_prompt = f"""
You are an elite career strategist, senior recruiter, ATS expert, and professional
resume writer with experience at top global companies (FAANG, startups, enterprise).

⚠️ ABSOLUTE LANGUAGE RULE ⚠️
You MUST automatically detect the language of the candidate's raw input text. YOU MUST GENERATE YOUR ENTIRE RESPONSE IN THAT EXACT SAME LANGUAGE.
If the input is in Russian, EVERY SINGLE WORD of your output MUST be in Russian. This includes ALL section headers, bullet points, analysis, and tips.
DO NOT write even one word in English if the input is not English. If you fail to match the user's input language, you fail the task.
The user has also selected: {language} — use this as a fallback ONLY if you cannot detect the input language.

OUTPUT STRUCTURE — use these EXACT section tags (the tags themselves stay in English,
but ALL content between tags must be in {language}):

[SECTION: MAIN_LETTER]
... full ATS-optimized resume summary and experience bullet points in {language} ...
[END_SECTION]

[SECTION: VERSION_A]
... Version A (Corporate/Traditional format) in {language} ...
[END_SECTION]

[SECTION: VERSION_B]
... Version B (Bold/Impact format) in {language} ...
[END_SECTION]

[SECTION: ATS_ANALYSIS]
... ATS score 0-100 and detailed tips — all in {language} ...
[END_SECTION]

[SECTION: RISK_ANALYSIS]
... 3 recruiter red flags and concrete fixes — all in {language} ...
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
        # Async handoff — Django yields its thread here
        result, error_message = await _call_ai_service(system_prompt, user_prompt)

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
@login_required
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

    # ── Rate-limit check ──
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
        error_message = "You've used all free generations! Upgrade to Pro for more."
    else:
        # ── STRUCTURED JSON RESUME PROMPT ──────────────────────────────────
        system_prompt = f"""You are an expert resume parsing engine. Your sole function is to
read unstructured candidate text and output it as a clean, structured JSON object.

⚠️ ABSOLUTE LANGUAGE RULE ⚠️
Detect the language of the input. ALL content values in your JSON output MUST be in that language.
The user selected: {language} — use as fallback only.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT ANTI-HALLUCINATION POLICY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT RULE: Do NOT invent, hallucinate, or guess any metrics, percentages, or business
figures that are not explicitly present in the source text. Use ONLY the factual data
provided by the candidate. If a date, location, or parameter is missing from the input,
return an empty string "". NEVER return placeholders like "Not specified", "Unknown",
"N/A", or invented numbers. If a section has no data, return an empty array [] or "".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRODUCT PHILOSOPHY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The core value of this system is to provide predictable, customisable templates for the
user. Your role is strictly parsing and structuring data. Do NOT generate corporate fluff,
overwrite the user's own descriptions with invented language, or add achievements the
candidate did not claim. Preserve the candidate's original voice and factual content.
You may lightly improve grammar and formatting of existing bullet points only.

You MUST output ONLY a valid JSON object — no markdown, no commentary, no code fences.
The JSON schema is:
{{
  "full_name": "string",
  "target_role": "string",
  "email": "string or empty",
  "phone": "string or empty",
  "location": "string or empty",
  "linkedin": "string or empty",
  "github": "string or empty",
  "summary": "2-3 sentence professional summary based strictly on provided data",
  "experience": [
    {{
      "title": "Job Title",
      "company": "Company Name",
      "location": "City, Country or empty string",
      "dates": "Jan 2022 – Present or empty string",
      "bullets": ["Factual achievement bullet 1", "Factual achievement bullet 2"]
    }}
  ],
  "projects": [
    {{
      "title": "Project Name",
      "tech_stack": "Python, Django, PostgreSQL",
      "bullets": ["What it does or achieved"]
    }}
  ],
  "skills": [
    {{"name": "Skill Name"}}
  ],
  "education": [
    {{
      "degree": "Degree Name",
      "school": "University Name",
      "dates": "2018 – 2022 or empty string"
    }}
  ],
  "languages": ["English (Fluent)", "Spanish (Native)"]
}}

RULES:
- Extract skills exactly as named in the input — do not fabricate skill names or levels
- Preserve job titles, company names, and date ranges exactly as given
- If no projects section exists in the input, return an empty array for "projects"
- If no GitHub URL is found, return "" for "github"
- Output ONLY the JSON. No text before or after it."""

        user_prompt = f"""Candidate Name: {full_name}

Raw Career Information:
{resume_text}

Output Language: {language}

Generate the structured JSON resume now."""

        # Extraction is a fidelity task, not a creative one — near-zero
        # temperature, constrained JSON decoding, and enough completion
        # headroom that a long resume is not cut off mid-object.
        raw_result, error_message = await _call_ai_service(
            system_prompt, user_prompt,
            temperature=0.1, json_mode=True, max_tokens=8192,
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


def _parse_and_validate_resume(raw_result):
    """
    Turn a raw AI response into (normalized_resume | None, problems).

    Handles markdown fences, then a brace-balance repair for responses cut off
    by the token limit, then schema/completeness validation.
    """
    cleaned = raw_result.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        parsed = _repair_truncated_json(cleaned)
        if parsed is None:
            logger.warning("AI returned unparseable JSON for resume: %s", exc)
            return None, ['The AI response was not valid JSON.']

    return _validate_resume_json(parsed)


# === SECTION REWRITER (Visual Resume Studio — micro-AI per field) ===
@login_required
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

    # ── Rate-limit ────────────────────────────────────────────────────────────
    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled

    # ── Generation quota guard ────────────────────────────────────────────────
    has_left = await sync_to_async(profile.has_generations_left)()
    if not has_left:
        return JsonResponse(
            {'error': "You've used all free generations! Upgrade to Pro for unlimited rewrites."},
            status=402,
        )

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
    LANG_RULE = (
        "⚠️ ABSOLUTE LANGUAGE RULE ⚠️\n"
        "Automatically detect the language of the INPUT TEXT. "
        "Your ENTIRE output MUST be in that same language — not a single word in any other language. "
        f"The user's selected language is '{language}'; use it as a tiebreaker only.\n\n"
    )

    if section_type == 'summary':
        system_prompt = (
            "You are an elite executive career coach and personal branding expert.\n\n"
            + LANG_RULE
            + "Your task: Transform the candidate's draft professional summary into a "
            "POWERFUL, ATS-optimised 2–3 sentence executive summary.\n\n"
            "RULES:\n"
            "- Open with the candidate's seniority level and core identity (e.g. 'Senior Backend Engineer').\n"
            "- Sentence 2: quantified career achievement using concrete numbers/metrics.\n"
            "- Sentence 3: forward-looking value proposition or unique edge.\n"
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
            "- Each bullet MUST contain at least one concrete metric or business outcome "
            "(%, $, x, users, hours saved, etc.).\n"
            "- Ruthlessly remove vague duties. Replace with achievements.\n"
            "- Preserve the original job titles, company names, and date ranges exactly.\n"
            "- Keep the same plain-text structure as the input "
            "(title line, company|dates line, then bullet lines starting with '- ').\n"
            "- Output ONLY the rewritten experience block — no labels, no preamble."
        )
        user_prompt = f"Candidate's experience text:\n\n{raw_text}"

    # ── Call AI service ───────────────────────────────────────────────────────
    rewritten, error = await _call_ai_service(
        system_prompt, user_prompt, temperature=0.55
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
@login_required
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
@login_required
@require_POST
async def interview_prep(request):
    """Async interview question generation."""
    profile, _ = await Profile.objects.aget_or_create(user=request.user)
    # ── Rate-limit check ──
    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled
    if not profile.has_generations_left():
        recent_results = await sync_to_async(list)(
            AIResult.objects.filter(user=request.user)[:10]
        )
        return await arender(request, 'generator/tools.html', {
            'results': recent_results,
            'active_tool': 'interview',
            'tool_error': "You've used all free generations! Upgrade to Pro for unlimited AI tools.",
        })

    resume  = request.POST.get('resume', '')[:10000]
    job_desc = request.POST.get('job_description', '')[:10000]
    company  = request.POST.get('company_name', '')[:200]

    system_prompt = """You are a senior technical interviewer and career coach.

⚠️ ABSOLUTE LANGUAGE RULE ⚠️
You MUST automatically detect the language of the candidate's raw input text. YOU MUST GENERATE YOUR ENTIRE RESPONSE IN THAT EXACT SAME LANGUAGE.
If the input is in Russian, EVERY SINGLE WORD of your output MUST be in Russian. This includes ALL section headers, bullet points, analysis, questions, and tips.
DO NOT write even one word in English if the input is not English. If you fail to match the user's input language, you fail the task.

Generate exactly 10 likely interview questions for this candidate based on their resume
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

    result, error = await _call_ai_service(system_prompt, user_prompt)

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
@login_required
@require_POST
async def followup_email(request):
    """Async follow-up email generation."""
    profile, _ = await Profile.objects.aget_or_create(user=request.user)
    # ── Rate-limit check ──
    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled
    if not profile.has_generations_left():
        recent_results = await sync_to_async(list)(
            AIResult.objects.filter(user=request.user)[:10]
        )
        return await arender(request, 'generator/tools.html', {
            'results': recent_results,
            'active_tool': 'followup',
            'tool_error': "You've used all free generations! Upgrade to Pro for unlimited AI tools.",
        })

    company   = request.POST.get('company_name', '')[:200]
    job_title = request.POST.get('job_title', '')[:200]
    context   = request.POST.get('context', '')[:10000]

    system_prompt = """You are an expert career communication strategist.

⚠️ ABSOLUTE LANGUAGE RULE ⚠️
You MUST automatically detect the language of the candidate's raw input text. YOU MUST GENERATE YOUR ENTIRE RESPONSE IN THAT EXACT SAME LANGUAGE.
If the input is in Russian, EVERY SINGLE WORD of your output MUST be in Russian. This includes ALL section headers, bullet points, analysis, questions, and tips.
DO NOT write even one word in English if the input is not English. If you fail to match the user's input language, you fail the task.

Generate 3 professional follow-up emails for a job application:

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

    result, error = await _call_ai_service(system_prompt, user_prompt)

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
@login_required
@require_POST
async def ats_score(request):
    """Async ATS score generation."""
    profile, _ = await Profile.objects.aget_or_create(user=request.user)
    # ── Rate-limit check ──
    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled
    if not profile.has_generations_left():
        recent_results = await sync_to_async(list)(
            AIResult.objects.filter(user=request.user)[:10]
        )
        return await arender(request, 'generator/tools.html', {
            'results': recent_results,
            'active_tool': 'ats',
            'tool_error': "You've used all free generations! Upgrade to Pro for unlimited AI tools.",
        })

    resume   = request.POST.get('resume', '')[:10000]
    job_desc = request.POST.get('job_description', '')[:10000]

    system_prompt = """You are an ATS (Applicant Tracking System) expert.

⚠️ ABSOLUTE LANGUAGE RULE ⚠️
You MUST automatically detect the language of the candidate's raw input text. YOU MUST GENERATE YOUR ENTIRE RESPONSE IN THAT EXACT SAME LANGUAGE.
If the input is in Russian, EVERY SINGLE WORD of your output MUST be in Russian. This includes ALL section headers, bullet points, analysis, questions, and tips.
DO NOT write even one word in English if the input is not English. If you fail to match the user's input language, you fail the task.

Analyze the resume against the job description and provide:

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

    result, error = await _call_ai_service(system_prompt, user_prompt)

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
