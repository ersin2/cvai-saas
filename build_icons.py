"""
Build generator/static/generator/css/icons.css: every Font Awesome class the
app uses, drawn as a Lucide line icon (the landing page's style) via CSS mask.

    npm install --no-save lucide-static     # icon source, not a runtime dependency
    python build_icons.py

The build fails, rather than shipping a blank icon, if a template or script
uses an `fa-*` name that MAP below does not cover, or MAP names an icon
Lucide does not have. Add new icons to MAP and rerun.
"""
import os
import re
import sys
import urllib.parse

PROJECT = os.path.dirname(os.path.abspath(__file__))
LUCIDE = os.environ.get('LUCIDE_DIR', os.path.join(PROJECT, 'node_modules', 'lucide-static'))
ICONS = os.path.join(LUCIDE, 'icons')
OUT = os.path.join(PROJECT, 'generator', 'static', 'generator', 'css', 'icons.css')
if not os.path.isdir(ICONS):
    sys.exit(f'Lucide icons not found at {ICONS}. Run: npm install --no-save lucide-static')
VERSION = open(os.path.join(LUCIDE, 'package.json'), encoding='utf-8').read()
VERSION = re.search(r'"version":\s*"([^"]+)"', VERSION).group(1)

# Font Awesome name -> Lucide name
MAP = {
    'check': 'check', 'chevron-down': 'chevron-down', 'chevron-right': 'chevron-right',
    'wand-magic-sparkles': 'wand-sparkles', 'magic': 'wand-sparkles',
    'times': 'x', 'copy': 'copy', 'briefcase': 'briefcase', 'bolt': 'zap',
    'arrow-left': 'arrow-left', 'arrow-right': 'arrow-right',
    'file-alt': 'file-text', 'file-lines': 'file-text', 'file-pdf': 'file-text',
    'spinner': 'loader-circle', 'pen-ruler': 'pencil-ruler', 'chart-bar': 'chart-column',
    'user': 'user', 'plus': 'plus', 'plus-circle': 'circle-plus', 'minus': 'minus',
    'exclamation-circle': 'circle-alert', 'circle-exclamation': 'circle-alert',
    'crown': 'crown', 'clock-rotate-left': 'history', 'clock': 'clock',
    'triangle-exclamation': 'triangle-alert', 'exclamation-triangle': 'triangle-alert',
    'trash-alt': 'trash-2', 'trash-can': 'trash-2',
    'envelope-open-text': 'mail-open', 'envelope': 'mail',
    'circle-info': 'info', 'info-circle': 'info',
    'chart-pie': 'chart-pie', 'chart-line': 'chart-line',
    'user-tie': 'user-round', 'user-circle': 'circle-user',
    'sign-out-alt': 'log-out', 'rocket': 'rocket', 'paper-plane': 'send',
    'lock': 'lock', 'inbox': 'inbox',
    'circle-check': 'circle-check', 'check-circle': 'circle-check', 'times-circle': 'circle-x',
    'tags': 'tags', 'sticky-note': 'sticky-note', 'shield-halved': 'shield-half',
    'palette': 'palette', 'globe': 'globe', 'filter': 'funnel',
    'file-signature': 'file-signature', 'file-arrow-down': 'file-down',
    'external-link-alt': 'external-link', 'credit-card': 'credit-card',
    'comments': 'messages-square', 'calendar-alt': 'calendar', 'building': 'building-2',
    'brain': 'brain', 'bars': 'menu', 'trophy': 'trophy', 'star': 'star',
    'sliders-h': 'sliders-horizontal', 'sliders': 'sliders-horizontal', 'search': 'search',
    'save': 'save', 'sack-dollar': 'wallet', 'rotate-right': 'rotate-cw',
    'pencil-alt': 'pencil', 'pen-nib': 'pen-tool', 'money-bill-wave': 'banknote',
    'list': 'list', 'link': 'link', 'life-ring': 'life-buoy', 'layer-group': 'layers',
    'image': 'image', 'id-card': 'id-card', 'home': 'house', 'heart': 'heart',
    'headset': 'headset', 'graduation-cap': 'graduation-cap', 'ghost': 'ghost', 'font': 'type',
    'file-invoice-dollar': 'receipt', 'file-invoice': 'receipt-text', 'eye': 'eye',
    'download': 'download', 'columns': 'columns-2', 'code-branch': 'git-branch',
    'cloud-arrow-up': 'cloud-upload', 'circle-dot': 'circle-dot', 'certificate': 'award',
    'camera': 'camera', 'tag': 'tag', 'xmark': 'x',
}

missing = [lu for lu in set(MAP.values()) if not os.path.exists(os.path.join(ICONS, lu + '.svg'))]
if missing:
    sys.exit(f'Lucide has no icon named: {sorted(missing)}')

# Every fa- name the project uses must be mapped.
used = set()
for root in ('generator/templates', 'users/templates', 'templates', 'generator/static/generator/js'):
    for dirpath, _, files in os.walk(os.path.join(PROJECT, root)):
        for f in files:
            if f.endswith(('.html', '.js')) and 'landing' not in f:
                used |= set(re.findall(r'\bfa-([a-z0-9-]+)', open(os.path.join(dirpath, f), encoding='utf-8').read()))
used |= {'check-circle', 'times-circle', 'exclamation-triangle', 'info-circle',   # toast map in base_app
         'circle-check', 'circle-exclamation'}                                     # Studio toast
modifiers = {'spin', 'fw', 'solid', 'regular', 'lg', 'xs', 'sm', '2x', '3x'}
unmapped = sorted(n for n in used - modifiers if n not in MAP)
if unmapped:
    sys.exit(f'Unmapped Font Awesome icons: {unmapped}')


def data_uri(lucide):
    svg = open(os.path.join(ICONS, lucide + '.svg'), encoding='utf-8').read()
    svg = re.sub(r'<!--.*?-->', '', svg, flags=re.S)
    svg = re.sub(r'\s*class="[^"]*"', '', svg)
    svg = re.sub(r'\s*(width|height)="24"', '', svg)
    # The landing sprite strokes at 1.75; the mask only reads alpha, so the colour is irrelevant.
    svg = svg.replace('stroke="currentColor"', 'stroke="black"').replace('stroke-width="2"', 'stroke-width="1.75"')
    svg = re.sub(r'\s+', ' ', svg).replace('> <', '><').strip()
    return 'url("data:image/svg+xml,' + urllib.parse.quote(svg, safe=' =:/"-.,') .replace('"', "'") + '")'


lines = [f'''/* ================================================================
   CVAI — icons

   Every Font Awesome class the app uses (`<i class="fas fa-check">`),
   drawn as a thin line icon to match the landing page's sprite. No markup
   had to change, icons built in JavaScript work the same way, and the Font
   Awesome download (~250 KB of CSS and fonts) is gone.

   Each icon is an SVG used as a CSS mask over `currentColor`, so it takes
   the text colour and font size exactly as the font glyphs did.

   Icon artwork: Lucide v{VERSION} (https://lucide.dev), ISC License,
   Copyright (c) Lucide Icons and Contributors. Generated by
   build_icons.py — edit the mapping there, not this file.
   ================================================================ */

.fa, .fas, .far, .fa-solid, .fa-regular {{
  display: inline-block;
  width: 1em;
  height: 1em;
  flex: none;
  vertical-align: -0.125em;
  background-color: currentColor;
  -webkit-mask: var(--ico) center / contain no-repeat;
          mask: var(--ico) center / contain no-repeat;
  font-style: normal;
}}
.fa-fw {{ width: 1.25em; }}
.fa-spin {{ animation: cvai-ico-spin 0.9s linear infinite; }}
@keyframes cvai-ico-spin {{ to {{ transform: rotate(360deg); }} }}
@media (prefers-reduced-motion: reduce) {{ .fa-spin {{ animation-duration: 2.4s; }} }}
''']
lines.append(':root {')
for lu in sorted(set(MAP.values())):
    lines.append(f'  --ico-{lu}: {data_uri(lu)};')
lines.append('}\n')
for fa, lu in sorted(MAP.items()):
    lines.append(f'.fa-{fa} {{ --ico: var(--ico-{lu}); }}')
open(OUT, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines) + '\n')
print(f'{len(MAP)} classes, {len(set(MAP.values()))} icons, {os.path.getsize(OUT) // 1024} KB -> {OUT}')
print('used by the project:', len(used - modifiers))
