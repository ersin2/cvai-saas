from django import template
from django.utils.safestring import mark_safe
import re
import markdown as md

register = template.Library()


@register.filter(name='parse_sections')
def parse_sections(text):
    if not text: return {}

    sections = {'main': '', 'ver_a': '', 'ver_b': '', 'ats': '', 'risks': ''}

    # --- УМНЫЕ РЕГУЛЯРКИ (Ищут заголовки в любом стиле: 2. Version A, **Version A**, VERSION A) ---
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
    ЗАДАЧА 4: Конвертирует сырой Markdown от LLM в безопасный HTML.
    - Заменяет текстовые символы \\r\\n и \\n (буквальные, не настоящие переносы)
    - Использует расширения 'extra' и 'nl2br' для корректного форматирования
    """
    if not text:
        return ''
    # Заменяем литеральные escape-последовательности, которые иногда возвращает LLM
    text = text.replace('\\r\\n', '\n').replace('\\n', '\n')
    html = md.markdown(text, extensions=['extra', 'nl2br'])
    return mark_safe(html)