"""
pdf_engine.py — Scalable ReportLab PDF CV Factory
===================================================
To add a new template: append a new dict to TEMPLATES and implement
its builder in _build_<slug>(). The PDF tab carousel in home.html
automatically shows however many templates are in TEMPLATES.

Template dict schema
--------------------
{
    "slug":    str,   # URL-safe identifier
    "name":    str,   # Human-readable name shown in UI
    "preview": str,   # Emoji or short label for the card
    "desc":    str,   # One-line description
}
"""

import io
import os
import logging
import tempfile

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph,
    Spacer, Image as RLImage, FrameBreak, Flowable,
    HRFlowable,
)
from reportlab.graphics.shapes import Drawing, Line
from PIL import Image, ImageOps, ImageDraw

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE REGISTRY  (add new entries here; order = carousel order)
# ─────────────────────────────────────────────────────────────────────────────
TEMPLATES = [
    {
        "slug":    "classic_navy",
        "name":    "Classic Navy",
        "preview": "🔷",
        "desc":    "Navy sidebar, cyan accents — timeless & professional",
    },
    {
        "slug":    "modern_minimalist",
        "name":    "Modern Minimalist",
        "preview": "⬜",
        "desc":    "Clean white layout with subtle gray, zero decoration",
    },
    {
        "slug":    "tech_startup_bold",
        "name":    "Tech Startup Bold",
        "preview": "🟢",
        "desc":    "Dark charcoal + neon green — built for tech roles",
    },
    {
        "slug":    "executive_elegant",
        "name":    "Executive Elegant",
        "preview": "🟡",
        "desc":    "Ivory background, serif fonts, gold rule dividers",
    },
    {
        "slug":    "creative_accent",
        "name":    "Creative Accent",
        "preview": "🎨",
        "desc":    "Coral/teal split header — ideal for design & creative",
    },
]


def get_template_by_slug(slug: str) -> dict:
    """Return template meta dict or fall back to classic_navy."""
    for t in TEMPLATES:
        if t["slug"] == slug:
            return t
    return TEMPLATES[0]


def get_templates_for_plan(pdf_limit: int) -> list:
    """Return the subset of TEMPLATES the user's plan allows."""
    return TEMPLATES[:pdf_limit]


# ─────────────────────────────────────────────────────────────────────────────
# SHARED UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

class ProSkillBar(Flowable):
    """Horizontal skill bar — reusable across all templates."""

    def __init__(self, name, level_percent, width=150, height=14,
                 bar_bg="#34495e", bar_fill="#4cc9f0", text_color="white"):
        Flowable.__init__(self)
        self.name = name
        self.level = level_percent / 100.0
        self.width = width
        self.height = height
        self.bar_bg = bar_bg
        self.bar_fill = bar_fill
        self.text_color = text_color

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
        c.setFillColor(colors.HexColor("#ffffff") if self.text_color == "white"
                       else colors.HexColor(self.text_color))
        c.drawString(0, 7, self.name)

        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor("#bdc3c7"))
        c.drawRightString(self.width, 7, self.txt)

        c.setFillColor(colors.HexColor(self.bar_bg))
        c.roundRect(0, 0, self.width, 4, 2, fill=1, stroke=0)

        c.setFillColor(colors.HexColor(self.bar_fill))
        c.roundRect(0, 0, self.width * self.level, 4, 2, fill=1, stroke=0)


def _process_photo(photo_file) -> str | None:
    """Save uploaded photo as a circular-masked PNG temp file. Returns path."""
    if not photo_file:
        return None
    try:
        img = Image.open(photo_file).convert("RGBA")
        mask = Image.new('L', (500, 500), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, 500, 500), fill=255)
        output = ImageOps.fit(img.convert("RGB"), (500, 500), centering=(0.5, 0.5))
        output.putalpha(mask)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
        path = tmp.name
        output.save(tmp, format='PNG')
        tmp.close()
        return path
    except Exception as e:
        logger.warning("Photo processing failed: %s", e)
        return None


def _parse_skills(skills_str: str):
    """Parse 'Python-80,Django-70' → [(name, level), ...]"""
    result = []
    for item in (skills_str or "").split(','):
        parts = item.strip().split('-')
        name = parts[0].strip()
        if not name:
            continue
        try:
            lvl = float(parts[1])
        except (IndexError, ValueError):
            lvl = 50
        result.append((name, lvl))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 1 — CLASSIC NAVY
# ─────────────────────────────────────────────────────────────────────────────

def _build_classic_navy(req, buf):
    C_SIDE  = colors.HexColor("#2c3e50")
    C_ACC   = colors.HexColor("#4cc9f0")
    C_TEXT  = colors.HexColor("#2c3e50")
    C_GREY  = colors.HexColor("#7f8c8d")

    doc = BaseDocTemplate(buf, pagesize=A4,
                          rightMargin=0, leftMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_SIDE)
        canvas.rect(0, 0, 80 * mm, 297 * mm, fill=1, stroke=0)
        canvas.restoreState()

    frame_sb   = Frame(0, 0, 80*mm, 297*mm, id='sb',
                       leftPadding=20, rightPadding=20, topPadding=30, bottomPadding=30)
    frame_main = Frame(80*mm, 0, 130*mm, 297*mm, id='main',
                       leftPadding=25, rightPadding=25, topPadding=30, bottomPadding=30)
    doc.addPageTemplates([PageTemplate(id='L', frames=[frame_sb, frame_main], onPage=draw_bg)])

    s_sb_h = ParagraphStyle('SH', fontName='Helvetica-Bold', fontSize=12,
                             textColor=C_ACC, spaceBefore=20, spaceAfter=8, textTransform='uppercase')
    s_sb_t = ParagraphStyle('ST', fontName='Helvetica', fontSize=9.5,
                             textColor=colors.white, leading=14)
    s_sb_l = ParagraphStyle('SL', fontName='Helvetica-Bold', fontSize=8,
                             textColor=colors.HexColor("#bdc3c7"), spaceBefore=6)
    s_name  = ParagraphStyle('N', fontName='Helvetica-Bold', fontSize=32,
                              textColor=C_TEXT, leading=34, spaceAfter=5)
    s_role  = ParagraphStyle('R', fontName='Helvetica-Bold', fontSize=14,
                              textColor=C_ACC, textTransform='uppercase', spaceAfter=15)
    s_h2    = ParagraphStyle('H2', fontName='Helvetica-Bold', fontSize=13,
                              textColor=C_TEXT, spaceBefore=18, spaceAfter=8, textTransform='uppercase')
    s_body  = ParagraphStyle('B', fontName='Helvetica', fontSize=10.5,
                              textColor=colors.HexColor("#34495e"), leading=16, spaceAfter=8)
    s_bullet = ParagraphStyle('Bullet', parent=s_body, bulletIndent=5, leftIndent=15, spaceBefore=2, spaceAfter=4)
    s_job_title = ParagraphStyle('JT', parent=s_body, fontName='Helvetica-Bold', fontSize=11.5, spaceBefore=12, spaceAfter=2)
    s_job_meta = ParagraphStyle('JM', parent=s_body, fontName='Helvetica', fontSize=10.5, textColor=colors.HexColor("#444444"), spaceBefore=0, spaceAfter=6)

    story = []
    photo_path = _process_photo(req.FILES.get('photo'))
    if photo_path:
        story.append(RLImage(photo_path, width=50*mm, height=50*mm))
        story.append(Spacer(1, 20))

    contacts_data = [
        ("LOCATION", req.POST.get('location')),
        ("EMAIL",    req.POST.get('email')),
        ("PHONE",    req.POST.get('phone')),
        ("LINKEDIN", req.POST.get('linkedin'))
    ]
    if any(val for lbl, val in contacts_data):
        story.append(Paragraph("CONTACTS", s_sb_h))
        for lbl, val in contacts_data:
            if val:
                story.append(Paragraph(lbl, s_sb_l))
                story.append(Paragraph(val, s_sb_t))
                story.append(Spacer(1, 2))

    skills = _parse_skills(req.POST.get('skills_list', ''))
    if skills:
        story.append(Paragraph("SKILLS", s_sb_h))
        for name, lvl in skills:
            story.append(ProSkillBar(name, lvl, width=65*mm))
            story.append(Spacer(1, 10))

    langs = req.POST.get('languages')
    if langs:
        story.append(Paragraph("LANGUAGES", s_sb_h))
        story.append(Paragraph(langs, s_sb_t))

    story.append(FrameBreak())

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))
    
    line = Drawing(130*mm, 2)
    line.add(Line(0, 0, 130*mm, 0, strokeColor=colors.HexColor("#ecf0f1"), strokeWidth=2))
    story.append(line)
    story.append(Spacer(1, 10))

    if req.POST.get('about_me'):
        story.append(Paragraph("PROFILE", s_h2))
        story.append(Paragraph(req.POST.get('about_me'), s_body))
        story.append(Spacer(1, 5))

    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if exp:
        story.append(Paragraph("WORK EXPERIENCE", s_h2))
        for t in exp.split('\n'):
            t = t.strip()
            if t:
                if t.startswith('-') or t.startswith('*'):
                    # Render as proper bullet point
                    story.append(Paragraph(t[1:].strip(), s_bullet, bulletText='•'))
                elif len(t) < 100 and ('|' in t or any(char.isdigit() for char in t)):
                    # Company & Dates meta string
                    story.append(Paragraph(f'<font color="#444444">{t}</font>', s_job_meta))
                elif len(t) < 100:
                    # Job Title 
                    story.append(Paragraph(f"<b>{t}</b>", s_job_title))
                else:
                    # Regular text paragraph
                    story.append(Paragraph(t, s_body))

    if req.POST.get('references'):
        story.append(Paragraph("REFERENCES", s_h2))
        story.append(Paragraph(req.POST.get('references'), s_body))

    try:
        doc.build(story)
    finally:
        if photo_path:
            try: os.unlink(photo_path)
            except OSError: pass


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 2 — MODERN MINIMALIST
# ─────────────────────────────────────────────────────────────────────────────

def _build_modern_minimalist(req, buf):
    W, H = A4
    C_INK  = colors.HexColor("#1a1a2e")
    C_MID  = colors.HexColor("#4a5568")
    C_ACC  = colors.HexColor("#667eea")
    C_LINE = colors.HexColor("#e2e8f0")

    doc = BaseDocTemplate(buf, pagesize=A4,
                          rightMargin=20*mm, leftMargin=20*mm,
                          topMargin=20*mm, bottomMargin=20*mm)

    # single full-width frame
    frame = Frame(20*mm, 20*mm, W - 40*mm, H - 40*mm, id='main',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='L', frames=[frame])])

    s_name = ParagraphStyle('N', fontName='Helvetica-Bold', fontSize=36,
                             textColor=C_INK, leading=40, spaceAfter=4)
    s_role = ParagraphStyle('R', fontName='Helvetica', fontSize=13,
                             textColor=C_ACC, spaceAfter=10)
    s_h2   = ParagraphStyle('H2', fontName='Helvetica-Bold', fontSize=10,
                             textColor=C_ACC, spaceBefore=16, spaceAfter=6,
                             textTransform='uppercase', letterSpacing=2)
    s_body = ParagraphStyle('B', fontName='Helvetica', fontSize=10,
                             textColor=C_MID, leading=15, spaceAfter=6)
    s_info = ParagraphStyle('I', fontName='Helvetica', fontSize=9,
                             textColor=C_MID, leading=13)

    story = []
    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))

    contacts = " · ".join(filter(None, [req.POST.get('email'), req.POST.get('phone'),
                                         req.POST.get('location'), req.POST.get('linkedin')]))
    if contacts:
        story.append(Paragraph(contacts, s_info))
    story.append(HRFlowable(width="100%", thickness=1, color=C_LINE, spaceAfter=12, spaceBefore=6))

    if req.POST.get('about_me'):
        story.append(Paragraph("SUMMARY", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE, spaceAfter=6))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE, spaceAfter=6))
        for t in exp.split('\n'):
            t = t.strip()
            if t:
                story.append(Paragraph(f"<b>{t}</b>" if len(t) < 80 and any(c.isdigit() for c in t) else t, s_body))

    skills = _parse_skills(req.POST.get('skills_list', ''))
    if skills:
        story.append(Paragraph("SKILLS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE, spaceAfter=6))
        skill_text = "  ·  ".join([f"<b>{n}</b>" for n, _ in skills])
        story.append(Paragraph(skill_text, s_body))

    if req.POST.get('languages'):
        story.append(Paragraph("LANGUAGES", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE, spaceAfter=6))
        story.append(Paragraph(req.POST.get('languages'), s_body))

    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 3 — TECH STARTUP BOLD
# ─────────────────────────────────────────────────────────────────────────────

def _build_tech_startup_bold(req, buf):
    W, H = A4
    C_BG    = colors.HexColor("#0d1117")
    C_CARD  = colors.HexColor("#161b22")
    C_GREEN = colors.HexColor("#39d353")
    C_PUR   = colors.HexColor("#a855f7")
    C_TEXT  = colors.HexColor("#c9d1d9")
    C_MUTED = colors.HexColor("#8b949e")

    doc = BaseDocTemplate(buf, pagesize=A4,
                          rightMargin=0, leftMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W, H, fill=1, stroke=0)
        # top accent bar
        canvas.setFillColor(C_GREEN)
        canvas.rect(0, H - 4, W, 4, fill=1, stroke=0)
        canvas.restoreState()

    frame = Frame(15*mm, 15*mm, W - 30*mm, H - 30*mm, id='main',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='L', frames=[frame], onPage=draw_bg)])

    s_name = ParagraphStyle('N', fontName='Helvetica-Bold', fontSize=34,
                             textColor=C_GREEN, leading=36, spaceAfter=4)
    s_role = ParagraphStyle('R', fontName='Helvetica', fontSize=13,
                             textColor=C_PUR, spaceAfter=14)
    s_h2   = ParagraphStyle('H2', fontName='Helvetica-Bold', fontSize=10,
                             textColor=C_GREEN, spaceBefore=18, spaceAfter=6,
                             textTransform='uppercase', letterSpacing=2)
    s_body = ParagraphStyle('B', fontName='Helvetica', fontSize=10,
                             textColor=C_TEXT, leading=15, spaceAfter=5)
    s_info = ParagraphStyle('I', fontName='Helvetica', fontSize=9, textColor=C_MUTED, leading=13)

    story = []
    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Developer'), s_role))

    contacts = " | ".join(filter(None, [req.POST.get('email'), req.POST.get('phone'),
                                         req.POST.get('location'), req.POST.get('linkedin')]))
    if contacts:
        story.append(Paragraph(contacts, s_info))
    story.append(HRFlowable(width="100%", thickness=1, color=C_GREEN, spaceAfter=10, spaceBefore=8))

    if req.POST.get('about_me'):
        story.append(Paragraph("// ABOUT", s_h2))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if exp:
        story.append(Paragraph("// EXPERIENCE", s_h2))
        for t in exp.split('\n'):
            t = t.strip()
            if t:
                story.append(Paragraph(f"<b>{t}</b>" if len(t) < 80 and any(c.isdigit() for c in t) else t, s_body))

    skills = _parse_skills(req.POST.get('skills_list', ''))
    if skills:
        story.append(Paragraph("// TECH STACK", s_h2))
        for name, lvl in skills:
            story.append(ProSkillBar(name, lvl, width=150*mm,
                                     bar_bg="#21262d", bar_fill="#39d353", text_color="#c9d1d9"))
            story.append(Spacer(1, 8))

    if req.POST.get('languages'):
        story.append(Paragraph("// LANGUAGES", s_h2))
        story.append(Paragraph(req.POST.get('languages'), s_body))

    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 4 — EXECUTIVE ELEGANT
# ─────────────────────────────────────────────────────────────────────────────

def _build_executive_elegant(req, buf):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    W, H = A4
    C_BG   = colors.HexColor("#fafaf7")
    C_INK  = colors.HexColor("#1c1917")
    C_GOLD = colors.HexColor("#b45309")
    C_MID  = colors.HexColor("#57534e")
    C_LINE = colors.HexColor("#d4a853")

    doc = BaseDocTemplate(buf, pagesize=A4,
                          rightMargin=22*mm, leftMargin=22*mm,
                          topMargin=22*mm, bottomMargin=22*mm)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W, H, fill=1, stroke=0)
        canvas.restoreState()

    frame = Frame(22*mm, 22*mm, W - 44*mm, H - 44*mm, id='main',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='L', frames=[frame], onPage=draw_bg)])

    s_name = ParagraphStyle('N', fontName='Times-Bold', fontSize=32,
                             textColor=C_INK, leading=36, spaceAfter=4, alignment=1)
    s_role = ParagraphStyle('R', fontName='Times-Italic', fontSize=13,
                             textColor=C_GOLD, spaceAfter=10, alignment=1)
    s_h2   = ParagraphStyle('H2', fontName='Times-Bold', fontSize=10,
                             textColor=C_GOLD, spaceBefore=18, spaceAfter=6,
                             textTransform='uppercase', letterSpacing=1.5, alignment=1)
    s_body = ParagraphStyle('B', fontName='Times-Roman', fontSize=10.5,
                             textColor=C_MID, leading=16, spaceAfter=6)
    s_info = ParagraphStyle('I', fontName='Times-Roman', fontSize=9,
                             textColor=C_MID, leading=13, alignment=1)

    story = []
    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Executive'), s_role))

    contacts = "  ·  ".join(filter(None, [req.POST.get('email'), req.POST.get('phone'),
                                           req.POST.get('location'), req.POST.get('linkedin')]))
    if contacts:
        story.append(Paragraph(contacts, s_info))

    story.append(HRFlowable(width="100%", thickness=1.5, color=C_LINE, spaceAfter=10, spaceBefore=8))

    if req.POST.get('about_me'):
        story.append(Paragraph("Executive Summary", s_h2))
        story.append(HRFlowable(width="60%", thickness=0.5, color=C_LINE, spaceAfter=8, hAlign='CENTER'))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if exp:
        story.append(Paragraph("Professional Experience", s_h2))
        story.append(HRFlowable(width="60%", thickness=0.5, color=C_LINE, spaceAfter=8, hAlign='CENTER'))
        for t in exp.split('\n'):
            t = t.strip()
            if t:
                story.append(Paragraph(f"<b>{t}</b>" if len(t) < 80 and any(c.isdigit() for c in t) else t, s_body))

    skills = _parse_skills(req.POST.get('skills_list', ''))
    if skills:
        story.append(Paragraph("Core Competencies", s_h2))
        story.append(HRFlowable(width="60%", thickness=0.5, color=C_LINE, spaceAfter=8, hAlign='CENTER'))
        skill_text = "   ·   ".join([f"<b>{n}</b>" for n, _ in skills])
        story.append(Paragraph(skill_text, s_body))

    if req.POST.get('languages'):
        story.append(Paragraph("Languages", s_h2))
        story.append(HRFlowable(width="60%", thickness=0.5, color=C_LINE, spaceAfter=8, hAlign='CENTER'))
        story.append(Paragraph(req.POST.get('languages'), s_body))

    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE 5 — CREATIVE ACCENT
# ─────────────────────────────────────────────────────────────────────────────

def _build_creative_accent(req, buf):
    W, H = A4
    C_CORAL = colors.HexColor("#ff6b6b")
    C_TEAL  = colors.HexColor("#4ecdc4")
    C_DARK  = colors.HexColor("#1a1a2e")
    C_BODY  = colors.HexColor("#2d3748")
    C_LIGHT = colors.HexColor("#f7fafc")
    C_MID   = colors.HexColor("#718096")

    doc = BaseDocTemplate(buf, pagesize=A4,
                          rightMargin=0, leftMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_LIGHT)
        canvas.rect(0, 0, W, H, fill=1, stroke=0)
        # Top header band
        canvas.setFillColor(C_DARK)
        canvas.rect(0, H - 70*mm, W, 70*mm, fill=1, stroke=0)
        # Coral left stripe inside header
        canvas.setFillColor(C_CORAL)
        canvas.rect(0, H - 70*mm, 8*mm, 70*mm, fill=1, stroke=0)
        # Teal bottom line
        canvas.setFillColor(C_TEAL)
        canvas.rect(0, 0, W, 4, fill=1, stroke=0)
        canvas.restoreState()

    frame_header = Frame(10*mm, H - 68*mm, W - 20*mm, 64*mm, id='hdr',
                         leftPadding=20, rightPadding=10, topPadding=10, bottomPadding=10)
    frame_body   = Frame(15*mm, 10*mm, W - 30*mm, H - 85*mm, id='body',
                         leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='L',
                                       frames=[frame_header, frame_body],
                                       onPage=draw_bg)])

    s_name = ParagraphStyle('N', fontName='Helvetica-Bold', fontSize=30,
                             textColor=colors.white, leading=34, spaceAfter=4)
    s_role = ParagraphStyle('R', fontName='Helvetica', fontSize=13,
                             textColor=C_TEAL, spaceAfter=6)
    s_info = ParagraphStyle('I', fontName='Helvetica', fontSize=9,
                             textColor=colors.HexColor("#a0aec0"), leading=13)
    s_h2   = ParagraphStyle('H2', fontName='Helvetica-Bold', fontSize=10,
                             textColor=C_CORAL, spaceBefore=14, spaceAfter=6,
                             textTransform='uppercase', letterSpacing=1.5)
    s_body = ParagraphStyle('B', fontName='Helvetica', fontSize=10.5,
                             textColor=C_BODY, leading=16, spaceAfter=6)

    story = []
    # Header frame
    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Creative Professional'), s_role))
    contacts = "  |  ".join(filter(None, [req.POST.get('email'), req.POST.get('phone'),
                                           req.POST.get('location'), req.POST.get('linkedin')]))
    if contacts:
        story.append(Paragraph(contacts, s_info))

    story.append(FrameBreak())

    # Body frame
    if req.POST.get('about_me'):
        story.append(Paragraph("About Me", s_h2))
        story.append(HRFlowable(width="100%", thickness=1, color=C_TEAL, spaceAfter=8))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if exp:
        story.append(Paragraph("Experience", s_h2))
        story.append(HRFlowable(width="100%", thickness=1, color=C_TEAL, spaceAfter=8))
        for t in exp.split('\n'):
            t = t.strip()
            if t:
                story.append(Paragraph(f"<b>{t}</b>" if len(t) < 80 and any(c.isdigit() for c in t) else t, s_body))

    skills = _parse_skills(req.POST.get('skills_list', ''))
    if skills:
        story.append(Paragraph("Skills", s_h2))
        story.append(HRFlowable(width="100%", thickness=1, color=C_TEAL, spaceAfter=8))
        for name, lvl in skills:
            story.append(ProSkillBar(name, lvl, width=150*mm,
                                     bar_bg="#e2e8f0", bar_fill="#ff6b6b", text_color="#2d3748"))
            story.append(Spacer(1, 8))

    if req.POST.get('languages'):
        story.append(Paragraph("Languages", s_h2))
        story.append(HRFlowable(width="100%", thickness=1, color=C_TEAL, spaceAfter=8))
        story.append(Paragraph(req.POST.get('languages'), s_body))

    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

_BUILDERS = {
    "classic_navy":       _build_classic_navy,
    "modern_minimalist":  _build_modern_minimalist,
    "tech_startup_bold":  _build_tech_startup_bold,
    "executive_elegant":  _build_executive_elegant,
    "creative_accent":    _build_creative_accent,
}


def build_pdf(template_slug: str, request) -> io.BytesIO:
    """
    Build and return a BytesIO PDF for the given template slug.
    Falls back to classic_navy if the slug is unrecognised.
    """
    buf = io.BytesIO()
    builder = _BUILDERS.get(template_slug, _build_classic_navy)
    builder(request, buf)
    buf.seek(0)
    return buf
