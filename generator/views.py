import httpx
import os
import io
import re
import json
import tempfile
import logging
from pathlib import Path

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
# Free: 3 requests/minute   |   Pro/Elite: 30 requests/minute
# ---------------------------------------------------------------------------
RATE_LIMITS = {'free': 3, 'pro': 30, 'elite': 30}
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



# === HOME PAGE (authenticated dashboard) ===
@login_required
def home(request):
    return render(request, 'generator/home.html', {
        'result': None,
        'error_message': None,
        'resume_text': '',
        'pdf_templates': TEMPLATES,   # all 5 — plan-gating is in the template
    })



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

    resume_text  = request.POST.get('resume', '')
    job_desc     = request.POST.get('job_description', '')
    company_name = request.POST.get('company_name', 'Target Company')
    job_title    = request.POST.get('job_title_ai', 'Professional')
    tone         = request.POST.get('tone', 'Professional')
    language     = request.POST.get('language', 'English')
    first_name   = request.POST.get('first_name', '').strip()
    last_name    = request.POST.get('last_name', '').strip()
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

    # arender wraps Django's sync render() — required in async views to avoid
    # SynchronousOnlyOperation when the template evaluates user.profile lazily.
    return await arender(request, 'generator/home.html', {
        'result': result,
        'error_message': error_message,
        'resume_text': resume_text,
        'pdf_templates': TEMPLATES,
    })


# === AI RESUME GENERATION (JSON response for Live-Edit flow) ===
@login_required
@require_POST
async def generate_resume(request):
    """
    Async AI resume generation using the Elite ATS-Optimized prompt.
    Returns JSON {"result": "...", "error": "..."} for the frontend
    Quill.js live-edit flow instead of rendering a full page.

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

    resume_text  = request.POST.get('resume', '')
    language     = request.POST.get('language', 'English')
    first_name   = request.POST.get('first_name', '').strip()
    last_name    = request.POST.get('last_name', '').strip()
    full_name    = f"{first_name} {last_name}".strip() or "the candidate"

    # ── PDF upload: extract text and prepend to typed resume text ───────
    pdf_file = request.FILES.get('resume_pdf')
    if pdf_file:
        try:
            pdf_text = await sync_to_async(pdf_extract_text)(pdf_file)
            pdf_text = (pdf_text or '').strip()
            if pdf_text:
                # Combine: uploaded PDF text first, then any additional typed text
                resume_text = f"{pdf_text}\n\n{resume_text}".strip()
        except Exception as exc:
            logger.warning("PDF extraction failed in generate_resume: %s", exc)
            # Non-fatal — continue with whatever text the user typed

    if not profile.has_generations_left():
        error_message = "You've used all free generations! Upgrade to Pro for more."
    else:
        # ── ELITE ATS-OPTIMIZED SYSTEM PROMPT ──────────────────────────────
        system_prompt = f"""You are an Elite Executive Recruiter and an expert in ATS (Applicant Tracking System) optimization. Your goal is to transform the user's raw, unstructured text into a world-class, top-tier resume.

⚠️ ABSOLUTE LANGUAGE RULE ⚠️
You MUST automatically detect the language of the candidate's raw input text. YOU MUST GENERATE YOUR ENTIRE RESPONSE IN THAT EXACT SAME LANGUAGE.
If the input is in Russian, EVERY SINGLE WORD of your output MUST be in Russian. This includes ALL section headers, bullet points, analysis, and tips.
DO NOT write even one word in English if the input is not English. If you fail to match the user's input language, you fail the task.
The user has also selected: {language} — use this as a fallback ONLY if you cannot detect the input language.

CRITICAL RULES:

1. NO CHATBOT FLUFF: Output ONLY the resume content. Do not include introductory phrases like "Here is your resume" or "Sure, I can help." Do not add any commentary before or after the resume.

2. FORMAT: Use clean Markdown formatting. Use **bolding** for job titles, company names, and key metrics.

RESUME BUILDING STRATEGY:

- STRUCTURE: Organize the output into clear sections: **Professional Summary**, **Experience**, **Skills**, and **Education**. Adapt sections based on the user's input (e.g., add Certifications, Projects, or Languages if relevant data is provided).

- THE XYZ FORMULA: Rewrite ALL job duties into powerful achievement bullets using Google's XYZ formula: "Accomplished [X] as measured by [Y], by doing [Z]". Focus on impact, metrics, and numbers. Even if the user provides no numbers, infer logical qualitative impacts.

- ACTION VERBS: Start EVERY bullet point with a strong action verb (e.g., Orchestrated, Spearheaded, Engineered, Optimized, Accelerated, Streamlined, Championed, Delivered, Architected, Drove).

- KILL THE BUZZWORDS: Ruthlessly delete empty fluff ("hard worker," "team player," "results-driven," "go-getter"). Replace them with concrete skills and measurable achievements.

- SKILLS INJECTION: Extract ALL technical and soft skills from the user's story and list them in a dedicated, comma-separated section optimized for ATS keyword scanners. Group them logically (e.g., Technical Skills, Tools & Platforms, Leadership & Communication).

TONE: Confident, data-driven, highly professional, and concise. Make the candidate sound like the top 1% in their field."""

        user_prompt = f"""
========================================
CANDIDATE RAW DATA — TRANSFORM INTO AN ELITE RESUME
========================================
Candidate Name: {full_name}

Raw Career Information:
{resume_text}

Output Language: {language}

⚠️ REMINDER: Your ENTIRE output must be in {language}.
Generate the world-class, ATS-optimized resume now.
"""
        result, error_message = await _call_ai_service(system_prompt, user_prompt)

        if result:
            await Generation.objects.acreate(
                user=request.user,
                resume_text=resume_text,
                job_description='[AI Resume Generation]',
                company_name='',
                job_title='',
                tone='Professional',
                language=language,
                result=result,
            )
            await sync_to_async(profile.use_generation)()

    # Return JSON for the frontend Quill.js live-edit flow
    return JsonResponse({
        'result': result,
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
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)
    template_slug = request.POST.get('template_name', 'classic_navy')
    allowed_limit = profile.get_pdf_template_limit()
    allowed_slugs = [t['slug'] for t in TEMPLATES[:allowed_limit]]

    if template_slug not in allowed_slugs:
        # Silently fall back to the first allowed template
        template_slug = allowed_slugs[0]

    buffer = build_pdf(template_slug, request)
    filename = f'CVAI_{template_slug}.pdf'
    return FileResponse(buffer, as_attachment=True, filename=filename)


# === EXPORT RESUME PDF (from Quill live-edit content) ===
@login_required
@require_POST
def export_resume_pdf(request):
    """
    Accepts user-edited resume content from the Quill.js editor
    and builds a downloadable PDF using pdf_engine.

    Expects POST fields:
      - resume_content: plain text of the edited resume
      - full_name: candidate name for the PDF header
      - target_role: job title for the PDF header
      - template_name: which PDF template to use (default: classic_navy)
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)

    resume_content = request.POST.get('resume_content', '')
    full_name      = request.POST.get('full_name', 'Your Name')
    target_role    = request.POST.get('target_role', 'Professional')
    template_slug  = request.POST.get('template_name', 'classic_navy')

    # Validate template against plan limits (same guard as generate_pdf)
    allowed_limit = profile.get_pdf_template_limit()
    allowed_slugs = [t['slug'] for t in TEMPLATES[:allowed_limit]]
    if template_slug not in allowed_slugs:
        template_slug = allowed_slugs[0]

    # Strip HTML tags to get clean text for ReportLab templates
    clean_text = re.sub(r'<[^>]+>', '', resume_content)
    # Normalise whitespace: collapse blank lines but keep structure
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()

    # Inject the content into request.POST so pdf_engine builders can read it
    # (they read from request.POST['resume'], 'experience_text', 'full_name', etc.)
    mutable_post = request.POST.copy()
    mutable_post['resume']          = clean_text
    mutable_post['experience_text'] = clean_text
    mutable_post['full_name']       = full_name
    mutable_post['target_role']     = target_role
    request.POST = mutable_post

    buffer = build_pdf(template_slug, request)
    filename = f'CVAI_Resume_{template_slug}.pdf'
    return FileResponse(buffer, as_attachment=True, filename=filename)


# =====================================================
# === NEW FEATURES ===
# =====================================================


# === JOB URL SCRAPER ===
@login_required
@require_POST
def scrape_job_url(request):
    """Scrape a job posting URL and return the extracted text."""
    url = request.POST.get('url', '').strip()
    if not url:
        return JsonResponse({'error': 'No URL provided'}, status=400)

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
        # Trim to reasonable length
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        cleaned = '\n'.join(lines[:150])  # Max 150 lines

        return JsonResponse({'text': cleaned[:5000]})  # Max 5000 chars
    except Exception as e:
        logger.warning(f"Scrape error: {e}")
        return JsonResponse({'error': 'Could not fetch that URL. Try pasting the text instead.'}, status=400)

# === RESUME PDF PARSER ===
@login_required
@require_POST
def parse_resume_pdf(request):
    """Extract text from an uploaded PDF resume."""
    pdf_file = request.FILES.get('pdf_file')
    if not pdf_file:
        return JsonResponse({'error': 'No file uploaded.'}, status=400)

    # Validate file type
    if not pdf_file.name.lower().endswith('.pdf'):
        return JsonResponse({'error': 'Only PDF files are supported.'}, status=400)

    # Validate file size (max 5MB)
    if pdf_file.size > 5 * 1024 * 1024:
        return JsonResponse({'error': 'File too large. Maximum size is 5MB.'}, status=400)

    try:
        text = pdf_extract_text(pdf_file)
        text = text.strip()
        if not text:
            return JsonResponse({'error': 'Could not extract text from this PDF. It may be image-based.'}, status=400)
        return JsonResponse({'text': text[:10000]})  # Max 10k chars
    except Exception as e:
        logger.warning(f"PDF parse error: {e}")
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

    resume = request.POST.get('resume', '')
    job_desc = request.POST.get('job_description', '')
    company = request.POST.get('company_name', '')

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

    company = request.POST.get('company_name', '')
    job_title = request.POST.get('job_title', '')
    context = request.POST.get('context', '')

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

    resume = request.POST.get('resume', '')
    job_desc = request.POST.get('job_description', '')

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
    return redirect('tracker')


@login_required
@require_POST
def tracker_delete(request, pk):
    """Delete a job application."""
    app = get_object_or_404(JobApplication, pk=pk, user=request.user)
    app.delete()
    messages.success(request, 'Application removed.')
    return redirect('tracker')