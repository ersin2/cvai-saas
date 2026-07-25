"""
tests/test_suite.py - Comprehensive Django Unit Test Suite
==========================================================
Covers:
  1. Profile Model     - plan limits, generation counting, template access
  2. Generation Model  - creation, ordering, string repr
  3. JobApplication    - CRUD, status transitions, plan limits
  4. Rate Limiter      - per-plan thresholds (free/pro/elite)
  5. Auth Guard        - @login_required on all protected endpoints
  6. PDF Generation    - all 10 template slugs render cleanly
  7. PDF Parser        - magic-bytes validation (accept/reject)
  8. SSRF Guard        - private/loopback/link-local addresses blocked
  9. Tracker Views     - add / update / delete job applications
 10. Plan Enforcement  - template slug gating by plan tier

Run with:
    python manage.py test tests.test_suite -v 2

Fixes applied:
- Rate limiter: cache is cleared before every PDF subTest iteration so the
  Elite quota (50 req/min) is never exhausted across a single test method.
- StaticFiles: STORAGES is overridden to use the plain StaticFilesStorage so
  tests that render HTML templates don't need `collectstatic` to have been run.
"""

import io
import json
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from generator.models import Generation, JobApplication, AIResult
from generator.pdf_engine import build_pdf, TEMPLATES
from generator.views import (
    _check_rate_limit,
    RATE_LIMITS,
    _validate_resume_json,
    _repair_truncated_json,
    _parse_and_validate_resume,
    _truncate_on_boundary,
)
from users.models import Profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
    b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>\nstream\n"
    b"BT /F1 12 Tf 100 700 Td (test resume) Tj ET\n"
    b"endstream\nendobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"xref\n0 6\n"
    b"0000000000 65535 f\r\n"
    b"trailer<</Size 6/Root 1 0 R>>\n"
    b"startxref\n9\n%%EOF\n"
)

_RESUME_FORM = {
    "full_name":       "Test User",
    "target_role":     "Software Engineer",
    "email":           "test@example.com",
    "phone":           "+1-555-000-0000",
    "location":        "New York, NY",
    "linkedin":        "https://linkedin.com/in/testuser",
    "github":          "https://github.com/testuser",
    "about_me":        "Experienced engineer with 5 years building APIs.",
    "experience_text": (
        "Software Engineer\nAcme Corp | Jan 2020 - Present | NYC\n"
        "- Built REST APIs serving 1M requests/day\n"
    ),
    "projects_text":   "My Project\nTech: Python, Django\n- Does cool things\n",
    "skills_list":     "Python-90,Django-85,PostgreSQL-80",
    "education":       "B.Sc. CS - MIT (2015-2019)",
    "certifications":  "AWS Certified",
    "languages":       "English (Native)",
    "portfolio_url":   "https://testuser.dev",
}


def _make_user(username="testuser", password="testpass", plan="free", generations=3):
    """Create a User + Profile and return (user, profile)."""
    user = User.objects.create_user(username=username, password=password)
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.plan = plan
    profile.generations_count = generations
    profile.save()
    return user, profile


# ===========================================================================
# 1. PROFILE MODEL TESTS
# ===========================================================================

class ProfileModelTest(TestCase):

    def test_free_plan_defaults(self):
        user, profile = _make_user(plan="free", generations=3)
        self.assertEqual(profile.plan, "free")
        self.assertEqual(profile.generations_count, 3)
        self.assertTrue(profile.has_generations_left())

    def test_free_plan_exhausted(self):
        user, profile = _make_user(plan="free", generations=0)
        self.assertFalse(profile.has_generations_left())

    def test_pro_plan_unlimited(self):
        user, profile = _make_user(plan="pro")
        self.assertTrue(profile.has_generations_left())

    def test_elite_plan_unlimited(self):
        user, profile = _make_user(plan="elite")
        self.assertTrue(profile.has_generations_left())

    def test_use_generation_decrements_free(self):
        user, profile = _make_user(plan="free", generations=3)
        profile.use_generation()
        profile.refresh_from_db()
        self.assertEqual(profile.generations_count, 2)

    def test_use_generation_noop_for_pro(self):
        user, profile = _make_user(plan="pro", generations=9999)
        profile.use_generation()
        profile.refresh_from_db()
        self.assertEqual(profile.generations_count, 9999)  # unchanged

    def test_pdf_template_limit_free(self):
        user, profile = _make_user(plan="free")
        self.assertEqual(profile.get_pdf_template_limit(), 2)

    def test_pdf_template_limit_pro(self):
        user, profile = _make_user(plan="pro")
        self.assertEqual(profile.get_pdf_template_limit(), 10)

    def test_pdf_template_limit_elite(self):
        user, profile = _make_user(plan="elite")
        self.assertEqual(profile.get_pdf_template_limit(), 20)

    def test_job_tracker_limit_free(self):
        user, profile = _make_user(plan="free")
        self.assertEqual(profile.get_max_tracked_jobs(), 10)

    def test_job_tracker_limit_pro(self):
        user, profile = _make_user(plan="pro")
        self.assertEqual(profile.get_max_tracked_jobs(), 50)

    def test_job_tracker_limit_elite(self):
        user, profile = _make_user(plan="elite")
        self.assertEqual(profile.get_max_tracked_jobs(), 99999)

    def test_str_repr(self):
        user, profile = _make_user(plan="free")
        self.assertIn("testuser", str(profile))
        self.assertIn("free", str(profile))

    def test_plan_choices_valid(self):
        valid_plans = {"free", "pro", "elite"}
        for slug, label in Profile.PLAN_CHOICES:
            self.assertIn(slug, valid_plans)


# ===========================================================================
# 2. GENERATION MODEL TESTS
# ===========================================================================

class GenerationModelTest(TestCase):

    def setUp(self):
        self.user, self.profile = _make_user(plan="pro")

    def test_create_generation(self):
        gen = Generation.objects.create(
            user=self.user,
            resume_text="My resume",
            job_description="Software Engineer role",
            company_name="Acme",
            job_title="Backend Engineer",
            tone="Professional",
            language="English",
            result="[SECTION: MAIN_LETTER]\nDear Hiring Manager...[END_SECTION]",
        )
        self.assertIsNotNone(gen.pk)
        self.assertIsNotNone(gen.created_at)

    def test_ordering_latest_first(self):
        for i in range(3):
            Generation.objects.create(
                user=self.user, resume_text=f"resume {i}",
                job_description="jd", company_name=f"Co{i}",
                job_title="Eng", tone="Pro", language="English",
                result="result",
            )
        gens = list(Generation.objects.filter(user=self.user))
        self.assertGreaterEqual(gens[0].created_at, gens[-1].created_at)

    def test_str_repr(self):
        gen = Generation.objects.create(
            user=self.user, resume_text="r", job_description="jd",
            company_name="TestCo", job_title="Dev", tone="Pro",
            language="English", result="result",
        )
        self.assertIn("testuser", str(gen))
        self.assertIn("TestCo", str(gen))


# ===========================================================================
# 3. JOB APPLICATION MODEL TESTS
# ===========================================================================

class JobApplicationModelTest(TestCase):

    def setUp(self):
        self.user, _ = _make_user()

    def test_create_application(self):
        app = JobApplication.objects.create(
            user=self.user,
            company_name="Google",
            job_title="SWE",
            status="saved",
        )
        self.assertEqual(app.status, "saved")
        self.assertEqual(app.company_name, "Google")

    def test_status_choices(self):
        valid = {"saved", "applied", "interview", "offer", "rejected"}
        for slug, _ in JobApplication.STATUS_CHOICES:
            self.assertIn(slug, valid)

    def test_str_repr(self):
        app = JobApplication.objects.create(
            user=self.user, company_name="Meta",
            job_title="PM", status="applied"
        )
        self.assertIn("Meta", str(app))
        self.assertIn("applied", str(app))


# ===========================================================================
# 4. RATE LIMITER TESTS
# ===========================================================================

class RateLimiterTest(TestCase):

    def setUp(self):
        cache.clear()
        self.user, self.profile = _make_user(plan="free")

    def tearDown(self):
        cache.clear()

    def test_free_plan_allows_up_to_limit(self):
        """Free plan: first 3 requests should pass."""
        limit = RATE_LIMITS["free"]
        for i in range(limit):
            result = _check_rate_limit(self.user, "free")
            self.assertIsNone(result, f"Request {i+1} should be allowed")

    def test_free_plan_blocks_after_limit(self):
        """Free plan: 4th request must return 429."""
        limit = RATE_LIMITS["free"]
        for _ in range(limit):
            _check_rate_limit(self.user, "free")
        result = _check_rate_limit(self.user, "free")
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 429)

    def test_pro_plan_higher_limit(self):
        """Pro plan: should allow 20 requests before throttling."""
        limit = RATE_LIMITS["pro"]
        for i in range(limit):
            result = _check_rate_limit(self.user, "pro")
            self.assertIsNone(result, f"Pro request {i+1} should be allowed")
        result = _check_rate_limit(self.user, "pro")
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 429)

    def test_elite_plan_higher_limit(self):
        """Elite plan: should allow 50 requests before throttling."""
        limit = RATE_LIMITS["elite"]
        for i in range(limit):
            result = _check_rate_limit(self.user, "elite")
            self.assertIsNone(result, f"Elite request {i+1} should be allowed")
        result = _check_rate_limit(self.user, "elite")
        self.assertIsNotNone(result)

    def test_rate_limit_is_per_user(self):
        """Rate limit keys are user-scoped - different users are independent."""
        user2, _ = _make_user(username="user2", plan="free")
        limit = RATE_LIMITS["free"]
        for _ in range(limit + 1):
            _check_rate_limit(self.user, "free")
        result = _check_rate_limit(user2, "free")
        self.assertIsNone(result)

    def test_rate_limit_response_has_error_key(self):
        """429 response body must include the 'error' key."""
        limit = RATE_LIMITS["free"]
        resp = None
        for _ in range(limit + 1):
            resp = _check_rate_limit(self.user, "free")
        self.assertIsNotNone(resp)
        data = json.loads(resp.content)
        self.assertIn("error", data)


# ===========================================================================
# 5. AUTH GUARD TESTS
# ===========================================================================

class AuthGuardTest(TestCase):

    def setUp(self):
        self.client = Client()

    def _anon_post(self, url_name, data=None):
        return self.client.post(
            reverse(url_name),
            data=data or {},
            follow=False,
        )

    def test_download_pdf_requires_login(self):
        r = self._anon_post("download_pdf", {"template_name": "minimal_centered"})
        self.assertIn(r.status_code, [301, 302, 403])

    def test_parse_resume_pdf_requires_login(self):
        r = self._anon_post("parse_resume_pdf")
        self.assertIn(r.status_code, [301, 302, 403])

    def test_scrape_job_requires_login(self):
        r = self._anon_post("scrape_job", {"url": "https://example.com"})
        self.assertIn(r.status_code, [301, 302, 403])

    def test_generate_resume_requires_login(self):
        r = self._anon_post("generate_resume", {"resume": "test"})
        self.assertIn(r.status_code, [301, 302, 403])

    def test_dashboard_requires_login(self):
        r = self.client.get(reverse("dashboard"), follow=False)
        self.assertIn(r.status_code, [301, 302, 403])

    def test_history_requires_login(self):
        r = self.client.get(reverse("history"), follow=False)
        self.assertIn(r.status_code, [301, 302, 403])

    def test_tools_requires_login(self):
        r = self.client.get(reverse("tools"), follow=False)
        self.assertIn(r.status_code, [301, 302, 403])

    def test_tracker_requires_login(self):
        r = self.client.get(reverse("tracker"), follow=False)
        self.assertIn(r.status_code, [301, 302, 403])


# ===========================================================================
# 6. PDF GENERATION - ALL 10 TEMPLATES
# ===========================================================================

class PDFGenerationTest(TestCase):

    def setUp(self):
        self.user, self.profile = _make_user(plan="elite")
        self.client = Client()
        self.client.force_login(self.user)
        cache.clear()  # reset rate-limit counter before every test method

    def tearDown(self):
        cache.clear()

    def _post_pdf(self, slug, mode="preview"):
        data = dict(_RESUME_FORM, template_name=slug)
        return self.client.post(
            reverse("download_pdf") + "?mode=" + mode,
            data=data,
        )

    def test_all_10_templates_return_200(self):
        """Each template is tested with a fresh rate-limit counter."""
        all_slugs = [t["slug"] for t in TEMPLATES]
        self.assertEqual(len(all_slugs), 10, "Expected exactly 10 templates")
        for slug in all_slugs:
            # Clear cache before each slug so the Elite 50 req/min counter
            # never accumulates across subtests within a single test method.
            cache.clear()
            with self.subTest(template=slug):
                r = self._post_pdf(slug)
                self.assertEqual(r.status_code, 200, "Template '{}' failed".format(slug))
                self.assertEqual(r["Content-Type"], "application/pdf")
                # Verify streaming body is a valid PDF
                body = b"".join(r.streaming_content)
                self.assertTrue(body.startswith(b"%PDF"), "Bad PDF bytes for '{}'".format(slug))

    def test_preview_mode_returns_inline_disposition(self):
        cache.clear()
        r = self._post_pdf("minimal_centered", mode="preview")
        self.assertEqual(r.status_code, 200)
        self.assertIn("inline", r.get("Content-Disposition", ""))

    def test_download_mode_returns_attachment(self):
        cache.clear()
        r = self._post_pdf("minimal_centered", mode="download")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.get("Content-Disposition", ""))

    def test_pdf_content_is_non_empty(self):
        cache.clear()  # ensure rate-limit window is fresh
        r = self._post_pdf("hacker_terminal")
        # FileResponse is a StreamingHttpResponse — consume via streaming_content
        body = b"".join(r.streaming_content)
        self.assertGreater(len(body), 100)

    def test_pdf_starts_with_magic_bytes(self):
        cache.clear()  # ensure rate-limit window is fresh
        r = self._post_pdf("academic_classic")
        # FileResponse is a StreamingHttpResponse — consume via streaming_content
        body = b"".join(r.streaming_content)
        self.assertTrue(body.startswith(b"%PDF"), "PDF magic bytes missing")

    def test_invalid_template_falls_back(self):
        """An unknown template slug falls back to the first allowed one."""
        cache.clear()
        r = self._post_pdf("nonexistent_template_xyz")
        self.assertEqual(r.status_code, 200)

    def test_build_pdf_direct_call(self):
        """pdf_engine.build_pdf() works standalone without a real HTTP request."""
        mock_request = MagicMock()
        mock_request.POST = dict(
            _RESUME_FORM,
            template_name="minimal_centered",
            primary_color="#1a1a2e",
            bg_color="#ffffff",
            accent_color="#475569",
            font_family="Helvetica",
        )
        mock_request.FILES = {}
        buf = build_pdf("minimal_centered", mock_request)
        data = buf.read()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 500)

    def test_build_pdf_all_templates_direct(self):
        """Every template builder produces valid PDF bytes (bypasses rate limiter)."""
        mock_request = MagicMock()
        mock_request.POST = dict(
            _RESUME_FORM,
            primary_color="#2d3748",
            bg_color="#ffffff",
            accent_color="#4a5568",
            font_family="Helvetica",
        )
        mock_request.FILES = {}
        for tpl in TEMPLATES:
            with self.subTest(slug=tpl["slug"]):
                buf = build_pdf(tpl["slug"], mock_request)
                data = buf.read()
                self.assertTrue(
                    data.startswith(b"%PDF"),
                    "Template '{}' did not produce a valid PDF".format(tpl["slug"]),
                )


# ===========================================================================
# 7. PDF PARSER - MAGIC-BYTES VALIDATION
# ===========================================================================

class PDFParserTest(TestCase):

    def setUp(self):
        self.user, self.profile = _make_user(plan="elite")
        self.client = Client()
        self.client.force_login(self.user)
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_valid_pdf_magic_bytes_accepted(self):
        """PDF with valid %PDF magic bytes passes the magic check."""
        f = io.BytesIO(_PDF_BYTES)
        r = self.client.post(
            reverse("parse_resume_pdf"),
            {"pdf_file": ("resume.pdf", f, "application/pdf")},
            format="multipart",
        )
        # 200 = extracted OK; 400 = text empty (image PDF) - both passed magic check
        self.assertIn(r.status_code, [200, 400])
        self.assertNotEqual(r.status_code, 403)
        self.assertNotEqual(r.status_code, 500)

    def test_real_pdf_text_is_actually_extracted(self):
        """
        Regression: pdfminer.six only accepts io.IOBase, but Django hands the
        view an InMemoryUploadedFile. Passing the upload straight through raised
        "Unsupported input type", so EVERY PDF resume upload silently failed as
        though the file were unreadable. The older assertion above allowed 400,
        which is why this went unnoticed — so assert on the extracted text.
        """
        from reportlab.pdfgen import canvas

        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(72, 720, "Priya Raman")
        c.drawString(72, 700, "Staff Data Engineer")
        c.save()
        buf.seek(0)

        r = self.client.post(
            reverse("parse_resume_pdf"),
            {"pdf_file": ("resume.pdf", buf, "application/pdf")},
            format="multipart",
        )
        self.assertEqual(r.status_code, 200, r.content[:300])
        text = r.json().get("text", "")
        self.assertIn("Priya Raman", text)
        self.assertIn("Staff Data Engineer", text)

    def test_non_pdf_magic_bytes_rejected(self):
        """A ZIP file with .pdf extension must be rejected with HTTP 400."""
        fake_pdf = io.BytesIO(b"PK\x03\x04This is actually a ZIP")
        r = self.client.post(
            reverse("parse_resume_pdf"),
            {"pdf_file": ("resume.pdf", fake_pdf, "application/pdf")},
            format="multipart",
        )
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertIn("error", data)

    def test_executable_disguised_as_pdf_rejected(self):
        """EXE bytes disguised as PDF must be rejected."""
        exe_bytes = io.BytesIO(b"MZ\x90\x00This is an EXE header")
        r = self.client.post(
            reverse("parse_resume_pdf"),
            {"pdf_file": ("resume.pdf", exe_bytes, "application/pdf")},
            format="multipart",
        )
        self.assertEqual(r.status_code, 400)

    def test_empty_upload_rejected(self):
        r = self.client.post(reverse("parse_resume_pdf"), {})
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertIn("error", data)

    def test_oversized_file_rejected(self):
        """Files over 5 MB must be rejected."""
        big_file = io.BytesIO(b"%PDF" + b"A" * (6 * 1024 * 1024))
        r = self.client.post(
            reverse("parse_resume_pdf"),
            {"pdf_file": ("big.pdf", big_file, "application/pdf")},
            format="multipart",
        )
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertIn("error", data)
        self.assertIn("5MB", data["error"])


# ===========================================================================
# 7b. AI RESUME JSON VALIDATION
# A partial AI response used to be accepted as a complete resume, billed, and
# rendered with silently empty fields. These cover the validator that catches it.
# ===========================================================================

class ResumeJSONValidationTest(TestCase):

    def test_complete_response_has_no_problems(self):
        payload = {
            "full_name": "Jane Doe",
            "target_role": "Engineer",
            "summary": "Backend engineer.",
            "experience": [
                {"title": "Dev", "company": "Acme", "dates": "2020-2023", "bullets": ["Shipped"]}
            ],
            "education": [{"degree": "BSc", "school": "MIT", "dates": "2016-2020"}],
            "skills": [{"name": "Python"}],
        }
        out, problems = _validate_resume_json(payload)
        self.assertEqual(problems, [])
        self.assertEqual(out["full_name"], "Jane Doe")

    def test_missing_keys_are_normalized_not_dropped(self):
        """Absent keys come back as ''/[] so the frontend never sees undefined."""
        out, _ = _validate_resume_json({"full_name": "Jane", "summary": "x"})
        for key in ("email", "phone", "location", "linkedin", "github", "target_role"):
            self.assertEqual(out[key], "")
        for key in ("experience", "projects", "skills", "education", "languages"):
            self.assertEqual(out[key], [])

    def test_name_only_response_is_flagged_as_empty(self):
        """The exact failure mode users reported: a near-empty extraction."""
        out, problems = _validate_resume_json({"full_name": "Jane Doe"})
        self.assertTrue(problems)
        self.assertTrue(any("effectively empty" in p for p in problems))

    def test_missing_full_name_flagged(self):
        _, problems = _validate_resume_json({"summary": "Engineer.", "experience": []})
        self.assertTrue(any("full_name" in p for p in problems))

    def test_wrong_types_flagged_and_coerced(self):
        out, problems = _validate_resume_json(
            {"full_name": "Jane", "summary": "s", "experience": "not-a-list"}
        )
        self.assertTrue(any("experience" in p and "array" in p for p in problems))
        self.assertEqual(out["experience"], [])

    def test_experience_entry_without_title_or_company_flagged(self):
        _, problems = _validate_resume_json({
            "full_name": "Jane", "summary": "s",
            "experience": [{"dates": "2020-2023", "bullets": ["did stuff"]}],
        })
        self.assertTrue(any("experience[0]" in p for p in problems))

    def test_non_dict_response_rejected(self):
        out, problems = _validate_resume_json(["not", "an", "object"])
        self.assertIsNone(out)
        self.assertTrue(problems)

    def test_truncated_json_is_repaired(self):
        """A response cut off by the token limit should be salvaged, not lost."""
        truncated = (
            '{"full_name": "Jane Doe", "summary": "Engineer.", '
            '"experience": [{"title": "Dev", "company": "Acme", "bullets": ["Shipped a thing"'
        )
        parsed = _repair_truncated_json(truncated)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["full_name"], "Jane Doe")

    def test_unrecoverable_garbage_returns_none(self):
        self.assertIsNone(_repair_truncated_json("this is not json at all"))

    def test_short_text_is_not_truncated(self):
        text = "Jane Doe\nEngineer"
        self.assertEqual(_truncate_on_boundary(text, 1000), text)

    def test_truncation_marks_the_cut_and_breaks_on_a_line(self):
        """The old code sliced mid-word with no signal that content was lost."""
        text = "\n".join(f"Line {i} of the resume" for i in range(200))
        out = _truncate_on_boundary(text, 500, "uploaded resume")
        self.assertIn("truncated", out)
        self.assertLess(len(out), len(text))
        # Content before the marker must end on a whole line, not mid-word.
        body = out.split("\n\n[...")[0]
        self.assertTrue(text.startswith(body))

    def test_parse_and_validate_strips_markdown_fences(self):
        raw = '```json\n{"full_name": "Jane", "summary": "Engineer."}\n```'
        out, problems = _parse_and_validate_resume(raw)
        self.assertIsNotNone(out)
        self.assertEqual(out["full_name"], "Jane")
        self.assertEqual(problems, [])


# ===========================================================================
# 8. SSRF GUARD TESTS
# ===========================================================================

class SSRFGuardTest(TestCase):

    def setUp(self):
        self.user, self.profile = _make_user(plan="elite")
        self.client = Client()
        self.client.force_login(self.user)
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _scrape(self, url):
        return self.client.post(reverse("scrape_job"), {"url": url})

    def test_localhost_blocked(self):
        r = self._scrape("http://localhost/admin")
        self.assertEqual(r.status_code, 400)

    def test_127_0_0_1_blocked(self):
        r = self._scrape("http://127.0.0.1:8000/")
        self.assertEqual(r.status_code, 400)

    def test_0_0_0_0_blocked(self):
        r = self._scrape("http://0.0.0.0/")
        self.assertEqual(r.status_code, 400)

    def test_rfc1918_class_a_blocked(self):
        r = self._scrape("http://10.0.0.1/internal")
        self.assertEqual(r.status_code, 400)

    def test_rfc1918_class_b_blocked(self):
        r = self._scrape("http://172.16.0.1/internal")
        self.assertEqual(r.status_code, 400)

    def test_rfc1918_class_c_blocked(self):
        r = self._scrape("http://192.168.1.1/router")
        self.assertEqual(r.status_code, 400)

    def test_aws_metadata_blocked(self):
        r = self._scrape("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(r.status_code, 400)

    def test_ipv6_loopback_blocked(self):
        r = self._scrape("http://[::1]/")
        self.assertEqual(r.status_code, 400)

    def test_ftp_scheme_blocked(self):
        r = self._scrape("ftp://example.com/file.txt")
        self.assertEqual(r.status_code, 400)

    def test_file_scheme_blocked(self):
        r = self._scrape("file:///etc/passwd")
        self.assertEqual(r.status_code, 400)

    def test_empty_url_rejected(self):
        r = self._scrape("")
        self.assertEqual(r.status_code, 400)

    def test_ssrf_error_response_has_error_key(self):
        r = self._scrape("http://127.0.0.1/secret")
        data = r.json()
        self.assertIn("error", data)


# ===========================================================================
# 9. TRACKER VIEWS TESTS
# ===========================================================================

@override_settings(
    STORAGES={
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
    }
)
class TrackerViewTest(TestCase):

    def setUp(self):
        self.user, self.profile = _make_user(plan="pro")
        self.client = Client()
        self.client.force_login(self.user)

    def test_get_tracker_page(self):
        r = self.client.get(reverse("tracker"))
        self.assertEqual(r.status_code, 200)

    def test_add_application(self):
        r = self.client.post(reverse("tracker"), {
            "company_name": "Stripe",
            "job_title":    "Backend Engineer",
            "status":       "saved",
            "job_url":      "https://stripe.com/jobs/1",
            "notes":        "Exciting opportunity",
        }, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(JobApplication.objects.filter(company_name="Stripe").exists())

    def test_update_application_status(self):
        app = JobApplication.objects.create(
            user=self.user, company_name="Stripe",
            job_title="Eng", status="saved",
        )
        r = self.client.post(
            reverse("tracker_update", kwargs={"pk": app.pk}),
            {"status": "interview"},
        )
        self.assertEqual(r.status_code, 200)
        app.refresh_from_db()
        self.assertEqual(app.status, "interview")

    def test_update_invalid_status_rejected(self):
        app = JobApplication.objects.create(
            user=self.user, company_name="Meta",
            job_title="PM", status="saved",
        )
        r = self.client.post(
            reverse("tracker_update", kwargs={"pk": app.pk}),
            {"status": "hacked"},
        )
        self.assertEqual(r.status_code, 400)
        app.refresh_from_db()
        self.assertEqual(app.status, "saved")

    def test_delete_application(self):
        app = JobApplication.objects.create(
            user=self.user, company_name="Netflix",
            job_title="SRE", status="applied",
        )
        r = self.client.post(
            reverse("tracker_delete", kwargs={"pk": app.pk})
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(JobApplication.objects.filter(pk=app.pk).exists())

    def test_cannot_delete_other_users_application(self):
        other_user, _ = _make_user(username="hacker")
        app = JobApplication.objects.create(
            user=other_user, company_name="Amazon",
            job_title="Dev", status="saved",
        )
        r = self.client.post(
            reverse("tracker_delete", kwargs={"pk": app.pk})
        )
        self.assertEqual(r.status_code, 404)

    def test_free_plan_job_limit_enforced(self):
        """Free plan: exceeding 10 tracked jobs should redirect to pricing."""
        user, profile = _make_user(username="freelancer", plan="free")
        c = Client()
        c.force_login(user)
        for i in range(10):
            JobApplication.objects.create(
                user=user, company_name="Co{}".format(i),
                job_title="Dev", status="saved",
            )
        r = c.post(reverse("tracker"), {
            "company_name": "TooMany",
            "job_title":    "Dev",
            "status":       "saved",
        }, follow=False)
        self.assertIn(r.status_code, [301, 302])


# ===========================================================================
# 10. PLAN ENFORCEMENT - TEMPLATE GATING
# ===========================================================================

class PlanEnforcementTest(TestCase):

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _pdf_as_plan(self, plan, slug, suffix=""):
        username = "user_{}_{}{}".format(plan, slug[:4], suffix)
        user, profile = _make_user(username=username, plan=plan)
        c = Client()
        c.force_login(user)
        data = dict(_RESUME_FORM, template_name=slug)
        r = c.post(reverse("download_pdf") + "?mode=preview", data=data)
        return r

    def test_free_can_access_first_template(self):
        first_slug = TEMPLATES[0]["slug"]
        r = self._pdf_as_plan("free", first_slug)
        self.assertEqual(r.status_code, 200)

    def test_free_blocked_from_third_template(self):
        """Free users get only 2 templates - the 3rd falls back silently (still 200)."""
        third_slug = TEMPLATES[2]["slug"]
        r = self._pdf_as_plan("free", third_slug)
        self.assertEqual(r.status_code, 200)

    def test_pro_can_access_all_10_templates(self):
        """Pro plan allows up to 10 templates."""
        for i in range(10):
            slug = TEMPLATES[i]["slug"]
            user, _ = _make_user(username="prouser_{}".format(i), plan="pro")
            c = Client()
            c.force_login(user)
            data = dict(_RESUME_FORM, template_name=slug)
            r = c.post(reverse("download_pdf") + "?mode=preview", data=data)
            with self.subTest(template=slug):
                self.assertEqual(r.status_code, 200)

    def test_elite_can_access_all_templates(self):
        """Elite plan can access any template."""
        for tpl in TEMPLATES:
            user, _ = _make_user(
                username="elite_{}".format(tpl["slug"][:8]),
                plan="elite"
            )
            c = Client()
            c.force_login(user)
            data = dict(_RESUME_FORM, template_name=tpl["slug"])
            r = c.post(reverse("download_pdf") + "?mode=preview", data=data)
            with self.subTest(template=tpl["slug"]):
                self.assertEqual(r.status_code, 200)
