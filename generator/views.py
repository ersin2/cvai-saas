import httpx
import os
import io
import json
import tempfile
import logging
from pathlib import Path

from asgiref.sync import sync_to_async          # wrap sync ORM methods for use in async views
from django.conf import settings                # reads AI_SERVICE_URL
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from pdfminer.high_level import extract_text as pdf_extract_text
from users.models import Profile
from .models import Generation, JobApplication, AIResult

# ReportLab (PDF)
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph,
    Spacer, Image as RLImage, FrameBreak, Flowable
)
from reportlab.graphics.shapes import Drawing, Line
from reportlab.lib.units import mm
from PIL import Image, ImageOps, ImageDraw

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ASYNC AI MICROSERVICE CLIENT
# Django is a thin client here — it builds prompts and delegates ALL LLM logic
# to the FastAPI ai_service running on AI_SERVICE_URL.
# ---------------------------------------------------------------------------
async def _call_ai_service(
    system_prompt: str,
    user_prompt: str,
    provider: str = "groq",
    temperature: float = 0.7,
):
    """
    Async handoff to the FastAPI AI microservice.

    Django yields its worker thread here (via httpx.AsyncClient) while
    the FastAPI service handles the blocking LLM network call on its own
    async event loop.  Returns (result_text | None, error_message | None).
    """
    ai_url = getattr(settings, "AI_SERVICE_URL", "http://127.0.0.1:8001")
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{ai_url}/generate",
                json={
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "provider": provider,
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # FastAPI returns {"result": "...", "error": null} or {"result": null, "error": "..."}
            return data.get("result"), data.get("error")

    except httpx.ConnectError:
        logger.error(
            "Cannot reach AI service at %s — is ai_worker running?", ai_url
        )
        return None, "AI service is temporarily unavailable. Please try again later."
    except httpx.TimeoutException:
        return None, "AI took too long to respond. Please try again."
    except Exception as exc:
        logger.exception("AI service unexpected error: %s", exc)
        return None, "Something went wrong with AI generation."


# --- SKILL BAR FLOWABLE (PDF) ---
class ProSkillBar(Flowable):
    def __init__(self, name, level_percent, width=150, height=14):
        Flowable.__init__(self)
        self.name = name
        self.level = level_percent / 100.0
        self.width = width
        self.height = height

        if self.level >= 0.9:
            self.txt = "EXPERT"
        elif self.level >= 0.70:
            self.txt = "SENIOR"
        elif self.level >= 0.40:
            self.txt = "MIDDLE"
        else:
            self.txt = "JUNIOR"

    def draw(self):
        c = self.canv
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(colors.white)
        c.drawString(0, 7, self.name)

        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor("#bdc3c7"))
        c.drawRightString(self.width, 7, self.txt)

        c.setFillColor(colors.HexColor("#34495e"))
        c.roundRect(0, 0, self.width, 4, 2, fill=1, stroke=0)

        c.setFillColor(colors.HexColor("#4cc9f0"))
        c.roundRect(0, 0, self.width * self.level, 4, 2, fill=1, stroke=0)


# === LANDING PAGE (unauthenticated) ===
def landing(request):
    if request.user.is_authenticated:
        return redirect('home')
    return render(request, 'generator/landing.html')


# === HOME PAGE (authenticated dashboard) ===
@login_required
def home(request):
    result = None
    error_message = None
    # Profile is guaranteed by post_save signal in users/signals.py
    return render(request, 'generator/home.html', {
        'result': result,
        'error_message': error_message,
        'resume_text': '',
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
    # Async profile fetch — guaranteed to exist via post_save signal
    profile = await Profile.objects.aget(user=request.user)

    resume_text = request.POST.get('resume', '')
    job_desc = request.POST.get('job_description', '')
    company_name = request.POST.get('company_name', 'Target Company')
    job_title = request.POST.get('job_title_ai', 'Professional')
    tone = request.POST.get('tone', 'Professional')
    language = request.POST.get('language', 'English')

    if not profile.has_generations_left():
        error_message = "You've used all free generations! Upgrade to Pro for more."
    else:
        system_prompt = """
You are an elite career strategist, senior recruiter, ATS optimization expert,
and professional cover letter writer with experience hiring for top global
companies (FAANG, startups, enterprise, and tech firms).

Your goal is NOT to simply write a cover letter.
Your goal is to maximize the candidate's chances of getting an interview.

STRUCTURE YOUR RESPONSE EXACTLY LIKE THIS:
1. MAIN COVER LETTER
2. VERSION A (Corporate/Traditional)
3. VERSION B (Bold/Impact)
4. ATS ANALYSIS (Score 0-100 & Tips)
5. RECRUITER RISK ANALYSIS (3 risks & fixes)

Do not include any conversational filler before or after.
"""

        user_prompt = f"""
========================
INPUT DATA
========================
Candidate CV: {resume_text}
Job Description: {job_desc}
Target Company: {company_name}
Job Title: {job_title}
Preferred Tone: {tone}
Language: {language}

Generate the elite response now.
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

    return render(request, 'generator/home.html', {
        'result': result,
        'error_message': error_message,
        'resume_text': resume_text,
    })


# === GENERATION HISTORY ===
@login_required
def history(request):
    generations = Generation.objects.filter(user=request.user)[:20]
    return render(request, 'generator/history.html', {
        'generations': generations,
    })


# === PDF GENERATOR (NAVY PRO STYLE) ===
@login_required
@require_POST
def generate_pdf(request):
    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=A4,
        rightMargin=0, leftMargin=0, topMargin=0, bottomMargin=0
    )

    # COLORS
    C_SIDEBAR = colors.HexColor("#2c3e50")
    C_ACCENT = colors.HexColor("#4cc9f0")
    C_TEXT = colors.HexColor("#2c3e50")
    C_GREY = colors.HexColor("#7f8c8d")

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_SIDEBAR)
        canvas.rect(0, 0, 80 * mm, 297 * mm, fill=1, stroke=0)
        canvas.restoreState()

    frame_sb = Frame(
        0, 0, 80 * mm, 297 * mm, id='sb',
        leftPadding=20, rightPadding=20, topPadding=30, bottomPadding=30
    )
    frame_main = Frame(
        80 * mm, 0, 130 * mm, 297 * mm, id='main',
        leftPadding=25, rightPadding=25, topPadding=30, bottomPadding=30
    )
    doc.addPageTemplates([
        PageTemplate(id='Layout', frames=[frame_sb, frame_main], onPage=draw_bg)
    ])

    styles = getSampleStyleSheet()

    # Sidebar styles
    s_sb_h = ParagraphStyle(
        'SB_H', fontName='Helvetica-Bold', fontSize=12,
        textColor=C_ACCENT, spaceBefore=20, spaceAfter=8, textTransform='uppercase'
    )
    s_sb_t = ParagraphStyle(
        'SB_T', fontName='Helvetica', fontSize=9.5,
        textColor=colors.white, leading=14
    )
    s_sb_l = ParagraphStyle(
        'SB_L', fontName='Helvetica-Bold', fontSize=8,
        textColor=colors.HexColor("#bdc3c7"), spaceBefore=6
    )

    # Main styles
    s_name = ParagraphStyle(
        'Name', fontName='Helvetica-Bold', fontSize=32,
        textColor=C_TEXT, leading=34, spaceAfter=5
    )
    s_role = ParagraphStyle(
        'Role', fontName='Helvetica-Bold', fontSize=14,
        textColor=C_ACCENT, textTransform='uppercase', spaceAfter=20
    )
    s_h2 = ParagraphStyle(
        'H2', fontName='Helvetica-Bold', fontSize=13,
        textColor=C_TEXT, spaceBefore=18, spaceAfter=8, textTransform='uppercase'
    )
    s_body = ParagraphStyle(
        'Body', fontName='Helvetica', fontSize=10.5,
        textColor=colors.HexColor("#34495e"), leading=16, spaceAfter=8
    )

    story = []
    tmp_file_path = None

    # --- 1. SIDEBAR ---
    photo = request.FILES.get('photo')
    if photo:
        try:
            img = Image.open(photo).convert("RGB")
            mask = Image.new('L', (500, 500), 0)
            draw = ImageDraw.Draw(mask)
            draw.ellipse((0, 0, 500, 500), fill=255)
            output = ImageOps.fit(img, (500, 500), centering=(0.5, 0.5))
            output.putalpha(mask)

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
            tmp_file_path = tmp.name
            output.save(tmp, format='PNG')
            tmp.close()
            story.append(RLImage(tmp_file_path, width=50 * mm, height=50 * mm))
            story.append(Spacer(1, 20))
        except Exception as e:
            logger.warning(f"Failed to process profile photo: {e}")

    story.append(Paragraph("CONTACTS", s_sb_h))

    contacts = [
        ("LOCATION", request.POST.get('location')),
        ("EMAIL", request.POST.get('email')),
        ("PHONE", request.POST.get('phone')),
        ("LINKEDIN", request.POST.get('linkedin')),
    ]

    for lbl, val in contacts:
        if val:
            story.append(Paragraph(lbl, s_sb_l))
            story.append(Paragraph(val, s_sb_t))
            story.append(Spacer(1, 2))

    # Skills
    skills_str = request.POST.get('skills_list', '')
    if skills_str:
        story.append(Paragraph("SKILLS", s_sb_h))
        for item in skills_str.split(','):
            parts = item.split('-')
            name = parts[0].strip()
            try:
                lvl = float(parts[1])
            except (IndexError, ValueError):
                lvl = 50
            story.append(ProSkillBar(name, lvl, width=65 * mm))
            story.append(Spacer(1, 10))

    # Languages
    langs = request.POST.get('languages')
    if langs:
        story.append(Paragraph("LANGUAGES", s_sb_h))
        story.append(Paragraph(langs, s_sb_t))

    story.append(FrameBreak())

    # --- 2. MAIN CONTENT ---
    story.append(Paragraph(request.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(request.POST.get('target_role', 'Professional'), s_role))

    line = Drawing(400, 2)
    line.add(Line(0, 0, 130 * mm, 0, strokeColor=colors.HexColor("#ecf0f1"), strokeWidth=2))
    story.append(line)

    # Summary
    about = request.POST.get('about_me')
    if about:
        story.append(Paragraph("PROFILE", s_h2))
        story.append(Paragraph(about, s_body))

    # Experience
    exp = request.POST.get('experience_text')
    if not exp:
        exp = request.POST.get('resume', '')

    if exp:
        story.append(Paragraph("WORK EXPERIENCE", s_h2))
        for text_line in exp.split('\n'):
            text_line = text_line.strip()
            if text_line:
                if len(text_line) < 80 and any(c.isdigit() for c in text_line):
                    story.append(Paragraph(f"<b>{text_line}</b>", s_body))
                else:
                    story.append(Paragraph(text_line, s_body))

    # References
    refs = request.POST.get('references')
    if refs:
        story.append(Paragraph("REFERENCES", s_h2))
        story.append(Paragraph(refs, s_body))

    # ЗАДАЧА 3: try/finally гарантирует удаление временного файла
    # даже если doc.build() выбросит исключение
    try:
        doc.build(story)
    finally:
        if tmp_file_path:
            try:
                os.unlink(tmp_file_path)
            except OSError:
                pass

    buffer.seek(0)
    return FileResponse(buffer, as_attachment=True, filename='CV_Elite.pdf')


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
    profile = request.user.profile
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
    profile = await Profile.objects.aget(user=request.user)
    if not profile.has_generations_left():
        recent_results = await sync_to_async(list)(
            AIResult.objects.filter(user=request.user)[:10]
        )
        return render(request, 'generator/tools.html', {
            'results': recent_results,
            'active_tool': 'interview',
            'tool_error': "You've used all free generations! Upgrade to Pro for unlimited AI tools.",
        })

    resume = request.POST.get('resume', '')
    job_desc = request.POST.get('job_description', '')
    company = request.POST.get('company_name', '')

    system_prompt = """You are a senior technical interviewer and career coach.
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
    return render(request, 'generator/tools.html', {
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
    profile = await Profile.objects.aget(user=request.user)
    if not profile.has_generations_left():
        recent_results = await sync_to_async(list)(
            AIResult.objects.filter(user=request.user)[:10]
        )
        return render(request, 'generator/tools.html', {
            'results': recent_results,
            'active_tool': 'followup',
            'tool_error': "You've used all free generations! Upgrade to Pro for unlimited AI tools.",
        })

    company = request.POST.get('company_name', '')
    job_title = request.POST.get('job_title', '')
    context = request.POST.get('context', '')

    system_prompt = """You are an expert career communication strategist.
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
    return render(request, 'generator/tools.html', {
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
    profile = await Profile.objects.aget(user=request.user)
    if not profile.has_generations_left():
        recent_results = await sync_to_async(list)(
            AIResult.objects.filter(user=request.user)[:10]
        )
        return render(request, 'generator/tools.html', {
            'results': recent_results,
            'active_tool': 'ats',
            'tool_error': "You've used all free generations! Upgrade to Pro for unlimited AI tools.",
        })

    resume = request.POST.get('resume', '')
    job_desc = request.POST.get('job_description', '')

    system_prompt = """You are an ATS (Applicant Tracking System) expert.
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
    return render(request, 'generator/tools.html', {
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