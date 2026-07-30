"""Regenerate the template-gallery thumbnails from the REAL PDF engine.

The previous thumbnails were placeholders — flat rectangles with the template
name typed on them. These are page 1 of an actual PDF built by pdf_engine for
each template, so the gallery shows what the user will really get.

Re-run after changing a layout builder:
    .venv\\Scripts\\python.exe scratchpad/gen_thumbs.py
"""
import os
import sys
import pathlib

import django

ROOT = pathlib.Path(r"c:\Users\User\OneDrive\Рабочий стол\ai-letter-main")
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aigen.settings")
django.setup()

import fitz                                          # noqa: E402  (PyMuPDF)
from django.test import RequestFactory               # noqa: E402
from generator.pdf_engine import build_pdf, TEMPLATES  # noqa: E402

OUT = ROOT / "generator" / "static" / "img" / "templates"

# Generic sample candidate — illustrative placeholder content only.
SAMPLE = {
    "full_name":       "Alex Morgan",
    "target_role":     "Senior Backend Engineer",
    "email":           "alex.morgan@example.com",
    "phone":           "+1 555 0142",
    "location":        "Berlin, Germany",
    "linkedin":        "linkedin.com/in/example",
    "github":          "github.com/example",
    "portfolio_url":   "example.com",
    "about_me": (
        "Backend engineer focused on distributed systems and developer tooling. "
        "Builds services that stay simple as they scale, and enjoys turning "
        "ambiguous product requirements into clear technical plans."
    ),
    "experience_text": (
        "Senior Backend Engineer\n"
        "Northwind Systems | 2021 - Present\n"
        "- Designed the event pipeline that powers customer-facing analytics\n"
        "- Led the migration from a monolith to service boundaries\n"
        "- Mentors two engineers and runs the design review rotation\n"
        "\n"
        "Backend Engineer\n"
        "Lumen Labs | 2018 - 2021\n"
        "- Built and owned the public REST API and its client libraries\n"
        "- Cut median request latency by profiling the hot query paths\n"
    ),
    "projects_text": (
        "Ledger\n"
        "Python, PostgreSQL\n"
        "- Open-source double-entry accounting library\n"
    ),
    "skills_list":     "Python-90,Go-75,PostgreSQL-85,Kubernetes-70,gRPC-65,Redis-70",
    "education":       "BSc Computer Science — Technical University — 2014 - 2018",
    "certifications":  "Certified Kubernetes Administrator — 2022",
    "languages":       "English (Fluent), German (Conversational)",
}

rf = RequestFactory()
OUT.mkdir(parents=True, exist_ok=True)

print(f"writing {len(TEMPLATES)} thumbnails -> {OUT}\n")
for tpl in TEMPLATES:
    slug = tpl["slug"]
    request = rf.post("/download-pdf/", SAMPLE)
    request.user = None                      # builders only read POST

    pdf_buf = build_pdf(slug, request)
    doc = fitz.open(stream=pdf_buf.getvalue(), filetype="pdf")
    page = doc.load_page(0)

    # Crop the top of the page to an exact 4:3 band. A full A4 shrunk into a
    # ~180px gallery card turns the body text into unreadable grey noise; the
    # top band is where each layout is actually distinguishable — name, role,
    # contact strip, sidebar colour, first section rule — and it stays legible
    # when small. 4:3 so the CSS aspect-ratio matches the asset exactly and the
    # card never crops the sides.
    r = page.rect
    clip = fitz.Rect(r.x0, r.y0, r.x1, r.y0 + r.width * 3 / 4)

    # 3x zoom off the crop keeps it crisp on retina at card size.
    pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), clip=clip, alpha=False)
    dest = OUT / f"{slug}.jpg"
    pix.save(dest, jpg_quality=90)
    doc.close()
    print(f"  {slug:<24} {pix.width}x{pix.height}  {dest.stat().st_size/1024:6.1f} KB")

print("\ndone — run collectstatic to publish")
