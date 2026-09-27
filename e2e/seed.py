"""
Demo data for the browser checks (e2e/run.js). Never run against production:
it refuses any database that is not SQLite or on localhost.

Users (password demo-pass-123): demo (Pro, full history), freebie (free plan),
ops (staff, for the AI-cost page), leaver (deletes their account in a check).
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aigen.settings")

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402

if connection.vendor != "sqlite" and connection.settings_dict.get("HOST") not in ("localhost", "127.0.0.1"):
    sys.exit("Refusing to seed a database that is not local")

from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402

from generator.models import AIResult, AIUsageDay, Generation, JobApplication  # noqa: E402
from users.models import Profile  # noqa: E402

User.objects.filter(username__in=["demo", "freebie", "ops", "leaver"]).delete()
AIUsageDay.objects.all().delete()
demo = User.objects.create_user("demo", email="demo@example.com", password="demo-pass-123",
                                first_name="Maria", last_name="Keller")
Profile.objects.filter(user=demo).update(plan="pro", is_premium=True, email_verified=True,
                                         monthly_usage=13, base_resume="Maria Keller — Backend Engineer…")
free = User.objects.create_user("freebie", email="free@example.com", password="demo-pass-123")
Profile.objects.filter(user=free).update(email_verified=True, generations_count=2)

resume = {
    "full_name": "Maria Keller", "target_role": "Backend Engineer", "email": "maria@example.com",
    "phone": "+49 30 1234", "location": "Berlin", "linkedin": "", "github": "",
    "summary": "Backend engineer building payment APIs in Python and Django.",
    "experience": [{"title": "Senior Backend Engineer", "company": "Northwind", "location": "Berlin",
                    "dates": "2021 – Present", "bullets": ["Cut p95 latency from 900ms to 210ms",
                                                           "Built the invoicing API for 40 merchants"]}],
    "projects": [], "skills": [{"category": "Backend", "items": ["Python", "Django", "PostgreSQL"]}],
    "education": [{"degree": "BSc Computer Science", "school": "TU Berlin", "dates": "2015 – 2019"}],
    "languages": ["English", "German"],
}
Generation.objects.create(user=demo, resume_text="…", job_description="[AI Resume Studio]",
                          result=json.dumps(resume))
Generation.objects.create(user=demo, resume_text="…", job_description="Backend role at Stripe",
                          company_name="Stripe", job_title="Backend Engineer",
                          result="[SECTION: MAIN_LETTER]Dear Stripe team,\n\nI build payment APIs.[END_SECTION]")
Generation.objects.create(user=demo, resume_text="Did backend work", job_description="[Section Rewrite — summary]",
                          result="Backend engineer who cut payment latency by 77%.")
for company, title, status in [("Stripe", "Backend Engineer", "interview"), ("Linear", "Platform Engineer", "applied"),
                               ("Vercel", "API Engineer", "saved"), ("Notion", "Senior Engineer", "offer"),
                               ("Figma", "Backend Engineer", "rejected"), ("Monzo", "Payments Engineer", "applied")]:
    JobApplication.objects.create(user=demo, company_name=company, job_title=title, status=status,
                                  job_url="https://jobs.example/" + company.lower(), salary_range="€80–100k",
                                  notes="Referral from a friend")
AIResult.objects.create(user=demo, result_type="ats", input_summary="ATS Score Check", score=72,
                        result="ATS COMPATIBILITY SCORE: 72/100\n\n| Matched | Missing |\n|---|---|\n"
                               "| Python | Kubernetes |\n| Django | Terraform |\n\n**Top improvements**\n- Add Kubernetes")
AIResult.objects.create(user=demo, result_type="interview", input_summary="Stripe — Interview Prep",
                        result="### Q1: Tell me about a latency win\n**Why they ask:** Payments are latency-sensitive.\n"
                               "**Strong answer:** I cut p95 from 900ms to 210ms.\n**Tip:** Lead with the number.")
AIResult.objects.create(user=demo, result_type="followup", input_summary="Linear — Follow-Up Emails",
                        result="**Subject:** Following up on my application\n\nHi team, I wanted to follow up…")
ops = User.objects.create_user("ops", email="ops@example.com", password="demo-pass-123", is_staff=True)
Profile.objects.filter(user=ops).update(email_verified=True)
today = timezone.now().date()
for back, calls in enumerate([42, 37, 51, 12]):
    AIUsageDay.objects.create(date=today - datetime.timedelta(days=back), model="claude-sonnet-5", calls=calls,
                              input_tokens=calls * 3000, output_tokens=calls * 900)
AIUsageDay.objects.create(date=today, model="claude-opus-5", calls=6, input_tokens=40000, output_tokens=9000)
leaver = User.objects.create_user("leaver", email="leaver@example.com", password="demo-pass-123")
Profile.objects.filter(user=leaver).update(email_verified=True)
print("seeded")
