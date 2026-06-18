"""
pdf_engine.py — Scalable ReportLab PDF CV Factory  (v2.0)
==========================================================
Architecture
------------
4 reusable BASE LAYOUT BUILDERS:
  _build_sidebar_layout       → navy sidebar + main column
  _build_minimal_layout       → full-width single-column, clean
  _build_split_header_layout  → coloured header band + body
  _build_modern_columns       → dark full-bleed + top accent bar

20 TEMPLATE CONFIGS (5 categories × 4 templates each) each contains:
  slug          str   URL-safe identifier
  name          str   Human label shown in UI
  image_url     str   /static/img/templates/<slug>.jpg
  desc          str   One-line description
  category      str   classic | minimalist | tech | creative | executive
  layout        str   sidebar | minimal | split_header | modern_columns
  primary_color str   Hex accent colour
  bg_color      str   Page/panel background
  accent_color  str   Secondary accent
  font_family   str   'Helvetica' | 'Times-Roman' | 'Courier'
  dark_sidebar  bool  (sidebar only) paint sidebar dark?

New fields supported by all builders:
  education       str   free-text education block
  certifications  str   comma/newline separated certs
  portfolio_url   str   Portfolio / GitHub URL
"""

import io
import os
import logging
import tempfile
import contextlib

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

W_A4, H_A4 = A4   # 595.27 × 841.89 pts


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE REGISTRY  — 20 templates across 5 categories
# ─────────────────────────────────────────────────────────────────────────────
TEMPLATES = [

    # ── CLASSIC (sidebar layout) ─────────────────────────────────────────────
    {
        "slug":          "classic_navy",
        "name":          "Classic Navy",
        "image_url":     "/static/img/templates/classic_navy.jpg",
        "desc":          "Navy sidebar, cyan accents — timeless & professional",
        "category":      "classic",
        "layout":        "sidebar",
        "primary_color": "#4cc9f0",
        "bg_color":      "#2c3e50",
        "accent_color":  "#ecf0f1",
        "font_family":   "Helvetica",
        "dark_sidebar":  True,
    },
    {
        "slug":          "classic_burgundy",
        "name":          "Classic Burgundy",
        "image_url":     "/static/img/templates/classic_burgundy.jpg",
        "desc":          "Deep burgundy sidebar, warm ivory tones — authoritative",
        "category":      "classic",
        "layout":        "sidebar",
        "primary_color": "#f7c59f",
        "bg_color":      "#6d2b3d",
        "accent_color":  "#fdf6ec",
        "font_family":   "Times-Roman",
        "dark_sidebar":  True,
    },
    {
        "slug":          "classic_forest",
        "name":          "Classic Forest",
        "image_url":     "/static/img/templates/classic_forest.jpg",
        "desc":          "Forest green sidebar with warm sand accents",
        "category":      "classic",
        "layout":        "sidebar",
        "primary_color": "#a8d5ba",
        "bg_color":      "#2d6a4f",
        "accent_color":  "#fefae0",
        "font_family":   "Helvetica",
        "dark_sidebar":  True,
    },
    {
        "slug":          "classic_slate",
        "name":          "Classic Slate",
        "image_url":     "/static/img/templates/classic_slate.jpg",
        "desc":          "Steel-slate sidebar, light lavender accents — balanced",
        "category":      "classic",
        "layout":        "sidebar",
        "primary_color": "#b8c5d6",
        "bg_color":      "#4a5568",
        "accent_color":  "#edf2f7",
        "font_family":   "Helvetica",
        "dark_sidebar":  True,
    },

    # ── MINIMALIST (minimal layout) ───────────────────────────────────────────
    {
        "slug":          "minimalist_white",
        "name":          "Pure White",
        "image_url":     "/static/img/templates/minimalist_white.jpg",
        "desc":          "Ultra-clean white, zero decoration — ATS optimised",
        "category":      "minimalist",
        "layout":        "minimal",
        "primary_color": "#667eea",
        "bg_color":      "#ffffff",
        "accent_color":  "#e2e8f0",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },
    {
        "slug":          "minimalist_onyx",
        "name":          "Onyx Dark",
        "image_url":     "/static/img/templates/minimalist_onyx.jpg",
        "desc":          "Near-black background, white text — bold minimalism",
        "category":      "minimalist",
        "layout":        "minimal",
        "primary_color": "#a78bfa",
        "bg_color":      "#1a1a2e",
        "accent_color":  "#e2e8f0",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },
    {
        "slug":          "minimalist_soft_gray",
        "name":          "Soft Gray",
        "image_url":     "/static/img/templates/minimalist_soft_gray.jpg",
        "desc":          "Warm light gray, clean Inter layout — calm & readable",
        "category":      "minimalist",
        "layout":        "minimal",
        "primary_color": "#4a5568",
        "bg_color":      "#f7f8fc",
        "accent_color":  "#cbd5e0",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },
    {
        "slug":          "minimalist_ice_blue",
        "name":          "Ice Blue",
        "image_url":     "/static/img/templates/minimalist_ice_blue.jpg",
        "desc":          "Frosty light blue, cool & modern single-column",
        "category":      "minimalist",
        "layout":        "minimal",
        "primary_color": "#0ea5e9",
        "bg_color":      "#f0f9ff",
        "accent_color":  "#bae6fd",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },

    # ── TECH / STARTUP (modern_columns layout) ───────────────────────────────
    {
        "slug":          "tech_dark_neon",
        "name":          "Dark Neon",
        "image_url":     "/static/img/templates/tech_dark_neon.jpg",
        "desc":          "Dark charcoal + neon green — built for tech roles",
        "category":      "tech",
        "layout":        "modern_columns",
        "primary_color": "#39d353",
        "bg_color":      "#0d1117",
        "accent_color":  "#a855f7",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },
    {
        "slug":          "tech_hacker_terminal",
        "name":          "Hacker Terminal",
        "image_url":     "/static/img/templates/tech_hacker_terminal.jpg",
        "desc":          "Matrix-green on pitch-black, monospace — hacker aesthetic",
        "category":      "tech",
        "layout":        "modern_columns",
        "primary_color": "#00ff41",
        "bg_color":      "#000000",
        "accent_color":  "#008f11",
        "font_family":   "Courier",
        "dark_sidebar":  False,
    },
    {
        "slug":          "tech_fintech_blue",
        "name":          "Fintech Blue",
        "image_url":     "/static/img/templates/tech_fintech_blue.jpg",
        "desc":          "Deep navy + electric blue — finance and banking ready",
        "category":      "tech",
        "layout":        "modern_columns",
        "primary_color": "#3b82f6",
        "bg_color":      "#0f172a",
        "accent_color":  "#06b6d4",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },
    {
        "slug":          "tech_cyber_purple",
        "name":          "Cyber Purple",
        "image_url":     "/static/img/templates/tech_cyber_purple.jpg",
        "desc":          "Deep space purple + magenta flares — cutting-edge",
        "category":      "tech",
        "layout":        "modern_columns",
        "primary_color": "#e879f9",
        "bg_color":      "#13001e",
        "accent_color":  "#818cf8",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },

    # ── CREATIVE (split_header layout) ───────────────────────────────────────
    {
        "slug":          "creative_coral",
        "name":          "Coral Accent",
        "image_url":     "/static/img/templates/creative_coral.jpg",
        "desc":          "Coral/teal split header — ideal for design & creative",
        "category":      "creative",
        "layout":        "split_header",
        "primary_color": "#ff6b6b",
        "bg_color":      "#1a1a2e",
        "accent_color":  "#4ecdc4",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },
    {
        "slug":          "creative_sunset",
        "name":          "Sunset Orange",
        "image_url":     "/static/img/templates/creative_sunset.jpg",
        "desc":          "Warm sunset gradient header — vibrant and energetic",
        "category":      "creative",
        "layout":        "split_header",
        "primary_color": "#f97316",
        "bg_color":      "#1c1410",
        "accent_color":  "#fbbf24",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },
    {
        "slug":          "creative_mint",
        "name":          "Mint Crisp",
        "image_url":     "/static/img/templates/creative_mint.jpg",
        "desc":          "Fresh mint header on white — clean and creative",
        "category":      "creative",
        "layout":        "split_header",
        "primary_color": "#06d6a0",
        "bg_color":      "#073b4c",
        "accent_color":  "#80ffdb",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },
    {
        "slug":          "creative_pastel_pink",
        "name":          "Pastel Pink",
        "image_url":     "/static/img/templates/creative_pastel_pink.jpg",
        "desc":          "Blush pink header, soft lavender body — editorial style",
        "category":      "creative",
        "layout":        "split_header",
        "primary_color": "#f9a8d4",
        "bg_color":      "#831843",
        "accent_color":  "#c084fc",
        "font_family":   "Helvetica",
        "dark_sidebar":  False,
    },

    # ── EXECUTIVE (minimal layout with serif/gold) ────────────────────────────
    {
        "slug":          "executive_gold",
        "name":          "Gold Standard",
        "image_url":     "/static/img/templates/executive_gold.jpg",
        "desc":          "Ivory background, serif fonts, gold rule dividers",
        "category":      "executive",
        "layout":        "minimal",
        "primary_color": "#b45309",
        "bg_color":      "#fafaf7",
        "accent_color":  "#d4a853",
        "font_family":   "Times-Roman",
        "dark_sidebar":  False,
    },
    {
        "slug":          "executive_silver",
        "name":          "Silver Serif",
        "image_url":     "/static/img/templates/executive_silver.jpg",
        "desc":          "Cool silver on off-white — understated executive gravitas",
        "category":      "executive",
        "layout":        "minimal",
        "primary_color": "#94a3b8",
        "bg_color":      "#f8fafc",
        "accent_color":  "#64748b",
        "font_family":   "Times-Roman",
        "dark_sidebar":  False,
    },
    {
        "slug":          "executive_bronze",
        "name":          "Bronze Minimal",
        "image_url":     "/static/img/templates/executive_bronze.jpg",
        "desc":          "Warm bronze accents on cream — refined and minimal",
        "category":      "executive",
        "layout":        "minimal",
        "primary_color": "#92400e",
        "bg_color":      "#fffbf5",
        "accent_color":  "#c2956c",
        "font_family":   "Times-Roman",
        "dark_sidebar":  False,
    },
    {
        "slug":          "executive_platinum",
        "name":          "Platinum",
        "image_url":     "/static/img/templates/executive_platinum.jpg",
        "desc":          "Ultra-premium platinum grey with deep charcoal text",
        "category":      "executive",
        "layout":        "minimal",
        "primary_color": "#9ca3af",
        "bg_color":      "#f9fafb",
        "accent_color":  "#374151",
        "font_family":   "Times-Roman",
        "dark_sidebar":  False,
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
    """Horizontal skill bar — reusable across all templates.

    Layout (bottom-up, all coords in points):
      0–5   : bar track
      5–9   : gap between bar and label
      9–19  : skill name + level badge row
    Total height = 22 pts (self.height).
    """

    def __init__(self, name, level_percent, width=150, height=22,
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
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(colors.HexColor("#ffffff") if self.text_color == "white"
                       else colors.HexColor(self.text_color))
        c.drawString(0, 12, self.name)
        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.HexColor("#bdc3c7"))
        c.drawRightString(self.width, 12, self.txt)
        c.setFillColor(colors.HexColor(self.bar_bg))
        c.roundRect(0, 2, self.width, 5, 2, fill=1, stroke=0)
        c.setFillColor(colors.HexColor(self.bar_fill))
        c.roundRect(0, 2, self.width * self.level, 5, 2, fill=1, stroke=0)


@contextlib.contextmanager
def _process_photo(photo_file):
    """
    Context manager: process the uploaded photo into a circular-masked PNG
    temp file, yield its path, then ALWAYS delete it — even if the PDF
    builder raises an exception mid-way.

    Usage::

        with _process_photo(req.FILES.get('photo')) as photo_path:
            if photo_path:
                story.append(RLImage(photo_path, ...))
    """
    if not photo_file:
        yield None
        return

    path = None
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
        yield path
    except Exception as exc:
        logger.warning("Photo processing failed: %s", exc)
        yield None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


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


def _safe_hex(color_str: str) -> colors.Color:
    """Return a ReportLab HexColor, falling back to black on bad input."""
    try:
        return colors.HexColor(color_str)
    except Exception:
        return colors.black


def _render_experience(story, req, s_job_title, s_job_meta, s_bullet, s_body):
    """Shared experience renderer used by all builders."""
    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if not exp:
        return
    for t in exp.split('\n'):
        t = t.strip()
        if not t:
            continue
        if t.startswith('-') or t.startswith('*'):
            story.append(Paragraph(t[1:].strip(), s_bullet, bulletText='•'))
        elif len(t) < 100 and ('|' in t or any(char.isdigit() for char in t)):
            story.append(Paragraph(f'<font color="#888888">{t}</font>', s_job_meta))
        elif len(t) < 100:
            story.append(Paragraph(f"<b>{t}</b>", s_job_title))
        else:
            story.append(Paragraph(t, s_body))


def _render_education(story, req, s_h2, s_body, s_job_title=None, s_job_meta=None,
                      hr_color=None, hr_width="100%", centered=False):
    """Render the Education section if the field is filled."""
    edu = req.POST.get('education', '').strip()
    if not edu:
        return
    story.append(Paragraph("EDUCATION", s_h2))
    if hr_color:
        kw = {"hAlign": "CENTER"} if centered else {}
        story.append(HRFlowable(width=hr_width, thickness=0.5,
                                color=hr_color, spaceAfter=6, **kw))
    for line in edu.split('\n'):
        line = line.strip()
        if line:
            style = s_job_title if (s_job_title and len(line) < 80) else s_body
            story.append(Paragraph(line, style))


def _render_certifications(story, req, s_h2, s_body,
                            hr_color=None, hr_width="100%", centered=False):
    """Render the Certifications section if the field is filled."""
    certs = req.POST.get('certifications', '').strip()
    if not certs:
        return
    story.append(Paragraph("CERTIFICATIONS", s_h2))
    if hr_color:
        kw = {"hAlign": "CENTER"} if centered else {}
        story.append(HRFlowable(width=hr_width, thickness=0.5,
                                color=hr_color, spaceAfter=6, **kw))
    for line in certs.replace(',', '\n').split('\n'):
        line = line.strip()
        if line:
            story.append(Paragraph(f"• {line}", s_body))


def _render_portfolio(story, req, s_body, s_h2=None,
                      hr_color=None, hr_width="100%"):
    """Render Portfolio / GitHub URL if provided."""
    url = req.POST.get('portfolio_url', '').strip()
    if not url:
        return
    if s_h2:
        story.append(Paragraph("PORTFOLIO / GITHUB", s_h2))
        if hr_color:
            story.append(HRFlowable(width=hr_width, thickness=0.5,
                                    color=hr_color, spaceAfter=6))
    story.append(Paragraph(f'<link href="{url}">{url}</link>', s_body))


# ─────────────────────────────────────────────────────────────────────────────
# BASE LAYOUT BUILDER 1 — SIDEBAR
# Two-column: coloured sidebar (left) + white/light main (right).
# Used by: classic_navy, classic_burgundy, classic_forest, classic_slate
# ─────────────────────────────────────────────────────────────────────────────

def _build_sidebar_layout(req, buf, cfg: dict):
    # POST overrides take priority over template defaults — allow live customisation
    _pc   = req.POST.get('primary_color') or cfg['primary_color']
    _bg   = req.POST.get('bg_color')      or cfg['bg_color']
    _acc  = req.POST.get('accent_color')  or cfg.get('accent_color', '#ecf0f1')
    _font = req.POST.get('font_family')   or cfg.get('font_family', 'Helvetica')

    C_SIDE    = _safe_hex(_bg)
    C_ACC     = _safe_hex(_pc)
    C_TEXT    = colors.HexColor("#2c3e50")
    C_MAIN_BG = _safe_hex(_acc)
    font      = _font
    font_b    = "Times-Bold" if "Times" in font else "Helvetica-Bold"

    doc = BaseDocTemplate(buf, pagesize=A4,
                          rightMargin=0, leftMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_SIDE)
        canvas.rect(0, 0, 80 * mm, 297 * mm, fill=1, stroke=0)
        canvas.restoreState()

    frame_sb   = Frame(0, 0, 80*mm, 297*mm, id='sb',
                       leftPadding=20, rightPadding=20,
                       topPadding=int(40*mm), bottomPadding=20)
    frame_main = Frame(80*mm, 0, 130*mm, 297*mm, id='main',
                       leftPadding=25, rightPadding=25,
                       topPadding=30, bottomPadding=30)
    doc.addPageTemplates([PageTemplate(id='L',
                                       frames=[frame_sb, frame_main],
                                       onPage=draw_bg)])

    s_sb_h = ParagraphStyle('SH', fontName=font_b, fontSize=10,
                             textColor=C_ACC, spaceBefore=22, spaceAfter=8,
                             textTransform='uppercase', letterSpacing=1)
    s_sb_t = ParagraphStyle('ST', fontName=font, fontSize=9.5,
                             textColor=colors.white, leading=14)
    s_sb_l = ParagraphStyle('SL', fontName=font_b, fontSize=7.5,
                             textColor=colors.HexColor("#bdc3c7"),
                             spaceBefore=8, spaceAfter=1)
    s_name  = ParagraphStyle('N', fontName=font_b, fontSize=30,
                              textColor=C_TEXT, leading=32, spaceAfter=4)
    s_role  = ParagraphStyle('R', fontName=font_b, fontSize=13,
                              textColor=C_ACC, textTransform='uppercase',
                              spaceAfter=14)
    s_h2    = ParagraphStyle('H2', fontName=font_b, fontSize=12,
                              textColor=C_TEXT, spaceBefore=22, spaceAfter=10,
                              textTransform='uppercase')
    s_body  = ParagraphStyle('B', fontName=font, fontSize=10,
                              textColor=colors.HexColor("#34495e"),
                              leading=16, spaceAfter=6)
    s_bullet = ParagraphStyle('Bul', parent=s_body,
                               leftIndent=16, bulletIndent=6,
                               spaceBefore=2, spaceAfter=4)
    s_job_title = ParagraphStyle('JT', parent=s_body,
                                  fontName=font_b, fontSize=11,
                                  spaceBefore=8, spaceAfter=2)
    s_job_meta  = ParagraphStyle('JM', parent=s_body, fontSize=9.5,
                                  textColor=colors.HexColor("#555555"),
                                  spaceBefore=0, spaceAfter=6)

    story = []

    # ── Photo ──────────────────────────────────────────────────────────────
    with _process_photo(req.FILES.get('photo')) as photo_path:
        if photo_path:
            story.append(RLImage(photo_path, width=50*mm, height=50*mm))
            story.append(Spacer(1, 16))

    # ── Sidebar: Contacts ──────────────────────────────────────────────────
    contacts_data = [
        ("LOCATION", req.POST.get('location')),
        ("EMAIL",    req.POST.get('email')),
        ("PHONE",    req.POST.get('phone')),
        ("LINKEDIN", req.POST.get('linkedin')),
    ]
    if any(v for _, v in contacts_data):
        story.append(Paragraph("CONTACTS", s_sb_h))
        for lbl, val in contacts_data:
            if val:
                story.append(Paragraph(lbl, s_sb_l))
                story.append(Paragraph(val, s_sb_t))
                story.append(Spacer(1, 3))

    # ── Sidebar: Skills ────────────────────────────────────────────────────
    skills = _parse_skills(req.POST.get('skills_list', ''))
    if skills:
        story.append(Paragraph("SKILLS", s_sb_h))
        for name, lvl in skills:
            story.append(ProSkillBar(name, lvl, width=int(58*mm),
                                     bar_bg="#34495e",
                                     bar_fill=_pc,
                                     text_color="white"))
            story.append(Spacer(1, 6))

    if req.POST.get('languages'):
        story.append(Paragraph("LANGUAGES", s_sb_h))
        story.append(Paragraph(req.POST.get('languages'), s_sb_t))

    # ── Sidebar: Portfolio ─────────────────────────────────────────────────
    port = req.POST.get('portfolio_url', '').strip()
    if port:
        story.append(Paragraph("PORTFOLIO", s_sb_h))
        story.append(Paragraph(port, s_sb_t))

    story.append(FrameBreak())

    # ── Main: Header ───────────────────────────────────────────────────────
    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))

    _divider = Drawing(130*mm, 2)
    _divider.add(Line(0, 0, 130*mm, 0,
                      strokeColor=_safe_hex(_acc), strokeWidth=1.5))
    story.append(_divider)
    story.append(Spacer(1, 10))

    if req.POST.get('about_me'):
        story.append(Paragraph("PROFILE", s_h2))
        story.append(Paragraph(req.POST.get('about_me'), s_body))
        story.append(Spacer(1, 4))

    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if exp:
        story.append(Paragraph("WORK EXPERIENCE", s_h2))
        _render_experience(story, req, s_job_title, s_job_meta, s_bullet, s_body)

    _render_education(story, req, s_h2, s_body, s_job_title, s_job_meta)
    _render_certifications(story, req, s_h2, s_body)

    if req.POST.get('references'):
        story.append(Paragraph("REFERENCES", s_h2))
        story.append(Paragraph(req.POST.get('references'), s_body))

    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# BASE LAYOUT BUILDER 2 — MINIMAL
# Full-width single-column with optional coloured background.
# Used by: minimalist_*, executive_*
# ─────────────────────────────────────────────────────────────────────────────

def _build_minimal_layout(req, buf, cfg: dict):
    W, H = A4
    # POST overrides take priority — allow live customisation from the Studio UI
    _pc   = req.POST.get('primary_color') or cfg['primary_color']
    _bg   = req.POST.get('bg_color')      or cfg['bg_color']
    _acc  = req.POST.get('accent_color')  or cfg.get('accent_color', '#e2e8f0')
    _font = req.POST.get('font_family')   or cfg.get('font_family', 'Helvetica')

    C_BG   = _safe_hex(_bg)
    C_ACC  = _safe_hex(_pc)
    C_LINE = _safe_hex(_acc)

    # Determine ink color from bg (dark bg → white text)
    dark_bg = _bg.lower() in (
        "#1a1a2e", "#000000", "#0d1117", "#0f172a", "#13001e"
    )
    C_INK  = colors.white if dark_bg else colors.HexColor("#1a1a2e")
    C_MID  = colors.HexColor("#aaaaaa") if dark_bg else colors.HexColor("#4a5568")

    font   = _font
    font_b = "Times-Bold" if "Times" in font else "Helvetica-Bold"
    font_i = "Times-Italic" if "Times" in font else "Helvetica-Oblique"

    doc = BaseDocTemplate(buf, pagesize=A4,
                          rightMargin=20*mm, leftMargin=20*mm,
                          topMargin=20*mm, bottomMargin=20*mm)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W, H, fill=1, stroke=0)
        canvas.restoreState()

    frame = Frame(20*mm, 20*mm, W - 40*mm, H - 40*mm, id='main',
                  leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='L', frames=[frame], onPage=draw_bg)])

    s_name = ParagraphStyle('N', fontName=font_b, fontSize=34,
                             textColor=C_INK, leading=38, spaceAfter=4)
    s_role = ParagraphStyle('R', fontName=font_i, fontSize=13,
                             textColor=C_ACC, spaceAfter=8)
    s_h2   = ParagraphStyle('H2', fontName=font_b, fontSize=10,
                             textColor=C_ACC, spaceBefore=18, spaceAfter=6,
                             textTransform='uppercase', letterSpacing=2)
    s_body = ParagraphStyle('B', fontName=font, fontSize=10,
                             textColor=C_MID, leading=15, spaceAfter=6)
    s_info = ParagraphStyle('I', fontName=font, fontSize=9,
                             textColor=C_MID, leading=13)
    s_bullet = ParagraphStyle('Bul', parent=s_body,
                               leftIndent=14, bulletIndent=5,
                               spaceBefore=2, spaceAfter=4)
    s_job_title = ParagraphStyle('JT', parent=s_body,
                                  fontName=font_b, fontSize=11,
                                  spaceBefore=8, spaceAfter=2)
    s_job_meta  = ParagraphStyle('JM', parent=s_body, fontSize=9.5,
                                  spaceBefore=0, spaceAfter=6)

    story = []

    # Photo (top-right conceptually — just inlined at top for single-col)
    with _process_photo(req.FILES.get('photo')) as photo_path:
        if photo_path:
            story.append(RLImage(photo_path, width=36*mm, height=36*mm))
            story.append(Spacer(1, 6))

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))

    contacts = " · ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'),
        req.POST.get('location'), req.POST.get('linkedin'),
    ]))
    if contacts:
        story.append(Paragraph(contacts, s_info))
    story.append(HRFlowable(width="100%", thickness=1.5, color=C_LINE,
                             spaceAfter=12, spaceBefore=6))

    if req.POST.get('about_me'):
        story.append(Paragraph("SUMMARY", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE, spaceAfter=6))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE, spaceAfter=6))
        _render_experience(story, req, s_job_title, s_job_meta, s_bullet, s_body)

    skills = _parse_skills(req.POST.get('skills_list', ''))
    if skills:
        story.append(Paragraph("SKILLS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE, spaceAfter=6))
        skill_text = "  ·  ".join([f"<b>{n}</b>" for n, _ in skills])
        story.append(Paragraph(skill_text, s_body))

    _render_education(story, req, s_h2, s_body, s_job_title, s_job_meta,
                      hr_color=C_LINE)
    _render_certifications(story, req, s_h2, s_body, hr_color=C_LINE)
    _render_portfolio(story, req, s_body, s_h2=s_h2, hr_color=C_LINE)

    if req.POST.get('languages'):
        story.append(Paragraph("LANGUAGES", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_LINE, spaceAfter=6))
        story.append(Paragraph(req.POST.get('languages'), s_body))

    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# BASE LAYOUT BUILDER 3 — SPLIT HEADER
# Coloured header band on top, light body below.
# Used by: creative_coral, creative_sunset, creative_mint, creative_pastel_pink
# ─────────────────────────────────────────────────────────────────────────────

def _build_split_header_layout(req, buf, cfg: dict):
    W, H = A4
    # POST overrides take priority — allow live customisation from the Studio UI
    _pc   = req.POST.get('primary_color') or cfg['primary_color']
    _bg   = req.POST.get('bg_color')      or cfg['bg_color']
    _acc  = req.POST.get('accent_color')  or cfg.get('accent_color', '#4ecdc4')
    _font = req.POST.get('font_family')   or cfg.get('font_family', 'Helvetica')

    C_HDR   = _safe_hex(_bg)
    C_ACC1  = _safe_hex(_pc)
    C_ACC2  = _safe_hex(_acc)
    C_LIGHT = colors.HexColor("#f7fafc")
    C_BODY  = colors.HexColor("#2d3748")

    font   = _font
    font_b = "Times-Bold" if "Times" in font else "Helvetica-Bold"

    doc = BaseDocTemplate(buf, pagesize=A4,
                          rightMargin=0, leftMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_LIGHT)
        canvas.rect(0, 0, W, H, fill=1, stroke=0)
        canvas.setFillColor(C_HDR)
        canvas.rect(0, H - 70*mm, W, 70*mm, fill=1, stroke=0)
        # Left accent stripe inside header
        canvas.setFillColor(C_ACC1)
        canvas.rect(0, H - 70*mm, 8*mm, 70*mm, fill=1, stroke=0)
        # Bottom accent line
        canvas.setFillColor(C_ACC2)
        canvas.rect(0, 0, W, 4, fill=1, stroke=0)
        canvas.restoreState()

    frame_header = Frame(10*mm, H - 68*mm, W - 20*mm, 64*mm, id='hdr',
                         leftPadding=20, rightPadding=10,
                         topPadding=10, bottomPadding=10)
    frame_body   = Frame(15*mm, 10*mm, W - 30*mm, H - 85*mm, id='body',
                         leftPadding=0, rightPadding=0,
                         topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='L',
                                       frames=[frame_header, frame_body],
                                       onPage=draw_bg)])

    s_name = ParagraphStyle('N', fontName=font_b, fontSize=28,
                             textColor=colors.white, leading=32, spaceAfter=4)
    s_role = ParagraphStyle('R', fontName=font, fontSize=13,
                             textColor=C_ACC2, spaceAfter=6)
    s_info = ParagraphStyle('I', fontName=font, fontSize=9,
                             textColor=colors.HexColor("#a0aec0"), leading=13)
    s_h2   = ParagraphStyle('H2', fontName=font_b, fontSize=10,
                             textColor=C_ACC1, spaceBefore=14, spaceAfter=6,
                             textTransform='uppercase', letterSpacing=1.5)
    s_body = ParagraphStyle('B', fontName=font, fontSize=10,
                             textColor=C_BODY, leading=16, spaceAfter=6)
    s_bullet = ParagraphStyle('Bul', parent=s_body,
                               leftIndent=14, bulletIndent=5,
                               spaceBefore=2, spaceAfter=4)
    s_job_title = ParagraphStyle('JT', parent=s_body,
                                  fontName=font_b, fontSize=11,
                                  spaceBefore=8, spaceAfter=2)
    s_job_meta  = ParagraphStyle('JM', parent=s_body, fontSize=9.5,
                                  textColor=colors.HexColor("#718096"),
                                  spaceBefore=0, spaceAfter=6)

    story = []

    # ── Header frame ────────────────────────────────────────────────────────
    with _process_photo(req.FILES.get('photo')) as photo_path:
        if photo_path:
            story.append(RLImage(photo_path, width=40*mm, height=40*mm))
            story.append(Spacer(1, 6))

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Creative Professional'), s_role))
    contacts = "  |  ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'),
        req.POST.get('location'), req.POST.get('linkedin'),
    ]))
    if contacts:
        story.append(Paragraph(contacts, s_info))

    story.append(FrameBreak())

    # ── Body frame ──────────────────────────────────────────────────────────
    if req.POST.get('about_me'):
        story.append(Paragraph("About Me", s_h2))
        story.append(HRFlowable(width="100%", thickness=1, color=C_ACC2, spaceAfter=8))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if exp:
        story.append(Paragraph("Experience", s_h2))
        story.append(HRFlowable(width="100%", thickness=1, color=C_ACC2, spaceAfter=8))
        _render_experience(story, req, s_job_title, s_job_meta, s_bullet, s_body)

    skills = _parse_skills(req.POST.get('skills_list', ''))
    if skills:
        story.append(Paragraph("Skills", s_h2))
        story.append(HRFlowable(width="100%", thickness=1, color=C_ACC2, spaceAfter=8))
        for name, lvl in skills:
            story.append(ProSkillBar(name, lvl, width=150*mm,
                                     bar_bg="#e2e8f0",
                                     bar_fill=_pc,
                                     text_color="#2d3748"))
            story.append(Spacer(1, 6))

    _render_education(story, req, s_h2, s_body, s_job_title, s_job_meta,
                      hr_color=C_ACC2)
    _render_certifications(story, req, s_h2, s_body, hr_color=C_ACC2)
    _render_portfolio(story, req, s_body, s_h2=s_h2, hr_color=C_ACC2)

    if req.POST.get('languages'):
        story.append(Paragraph("Languages", s_h2))
        story.append(HRFlowable(width="100%", thickness=1, color=C_ACC2, spaceAfter=8))
        story.append(Paragraph(req.POST.get('languages'), s_body))

    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# BASE LAYOUT BUILDER 4 — MODERN COLUMNS (full-bleed dark + top accent bar)
# Single column, dark background, terminal/tech aesthetic.
# Used by: tech_dark_neon, tech_hacker_terminal, tech_fintech_blue, tech_cyber_purple
# ─────────────────────────────────────────────────────────────────────────────

def _build_modern_columns(req, buf, cfg: dict):
    W, H = A4
    # POST overrides take priority — allow live customisation from the Studio UI
    _pc   = req.POST.get('primary_color') or cfg['primary_color']
    _bg   = req.POST.get('bg_color')      or cfg['bg_color']
    _acc  = req.POST.get('accent_color')  or cfg.get('accent_color', '#a855f7')
    _font = req.POST.get('font_family')   or cfg.get('font_family', 'Helvetica')

    C_BG    = _safe_hex(_bg)
    C_GREEN = _safe_hex(_pc)
    C_PUR   = _safe_hex(_acc)
    C_TEXT  = colors.HexColor("#c9d1d9")
    C_MUTED = colors.HexColor("#8b949e")

    font   = _font
    font_b = "Courier-Bold" if "Courier" in font else "Helvetica-Bold"

    doc = BaseDocTemplate(buf, pagesize=A4,
                          rightMargin=0, leftMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W, H, fill=1, stroke=0)
        canvas.setFillColor(C_GREEN)
        canvas.rect(0, H - 4, W, 4, fill=1, stroke=0)
        canvas.restoreState()

    frame = Frame(15*mm, 15*mm, W - 30*mm, H - 30*mm, id='main',
                  leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='L', frames=[frame], onPage=draw_bg)])

    prefix = "//" if "Courier" not in font else ">>>"

    s_name = ParagraphStyle('N', fontName=font_b, fontSize=32,
                             textColor=C_GREEN, leading=34, spaceAfter=4)
    s_role = ParagraphStyle('R', fontName=font, fontSize=13,
                             textColor=C_PUR, spaceAfter=12)
    s_h2   = ParagraphStyle('H2', fontName=font_b, fontSize=10,
                             textColor=C_GREEN, spaceBefore=18, spaceAfter=6,
                             textTransform='uppercase', letterSpacing=2)
    s_body = ParagraphStyle('B', fontName=font, fontSize=10,
                             textColor=C_TEXT, leading=15, spaceAfter=5)
    s_info = ParagraphStyle('I', fontName=font, fontSize=9,
                             textColor=C_MUTED, leading=13)
    s_bullet = ParagraphStyle('Bul', parent=s_body,
                               leftIndent=14, bulletIndent=5,
                               spaceBefore=2, spaceAfter=4)
    s_job_title = ParagraphStyle('JT', parent=s_body,
                                  fontName=font_b, fontSize=11,
                                  spaceBefore=8, spaceAfter=2)
    s_job_meta  = ParagraphStyle('JM', parent=s_body, fontSize=9.5,
                                  textColor=C_MUTED,
                                  spaceBefore=0, spaceAfter=6)

    story = []

    with _process_photo(req.FILES.get('photo')) as photo_path:
        if photo_path:
            story.append(RLImage(photo_path, width=40*mm, height=40*mm))
            story.append(Spacer(1, 10))

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Developer'), s_role))

    contacts = " | ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'),
        req.POST.get('location'), req.POST.get('linkedin'),
    ]))
    if contacts:
        story.append(Paragraph(contacts, s_info))
    story.append(HRFlowable(width="100%", thickness=1,
                             color=C_GREEN, spaceAfter=10, spaceBefore=8))

    if req.POST.get('about_me'):
        story.append(Paragraph(f"{prefix} ABOUT", s_h2))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text') or req.POST.get('resume', '')
    if exp:
        story.append(Paragraph(f"{prefix} EXPERIENCE", s_h2))
        _render_experience(story, req, s_job_title, s_job_meta, s_bullet, s_body)

    skills = _parse_skills(req.POST.get('skills_list', ''))
    if skills:
        story.append(Paragraph(f"{prefix} TECH STACK", s_h2))
        for name, lvl in skills:
            story.append(ProSkillBar(name, lvl, width=int(150*mm),
                                     bar_bg="#21262d",
                                     bar_fill=_pc,
                                     text_color="#c9d1d9"))
            story.append(Spacer(1, 6))

    _render_education(story, req, s_h2, s_body, s_job_title, s_job_meta,
                      hr_color=C_GREEN)
    _render_certifications(story, req, s_h2, s_body, hr_color=C_GREEN)
    _render_portfolio(story, req, s_body, s_h2=s_h2, hr_color=C_GREEN)

    if req.POST.get('languages'):
        story.append(Paragraph(f"{prefix} LANGUAGES", s_h2))
        story.append(Paragraph(req.POST.get('languages'), s_body))

    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT DISPATCH MAP
# ─────────────────────────────────────────────────────────────────────────────

_LAYOUT_BUILDERS = {
    "sidebar":        _build_sidebar_layout,
    "minimal":        _build_minimal_layout,
    "split_header":   _build_split_header_layout,
    "modern_columns": _build_modern_columns,
}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def build_pdf(template_slug: str, request) -> io.BytesIO:
    """
    Build and return a BytesIO PDF for the given template slug.
    Looks up the template config, dispatches to the correct base layout
    builder passing the config dict, falls back to classic_navy on error.
    """
    buf = io.BytesIO()
    cfg = get_template_by_slug(template_slug)
    layout_key = cfg.get("layout", "sidebar")
    builder = _LAYOUT_BUILDERS.get(layout_key, _build_sidebar_layout)
    try:
        builder(request, buf, cfg)
    except Exception as exc:
        logger.exception("PDF build failed for slug=%s layout=%s: %s",
                         template_slug, layout_key, exc)
        # Fallback: re-render with the first template
        buf = io.BytesIO()
        _build_sidebar_layout(request, buf, TEMPLATES[0])
    buf.seek(0)
    return buf
