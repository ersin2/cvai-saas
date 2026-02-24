from django import template
from django.utils.safestring import mark_safe
import re
import markdown as md

register = template.Library()


@register.filter(name='parse_sections')
def parse_sections(text):
    """
    Parse the AI output into its 5 sections.

    Strategy (in priority order):
    1. Try the new structured [SECTION: TAG] ... [END_SECTION] format first.
       This is language-agnostic and 100% reliable.
    2. Fall back to the legacy numbered-header regex approach for older records.
    """
    if not text:
        return {}

    sections = {'main': '', 'ver_a': '', 'ver_b': '', 'ats': '', 'risks': ''}

    # ── Strategy 1: structured tag format ────────────────────────────────────
    TAG_MAP = {
        'MAIN_LETTER': 'main',
        'VERSION_A':   'ver_a',
        'VERSION_B':   'ver_b',
        'ATS_ANALYSIS': 'ats',
        'RISK_ANALYSIS': 'risks',
    }

    tag_pattern = re.compile(
        r'\[SECTION:\s*([A-Z_]+)\](.*?)\[END_SECTION\]',
        re.DOTALL | re.IGNORECASE
    )
    matches = tag_pattern.findall(text)
    if matches:
        for tag, content in matches:
            key = TAG_MAP.get(tag.strip().upper())
            if key:
                sections[key] = content.strip()
        return sections

    # ── Strategy 2: legacy numbered-header regex ──────────────────────────────
    patterns = {
        'ver_a': r'(?i)(?:\n|^)\s*(?:2\.?|\*+|#+)\s*(?:VERSION|OPTION|VARIANT)\s*A[:\.\-\s]*',
        'ver_b': r'(?i)(?:\n|^)\s*(?:3\.?|\*+|#+)\s*(?:VERSION|OPTION|VARIANT)\s*B[:\.\-\s]*',
        'ats':   r'(?i)(?:\n|^)\s*(?:4\.?|\*+|#+)\s*(?:ATS|COMPATIBILITY)\s*(?:ANALYSIS|SCORE|CHECK)?[:\.\-\s]*',
        'risks': r'(?i)(?:\n|^)\s*(?:5\.?|\*+|#+)\s*(?:RECRUITER|RISK)\s*(?:RISK|ANALYSIS|FLAGS)?[:\.\-\s]*'
    }

    def clean(s):
        s = re.sub(r'(?i)^(?:1\.?|MAIN|COVER\s*LETTER)[:\.\-\s]*', '', s.strip())
        return s.strip()

    split_1 = re.split(patterns['ver_a'], text, maxsplit=1)
    sections['main'] = clean(split_1[0])

    if len(split_1) > 1:
        rest = split_1[1]

        split_2 = re.split(patterns['ver_b'], rest, maxsplit=1)
        sections['ver_a'] = clean(split_2[0])

        if len(split_2) > 1:
            rest = split_2[1]

            split_3 = re.split(patterns['ats'], rest, maxsplit=1)
            sections['ver_b'] = clean(split_3[0])

            if len(split_3) > 1:
                rest = split_3[1]

                split_4 = re.split(patterns['risks'], rest, maxsplit=1)
                sections['ats'] = clean(split_4[0])

                if len(split_4) > 1:
                    sections['risks'] = clean(split_4[1])

    return sections


@register.filter(name='markdown_to_html')
def markdown_to_html(text):
    """
    Converts raw Markdown from LLM to safe HTML.
    """
    if not text:
        return ''
    text = text.replace('\\r\\n', '\n').replace('\\n', '\n')
    html = md.markdown(text, extensions=['extra', 'nl2br'])
    return mark_safe(html)