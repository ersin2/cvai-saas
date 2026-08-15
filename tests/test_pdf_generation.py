"""
Every section the user filled in must appear in the exported PDF.

WHY THIS FILE EXISTS
--------------------
A resume was reported as coming out "60-70% empty", with PROJECTS and the
categorised TECHNICAL SKILLS missing. The existing PDF tests did not catch it,
and could not have: they assert that `build_pdf()` returns bytes beginning with
`%PDF`. A resume missing half its sections satisfies both — it is a perfectly
valid PDF of an incomplete document.

So the assertions here are on the *extracted text*, section by section, for all
ten templates. The failure mode being guarded against is silent omission, which
is invisible to a status code, invisible to a byte count, and invisible on
screen until someone reads the export.

Three specific regressions are pinned:

  * `hacker_terminal` was the only builder that never read `languages` or
    `certifications`, so both vanished without a word.
  * `top_bottom_split` gave 30% of the page to a header holding three lines and
    pushed a complete resume onto page 2.
  * ReportLab parses Paragraph text as XML, so an unescaped "C++ <T>" was
    swallowed whole and "AI & LLM" rendered corrupted — and those are the exact
    category names real resumes use.
"""

import io

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, RequestFactory, Client, override_settings
from django.urls import reverse

from users.models import Profile

from generator.pdf_engine import build_pdf, TEMPLATES, _parse_skill_groups


# A resume with something in every field the engine knows how to render.
FULL_RESUME = {
    "full_name": "Alex Rivera",
    "target_role": "Senior Backend Engineer",
    "email": "alex@example.com",
    "phone": "+1 555 0100",
    "location": "Berlin, DE",
    "linkedin": "linkedin.com/in/arivera",
    "github": "github.com/arivera",
    "about_me": "Backend engineer focused on payments infrastructure and LLM systems.",
    "experience_text": (
        "Backend Engineer | Northwind Labs | 2022-Present\n"
        "Cut checkout API p95 latency from 840ms to 210ms.\n"
        "Led migration of the billing service to Python 3.12."
    ),
    "projects_text": (
        "CVAI Resume Engine | Python, ReportLab, Claude\n"
        "Built an ATS-safe PDF renderer covering ten layouts.\n"
        "Shipped structured LLM extraction against a JSON schema.\n"
        "\n"
        "Vector Search Service | pgvector, FastAPI\n"
        "Served 2M embeddings at p95 under 40ms.\n"
        "\n"
        "Ledger Reconciler | Go, Kafka\n"
        "Reconciled 4M daily transactions with an idempotent replay log."
    ),
    # Categorised, and deliberately containing the two characters that ReportLab
    # treats as markup: '&' in the headings, '+' and '<' hazards in the names.
    "skills_list": (
        "AI & LLM: Claude, RAG, LangChain\n"
        "Backend: Python, Django, PostgreSQL\n"
        "DevOps & Tools: Docker, Kubernetes\n"
        "Languages: C++, Go"
    ),
    "education": "BSc Computer Science | TU Berlin | 2018",
    "languages": "English (native), German (B2)",
    "certifications": "AWS Solutions Architect | 2023",
}

# (label, probe string that must survive into the extracted text)
REQUIRED_CONTENT = [
    ("summary",            "payments infrastructure"),
    ("experience",         "Northwind Labs"),
    ("experience bullet",  "840ms"),
    ("project 1",          "CVAI Resume Engine"),
    ("project 2",          "Vector Search Service"),
    ("project 3",          "Ledger Reconciler"),
    ("project bullet",     "ATS-safe PDF renderer"),
    ("project tech stack", "pgvector"),
    ("skill category",     "AI & LLM"),
    ("skill category 2",   "DevOps & Tools"),
    ("skill name",         "LangChain"),
    ("skill with C++",     "C++"),
    ("education",          "TU Berlin"),
    ("languages",          "German"),
    ("certifications",     "AWS Solutions Architect"),
]


def _extract(pdf_bytes):
    from pdfminer.high_level import extract_text
    from pdfminer.layout import LAParams
    return extract_text(io.BytesIO(pdf_bytes), laparams=LAParams())


def _render(slug, data=None):
    request = RequestFactory().post(
        "/download-pdf/", {**(data or FULL_RESUME), "template_name": slug}
    )
    buf = build_pdf(slug, request)
    return buf.getvalue() if hasattr(buf, "getvalue") else buf.read()


class SkillGroupParsingTest(TestCase):
    """The wire format has to carry categories without breaking saved resumes."""

    def test_grouped_format_keeps_its_categories(self):
        groups = _parse_skill_groups(
            "AI & LLM: Claude, RAG\nBackend: Python, Django"
        )
        self.assertEqual(
            groups,
            [
                ("AI & LLM", [("Claude", None), ("RAG", None)]),
                ("Backend", [("Python", None), ("Django", None)]),
            ],
        )

    def test_legacy_single_line_still_parses(self):
        """
        Resumes saved before skills were grouped are a single comma-joined line
        with a '-<level>' suffix. They must keep working — they are replayed
        from History and from every previously saved draft.
        """
        groups = _parse_skill_groups("Python-85,Django-70")
        self.assertEqual(
            groups, [(None, [("Python", 85.0), ("Django", 70.0)])]
        )

    def test_a_hyphenated_skill_name_is_not_mistaken_for_a_level(self):
        self.assertEqual(
            _parse_skill_groups("Objective-C-80"),
            [(None, [("Objective-C", 80.0)])],
        )
        self.assertEqual(
            _parse_skill_groups("Objective-C"),
            [(None, [("Objective-C", None)])],
        )

    def test_empty_input_yields_no_groups(self):
        self.assertEqual(_parse_skill_groups(""), [])
        self.assertEqual(_parse_skill_groups(None), [])


class EverySectionReachesThePdfTest(TestCase):
    """The regression that prompted this file."""

    def test_every_template_renders_every_section(self):
        for template in TEMPLATES:
            slug = template["slug"]
            text = _extract(_render(slug))
            for label, probe in REQUIRED_CONTENT:
                with self.subTest(template=slug, section=label):
                    self.assertIn(
                        probe, text,
                        f"{slug} dropped the {label} section: "
                        f"{probe!r} is not in the exported PDF, so the user "
                        f"filled that field in and it silently did not ship.",
                    )

    def test_markup_characters_in_skills_survive(self):
        """
        ReportLab parses Paragraph text as XML. Unescaped, 'C++ <T>' was
        swallowed entirely and 'R&D' picked up a stray semicolon.
        """
        data = dict(FULL_RESUME, skills_list="Systems: C++, R&D, Go<T>")
        text = _extract(_render("minimal_centered", data))
        self.assertIn("C++", text)
        self.assertIn("R&D", text)
        self.assertNotIn("R&D;", text)
        self.assertIn("Go<T>", text)

    def test_bullets_extract_as_text_not_as_a_cid_placeholder(self):
        """
        U+2022 is outside the standard-14 fonts' encoding, so every bulleted
        line came out of the extractor as '(cid:127) Cut checkout API...'.

        This product's entire claim is that screening software can read the
        export. Literal '(cid:127)' in front of every achievement is the worst
        possible place to be wrong about that, and it is invisible on screen —
        the bullet looks perfect, only the extracted text is junk.
        """
        for template in TEMPLATES:
            slug = template["slug"]
            text = _extract(_render(slug))
            with self.subTest(template=slug):
                self.assertNotIn(
                    "(cid:", text,
                    f"{slug} emitted an unmappable glyph; an ATS reading this "
                    f"PDF sees a cid placeholder instead of the character.",
                )

    def test_no_section_is_printed_twice(self):
        """
        left_sidebar_dark printed LANGUAGES in its dark rail and then again in
        the main column, under a second heading — it rendered the sidebar block
        itself *and* called the shared _render_edu_certs_lang, which renders the
        same field.

        Counting `POST.get('languages')` per builder does not catch this: the
        second render happens inside the shared helper, so the builder's own
        source only mentions the field once. Counting the value in the output
        does catch it.
        """
        for template in TEMPLATES:
            slug = template["slug"]
            text = _extract(_render(slug))
            for label, probe in [
                ("languages", "English (native)"),
                ("education", "TU Berlin"),
                ("certifications", "AWS Solutions Architect"),
                ("summary", "payments infrastructure"),
            ]:
                with self.subTest(template=slug, section=label):
                    self.assertEqual(
                        text.count(probe), 1,
                        f"{slug} rendered the {label} section "
                        f"{text.count(probe)} times.",
                    )

    def test_a_full_resume_fits_one_page(self):
        """
        top_bottom_split spent 30% of the page on a three-line header and
        pushed the rest onto page 2.
        """
        for template in TEMPLATES:
            slug = template["slug"]
            pages = _extract(_render(slug)).count("\f") or 1
            with self.subTest(template=slug):
                self.assertEqual(
                    pages, 1,
                    f"{slug} needed {pages} pages for a standard resume.",
                )

    def test_a_sparse_resume_still_renders(self):
        """Only the sections that were filled in, and no crash on the empty ones."""
        sparse = {
            "full_name": "Alex Rivera",
            "target_role": "Engineer",
            "email": "alex@example.com",
        }
        for template in TEMPLATES:
            slug = template["slug"]
            with self.subTest(template=slug):
                pdf = _render(slug, sparse)
                self.assertTrue(pdf.startswith(b"%PDF"))
                # Case-insensitive: hacker_terminal and academic_classic
                # upper-case the name by design.
                self.assertIn("alex rivera", _extract(pdf).lower())


class CoverLetterPdfTest(TestCase):
    """
    A cover letter is a business letter, not a resume with different words.

    Exporting one through build_pdf would have laid it out with resume section
    headings and a skills rail, so it gets its own builder — and that builder
    needs its own test, because "returns bytes starting with %PDF" is satisfied
    by a letter that lost its body.
    """

    BODY = (
        "Two years on Northwind's payment infrastructure make this a natural\n"
        "next step, and the posting asks for exactly that ownership.\n"
        "\n"
        "On the checkout API I cut p95 latency from 840ms to 210ms.\n"
        "\n"
        "I would welcome the chance to talk it through."
    )

    FIELDS = {
        "full_name": "Alex Rivera",
        "email": "alex@example.com",
        "phone": "+1 555 0100",
        "location": "Berlin, DE",
        "company_name": "Northwind Labs",
        "job_title": "Senior Backend Engineer",
    }

    def _render(self, **overrides):
        from generator.pdf_engine import build_cover_letter_pdf
        data = {**self.FIELDS, "body": self.BODY, **overrides}
        request = RequestFactory().post("/download-cover-letter/", data)
        return build_cover_letter_pdf(request).getvalue()

    def test_the_letter_reaches_the_pdf(self):
        text = _extract(self._render())
        for label, probe in [
            ("sender", "Alex Rivera"),
            ("contact", "alex@example.com"),
            ("recipient", "Northwind Labs"),
            ("role", "Senior Backend Engineer"),
            ("body", "840ms to 210ms"),
            ("closing", "talk it through"),
        ]:
            with self.subTest(part=label):
                self.assertIn(probe, text, f"the {label} is missing from the exported letter")

    def test_soft_wraps_are_joined_but_paragraphs_are_kept(self):
        """
        The model wraps its prose at some column. Treating those as real breaks
        produced a letter that read like terminal output; treating blank lines
        as nothing would have run it into one block.
        """
        text = _extract(self._render())
        self.assertIn("natural next step", text)          # soft wrap joined
        self.assertNotIn("natural\nnext step", text)
        self.assertIn("On the checkout API", text)        # paragraph kept apart

    def test_markup_in_the_letter_is_escaped_not_executed(self):
        text = _extract(self._render(body="Regards, <b>Alex</b> & Co <script>x</script>"))
        self.assertIn("<b>Alex</b>", text)
        self.assertIn("& Co", text)

    def test_a_letter_with_no_optional_fields_still_renders(self):
        pdf = self._render(company_name="", job_title="", phone="", location="")
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn("840ms", _extract(pdf))


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class CoverLetterDownloadViewTest(TestCase):
    """
    Exercise the endpoint, not just the builder.

    CoverLetterPdfTest calls build_cover_letter_pdf directly and passed while
    the view returned a 500 on every request: the view used re.sub and views.py
    did not import re. A builder test cannot see that — only a request can.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("letter_probe", password="pw-12345")
        Profile.objects.get_or_create(user=self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def test_posting_a_letter_returns_a_pdf_attachment(self):
        resp = self.client.post(reverse("download_cover_letter"), {
            "full_name": "Alex Rivera",
            "email": "alex@example.com",
            "company_name": "Northwind Labs",
            "job_title": "Senior Backend Engineer",
            "body": "On the checkout API I cut p95 latency from 840ms to 210ms.",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("attachment", resp.get("Content-Disposition", ""))

        pdf = b"".join(resp.streaming_content)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn("840ms", _extract(pdf))

    def test_an_empty_letter_is_refused_rather_than_exported_blank(self):
        resp = self.client.post(reverse("download_cover_letter"), {"body": "   "})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no letter", resp.json()["error"].lower())

    def test_the_filename_is_built_from_the_company(self):
        resp = self.client.post(reverse("download_cover_letter"), {
            "company_name": "Northwind Labs / EU",
            "body": "Body text.",
        })
        self.assertIn("Northwind_Labs_EU", resp["Content-Disposition"])

    def test_signed_out_users_are_redirected(self):
        self.client.logout()
        resp = self.client.post(reverse("download_cover_letter"), {"body": "x"})
        self.assertEqual(resp.status_code, 302)
