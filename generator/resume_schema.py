"""
Turning a resume into structured data, and checking that it worked.

Three stages, in the order the pipeline uses them:

  1. text extraction  — pull readable text out of an uploaded PDF, in reading
     order rather than raw position order (`_extract_resume_text`)
  2. schema           — the JSON contract handed to the model, with a
     description on every field (`RESUME_JSON_SCHEMA`)
  3. recovery         — repair a response truncated mid-write, then verify the
     result is actually complete rather than a conforming shell
     (`_repair_truncated_json`, `_validate_resume_json`)

None of this is HTTP-aware, which is why it no longer lives in views.py.
"""

import io
import json
import logging
import re

from pdfminer.high_level import extract_text as pdf_extract_text
from pdfminer.layout import LAParams

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RESUME TEXT EXTRACTION
# ---------------------------------------------------------------------------
# boxes_flow controls how pdfminer orders text boxes on the page.
#
# This used to be set to None on the theory that it "emits each text box whole,
# top-to-bottom / left-to-right". It does the opposite: None disables
# reading-flow reconstruction and sorts boxes by raw position, so on a
# two-column resume it reads ACROSS both columns at each vertical band. The
# sidebar and the main column interleave line by line, and the model receives
# "Senior Engineer / Stripe / EDUCATION / - led migration / UC Berkeley" —
# which is exactly the garbage the old comment claimed to be preventing.
#
# Measured on a two-column fixture (see ResumeExtractionTest), scoring whether
# each job title stays adjacent to its own employer:
#     boxes_flow=None   1/2 titles correct, sidebar split apart
#     boxes_flow=0.5    2/2 titles correct, sidebar contiguous
# Single-column extraction is byte-identical either way, so the default costs
# nothing for simple resumes.
_RESUME_LAPARAMS = LAParams(
    # 0.5 is pdfminer's default: reconstruct reading flow, keeping columns whole.
    boxes_flow=0.5,
    line_margin=0.4,   # tighter: keeps bullet lines inside their own block
    char_margin=1.5,
    word_margin=0.1,
)

# Total characters of resume context handed to the model. The previous 10k cap
# silently amputated 3-page CVs; Llama 3.1 has ample context for this.
RESUME_TEXT_BUDGET = 24000


def _truncate_on_boundary(text, limit, label='resume'):
    """
    Trim `text` to `limit` chars on a line boundary rather than mid-word, and
    mark the cut explicitly so the model knows the input was incomplete instead
    of silently treating a severed resume as the whole document.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind('\n')
    if nl > limit * 0.6:      # only back up to a newline if it isn't far back
        cut = cut[:nl]
    logger.info("Truncated %s text from %d to %d chars", label, len(text), len(cut))
    return cut.rstrip() + f"\n\n[...{label} truncated — later sections omitted]"


def _extract_resume_text(pdf_file):
    """
    Extract resume text from an uploaded PDF, preserving section structure.

    Returns (text, error_message). Shared by parse_resume_pdf and
    generate_resume so both paths get identical layout handling.

    The upload is copied into a BytesIO first: pdfminer.six only accepts
    io.IOBase, and Django hands us an InMemoryUploadedFile/TemporaryUploadedFile,
    which it rejects with "Unsupported input type". Passing the upload straight
    through made every PDF resume upload fail as if the file were unreadable.
    """
    try:
        pdf_file.seek(0)
        stream = io.BytesIO(pdf_file.read())
        pdf_file.seek(0)
        text = pdf_extract_text(stream, laparams=_RESUME_LAPARAMS) or ''
    except Exception as exc:
        logger.warning("PDF extraction failed: %s", exc)
        return '', 'Failed to parse PDF. Try pasting your resume text instead.'

    # Collapse runs of blank lines (pdfminer emits many) without destroying the
    # single blank line that separates one section from the next.
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if not text:
        return '', 'Could not extract text from this PDF. It may be image-based.'
    return text, None

# ---------------------------------------------------------------------------
# AI RESUME JSON — SCHEMA VALIDATION
# ---------------------------------------------------------------------------
# The parser used to accept any JSON that json.loads() survived, so a response
# containing only {"full_name": "..."} was billed and rendered as a complete
# resume with every other field silently blank. These helpers make an incomplete
# response detectable so it can be retried or flagged.
_RESUME_STR_FIELDS = (
    'full_name', 'target_role', 'email', 'phone',
    'location', 'linkedin', 'github', 'summary',
)
_RESUME_LIST_FIELDS = ('experience', 'projects', 'skills', 'education', 'languages')

# Shape of each repeating section, keyed by its list field above.
_RESUME_ITEM_SHAPES = {
    'experience': ('title', 'company', 'location', 'dates'),
    'projects':   ('title', 'tech_stack'),
    'skills':     ('name',),
    'education':  ('degree', 'school', 'dates'),
}
# Sections whose items carry a free-form bullet list on top of their string keys.
_RESUME_BULLET_SECTIONS = ('experience', 'projects')


# What each field MEANS. The schema previously carried shape only — every field
# was a bare {"type": "string"} — which guarantees a conforming object and tells
# the model nothing about what belongs in it. That is how "Senior Engineer at
# Stripe" ends up entirely in `title`, how a graduation year lands in an
# experience `dates`, and how six source bullets get compressed into two.
#
# Structured outputs enforce the container; these descriptions are the only
# thing carrying the semantics.
_FIELD_DESCRIPTIONS = {
    # Top-level identity / contact
    'full_name':   "The candidate's full name exactly as written. Never a job title.",
    'target_role': "The role the candidate is targeting, e.g. 'Senior Backend Engineer'. "
                   "Use their current or most recent title if no target is stated.",
    'email':       "Email address only, no label. Empty string if absent.",
    'phone':       "Phone number as written. Empty string if absent.",
    'location':    "City and region/country, e.g. 'Berlin, Germany'. Not a full street address.",
    'linkedin':    "LinkedIn URL or handle. Empty string if absent.",
    'github':      "GitHub URL or handle. Empty string if absent. Do not infer one from the name.",
    'summary':     "2-3 sentence professional summary built ONLY from facts in the source. "
                   "If the source has no summary, write one from the experience listed — "
                   "do not invent seniority, metrics or domains that are not present.",
    # Repeating-section items
    'title':       "Job title ONLY, e.g. 'Senior Backend Engineer'. Never include the "
                   "employer, dates or location here — those are separate fields.",
    'company':     "Employer name ONLY, e.g. 'Stripe'. Never include the job title.",
    'dates':       "The date range for THIS entry exactly as written, e.g. 'Jan 2022 - Present'. "
                   "Take it from the line belonging to this entry; do not borrow a date "
                   "from a neighbouring entry or from the education section.",
    'tech_stack':  "Technologies used on this project, comma-separated. Empty string if absent.",
    'name':        "A single skill exactly as named in the source. Do not invent a "
                   "proficiency level, and do not split or merge skills.",
    'degree':      "Degree or qualification, e.g. 'B.Sc. Computer Science'.",
    'school':      "Institution name only.",
    'bullets':     "Every bullet point for this entry, one array item each, preserving the "
                   "candidate's own wording and any numbers they cite. Do not summarise, "
                   "merge or drop bullets, and do not invent metrics.",
    'languages':   "Spoken languages with proficiency if stated, e.g. 'English (Native)'. "
                   "Not programming languages — those belong in skills.",
    # Section-level (arrays)
    'experience':  "Every role in the source, most recent first. One entry per role.",
    'projects':    "Personal or professional projects. Empty array if the source has none.",
    'skills':      "Every skill named in the source, one entry each.",
    'education':   "Every qualification in the source.",
}


def _described(field, node):
    """Attach the field's description, if one is defined for that key."""
    desc = _FIELD_DESCRIPTIONS.get(field)
    return {**node, 'description': desc} if desc else node


def _str_obj(*keys, bullets=False):
    """One object in a repeating section: all-string keys, optional bullets."""
    props = {k: _described(k, {'type': 'string'}) for k in keys}
    if bullets:
        props['bullets'] = _described(
            'bullets', {'type': 'array', 'items': {'type': 'string'}}
        )
    return {
        'type': 'object',
        'properties': props,
        'required': list(props),
        'additionalProperties': False,
    }


def _build_resume_schema():
    """
    JSON Schema for the resume object, derived from the same field tuples
    _validate_resume_json checks — so the contract sent to the model and the
    contract enforced on the way back cannot drift apart.

    Sent to Claude as output_config.format, which *guarantees* a conforming
    object. That is what makes the markdown-fence stripping and the
    truncated-JSON repair in _parse_and_validate_resume dead code on this
    path rather than load-bearing.

    Every field is listed in `required` and every object sets
    additionalProperties:false — both are structured-output requirements.
    Missing data is an empty string or empty array, per the system prompt;
    there is no "omit the key" option.
    """
    props = {k: _described(k, {'type': 'string'}) for k in _RESUME_STR_FIELDS}
    for name, keys in _RESUME_ITEM_SHAPES.items():
        props[name] = _described(name, {
            'type': 'array',
            'items': _str_obj(*keys, bullets=name in _RESUME_BULLET_SECTIONS),
        })
    props['languages'] = _described(
        'languages', {'type': 'array', 'items': {'type': 'string'}}
    )
    return {
        'type': 'object',
        'properties': props,
        'required': list(props),
        'additionalProperties': False,
    }

RESUME_JSON_SCHEMA = _build_resume_schema()

def _repair_truncated_json(raw):
    """
    Best-effort recovery of a JSON object cut off mid-write (the usual shape of
    a max_tokens truncation): drop the dangling tail and close open brackets.
    Returns a parsed dict or None.
    """
    start = raw.find('{')
    if start == -1:
        return None
    candidate = raw[start:]
    # Walk back to the last plausible value end, then balance the brackets.
    for end in range(len(candidate) - 1, 0, -1):
        if candidate[end] not in '}]"0123456789truefalsnl':
            continue
        stack, in_str, esc = [], False, False
        for ch in candidate[:end + 1]:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str and ch in '{[':
                stack.append(ch)
            elif not in_str and ch in '}]':
                if stack:
                    stack.pop()
        if in_str:
            continue
        closed = candidate[:end + 1].rstrip().rstrip(',')
        closed += ''.join('}' if b == '{' else ']' for b in reversed(stack))
        try:
            parsed = json.loads(closed)
            if isinstance(parsed, dict):
                logger.info("Recovered truncated resume JSON (%d chars salvaged)", end)
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _validate_resume_json(parsed):
    """
    Normalize an AI resume object and report what's wrong with it.

    Returns (normalized_dict, problems) where `problems` is a list of
    human-readable strings. An empty list means the response is usable.
    Missing keys are filled with ""/[] so the frontend never sees undefined.
    """
    problems = []
    if not isinstance(parsed, dict):
        return None, ['Response was not a JSON object.']

    out = {}
    for key in _RESUME_STR_FIELDS:
        val = parsed.get(key, '')
        if val is None:
            val = ''
        if not isinstance(val, str):
            problems.append(f'"{key}" must be a string, got {type(val).__name__}.')
            val = str(val)
        out[key] = val.strip()

    for key in _RESUME_LIST_FIELDS:
        val = parsed.get(key, [])
        if val is None:
            val = []
        if not isinstance(val, list):
            problems.append(f'"{key}" must be an array, got {type(val).__name__}.')
            val = []
        out[key] = val

    # Structural checks on the repeating sections.
    for i, job in enumerate(out['experience']):
        if not isinstance(job, dict):
            problems.append(f'experience[{i}] must be an object.')
            continue
        if not str(job.get('title', '')).strip() and not str(job.get('company', '')).strip():
            problems.append(f'experience[{i}] has neither a title nor a company.')
        bullets = job.get('bullets', [])
        if bullets is not None and not isinstance(bullets, list):
            problems.append(f'experience[{i}].bullets must be an array.')

    for i, edu in enumerate(out['education']):
        if not isinstance(edu, dict):
            problems.append(f'education[{i}] must be an object.')
        elif not str(edu.get('degree', '')).strip() and not str(edu.get('school', '')).strip():
            problems.append(f'education[{i}] has neither a degree nor a school.')

    # Completeness: a resume with a name but no content whatsoever means the
    # model gave up partway. This is the signal the old code never had.
    if not out['full_name']:
        problems.append('"full_name" is empty — the candidate name was not extracted.')
    if not (out['summary'] or out['experience'] or out['education']):
        problems.append(
            'No summary, experience or education was extracted — '
            'the response is effectively empty.'
        )
    return out, problems

def _parse_and_validate_resume(raw_result):
    """
    Turn a raw AI response into (normalized_resume | None, problems).

    Handles markdown fences, then a brace-balance repair for responses cut off
    by the token limit, then schema/completeness validation.
    """
    cleaned = raw_result.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        parsed = _repair_truncated_json(cleaned)
        if parsed is None:
            logger.warning("AI returned unparseable JSON for resume: %s", exc)
            return None, ['The AI response was not valid JSON.']

    return _validate_resume_json(parsed)
