"""
pdf_engine.py — Visual Resume Studio PDF Factory  (v3.0)
=========================================================
10 TRULY DISTINCT layout builders:
  1. _build_minimal_centered       — single-column, centred serif elegance
  2. _build_left_sidebar_dark      — 25% dark photo/skills sidebar, 75% light main
  3. _build_right_sidebar_light    — 70% main + 30% light-colour right sidebar
  4. _build_split_header           — full-width colour top band + 2-column body
  5. _build_timeline_modern        — 1-column with a vertical timeline line on left
  6. _build_two_column_equal       — 50/50 equal-width split
  7. _build_hacker_terminal        — black bg, green monospace, no photo
  8. _build_academic_classic       — ultra-dense, strict HR rules, ATS-optimised
  9. _build_top_bottom_split       — 30% coloured header + 70% white main body
 10. _build_creative_masonry       — asymmetric left 65% + right 35% creative layout

All builders support: experience_text, projects_text, skills_list, education,
certifications, portfolio_url, languages, photo, full_name, target_role, about_me,
email, phone, location, linkedin. POST overrides (primary_color, bg_color,
accent_color, font_family) are honoured everywhere.
"""

import datetime
import io
import os
import logging
import re
import tempfile

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph,
    Spacer, Image as RLImage, FrameBreak, Flowable,
    HRFlowable,
)
from reportlab.platypus.flowables import KeepTogether
from reportlab.graphics.shapes import Drawing, Line, Rect
from PIL import Image, ImageOps, ImageDraw as PILDraw

logger = logging.getLogger(__name__)

# Longest edge the uploaded photo is decoded to. The avatar is rendered at
# 28–40mm (~470px at 300dpi) and masked to a 500px circle, so 1000px is already
# 2x the pixels that reach the page — anything larger is memory spent on detail
# the PDF throws away.
_PHOTO_WORK_PX = 1000

# Hard backstop against decompression bombs. Above this, Pillow raises rather
# than allocating, and _process_photo's handler skips the photo. Generous
# enough for any real camera (flagship phones top out around 50MP).
Image.MAX_IMAGE_PIXELS = 80_000_000

W_A4, H_A4 = A4   # 595.27 × 841.89 pts


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE REGISTRY — 10 templates, each using a distinct layout builder
# ─────────────────────────────────────────────────────────────────────────────

TEMPLATES = [
    {
        "slug":          "minimal_centered",
        "name":          "Minimal Centered",
        "image_url":     "/static/img/templates/minimal_centered.jpg",
        "desc":          "Single-column centred serif — timeless & ATS-friendly",
        "category":      "minimalist",
        "layout":        "minimal_centered",
        "primary_color": "#1a1a2e",
        "bg_color":      "#ffffff",
        "accent_color":  "#475569",
        "font_family":   "Times-Roman",
    },
    {
        "slug":          "left_sidebar_dark",
        "name":          "Dark Sidebar",
        "image_url":     "/static/img/templates/left_sidebar_dark.jpg",
        "desc":          "25% dark photo/skills sidebar — bold & professional",
        "category":      "classic",
        "layout":        "left_sidebar_dark",
        "primary_color": "#4cc9f0",
        "bg_color":      "#1e2a38",
        "accent_color":  "#f0f4f8",
        "font_family":   "Helvetica",
    },
    {
        "slug":          "right_sidebar_light",
        "name":          "Light Right Panel",
        "image_url":     "/static/img/templates/right_sidebar_light.jpg",
        "desc":          "70% main area + 30% soft-colour right panel",
        "category":      "minimalist",
        "layout":        "right_sidebar_light",
        "primary_color": "#7c3aed",
        "bg_color":      "#f3f0ff",
        "accent_color":  "#ede9fe",
        "font_family":   "Helvetica",
    },
    {
        "slug":          "split_header",
        "name":          "Bold Split Header",
        "image_url":     "/static/img/templates/split_header.jpg",
        "desc":          "Full-width colour header band + 2-column body",
        "category":      "creative",
        "layout":        "split_header",
        "primary_color": "#ff6b6b",
        "bg_color":      "#2d3436",
        "accent_color":  "#fdcb6e",
        "font_family":   "Helvetica",
    },
    {
        "slug":          "timeline_modern",
        "name":          "Timeline Modern",
        "image_url":     "/static/img/templates/timeline_modern.jpg",
        "desc":          "Vertical timeline line down the left side",
        "category":      "tech",
        "layout":        "timeline_modern",
        "primary_color": "#00b4d8",
        "bg_color":      "#ffffff",
        "accent_color":  "#caf0f8",
        "font_family":   "Helvetica",
    },
    {
        "slug":          "two_column_equal",
        "name":          "50/50 Grid",
        "image_url":     "/static/img/templates/two_column_equal.jpg",
        "desc":          "Equal 50/50 two-column split — balanced & clean",
        "category":      "minimalist",
        "layout":        "two_column_equal",
        "primary_color": "#059669",
        "bg_color":      "#f8fafb",
        "accent_color":  "#d1fae5",
        "font_family":   "Helvetica",
    },
    {
        "slug":          "hacker_terminal",
        "name":          "Hacker Terminal",
        "image_url":     "/static/img/templates/hacker_terminal.jpg",
        "desc":          "Black bg, green monospace text — developer aesthetic",
        "category":      "tech",
        "layout":        "hacker_terminal",
        "primary_color": "#00ff41",
        "bg_color":      "#0d0d0d",
        "accent_color":  "#008f11",
        "font_family":   "Courier",
    },
    {
        "slug":          "academic_classic",
        "name":          "Academic Classic",
        "image_url":     "/static/img/templates/academic_classic.jpg",
        "desc":          "Ultra-dense, strict HR rules, ATS-optimised",
        "category":      "executive",
        "layout":        "academic_classic",
        "primary_color": "#1a1a1a",
        "bg_color":      "#ffffff",
        "accent_color":  "#555555",
        "font_family":   "Times-Roman",
    },
    {
        "slug":          "top_bottom_split",
        "name":          "Top/Bottom Split",
        "image_url":     "/static/img/templates/top_bottom_split.jpg",
        "desc":          "30% coloured top header + 70% white content area",
        "category":      "classic",
        "layout":        "top_bottom_split",
        "primary_color": "#2563eb",
        "bg_color":      "#1e3a5f",
        "accent_color":  "#dbeafe",
        "font_family":   "Helvetica",
    },
    {
        "slug":          "creative_masonry",
        "name":          "Creative Masonry",
        "image_url":     "/static/img/templates/creative_masonry.jpg",
        "desc":          "Asymmetric layout with scattered skills and bold accents",
        "category":      "creative",
        "layout":        "creative_masonry",
        "primary_color": "#e879f9",
        "bg_color":      "#0f0f1a",
        "accent_color":  "#818cf8",
        "font_family":   "Helvetica",
    },
]


def get_template_by_slug(slug: str) -> dict:
    """Return template meta dict or fall back to first template."""
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


def _process_photo(photo_file):
    """
    Process an uploaded photo into a circular-masked PNG temp file.
    Returns the temp file path (str) on success, or None if the photo
    is absent, corrupt, or otherwise unusable.

    IMPORTANT — caller must clean up after doc.build():
      ReportLab evaluates RLImage flowables lazily inside doc.build().
      A contextmanager would delete the file before doc.build() reads it,
      causing OSError: Cannot open resource '/tmp/...'.  Instead, every
      layout builder must follow this pattern::

          pp = _process_photo(req.FILES.get('photo'))
          try:
              doc.build(story)
          finally:
              if pp and os.path.exists(pp):
                  os.unlink(pp)

    Robust to:
      - InMemoryUploadedFile whose pointer may be at EOF (seeks to 0 first).
      - Corrupt/unsupported image formats (logs and returns None, never crashes).
      - Any unexpected PIL exception (returns None safely).
    """
    if not photo_file:
        return None

    path = None
    try:
        # Reset file pointer — InMemoryUploadedFile may be at EOF after
        # Django's multipart parser or a previous read in the same request.
        try:
            photo_file.seek(0)
        except (AttributeError, OSError):
            pass

        # Decode at reduced scale, never full size.
        #
        # This used to be a bare open() + load(), which materialises the whole
        # bitmap: a 50MP phone photo is ~200MB in RGBA and convert() copies it
        # again. On a small container running several workers that exhausts
        # memory and the worker dies mid-request — which reaches the browser as
        # a failed preview, not as a Python error, because nothing here raised.
        #
        # draft() asks the JPEG decoder for a 1/2, 1/4 or 1/8 scale image
        # directly in libjpeg, so the full-size bitmap is never allocated. It is
        # a no-op for formats that don't support it, hence the thumbnail()
        # below as the general-case bound.
        try:
            img = Image.open(photo_file)
            img.draft('RGB', (_PHOTO_WORK_PX, _PHOTO_WORK_PX))
            img.load()
            img.thumbnail((_PHOTO_WORK_PX, _PHOTO_WORK_PX), Image.LANCZOS)
            img = img.convert("RGBA")
        except Exception as img_exc:
            logger.warning("_process_photo: Image.open/decode failed — skipping: %s", img_exc)
            return None

        # Circular crop mask (500×500 px working resolution)
        mask   = Image.new('L', (500, 500), 0)
        mask_d = PILDraw.Draw(mask)
        mask_d.ellipse((0, 0, 500, 500), fill=255)
        output = ImageOps.fit(img.convert("RGB"), (500, 500), centering=(0.5, 0.5))
        output.putalpha(mask)

        tmp  = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
        path = tmp.name
        output.save(tmp, format='PNG')
        tmp.close()
        return path

    except Exception as exc:
        logger.warning("_process_photo: unexpected error — skipping photo: %s", exc)
        # Clean up any partially created temp file before returning None
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        return None


def _parse_skill_groups(skills_str: str):
    """Parse the skills field into [(category | None, [(name, level), ...]), ...].

    Wire format is one group per line, category first::

        AI & LLM: Claude, RAG, LangChain
        Backend: Python, Django, PostgreSQL

    A line with no colon is an uncategorised group, which is exactly what the
    old single-line format ('Python-80,Django-70') looks like — so resumes saved
    before skills were grouped still parse, as one unnamed group.

    A level may still be suffixed to a skill ('Python-80') for templates that
    draw proficiency bars. It is optional: when absent the level is None, and
    callers must not substitute a number the candidate never gave.
    """
    groups = []
    for line in (skills_str or "").splitlines():
        line = line.strip()
        if not line:
            continue

        category, _, remainder = line.partition(':')
        if not remainder.strip():
            # No colon on this line — the whole line is the skill list.
            category, remainder = '', line

        entries = []
        for item in remainder.split(','):
            item = item.strip()
            if not item:
                continue
            # Split the level off the RIGHT so names may contain hyphens
            # ('Objective-C-80' → 'Objective-C', 80).
            name, sep, tail = item.rpartition('-')
            if sep and name.strip():
                try:
                    entries.append((name.strip(), float(tail)))
                    continue
                except ValueError:
                    pass
            entries.append((item, None))

        if entries:
            groups.append((category.strip() or None, entries))
    return groups


def _esc(text) -> str:
    """Escape text for a ReportLab Paragraph, which parses its input as XML.

    Not cosmetic. Measured on the raw string:

        'C++ <templates> and Go'  ->  'C++ and Go'    (tag silently swallowed)
        'R&D'                     ->  'R&D;'          (stray semicolon)

    So a skill named C++/<T> disappears from the PDF without an error, and any
    category with an ampersand — 'AI & LLM', 'DevOps & Tools', the exact
    headings real resumes use — renders corrupted.
    """
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


def _render_skill_groups(story, groups, body_style):
    """Append one paragraph per skill group: '<b>Category:</b> a, b, c'.

    Groups with no category (everything saved before skills were grouped)
    render as a plain run of skills, exactly as they did before.
    """
    for category, entries in groups:
        names = ',  '.join(_esc(name) for name, _lvl in entries)
        if category:
            story.append(Paragraph(f'<b>{_esc(category)}:</b> {names}', body_style))
        else:
            story.append(Paragraph(names, body_style))


def _esc(text) -> str:
    """Escape text for a ReportLab Paragraph, which parses its input as XML.

    Not cosmetic. Measured on the raw string:

        'C++ <templates> and Go'  ->  'C++ and Go'    (tag silently swallowed)
        'R&D'                     ->  'R&D;'          (stray semicolon)

    So a skill named C++/<T> disappears from the PDF without an error, and any
    category with an ampersand — 'AI & LLM', 'DevOps & Tools', the exact
    headings real resumes use — renders corrupted.
    """
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


def _render_skill_groups(story, groups, body_style):
    """Append one paragraph per skill group: '<b>Category:</b> a, b, c'.

    Groups with no category (everything saved before skills were grouped)
    render as a plain run of skills, exactly as they did before.
    """
    for category, entries in groups:
        names = ',  '.join(_esc(name) for name, _lvl in entries)
        if category:
            story.append(Paragraph(f'<b>{_esc(category)}:</b> {names}', body_style))
        else:
            story.append(Paragraph(names, body_style))


def _safe_hex(color_str: str) -> colors.Color:
    """Convert a hex string to a ReportLab Color, falling back to black."""
    try:
        s = color_str.strip()
        if not s.startswith('#'):
            s = '#' + s
        return colors.HexColor(s)
    except Exception:
        return colors.black


def _post_colors(req, cfg: dict):
    """
    Extract primary/bg/accent/font from POST (user overrides),
    falling back to the template config defaults.
    Returns (_pc, _bg, _acc, _font) as raw strings.
    """
    return (
        req.POST.get('primary_color') or cfg.get('primary_color', '#333333'),
        req.POST.get('bg_color')      or cfg.get('bg_color',      '#ffffff'),
        req.POST.get('accent_color')  or cfg.get('accent_color',  '#888888'),
        req.POST.get('font_family')   or cfg.get('font_family',   'Helvetica'),
    )


def _font_variants(font: str):
    """Return (regular, bold, italic) font names for a base font."""
    # Map custom fonts to standard fallbacks since we skipped TTF embedding
    if font in ['Inter', 'Outfit']:
        font = 'Helvetica'
    elif font in ['JetBrains Mono']:
        font = 'Courier'
    elif font in ['Merriweather']:
        font = 'Times-Roman'
        
    if 'Times' in font:
        return ('Times-Roman', 'Times-Bold', 'Times-Italic')
    if 'Courier' in font:
        return ('Courier', 'Courier-Bold', 'Courier-Oblique')
    return ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique')


def _render_text_block(story, text, s_title, s_meta, s_bullet, s_body):
    """
    Parse free-form text (experience / projects) and append styled paragraphs.
    Each logical block (title + meta + bullets) is wrapped in KeepTogether so
    that a section header or lone meta line never orphans on a new page.

    Rules:
      - Lines starting with - or * → bulleted paragraph
      - Short lines with | or digits → grey meta line (dates/company)
      - Other short lines → bold title  (triggers a new logical block)
      - Long lines → body paragraph
    """
    current_block = []   # accumulates flowables for one job entry

    def _flush(blk):
        """Commit the current block to story, wrapped in KeepTogether."""
        if blk:
            story.append(KeepTogether(blk))
            story.append(Spacer(1, 6))  # breathing room between entries

    for t in (text or "").split('\n'):
        t = t.strip()
        if not t:
            continue
        if t.startswith('-') or t.startswith('*'):
            # U+00B7 MIDDLE DOT, not U+2022 BULLET.
            #
            # The round bullet is not in the standard-14 fonts' encoding, so
            # every bulleted line extracted as "(cid:127) Cut latency by 40%".
            # On a product whose whole claim is that screening software can read
            # the export, putting literal "(cid:127)" in front of every single
            # achievement is the worst place to lose that argument.
            #
            # Measured alternatives: ZapfDingbats and Symbol both extract as a
            # literal "n", which is worse. The middle dot renders as a small
            # centred dot and extracts as itself.
            current_block.append(Paragraph(_esc(t[1:].strip()), s_bullet, bulletText='·'))
        elif len(t) < 100 and ('|' in t or any(ch.isdigit() for ch in t)):
            current_block.append(Paragraph(f'<font color="#888888">{_esc(t)}</font>', s_meta))
        elif len(t) < 100:
            # A short non-meta line signals a new job title — flush previous block
            _flush(current_block)
            current_block = [Paragraph(f'<b>{_esc(t)}</b>', s_title)]
        else:
            current_block.append(Paragraph(_esc(t), s_body))

    _flush(current_block)  # flush any remaining block


def _education_markup(line):
    """One education entry as Paragraph markup: degree bold, rest beneath it.

    Entries arrive as "Degree | Institution | Dates". Rendered as a single run,
    an 88-character entry like

        BSc (Hons) Computer Science | Asia Pacific University (APU) — 2023 – Expected: Nov 2028

    cannot fit one line in a sidebar column, and ReportLab broke it wherever it
    happened to run out of room: mid-institution in creative_masonry, and in
    split_header immediately before the dates, orphaning "— 2023 – Expected:
    Nov 2028" onto a line of its own.

    Splitting on the separator gives the wrap a sensible place to happen, and
    <nobr> around a trailing date range stops that range being split again.
    """
    parts = [p.strip() for p in line.split('|') if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return f'<b>{_esc(parts[0])}</b>'

    degree = _esc(parts[0])
    rest = parts[1:]

    # A trailing segment carrying digits is a date range — keep it whole.
    #
    # Non-breaking spaces rather than <nobr>: the tag is ignored by the
    # Paragraph parser in the narrow sidebar columns (right_sidebar_light and
    # creative_masonry both still broke "2023 – Expected: Nov 2028" in half with
    # it applied), whereas U+00A0 is honoured everywhere because the line
    # breaker never treats it as a break opportunity.
    tail = rest[-1]
    if any(ch.isdigit() for ch in tail):
        tail = tail.replace(' ', ' ')
    detail = ' — '.join([_esc(p) for p in rest[:-1]] + [_esc(tail)])
    return f'<b>{degree}</b><br/>{detail}'


def _render_edu_certs_lang(story, req, s_h2, s_body, s_sub=None,
                            hr_color=None, w="100%", languages=True):
    """
    Append Education, Certifications, Languages, and Portfolio blocks.
    Each block is wrapped in KeepTogether to prevent orphaned section headers
    across page breaks.

    `languages=False` for two-column templates that already print languages in
    their sidebar. left_sidebar_dark did both and printed the section twice —
    once in the rail and once again in the main column, under two separate
    LANGUAGES headings.
    """
    s_sub = s_sub or s_body

    edu = req.POST.get('education', '').strip()
    if edu:
        blk = [Paragraph("EDUCATION", s_h2)]
        if hr_color:
            blk.append(HRFlowable(width=w, thickness=0.5,
                                  color=hr_color, spaceAfter=5))
        for line in edu.split('\n'):
            markup = _education_markup(line.strip())
            if markup:
                blk.append(Paragraph(markup, s_sub))
        story.append(KeepTogether(blk))

    certs = req.POST.get('certifications', '').strip()
    if certs:
        blk = [Paragraph("CERTIFICATIONS", s_h2)]
        if hr_color:
            blk.append(HRFlowable(width=w, thickness=0.5,
                                  color=hr_color, spaceAfter=5))
        for line in certs.replace(',', '\n').split('\n'):
            line = line.strip()
            if line:
                blk.append(Paragraph(f"· {_esc(line)}", s_body))
        story.append(KeepTogether(blk))

    lang = req.POST.get('languages', '').strip()
    if lang and languages:
        blk = [Paragraph("LANGUAGES", s_h2)]
        if hr_color:
            blk.append(HRFlowable(width=w, thickness=0.5,
                                  color=hr_color, spaceAfter=5))
        blk.append(Paragraph(_esc(lang), s_body))
        story.append(KeepTogether(blk))

    port = req.POST.get('portfolio_url', '').strip()
    if port:
        blk = [Paragraph("PORTFOLIO / GITHUB", s_h2)]
        if hr_color:
            blk.append(HRFlowable(width=w, thickness=0.5,
                                  color=hr_color, spaceAfter=5))
        blk.append(Paragraph(f'<link href="{port}">{port}</link>', s_body))
        story.append(KeepTogether(blk))


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 1 — MINIMAL CENTERED
# Single-column, heavily centred, serif elegance. Name in large caps centred.
# All section headers centred with decorative HR. Photo centred at top.
# ─────────────────────────────────────────────────────────────────────────────

def _build_minimal_centered(req, buf, cfg: dict):
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    C_BG   = _safe_hex(_bg)
    C_HEAD = _safe_hex(_pc)
    C_ACC  = _safe_hex(_acc)
    C_TEXT = colors.HexColor('#1a1a2e') if _bg == '#ffffff' else colors.HexColor('#1a1a2e')
    fn, fb, fi = _font_variants(_font)

    M = 22 * mm
    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=M, rightMargin=M,
                          topMargin=M, bottomMargin=M)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W_A4, H_A4, fill=1, stroke=0)
        # Gold bottom rule
        canvas.setFillColor(C_ACC)
        canvas.rect(0, 0, W_A4, 3, fill=1, stroke=0)
        canvas.restoreState()

    frame = Frame(M, M, W_A4 - 2*M, H_A4 - 2*M, id='main',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='P', frames=[frame], onPage=draw_bg)])

    CW = W_A4 - 2*M  # usable content width

    s_name   = ParagraphStyle('N',  fontName=fb, fontSize=30, textColor=C_HEAD,
                               leading=34, spaceAfter=2, alignment=1)
    s_role   = ParagraphStyle('R',  fontName=fi, fontSize=13, textColor=C_ACC,
                               spaceAfter=6, alignment=1)
    s_info   = ParagraphStyle('I',  fontName=fn, fontSize=9,  textColor=colors.HexColor('#555555'),
                               leading=13, spaceAfter=8, alignment=1)
    # Section headings are left-aligned even though the header block above is
    # centred, and that is deliberate.
    #
    # When these were centred (alignment=1) their glyphs occupied roughly
    # x=258-337 while every body paragraph started at x=62. Extractors that
    # reconstruct reading order from layout — pdfminer's default boxes_flow
    # among them — read that shared horizontal band as a column of its own and
    # grouped all four headings together, emitting SUMMARY/EXPERIENCE/SKILLS/
    # EDUCATION after the body text instead of each above its own section. A
    # parser using the headings to work out where experience begins would get
    # nothing useful. This was the only one of the ten templates affected, and
    # it is TEMPLATES[0]: the default fallback and one of the two templates
    # free accounts can use.
    #
    # The template's centred identity comes from the name/role/contact block,
    # which is untouched. Covered by ResumePdfReadingOrderTest.
    s_h2     = ParagraphStyle('H2', fontName=fb, fontSize=12,  textColor=C_HEAD,
                               spaceBefore=16, spaceAfter=4, alignment=0,
                               textTransform='uppercase', letterSpacing=2)
    s_body   = ParagraphStyle('B',  fontName=fn, fontSize=9.5, textColor=C_TEXT,
                               leading=14, spaceAfter=5)
    s_bullet = ParagraphStyle('Bul', parent=s_body, leftIndent=14, bulletIndent=4,
                               spaceBefore=1, spaceAfter=3)
    s_title  = ParagraphStyle('T',  fontName=fb, fontSize=10.5, textColor=C_TEXT,
                               spaceBefore=8, spaceAfter=2)
    s_meta   = ParagraphStyle('M',  fontName=fn, fontSize=9, textColor=colors.HexColor('#888888'),
                               spaceAfter=4)

    story = []

    pp = _process_photo(req.FILES.get('photo'))
    if pp:
            story.append(RLImage(pp, width=28*mm, height=28*mm, hAlign='CENTER'))
            story.append(Spacer(1, 8))

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))

    contacts = " · ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'),
        req.POST.get('location'), req.POST.get('linkedin'),
        req.POST.get('github'),
    ]))
    if contacts:
        story.append(Paragraph(contacts, s_info))

    story.append(HRFlowable(width="60%", thickness=1, color=C_ACC,
                             hAlign='CENTER', spaceAfter=10))

    if req.POST.get('about_me'):
        story.append(Paragraph("SUMMARY", s_h2))
        story.append(HRFlowable(width="40%", thickness=0.5, color=C_ACC,
                                 hAlign='CENTER', spaceAfter=6))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="40%", thickness=0.5, color=C_ACC,
                                 hAlign='CENTER', spaceAfter=6))
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("PROJECTS", s_h2))
        story.append(HRFlowable(width="40%", thickness=0.5, color=C_ACC,
                                 hAlign='CENTER', spaceAfter=6))
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("SKILLS", s_h2))
        story.append(HRFlowable(width="40%", thickness=0.5, color=C_ACC,
                                 hAlign='CENTER', spaceAfter=6))
        _render_skill_groups(story, skill_groups, s_body)

    _render_edu_certs_lang(story, req, s_h2, s_body, hr_color=C_ACC, w="40%")
    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 2 — LEFT SIDEBAR DARK
# 25% dark left sidebar: photo, name, contacts, skills bars.
# 75% light main: role/summary/experience/projects/education.
# ─────────────────────────────────────────────────────────────────────────────

def _build_left_sidebar_dark(req, buf, cfg: dict):
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    C_SIDE = _safe_hex(_bg)
    C_ACC  = _safe_hex(_pc)
    C_MAIN = _safe_hex(_acc)
    fn, fb, fi = _font_variants(_font)

    SB_W = int(W_A4 * 0.27)
    MAIN_W = W_A4 - SB_W
    PAD = 16

    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=0, rightMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_SIDE)
        canvas.rect(0, 0, SB_W, H_A4, fill=1, stroke=0)
        canvas.setFillColor(C_MAIN)
        canvas.rect(SB_W, 0, MAIN_W, H_A4, fill=1, stroke=0)
        canvas.restoreState()

    frame_sb   = Frame(0, 0, SB_W, H_A4, id='sb',
                       leftPadding=PAD, rightPadding=PAD,
                       topPadding=int(34*mm), bottomPadding=PAD)
    frame_main = Frame(SB_W, 0, MAIN_W, H_A4, id='main',
                       leftPadding=PAD+4, rightPadding=PAD+4,
                       topPadding=PAD*2, bottomPadding=PAD)
    doc.addPageTemplates([PageTemplate(id='P',
                                       frames=[frame_sb, frame_main],
                                       onPage=draw_bg)])

    C_W   = colors.white
    C_DIM = colors.HexColor('#99aabb')
    C_TEXT = colors.HexColor('#1e2a38') if _acc.startswith('#f') else colors.HexColor('#2d3748')

    s_sb_name = ParagraphStyle('SN', fontName=fb, fontSize=15, textColor=C_W,
                                leading=18, spaceAfter=3)
    s_sb_role = ParagraphStyle('SR', fontName=fn, fontSize=9, textColor=C_ACC,
                                spaceAfter=14)
    s_sb_h    = ParagraphStyle('SH', fontName=fb, fontSize=8, textColor=C_ACC,
                                spaceBefore=18, spaceAfter=6,
                                textTransform='uppercase', letterSpacing=1.2)
    s_sb_t    = ParagraphStyle('ST', fontName=fn, fontSize=8.5, textColor=C_W,
                                leading=13, spaceAfter=3)
    s_sb_dim  = ParagraphStyle('SD', fontName=fn, fontSize=7, textColor=C_DIM,
                                spaceAfter=1)
    s_h2      = ParagraphStyle('H2', fontName=fb, fontSize=12, textColor=C_TEXT,
                                spaceBefore=16, spaceAfter=6,
                                textTransform='uppercase')
    s_body    = ParagraphStyle('B',  fontName=fn, fontSize=9.5, textColor=C_TEXT,
                                leading=14, spaceAfter=5)
    s_bullet  = ParagraphStyle('Bul', parent=s_body, leftIndent=14, bulletIndent=4,
                                spaceBefore=1, spaceAfter=3)
    s_title   = ParagraphStyle('T',  fontName=fb, fontSize=10.5, textColor=C_TEXT,
                                spaceBefore=8, spaceAfter=2)
    s_meta    = ParagraphStyle('M',  fontName=fn, fontSize=8.5,
                                textColor=colors.HexColor('#666666'), spaceAfter=4)

    story = []

    # ── SIDEBAR ──────────────────────────────────────────────────────────────
    pp = _process_photo(req.FILES.get('photo'))
    if pp:
            story.append(RLImage(pp, width=40*mm, height=40*mm, hAlign='CENTER'))
            story.append(Spacer(1, 10))

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_sb_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_sb_role))

    for lbl, val in [("LOCATION", req.POST.get('location')),
                     ("EMAIL",    req.POST.get('email')),
                     ("PHONE",    req.POST.get('phone')),
                     ("LINKEDIN", req.POST.get('linkedin')),
                     ("GITHUB",   req.POST.get('github'))]:
        if val:
            story.append(Paragraph(lbl, s_sb_dim))
            story.append(Paragraph(val, s_sb_t))

    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("SKILLS", s_sb_h))
        _render_skill_groups(story, skill_groups, s_sb_t)

    lang = req.POST.get('languages', '').strip()
    if lang:
        story.append(Paragraph("LANGUAGES", s_sb_h))
        story.append(Paragraph(lang, s_sb_t))

    story.append(FrameBreak())

    # ── MAIN ─────────────────────────────────────────────────────────────────
    if req.POST.get('about_me'):
        story.append(Paragraph("PROFILE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                 color=_safe_hex(_pc), spaceAfter=6))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                 color=_safe_hex(_pc), spaceAfter=6))
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("PROJECTS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                 color=_safe_hex(_pc), spaceAfter=6))
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    # languages=False: the dark rail above already printed them. Passing the
    # default rendered the section a second time in the main column.
    _render_edu_certs_lang(story, req, s_h2, s_body, s_sub=s_meta,
                           hr_color=_safe_hex(_pc), languages=False)
    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 3 — RIGHT SIDEBAR LIGHT
# 70% main (white) on left, 30% tinted-colour right panel for skills/contacts.
# ─────────────────────────────────────────────────────────────────────────────

def _build_right_sidebar_light(req, buf, cfg: dict):
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    C_SIDE = _safe_hex(_bg)
    C_ACC  = _safe_hex(_pc)
    fn, fb, fi = _font_variants(_font)

    MAIN_W = int(W_A4 * 0.67)
    SB_W   = W_A4 - MAIN_W
    PAD    = 16

    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=0, rightMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, MAIN_W, H_A4, fill=1, stroke=0)
        canvas.setFillColor(C_SIDE)
        canvas.rect(MAIN_W, 0, SB_W, H_A4, fill=1, stroke=0)
        # Accent top bar
        canvas.setFillColor(C_ACC)
        canvas.rect(0, H_A4 - 4, W_A4, 4, fill=1, stroke=0)
        canvas.restoreState()

    frame_main = Frame(0, 0, MAIN_W, H_A4, id='main',
                       leftPadding=PAD+8, rightPadding=PAD,
                       topPadding=PAD+10, bottomPadding=PAD)
    frame_sb   = Frame(MAIN_W, 0, SB_W, H_A4, id='sb',
                       leftPadding=PAD, rightPadding=PAD,
                       topPadding=PAD+10, bottomPadding=PAD)
    doc.addPageTemplates([PageTemplate(id='P',
                                       frames=[frame_main, frame_sb],
                                       onPage=draw_bg)])

    C_TEXT = colors.HexColor('#1e1e2e')
    C_MUTED = colors.HexColor('#6b7280')

    s_name   = ParagraphStyle('N',  fontName=fb, fontSize=26, textColor=C_ACC,
                               leading=30, spaceAfter=2)
    s_role   = ParagraphStyle('R',  fontName=fi, fontSize=12, textColor=C_TEXT,
                               spaceAfter=10)
    s_h2     = ParagraphStyle('H2', fontName=fb, fontSize=12, textColor=C_ACC,
                               spaceBefore=16, spaceAfter=5,
                               textTransform='uppercase', letterSpacing=1.5)
    s_body   = ParagraphStyle('B',  fontName=fn, fontSize=9.5, textColor=C_TEXT,
                               leading=14, spaceAfter=5)
    s_bullet = ParagraphStyle('Bul', parent=s_body, leftIndent=14, bulletIndent=4,
                               spaceBefore=1, spaceAfter=3)
    s_title  = ParagraphStyle('T',  fontName=fb, fontSize=10.5, textColor=C_TEXT,
                               spaceBefore=8, spaceAfter=2)
    s_meta   = ParagraphStyle('M',  fontName=fn, fontSize=8.5, textColor=C_MUTED,
                               spaceAfter=4)
    s_sb_h   = ParagraphStyle('SH', fontName=fb, fontSize=8, textColor=C_ACC,
                               spaceBefore=16, spaceAfter=5,
                               textTransform='uppercase', letterSpacing=1)
    s_sb_t   = ParagraphStyle('ST', fontName=fn, fontSize=8.5, textColor=C_TEXT,
                               leading=13, spaceAfter=3)

    story = []

    # ── MAIN COLUMN ──────────────────────────────────────────────────────────
    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))
    story.append(HRFlowable(width="100%", thickness=1.5, color=C_ACC, spaceAfter=10))

    if req.POST.get('about_me'):
        story.append(Paragraph("PROFILE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_SIDE, spaceAfter=5))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_SIDE, spaceAfter=5))
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("PROJECTS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_SIDE, spaceAfter=5))
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    story.append(FrameBreak())

    # ── RIGHT SIDEBAR ────────────────────────────────────────────────────────
    pp = _process_photo(req.FILES.get('photo'))
    if pp:
            story.append(RLImage(pp, width=32*mm, height=32*mm, hAlign='CENTER'))
            story.append(Spacer(1, 10))

    for lbl, val in [("Location", req.POST.get('location')),
                     ("Email",    req.POST.get('email')),
                     ("Phone",    req.POST.get('phone')),
                     ("LinkedIn", req.POST.get('linkedin')),
                     ("GitHub",   req.POST.get('github'))]:
        if val:
            story.append(Paragraph(f'<b><font color="{_pc}">{lbl}</font></b>', s_sb_h))
            story.append(Paragraph(val, s_sb_t))

    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("SKILLS", s_sb_h))
        _render_skill_groups(story, skill_groups, s_sb_t)

    _render_edu_certs_lang(story, req, s_sb_h, s_sb_t)
    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 4 — SPLIT HEADER
# Full-width dark top band (35mm) with Name + Role + photo.
# Below: 2-column body — left 55% for experience/projects, right 45% for rest.
# ─────────────────────────────────────────────────────────────────────────────

def _build_split_header(req, buf, cfg: dict):
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    C_HDR  = _safe_hex(_bg)
    C_ACC1 = _safe_hex(_pc)
    C_ACC2 = _safe_hex(_acc)
    fn, fb, fi = _font_variants(_font)

    HDR_H = int(62 * mm)
    PAD = 16

    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=0, rightMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_HDR)
        canvas.rect(0, H_A4 - HDR_H, W_A4, HDR_H, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor('#f7f9fc'))
        canvas.rect(0, 0, W_A4, H_A4 - HDR_H, fill=1, stroke=0)
        canvas.setFillColor(C_ACC2)
        canvas.rect(0, 0, W_A4, 3, fill=1, stroke=0)
        canvas.restoreState()

    f_hdr   = Frame(PAD, H_A4 - HDR_H, W_A4 - 2*PAD, HDR_H - PAD, id='hdr',
                    leftPadding=PAD, rightPadding=PAD,
                    topPadding=PAD, bottomPadding=0)
    LEFT_W  = int((W_A4 - 2*PAD) * 0.55)
    RIGHT_W = W_A4 - 2*PAD - LEFT_W - 8
    BODY_H  = H_A4 - HDR_H - PAD
    f_left  = Frame(PAD, PAD, LEFT_W, BODY_H, id='left',
                    leftPadding=0, rightPadding=6,
                    topPadding=PAD, bottomPadding=0)
    f_right = Frame(PAD + LEFT_W + 8, PAD, RIGHT_W, BODY_H, id='right',
                    leftPadding=6, rightPadding=0,
                    topPadding=PAD, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='P',
                                       frames=[f_hdr, f_left, f_right],
                                       onPage=draw_bg)])

    s_name   = ParagraphStyle('N',  fontName=fb, fontSize=28, textColor=colors.white,
                               leading=32, spaceAfter=3)
    s_role   = ParagraphStyle('R',  fontName=fn, fontSize=12, textColor=C_ACC2,
                               spaceAfter=4)
    s_info   = ParagraphStyle('I',  fontName=fn, fontSize=8.5,
                               textColor=colors.HexColor('#a0aec0'), leading=13)
    s_h2     = ParagraphStyle('H2', fontName=fb, fontSize=12,
                               textColor=C_ACC1, spaceBefore=16, spaceAfter=5,
                               textTransform='uppercase', letterSpacing=1.2)
    C_TEXT   = colors.HexColor('#2d3748')
    s_body   = ParagraphStyle('B',  fontName=fn, fontSize=9.5, textColor=C_TEXT,
                               leading=14, spaceAfter=5)
    s_bullet = ParagraphStyle('Bul', parent=s_body, leftIndent=14, bulletIndent=4,
                               spaceBefore=1, spaceAfter=3)
    s_title  = ParagraphStyle('T',  fontName=fb, fontSize=10.5, textColor=C_TEXT,
                               spaceBefore=8, spaceAfter=2)
    s_meta   = ParagraphStyle('M',  fontName=fn, fontSize=8.5,
                               textColor=colors.HexColor('#718096'), spaceAfter=4)
    s_sb_h   = ParagraphStyle('SH', fontName=fb, fontSize=8, textColor=C_ACC1,
                               spaceBefore=14, spaceAfter=4,
                               textTransform='uppercase', letterSpacing=1)
    s_sb_t   = ParagraphStyle('ST', fontName=fn, fontSize=8.5, textColor=C_TEXT,
                               leading=13, spaceAfter=2)

    story = []

    # ── HEADER FRAME ─────────────────────────────────────────────────────────
    pp = _process_photo(req.FILES.get('photo'))
    if pp:
            story.append(RLImage(pp, width=38*mm, height=38*mm, hAlign='RIGHT'))

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))
    contacts = " · ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'),
        req.POST.get('location'), req.POST.get('linkedin'),
        req.POST.get('github'),
    ]))
    if contacts:
        story.append(Paragraph(contacts, s_info))

    story.append(FrameBreak())

    # ── LEFT BODY ────────────────────────────────────────────────────────────
    if req.POST.get('about_me'):
        story.append(Paragraph("PROFILE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_ACC1, spaceAfter=5))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_ACC1, spaceAfter=5))
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("PROJECTS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_ACC1, spaceAfter=5))
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    story.append(FrameBreak())

    # ── RIGHT BODY ───────────────────────────────────────────────────────────
    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("SKILLS", s_sb_h))
        _render_skill_groups(story, skill_groups, s_sb_t)

    _render_edu_certs_lang(story, req, s_sb_h, s_sb_t)
    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 5 — TIMELINE MODERN
# Single-column. A vertical accent line runs down the left margin.
# Each experience block gets a dot on the timeline.
# ─────────────────────────────────────────────────────────────────────────────

class _TimelineDot(Flowable):
    """Draws a small circle dot for the timeline column."""
    def __init__(self, color, size=6):
        Flowable.__init__(self)
        self.color = color
        self.size  = size
        self.width = size
        self.height = size

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.circle(self.size/2, self.size/2, self.size/2, fill=1, stroke=0)


def _build_timeline_modern(req, buf, cfg: dict):
    pp = None  # layout does not render photo
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    C_BG   = _safe_hex(_bg)
    C_LINE = _safe_hex(_pc)
    C_ACC  = _safe_hex(_acc)
    fn, fb, fi = _font_variants(_font)

    TL_X = int(18 * mm)   # x-position of timeline line
    M_L  = int(28 * mm)   # left margin (content starts after timeline)
    M_R  = int(18 * mm)
    M_T  = int(18 * mm)
    M_B  = int(18 * mm)

    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=0, rightMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W_A4, H_A4, fill=1, stroke=0)
        # Vertical timeline bar (left side)
        canvas.setFillColor(C_LINE)
        canvas.rect(TL_X - 1, M_B, 2, H_A4 - M_T - M_B, fill=1, stroke=0)
        canvas.restoreState()

    frame = Frame(M_L, M_B, W_A4 - M_L - M_R, H_A4 - M_T - M_B, id='main',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='P', frames=[frame], onPage=draw_bg)])

    C_TEXT  = colors.HexColor('#1e293b')
    C_MUTED = colors.HexColor('#64748b')

    s_name   = ParagraphStyle('N',  fontName=fb, fontSize=26, textColor=C_LINE,
                               leading=30, spaceAfter=2)
    s_role   = ParagraphStyle('R',  fontName=fi, fontSize=12, textColor=C_TEXT,
                               spaceAfter=6)
    s_info   = ParagraphStyle('I',  fontName=fn, fontSize=8.5, textColor=C_MUTED,
                               leading=13, spaceAfter=10)
    s_h2     = ParagraphStyle('H2', fontName=fb, fontSize=12, textColor=C_LINE,
                               spaceBefore=16, spaceAfter=4,
                               textTransform='uppercase', letterSpacing=1.5)
    s_body   = ParagraphStyle('B',  fontName=fn, fontSize=9.5, textColor=C_TEXT,
                               leading=14, spaceAfter=5)
    s_bullet = ParagraphStyle('Bul', parent=s_body, leftIndent=14, bulletIndent=4,
                               spaceBefore=1, spaceAfter=3)
    s_title  = ParagraphStyle('T',  fontName=fb, fontSize=10.5, textColor=C_TEXT,
                               spaceBefore=8, spaceAfter=2)
    s_meta   = ParagraphStyle('M',  fontName=fn, fontSize=8.5, textColor=C_MUTED,
                               spaceAfter=4)

    story = []

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))
    contacts = " · ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'),
        req.POST.get('location'), req.POST.get('linkedin'),
        req.POST.get('github'),
    ]))
    if contacts:
        story.append(Paragraph(contacts, s_info))

    pp = _process_photo(req.FILES.get('photo'))
    if pp:
            story.append(RLImage(pp, width=26*mm, height=26*mm))
            story.append(Spacer(1, 8))

    if req.POST.get('about_me'):
        story.append(Paragraph("PROFILE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_ACC, spaceAfter=5))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_ACC, spaceAfter=5))
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("PROJECTS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_ACC, spaceAfter=5))
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("SKILLS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_ACC, spaceAfter=5))
        _render_skill_groups(story, skill_groups, s_body)

    _render_edu_certs_lang(story, req, s_h2, s_body, hr_color=C_ACC)
    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 6 — TWO COLUMN EQUAL
# Exactly 50/50 split. Left: name/summary/experience. Right: skills/edu/certs.
# ─────────────────────────────────────────────────────────────────────────────

def _build_two_column_equal(req, buf, cfg: dict):
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    C_BG   = _safe_hex(_bg)
    C_ACC  = _safe_hex(_pc)
    C_RULE = _safe_hex(_acc)
    fn, fb, fi = _font_variants(_font)

    COL_W = W_A4 / 2
    PAD   = 16
    M     = int(12 * mm)

    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=0, rightMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W_A4, H_A4, fill=1, stroke=0)
        # Accent top bar
        canvas.setFillColor(C_ACC)
        canvas.rect(0, H_A4 - 4, W_A4, 4, fill=1, stroke=0)
        # Mid separator
        canvas.setStrokeColor(C_RULE)
        canvas.setLineWidth(0.75)
        canvas.line(COL_W, M, COL_W, H_A4 - M)
        canvas.restoreState()

    f_left  = Frame(0, M, COL_W, H_A4 - 2*M, id='left',
                    leftPadding=PAD, rightPadding=PAD//2,
                    topPadding=PAD, bottomPadding=0)
    f_right = Frame(COL_W, M, COL_W, H_A4 - 2*M, id='right',
                    leftPadding=PAD//2, rightPadding=PAD,
                    topPadding=PAD, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='P',
                                       frames=[f_left, f_right],
                                       onPage=draw_bg)])

    C_TEXT  = colors.HexColor('#1a2e1a') if '#f' in _bg else colors.HexColor('#1e293b')
    C_MUTED = colors.HexColor('#4b5563')

    s_name   = ParagraphStyle('N',  fontName=fb, fontSize=22, textColor=C_ACC,
                               leading=26, spaceAfter=2)
    s_role   = ParagraphStyle('R',  fontName=fi, fontSize=11, textColor=C_TEXT,
                               spaceAfter=8)
    s_info   = ParagraphStyle('I',  fontName=fn, fontSize=8, textColor=C_MUTED,
                               leading=12, spaceAfter=8)
    s_h2     = ParagraphStyle('H2', fontName=fb, fontSize=12, textColor=C_ACC,
                               spaceBefore=16, spaceAfter=5,
                               textTransform='uppercase', letterSpacing=1.5)
    s_body   = ParagraphStyle('B',  fontName=fn, fontSize=9, textColor=C_TEXT,
                               leading=13, spaceAfter=5)
    s_bullet = ParagraphStyle('Bul', parent=s_body, leftIndent=12, bulletIndent=4,
                               spaceBefore=1, spaceAfter=3)
    s_title  = ParagraphStyle('T',  fontName=fb, fontSize=10, textColor=C_TEXT,
                               spaceBefore=8, spaceAfter=2)
    s_meta   = ParagraphStyle('M',  fontName=fn, fontSize=8, textColor=C_MUTED,
                               spaceAfter=4)

    story = []

    # ── LEFT COLUMN ──────────────────────────────────────────────────────────
    pp = _process_photo(req.FILES.get('photo'))
    if pp:
            story.append(RLImage(pp, width=26*mm, height=26*mm))
            story.append(Spacer(1, 6))

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))
    contacts = " · ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'), req.POST.get('location'),
        req.POST.get('linkedin'), req.POST.get('github'),
    ]))
    if contacts:
        story.append(Paragraph(contacts, s_info))

    if req.POST.get('about_me'):
        story.append(Paragraph("PROFILE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_RULE, spaceAfter=5))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_RULE, spaceAfter=5))
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("PROJECTS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_RULE, spaceAfter=5))
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    story.append(FrameBreak())

    # ── RIGHT COLUMN ─────────────────────────────────────────────────────────
    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("SKILLS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_RULE, spaceAfter=5))
        _render_skill_groups(story, skill_groups, s_body)

    _render_edu_certs_lang(story, req, s_h2, s_body, hr_color=C_RULE)
    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 7 — HACKER TERMINAL
# Black background, green monospace font. No circular photo (renders flat).
# Uses a custom ASCII-art style header box.
# ─────────────────────────────────────────────────────────────────────────────

def _build_hacker_terminal(req, buf, cfg: dict):
    pp = None  # layout does not render photo
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    C_BG   = _safe_hex(_bg)
    C_GRN  = _safe_hex(_pc)
    C_DIM  = _safe_hex(_acc)
    fn, fb, fi = 'Courier', 'Courier-Bold', 'Courier-Oblique'

    M = int(14 * mm)
    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=M, rightMargin=M,
                          topMargin=M, bottomMargin=M)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W_A4, H_A4, fill=1, stroke=0)
        canvas.restoreState()

    frame = Frame(M, M, W_A4 - 2*M, H_A4 - 2*M, id='main',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='P', frames=[frame], onPage=draw_bg)])

    s_prompt = ParagraphStyle('P',  fontName=fb, fontSize=11, textColor=C_GRN,
                               leading=14, spaceAfter=2)
    s_cmd    = ParagraphStyle('C',  fontName=fb, fontSize=14, textColor=C_GRN,
                               leading=17, spaceAfter=6)
    s_role   = ParagraphStyle('R',  fontName=fn, fontSize=10, textColor=C_DIM,
                               spaceAfter=10)
    s_h2     = ParagraphStyle('H2', fontName=fb, fontSize=10, textColor=C_GRN,
                               spaceBefore=16, spaceAfter=4)
    s_body   = ParagraphStyle('B',  fontName=fn, fontSize=9, textColor=C_GRN,
                               leading=13, spaceAfter=4)
    s_bullet = ParagraphStyle('Bul', parent=s_body, leftIndent=16, bulletIndent=4,
                               spaceBefore=1, spaceAfter=2)
    s_title  = ParagraphStyle('T',  fontName=fb, fontSize=10, textColor=C_GRN,
                               spaceBefore=8, spaceAfter=2)
    s_meta   = ParagraphStyle('M',  fontName=fn, fontSize=8.5, textColor=C_DIM,
                               spaceAfter=3)
    s_dim    = ParagraphStyle('D',  fontName=fn, fontSize=8.5, textColor=C_DIM,
                               leading=12, spaceAfter=6)

    story = []

    name = req.POST.get('full_name', 'User')
    role = req.POST.get('target_role', 'Developer')
    story.append(Paragraph("$ whoami", s_prompt))
    story.append(Paragraph(name.upper(), s_cmd))
    story.append(Paragraph(f"// {role}", s_role))

    contacts = "  ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'),
        req.POST.get('location'), req.POST.get('linkedin'),
        req.POST.get('github'),
    ]))
    if contacts:
        story.append(Paragraph(f"$ contact  {contacts}", s_dim))

    # A real rule, not a row of box-drawing characters. U+2500 is absent from
    # Courier's WinAnsi encoding, so "─" * 72 rendered — and extracted — as
    # seventy-two literal letter n's across the page.
    story.append(HRFlowable(width="100%", thickness=0.6, color=C_DIM, spaceAfter=6))

    if req.POST.get('about_me'):
        story.append(Paragraph("$ cat about.txt", s_h2))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("$ git log --experience", s_h2))
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("$ ls ~/projects/", s_h2))
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("$ pip list --installed", s_h2))
        _render_skill_groups(story, skill_groups, s_body)

    edu = req.POST.get('education', '').strip()
    if edu:
        story.append(Paragraph("$ cat education.txt", s_h2))
        for line in edu.split('\n'):
            markup = _education_markup(line.strip())
            if markup:
                story.append(Paragraph(markup, s_body))

    # Certifications and languages were the only two sections this template
    # never read. A user who filled them in got a PDF with them silently
    # absent — no warning, and nothing on screen to suggest the template was
    # the reason. Every other template renders both.
    certs = req.POST.get('certifications', '').strip()
    if certs:
        story.append(Paragraph("$ cat certifications.txt", s_h2))
        for line in certs.replace(',', '\n').split('\n'):
            if line.strip():
                story.append(Paragraph(_esc(line.strip()), s_body))

    lang = req.POST.get('languages', '').strip()
    if lang:
        story.append(Paragraph("$ locale -a", s_h2))
        story.append(Paragraph(_esc(lang), s_body))

    port = req.POST.get('portfolio_url', '').strip()
    if port:
        story.append(Paragraph(f"$ open {_esc(port)}", s_dim))

    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 8 — ACADEMIC CLASSIC
# Ultra-dense single-column. Name centred in CAPS. Strict full-width HR rules
# above every section. No decoration. ATS-optimised.
# ─────────────────────────────────────────────────────────────────────────────

def _build_academic_classic(req, buf, cfg: dict):
    pp = None  # layout does not render photo
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    fn, fb, fi = _font_variants(_font)

    M = int(20 * mm)
    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=M, rightMargin=M,
                          topMargin=M, bottomMargin=M)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, W_A4, H_A4, fill=1, stroke=0)
        canvas.restoreState()

    frame = Frame(M, M, W_A4 - 2*M, H_A4 - 2*M, id='main',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='P', frames=[frame], onPage=draw_bg)])

    C_BLACK = colors.HexColor('#0a0a0a')
    C_GREY  = colors.HexColor('#333333')

    s_name   = ParagraphStyle('N',  fontName=fb, fontSize=18, textColor=C_BLACK,
                               leading=22, spaceAfter=1, alignment=1)
    s_role   = ParagraphStyle('R',  fontName=fn, fontSize=11, textColor=C_GREY,
                               spaceAfter=3, alignment=1)
    s_info   = ParagraphStyle('I',  fontName=fn, fontSize=9, textColor=C_GREY,
                               leading=13, spaceAfter=6, alignment=1)
    s_h2     = ParagraphStyle('H2', fontName=fb, fontSize=12, textColor=C_BLACK,
                               spaceBefore=16, spaceAfter=2,
                               textTransform='uppercase')
    s_body   = ParagraphStyle('B',  fontName=fn, fontSize=9.5, textColor=C_BLACK,
                               leading=14, spaceAfter=4)
    s_bullet = ParagraphStyle('Bul', parent=s_body, leftIndent=16, bulletIndent=5,
                               spaceBefore=1, spaceAfter=2)
    s_title  = ParagraphStyle('T',  fontName=fb, fontSize=10, textColor=C_BLACK,
                               spaceBefore=6, spaceAfter=1)
    s_meta   = ParagraphStyle('M',  fontName=fi, fontSize=9, textColor=C_GREY,
                               spaceAfter=3)

    HR = lambda: HRFlowable(width="100%", thickness=0.5,
                            color=colors.HexColor('#555555'), spaceAfter=5)

    story = []

    story.append(Paragraph(req.POST.get('full_name', 'Your Name').upper(), s_name))
    story.append(Paragraph(req.POST.get('target_role', ''), s_role))
    contacts = " | ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'),
        req.POST.get('location'), req.POST.get('linkedin'),
        req.POST.get('github'),
    ]))
    if contacts:
        story.append(Paragraph(contacts, s_info))
    story.append(HRFlowable(width="100%", thickness=1.2, color=C_BLACK, spaceAfter=4))

    if req.POST.get('about_me'):
        story.append(Paragraph("OBJECTIVE / SUMMARY", s_h2))
        story.append(HR())
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("PROFESSIONAL EXPERIENCE", s_h2))
        story.append(HR())
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("RESEARCH / PROJECTS", s_h2))
        story.append(HR())
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("SKILLS &amp; COMPETENCIES", s_h2))
        story.append(HR())
        _render_skill_groups(story, skill_groups, s_body)

    edu = req.POST.get('education', '').strip()
    if edu:
        story.append(Paragraph("EDUCATION", s_h2))
        story.append(HR())
        for line in edu.split('\n'):
            markup = _education_markup(line.strip())
            if markup:
                story.append(Paragraph(markup, s_body))

    certs = req.POST.get('certifications', '').strip()
    if certs:
        story.append(Paragraph("CERTIFICATIONS &amp; AWARDS", s_h2))
        story.append(HR())
        for line in certs.replace(',', '\n').split('\n'):
            if line.strip():
                story.append(Paragraph(f"· {_esc(line.strip())}", s_body))

    lang = req.POST.get('languages', '').strip()
    if lang:
        story.append(Paragraph("LANGUAGES", s_h2))
        story.append(HR())
        story.append(Paragraph(lang, s_body))

    port = req.POST.get('portfolio_url', '').strip()
    if port:
        story.append(Paragraph("PORTFOLIO / PUBLICATIONS", s_h2))
        story.append(HR())
        story.append(Paragraph(f'<link href="{port}">{port}</link>', s_body))

    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 9 — TOP/BOTTOM SPLIT
# ~30% coloured header panel at top (Name, Role, Photo, Contacts).
# ~70% white body below for Experience, Projects, Skills, Education.
# ─────────────────────────────────────────────────────────────────────────────

def _build_top_bottom_split(req, buf, cfg: dict):
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    C_TOP  = _safe_hex(_bg)
    C_ACC  = _safe_hex(_pc)
    C_PALE = _safe_hex(_acc)
    fn, fb, fi = _font_variants(_font)

    # The coloured band was 30% of the page while its contents — name, role and
    # one contact line — need barely a third of that. The other 70% had to carry
    # the whole resume, and a complete one spilled onto page 2 while the header
    # sat two-thirds empty. 23% still reads as a bold top band and gives the
    # body back roughly six lines.
    TOP_H = int(H_A4 * 0.23)
    M     = int(16 * mm)

    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=0, rightMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_TOP)
        canvas.rect(0, H_A4 - TOP_H, W_A4, TOP_H, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, W_A4, H_A4 - TOP_H, fill=1, stroke=0)
        # Accent separator line
        canvas.setFillColor(C_ACC)
        canvas.rect(0, H_A4 - TOP_H - 4, W_A4, 4, fill=1, stroke=0)
        canvas.restoreState()

    f_top  = Frame(M, H_A4 - TOP_H, W_A4 - 2*M, TOP_H - M//2, id='top',
                   leftPadding=0, rightPadding=0,
                   topPadding=M, bottomPadding=0)
    f_body = Frame(M, M, W_A4 - 2*M, H_A4 - TOP_H - M*2, id='body',
                   leftPadding=0, rightPadding=0,
                   topPadding=M//2, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='P',
                                       frames=[f_top, f_body],
                                       onPage=draw_bg)])

    C_TEXT  = colors.HexColor('#1e293b')
    C_MUTED = colors.HexColor('#64748b')

    s_name   = ParagraphStyle('N',  fontName=fb, fontSize=28, textColor=colors.white,
                               leading=32, spaceAfter=2)
    s_role   = ParagraphStyle('R',  fontName=fn, fontSize=12, textColor=C_PALE,
                               spaceAfter=6)
    s_info   = ParagraphStyle('I',  fontName=fn, fontSize=8.5,
                               textColor=colors.HexColor('#a0c4d8'), leading=13)
    s_h2     = ParagraphStyle('H2', fontName=fb, fontSize=12, textColor=C_ACC,
                               spaceBefore=10, spaceAfter=4,
                               textTransform='uppercase', letterSpacing=1.2)
    s_body   = ParagraphStyle('B',  fontName=fn, fontSize=9.5, textColor=C_TEXT,
                               leading=13, spaceAfter=4)
    s_bullet = ParagraphStyle('Bul', parent=s_body, leftIndent=14, bulletIndent=4,
                               spaceBefore=1, spaceAfter=3)
    s_title  = ParagraphStyle('T',  fontName=fb, fontSize=10.5, textColor=C_TEXT,
                               spaceBefore=8, spaceAfter=2)
    s_meta   = ParagraphStyle('M',  fontName=fn, fontSize=8.5, textColor=C_MUTED,
                               spaceAfter=4)

    story = []

    # ── TOP PANEL ────────────────────────────────────────────────────────────
    pp = _process_photo(req.FILES.get('photo'))
    if pp:
            story.append(RLImage(pp, width=36*mm, height=36*mm, hAlign='RIGHT'))

    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Professional'), s_role))
    contacts = " · ".join(filter(None, [
        req.POST.get('email'), req.POST.get('phone'),
        req.POST.get('location'), req.POST.get('linkedin'),
        req.POST.get('github'),
    ]))
    if contacts:
        story.append(Paragraph(contacts, s_info))

    story.append(FrameBreak())

    # ── BODY ─────────────────────────────────────────────────────────────────
    if req.POST.get('about_me'):
        story.append(Paragraph("SUMMARY", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_PALE, spaceAfter=5))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_PALE, spaceAfter=5))
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("PROJECTS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_PALE, spaceAfter=5))
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("SKILLS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_PALE, spaceAfter=5))
        _render_skill_groups(story, skill_groups, s_body)

    _render_edu_certs_lang(story, req, s_h2, s_body, hr_color=C_PALE)
    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT 10 — CREATIVE MASONRY
# Dark full-bleed background. Asymmetric: 62% left for main text, 38% right
# for creative skill/contact cards. Bold coloured name on left.
# ─────────────────────────────────────────────────────────────────────────────

def _build_creative_masonry(req, buf, cfg: dict):
    _pc, _bg, _acc, _font = _post_colors(req, cfg)
    C_BG   = _safe_hex(_bg)
    C_ACC  = _safe_hex(_pc)
    C_SEC  = _safe_hex(_acc)
    fn, fb, fi = _font_variants(_font)

    MAIN_W = int(W_A4 * 0.62)
    SIDE_W = W_A4 - MAIN_W
    M      = int(14 * mm)
    PAD    = 14

    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=0, rightMargin=0,
                          topMargin=0, bottomMargin=0)

    def draw_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W_A4, H_A4, fill=1, stroke=0)
        # Right panel slightly lighter
        canvas.setFillColor(colors.HexColor('#16161f'))
        canvas.rect(MAIN_W, 0, SIDE_W, H_A4, fill=1, stroke=0)
        # Top accent stripe
        canvas.setFillColor(C_ACC)
        canvas.rect(0, H_A4 - 5, W_A4, 5, fill=1, stroke=0)
        # Left margin accent strip
        canvas.setFillColor(C_ACC)
        canvas.rect(0, 0, 4, H_A4, fill=1, stroke=0)
        canvas.restoreState()

    f_main = Frame(8, M, MAIN_W - 8, H_A4 - 2*M, id='main',
                   leftPadding=PAD, rightPadding=PAD,
                   topPadding=PAD, bottomPadding=0)
    f_side = Frame(MAIN_W, M, SIDE_W, H_A4 - 2*M, id='side',
                   leftPadding=PAD, rightPadding=PAD,
                   topPadding=PAD, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='P',
                                       frames=[f_main, f_side],
                                       onPage=draw_bg)])

    C_W    = colors.white
    C_DIM  = colors.HexColor('#9ca3af')
    C_CARD = colors.HexColor('#1f1f2e')

    s_name   = ParagraphStyle('N',  fontName=fb, fontSize=28, textColor=C_ACC,
                               leading=32, spaceAfter=2)
    s_role   = ParagraphStyle('R',  fontName=fi, fontSize=12, textColor=C_SEC,
                               spaceAfter=10)
    s_h2     = ParagraphStyle('H2', fontName=fb, fontSize=12, textColor=C_ACC,
                               spaceBefore=16, spaceAfter=4,
                               textTransform='uppercase', letterSpacing=2)
    s_body   = ParagraphStyle('B',  fontName=fn, fontSize=9.5, textColor=C_W,
                               leading=14, spaceAfter=5)
    s_bullet = ParagraphStyle('Bul', parent=s_body, leftIndent=14, bulletIndent=4,
                               spaceBefore=1, spaceAfter=3)
    s_title  = ParagraphStyle('T',  fontName=fb, fontSize=10.5, textColor=C_W,
                               spaceBefore=8, spaceAfter=2)
    s_meta   = ParagraphStyle('M',  fontName=fn, fontSize=8.5, textColor=C_DIM,
                               spaceAfter=4)
    s_sb_h   = ParagraphStyle('SH', fontName=fb, fontSize=8, textColor=C_ACC,
                               spaceBefore=14, spaceAfter=4,
                               textTransform='uppercase', letterSpacing=1.2)
    s_sb_t   = ParagraphStyle('ST', fontName=fn, fontSize=8.5, textColor=C_W,
                               leading=13, spaceAfter=3)
    s_dim    = ParagraphStyle('D',  fontName=fn, fontSize=7.5, textColor=C_DIM,
                               leading=11)

    story = []

    # ── MAIN LEFT ────────────────────────────────────────────────────────────
    story.append(Paragraph(req.POST.get('full_name', 'Your Name'), s_name))
    story.append(Paragraph(req.POST.get('target_role', 'Creative Professional'), s_role))

    if req.POST.get('about_me'):
        story.append(Paragraph("ABOUT", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_SEC, spaceAfter=5))
        story.append(Paragraph(req.POST.get('about_me'), s_body))

    exp = req.POST.get('experience_text', '').strip()
    if exp:
        story.append(Paragraph("EXPERIENCE", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_SEC, spaceAfter=5))
        _render_text_block(story, exp, s_title, s_meta, s_bullet, s_body)

    proj = req.POST.get('projects_text', '').strip()
    if proj:
        story.append(Paragraph("PROJECTS", s_h2))
        story.append(HRFlowable(width="100%", thickness=0.5, color=C_SEC, spaceAfter=5))
        _render_text_block(story, proj, s_title, s_meta, s_bullet, s_body)

    story.append(FrameBreak())

    # ── SIDE RIGHT ───────────────────────────────────────────────────────────
    pp = _process_photo(req.FILES.get('photo'))
    if pp:
            story.append(RLImage(pp, width=30*mm, height=30*mm, hAlign='CENTER'))
            story.append(Spacer(1, 8))

    for lbl, val in [("Location", req.POST.get('location')),
                     ("Email",    req.POST.get('email')),
                     ("Phone",    req.POST.get('phone')),
                     ("LinkedIn", req.POST.get('linkedin')),
                     ("GitHub",   req.POST.get('github'))]:
        if val:
            story.append(Paragraph(lbl.upper(), s_sb_h))
            story.append(Paragraph(val, s_sb_t))

    skill_groups = _parse_skill_groups(req.POST.get('skills_list', ''))
    if skill_groups:
        story.append(Paragraph("SKILLS", s_sb_h))
        _render_skill_groups(story, skill_groups, s_sb_t)

    _render_edu_certs_lang(story, req, s_sb_h, s_sb_t)
    try:
        doc.build(story)
    finally:
        if pp and os.path.exists(pp):
            os.unlink(pp)


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT DISPATCH MAP
# ─────────────────────────────────────────────────────────────────────────────

_LAYOUT_BUILDERS = {
    "minimal_centered":    _build_minimal_centered,
    "left_sidebar_dark":   _build_left_sidebar_dark,
    "right_sidebar_light": _build_right_sidebar_light,
    "split_header":        _build_split_header,
    "timeline_modern":     _build_timeline_modern,
    "two_column_equal":    _build_two_column_equal,
    "hacker_terminal":     _build_hacker_terminal,
    "academic_classic":    _build_academic_classic,
    "top_bottom_split":    _build_top_bottom_split,
    "creative_masonry":    _build_creative_masonry,
}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def build_pdf(template_slug: str, request) -> io.BytesIO:
    """
    Build and return a BytesIO PDF for the given template slug.
    Dispatches to the correct layout builder; falls back to minimal_centered
    on any unrecoverable error.
    """
    buf = io.BytesIO()
    cfg = get_template_by_slug(template_slug)
    layout_key = cfg.get("layout", "minimal_centered")
    builder = _LAYOUT_BUILDERS.get(layout_key, _build_minimal_centered)
    try:
        builder(request, buf, cfg)
    except Exception as exc:
        logger.exception("PDF build failed slug=%s layout=%s: %s",
                         template_slug, layout_key, exc)
        buf = io.BytesIO()
        try:
            _build_minimal_centered(request, buf, TEMPLATES[0])
        except Exception:
            pass
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────────────
# COVER LETTER
#
# Separate from the resume builders on purpose. A cover letter is a business
# letter — sender block, date, recipient, salutation, prose, sign-off — not a
# resume with different content, so none of the ten layout builders fit it and
# reusing one would have produced a letter shaped like a CV.
# ─────────────────────────────────────────────────────────────────────────────

def build_cover_letter_pdf(request) -> io.BytesIO:
    """Render a cover letter to PDF from the posted fields.

    Reads: full_name, email, phone, location, company_name, job_title, body.
    `body` is the letter prose; any [SECTION: …] tags are stripped by the
    caller, since only the letter itself belongs in the file.
    """
    buf = io.BytesIO()

    name    = (request.POST.get('full_name') or '').strip()
    email   = (request.POST.get('email') or '').strip()
    phone   = (request.POST.get('phone') or '').strip()
    location = (request.POST.get('location') or '').strip()
    company = (request.POST.get('company_name') or '').strip()
    role    = (request.POST.get('job_title') or '').strip()
    body    = (request.POST.get('body') or '').strip()

    fn, fb, fi = _font_variants('Helvetica')
    M = 24 * mm

    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=M, rightMargin=M, topMargin=M, bottomMargin=M,
                          title=f"Cover letter — {name or 'Candidate'}",
                          author=name or 'Candidate')
    frame = Frame(M, M, W_A4 - 2 * M, H_A4 - 2 * M, id='letter',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id='L', frames=[frame])])

    C_INK = colors.HexColor('#1a1a2e')
    C_DIM = colors.HexColor('#6b7280')

    s_name = ParagraphStyle('N', fontName=fb, fontSize=16, textColor=C_INK,
                            leading=19, spaceAfter=2)
    s_meta = ParagraphStyle('M', fontName=fn, fontSize=9, textColor=C_DIM,
                            leading=13, spaceAfter=2)
    s_to   = ParagraphStyle('T', fontName=fn, fontSize=10, textColor=C_INK,
                            leading=14, spaceAfter=2)
    # 1.5 leading and a blank line between paragraphs: this is read on screen by
    # a person, not parsed, so it is set for reading rather than for density.
    s_body = ParagraphStyle('B', fontName=fn, fontSize=10.5, textColor=C_INK,
                            leading=15.75, spaceAfter=10)

    story = []

    if name:
        story.append(Paragraph(_esc(name), s_name))
    contact = ' · '.join(_esc(v) for v in (email, phone, location) if v)
    if contact:
        story.append(Paragraph(contact, s_meta))

    story.append(Spacer(1, 14))
    story.append(HRFlowable(width='100%', thickness=0.6,
                            color=colors.HexColor('#d8dee9'), spaceAfter=14))

    story.append(Paragraph(datetime.date.today().strftime('%d %B %Y'), s_meta))
    story.append(Spacer(1, 10))

    if company or role:
        if company:
            story.append(Paragraph(f'<b>{_esc(company)}</b>', s_to))
        if role:
            story.append(Paragraph(_esc(role), s_to))
        story.append(Spacer(1, 14))

    for para in re.split(r'\n\s*\n', body):
        para = para.strip()
        if not para:
            continue
        # Single newlines inside a paragraph are soft wraps in the model's
        # output, not deliberate breaks — joining them avoids a letter that
        # looks like it was pasted out of a terminal.
        story.append(Paragraph(_esc(' '.join(para.split('\n'))), s_body))

    if not body:
        story.append(Paragraph('This letter is empty.', s_body))

    doc.build(story)
    buf.seek(0)
    return buf
