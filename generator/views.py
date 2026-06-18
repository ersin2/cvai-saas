import httpx
import os
import io
import re
import json
import tempfile
import logging
from pathlib import Path
from urllib.parse import urlparse

from asgiref.sync import sync_to_async          # wrap sync ORM/render calls for use in async views
from django.conf import settings                # reads AI_SERVICE_URL
from django.core.cache import cache             # lightweight rate-limiting store
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from pdfminer.high_level import extract_text as pdf_extract_text
from users.models import Profile
from .models import Generation, JobApplication, AIResult
from .pdf_engine import build_pdf, get_templates_for_plan, TEMPLATES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RATE LIMITER  (uses Django's default cache — LocMemCache or Redis)
# Free: 3 req/min  |  Pro: 20 req/min  |  Elite: 50 req/min
# ---------------------------------------------------------------------------
RATE_LIMITS = {'free': 3, 'pro': 20, 'elite': 50}
RATE_WINDOW = 60  # seconds

def _check_rate_limit(user, plan: str):
    """
    Returns None if the user is within limits, or a JsonResponse(429) if throttled.
    Uses a simple counter-per-user stored in Django's cache.
    """
    limit = RATE_LIMITS.get(plan, 3)
    cache_key = f"rl:{user.id}"
    count = cache.get(cache_key, 0)
    if count >= limit:
        return JsonResponse(
            {'error': 'Too many requests. Please wait a minute and try again.'},
            status=429,
        )
    cache.set(cache_key, count + 1, RATE_WINDOW)
    return None


# ---------------------------------------------------------------------------
# ASYNC AI MICROSERVICE CLIENT
# Django forwards prompts to the FastAPI ai_worker service.
# Uses httpx.AsyncClient so the Django async event loop is never blocked.
# ---------------------------------------------------------------------------
async def _call_ai_service(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7,
):
    """
    Async POST to the FastAPI /generate endpoint.
    Returns (result_text | None, error_message | None).
    """
    ai_url = getattr(settings, "AI_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")
    payload = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "provider": "groq",
        "temperature": temperature,
    }
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{ai_url}/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                return None, data["error"]
            return data.get("result"), None

    except httpx.ConnectError:
        logger.error("Cannot reach AI service at %s — is ai_worker running?", ai_url)
        return None, "AI service is unavailable. Please start the ai_worker and try again."
    except httpx.TimeoutException:
        return None, "AI took too long to respond. Please try again."
    except httpx.HTTPStatusError as exc:
        logger.error("AI service HTTP error %s: %s", exc.response.status_code, exc.response.text[:200])
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
    return render(request, 'generator/home.html')



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
    job_title    = request.POST.get('job_title_ai', 'Professional')[:200]
    tone         = request.POST.get('tone', 'Professional')[:200]
    language     = request.POST.get('language', 'English')[:200]
    first_name   = request.POST.get('first_name', '').strip()[:200]
    last_name    = request.POST.get('last_name', '').strip()[:200]
    full_name    = f"{first_name} {last_name}".strip() or "the candidate"

    if not profile.has_generations_left():
        error_message = "You've used all free generations! Upgrade to Pro for more."
    else:
        # ── CRITICAL LANGUAGE LOCK ──────────────────────────────────────────
        # Every sentence of the output MUST be in the requested language.
        # We use tagged section markers so the parser works regardless of language.
        system_prompt = f"""
You are an elite career strategist, senior recruiter, ATS expert, and professional
cover letter writer with experience at top global companies (FAANG, startups, enterprise).

⚠️ ABSOLUTE LANGUAGE RULE ⚠️
You MUST automatically detect the language of the candidate's raw input text. YOU MUST GENERATE YOUR ENTIRE RESPONSE IN THAT EXACT SAME LANGUAGE.
If the input is in Russian, EVERY SINGLE WORD of your output MUST be in Russian. This includes ALL section headers, bullet points, analysis, and tips.
DO NOT write even one word in English if the input is not English. If you fail to match the user's input language, you fail the task.
The user has also selected: {language} — use this as a fallback ONLY if you cannot detect the input language.

OUTPUT STRUCTURE — use these EXACT section tags (the tags themselves stay in English,
but ALL content between tags must be in {language}):

[SECTION: MAIN_LETTER]
... full main cover letter in {language} signed by {full_name} ...
[END_SECTION]

[SECTION: VERSION_A]
... Version A (Corporate/Traditional) in {language} signed by {full_name} ...
[END_SECTION]

[SECTION: VERSION_B]
... Version B (Bold/Impact) in {language} signed by {full_name} ...
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
Sign every cover letter version with the candidate's full name: {full_name}.

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
    result = None
    profile, _ = await Profile.objects.aget_or_create(user=request.user)

    # ── Rate-limit check ──
    throttled = await sync_to_async(_check_rate_limit)(request.user, profile.plan)
    if throttled:
        return throttled

    resume_text  = request.POST.get('resume', '')[:10000]
    language     = request.POST.get('language', 'English')[:200]
    first_name   = request.POST.get('first_name', '').strip()[:200]
    last_name    = request.POST.get('last_name', '').strip()[:200]
    full_name    = f"{first_name} {last_name}".strip() or "the candidate"

    # ── PDF upload: extract text and prepend to typed resume text ───────
    pdf_file = request.FILES.get('resume_pdf')
    if pdf_file:
        try:
            pdf_text = await sync_to_async(pdf_extract_text)(pdf_file)
            pdf_text = (pdf_text or '').strip()
            if pdf_text:
                # Combine PDF text + typed text, then re-enforce the cap
                resume_text = f"{pdf_text}\n\n{resume_text}".strip()[:10000]
        except Exception as exc:
            logger.warning("PDF extraction failed in generate_resume: %s", exc)

    if not profile.has_generations_left():
        error_message = "You've used all free generations! Upgrade to Pro for more."
    else:
        # ── STRUCTURED JSON RESUME PROMPT ──────────────────────────────────
        system_prompt = f"""You are an Elite Executive Recruiter and ATS optimization expert.
Transform the candidate's raw text into a structured resume.

⚠️ ABSOLUTE LANGUAGE RULE ⚠️
Detect the language of the input. ALL content values in your JSON output MUST be in that language.
The user selected: {language} — use as fallback only.

You MUST output ONLY a valid JSON object — no markdown, no commentary, no code fences.
The JSON schema is:
{{
  "full_name": "string",
  "target_role": "string",
  "email": "string or empty",
  "phone": "string or empty",
  "location": "string or empty",
  "linkedin": "string or empty",
  "summary": "2-3 sentence professional summary",
  "experience": [
    {{
      "title": "Job Title",
      "company": "Company Name",
      "dates": "Jan 2022 – Present",
      "bullets": ["Achievement bullet 1", "Achievement bullet 2", "Achievement bullet 3"]
    }}
  ],
  "skills": [
    {{"name": "Skill Name", "level": 85}}
  ],
  "education": [
    {{
      "degree": "Degree Name",
      "school": "University Name",
      "dates": "2018 – 2022"
    }}
  ],
  "languages": ["English (Fluent)", "Spanish (Native)"]
}}

RULES:
- Rewrite job duties using Google's XYZ formula with action verbs and metrics
- Extract ALL skills with estimated proficiency levels (0-100)
- Kill buzzwords — replace with concrete achievements
- If data is missing (email, phone), leave as empty string
- Output ONLY the JSON. No text before or after it."""

        user_prompt = f"""Candidate Name: {full_name}

Raw Career Information:
{resume_text}

Output Language: {language}

Generate the structured JSON resume now."""

        raw_result, error_message = await _call_ai_service(system_prompt, user_prompt, temperature=0.4)

        if raw_result:
            # Attempt to parse the JSON from the AI response
            try:
                # Strip markdown code fences if the AI wrapped them
                cleaned = raw_result.strip()
                if cleaned.startswith('```'):
                    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
                    cleaned = re.sub(r'\s*```$', '', cleaned)
                parsed = json.loads(cleaned)
                result = parsed
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("AI returned non-JSON for resume: %s", exc)
                # Fallback: return raw text so the frontend can still display something
                result = {'_raw': raw_result}

            await Generation.objects.acreate(
                user=request.user,
                resume_text=resume_text,
                job_description='[AI Resume Studio]',
                company_name='',
                job_title='',
                tone='Professional',
                language=language,
                result=json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else raw_result,
            )
            await sync_to_async(profile.use_generation)()

    return JsonResponse({
        'resume': result,
        'error': error_message,
    })


# === GENERATION HISTORY ===
@login_required
def history(request):
    generations = Generation.objects.filter(user=request.user)[:20]
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

    # Inline mode: used by the live-preview iframe in the Visual Resume Studio.
    # The frontend appends ?mode=preview to the fetch URL so the browser can
    # render the PDF directly inside the <iframe> without forcing a download.
    is_preview = request.GET.get('mode') == 'preview'
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

    # ── SSRF guard ────────────────────────────────────────────────────
    try:
        parsed = urlparse(url)
    except Exception:
        return JsonResponse({'error': 'Invalid URL.'}, status=400)

    if parsed.scheme not in ('http', 'https'):
        return JsonResponse(
            {'error': 'Only http and https URLs are allowed.'},
            status=400,
        )

    hostname = (parsed.hostname or '').lower()
    _BLOCKED = (
        'localhost',
        '127.0.0.1',
        '0.0.0.0',
        '[::1]',
        '::1',
    )
    _BLOCKED_PREFIXES = (
        '192.168.',   # RFC-1918 class C
        '10.',        # RFC-1918 class A
        '172.16.',    # RFC-1918 class B start
        '169.254.',   # link-local (APIPA / AWS metadata)
    )
    if hostname in _BLOCKED or any(hostname.startswith(p) for p in _BLOCKED_PREFIXES):
        logger.warning("SSRF attempt blocked for URL: %s (user=%s)", url, request.user.id)
        return JsonResponse(
            {'error': 'Requests to internal or private network addresses are not allowed.'},
            status=400,
        )
    # ── End SSRF guard ────────────────────────────────────────────────

    try:
        from bs4 import BeautifulSoup
        resp = httpx.get(url, timeout=15, follow_redirects=True, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
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

    # Validate file size (max 5MB) — check before reading any bytes
    if pdf_file.size > 5 * 1024 * 1024:
        return JsonResponse({'error': 'File too large. Maximum size is 5MB.'}, status=400)

    # ── Magic bytes validation ───────────────────────────────────────────────
    # Read the first 4 bytes and confirm the PDF signature.
    # This catches renamed executables/scripts regardless of the file extension.
    magic = pdf_file.read(4)
    if magic != b'%PDF':
        logger.warning(
            "Rejected non-PDF upload from user=%s: magic=%r filename=%r",
            request.user.id, magic, pdf_file.name,
        )
        return JsonResponse(
            {'error': 'Invalid file format. Only real PDF files are accepted.'},
            status=400,
        )
    # Reset pointer so pdfminer reads the full file from the beginning
    pdf_file.seek(0)
    # ── End magic bytes validation ───────────────────────────────────────────

    try:
        text = pdf_extract_text(pdf_file)
        text = text.strip()
        if not text:
            return JsonResponse({'error': 'Could not extract text from this PDF. It may be image-based.'}, status=400)
        return JsonResponse({'text': text[:10000]})  # Max 10k chars
    except Exception as e:
        logger.warning("PDF parse error: %s", e)
        return JsonResponse({'error': 'Failed to parse PDF. Try pasting your resume text instead.'}, status=400)


# === DASHBOARD ===
@login_required
def dashboard(request):
    """Unified dashboard with stats, recent activity, and analytics."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    generations = Generation.objects.filter(user=request.user)
    applications = JobApplication.objects.filter(user=request.user)
    ai_results = AIResult.objects.filter(user=request.user)

    stats = {
        'total_generations': generations.count(),
        'total_applications': applications.count(),
        'total_ai_results': ai_results.count(),
        'saved': applications.filter(status='saved').count(),
        'applied': applications.filter(status='applied').count(),
        'interviews': applications.filter(status='interview').count(),
        'offers': applications.filter(status='offer').count(),
        'rejected': applications.filter(status='rejected').count(),
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
    recent_results = AIResult.objects.filter(user=request.user)[:10]
    return render(request, 'generator/tools.html', {
        'results': recent_results,
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
