/*
 * CVAIResult — one renderer for AI output, shared by AI Tools, History and the
 * Dashboard.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * The same job was being done three different ways: formatMarkdown() in
 * tools.html, a bare {{ gen.result }} in history.html, and buildModalBody() in
 * dashboard.html. Only one of them attempted markdown at all, and it had no
 * case for tables — which is exactly what the ATS prompt asks the model to
 * produce. So a user's ATS report rendered as literal pipes and dashes.
 *
 * TWO RULES THIS FILE EXISTS TO ENFORCE
 *
 * 1. Escape before formatting, never after.
 *    This text is model output derived from the resume and job description the
 *    user pasted in. If it reaches innerHTML unescaped, an <img src=x
 *    onerror=...> pasted into a resume executes when the result is viewed. The
 *    previous renderer escaped nothing. Every path here escapes first and then
 *    inserts only tags this file generated.
 *
 * 2. Degrade, never blank.
 *    The ATS enhancer upgrades a keyword table into pills. Models reword
 *    things, so when the expected shape is not found the enhancer leaves the
 *    plain rendered table alone. A clean table is a fine outcome; an empty
 *    panel because a regex missed is not.
 *
 * Parsing is line-based rather than a chain of global regexes: block type is
 * decided per line, and inline formatting is applied only inside a block's
 * text. The old chained-regex approach is what let a table fall through
 * untouched in the first place.
 */
(function (global) {
  'use strict';

  var ENTITIES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return ENTITIES[ch];
    });
  }

  /* Inline formatting. Input MUST already be escaped. Bold runs first so the
     italic rule cannot eat the inner asterisks of a ** pair. */
  function inline(text) {
    return text
      .replace(/`([^`]+)`/g, '<code class="res-code">$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
               '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }

  function isTableRow(line) {
    return /^\s*\|.*\|\s*$/.test(line);
  }

  function isTableDivider(line) {
    return /^\s*\|?[\s:-]*-{2,}[\s:|-]*\|?\s*$/.test(line) && line.indexOf('-') !== -1;
  }

  function splitRow(line) {
    var trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    return trimmed.split('|').map(function (cell) { return cell.trim(); });
  }

  /* A cell the model used as "nothing here" — an em dash, a hyphen, or blank. */
  function isEmptyCell(cell) {
    return !cell || /^[\s–—-]+$/.test(cell);
  }

  function renderTable(rows) {
    if (!rows.length) { return ''; }
    var head = rows[0];
    var body = rows.slice(1);
    var html = '<div class="res-table-wrap"><table class="res-table"><thead><tr>';
    head.forEach(function (cell) { html += '<th>' + inline(cell) + '</th>'; });
    html += '</tr></thead><tbody>';
    body.forEach(function (row) {
      html += '<tr>';
      head.forEach(function (_h, i) {
        html += '<td>' + inline(row[i] || '') + '</td>';
      });
      html += '</tr>';
    });
    return html + '</tbody></table></div>';
  }

  /**
   * Markdown → HTML. Handles headings, tables, horizontal rules, bullet and
   * numbered lists, blockquotes, and paragraphs.
   */
  function renderMarkdown(source) {
    if (!source) { return ''; }
    var lines = escapeHtml(source).replace(/\r\n?/g, '\n').split('\n');
    var out = [];
    var paragraph = [];
    var list = null;          // { ordered: bool, items: [] }

    function flushParagraph() {
      if (paragraph.length) {
        out.push('<p>' + inline(paragraph.join(' ')) + '</p>');
        paragraph = [];
      }
    }
    function flushList() {
      if (list) {
        var tag = list.ordered ? 'ol' : 'ul';
        out.push('<' + tag + ' class="res-list">' +
                 list.items.map(function (i) { return '<li>' + inline(i) + '</li>'; }).join('') +
                 '</' + tag + '>');
        list = null;
      }
    }
    function flushAll() { flushParagraph(); flushList(); }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      if (!line.trim()) { flushAll(); continue; }

      // Table: a row followed by a divider row.
      if (isTableRow(line) && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
        flushAll();
        var rows = [splitRow(line)];
        i += 2;                                  // skip the divider
        while (i < lines.length && isTableRow(lines[i])) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        i--;
        out.push(renderTable(rows));
        continue;
      }

      // Horizontal rule (--- or ***), checked before the list rule so a rule
      // is not mistaken for a bullet.
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        flushAll();
        out.push('<hr class="res-hr">');
        continue;
      }

      var heading = /^\s*(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        flushAll();
        var level = Math.min(heading[1].length + 2, 6);   // # -> h3
        out.push('<h' + level + ' class="res-h">' + inline(heading[2].trim()) + '</h' + level + '>');
        continue;
      }

      var bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
      var numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (bullet || numbered) {
        flushParagraph();
        var ordered = !!numbered;
        if (!list || list.ordered !== ordered) { flushList(); list = { ordered: ordered, items: [] }; }
        list.items.push((bullet || numbered)[1].trim());
        continue;
      }

      var quote = /^\s*&gt;\s+(.*)$/.exec(line);
      if (quote) {
        flushAll();
        out.push('<blockquote class="res-quote">' + inline(quote[1].trim()) + '</blockquote>');
        continue;
      }

      // A line that is only bold is a section heading in practice — the ATS
      // prompt emits "**KEYWORD MATCH**" rather than "## KEYWORD MATCH".
      var boldOnly = /^\s*\*\*([^*]+)\*\*\s*:?\s*$/.exec(line);
      if (boldOnly) {
        flushAll();
        out.push('<h4 class="res-h">' + inline(boldOnly[1].trim()) + '</h4>');
        continue;
      }

      paragraph.push(line.trim());
    }

    flushAll();
    return out.join('\n');
  }

  /* ── ATS enhancer ─────────────────────────────────────────────────────────
     Finds the matched/missing keyword table in already-rendered HTML and
     replaces it with two pill groups. Returns true if it changed anything. */

  function findKeywordTable(root) {
    var tables = root.querySelectorAll('table.res-table');
    for (var i = 0; i < tables.length; i++) {
      var headers = Array.prototype.map.call(
        tables[i].querySelectorAll('thead th'),
        function (th) { return th.textContent.toLowerCase(); }
      );
      if (headers.length < 2) { continue; }
      var hasMatched = headers.some(function (h) { return h.indexOf('match') !== -1; });
      var hasMissing = headers.some(function (h) {
        return h.indexOf('miss') !== -1 || h.indexOf('gap') !== -1;
      });
      if (hasMatched && hasMissing) {
        return { table: tables[i], headers: headers };
      }
    }
    return null;
  }

  function columnValues(table, index) {
    return Array.prototype.map.call(table.querySelectorAll('tbody tr'), function (tr) {
      var cell = tr.children[index];
      return cell ? cell.textContent.trim() : '';
    }).filter(function (v) { return !isEmptyCell(v); });
  }

  function pillGroup(title, values, kind) {
    if (!values.length) { return ''; }
    var pills = values.map(function (v) {
      return '<span class="res-pill res-pill-' + kind + '">' + escapeHtml(v) + '</span>';
    }).join('');
    return '<div class="res-pill-group">' +
             '<div class="res-pill-title res-pill-title-' + kind + '">' +
               escapeHtml(title) + ' <span class="res-pill-count">' + values.length + '</span>' +
             '</div>' +
             '<div class="res-pill-row">' + pills + '</div>' +
           '</div>';
  }

  function scoreRing(score) {
    var pct = Math.max(0, Math.min(100, Number(score) || 0));
    // 2πr for r=26, matching the stroke-dasharray below.
    var circumference = 163.4;
    var offset = circumference * (1 - pct / 100);
    var tone = pct >= 75 ? 'good' : (pct >= 50 ? 'warn' : 'bad');
    return '<div class="res-score res-score-' + tone + '">' +
             '<div class="res-score-ring">' +
               '<svg viewBox="0 0 60 60" aria-hidden="true">' +
                 '<circle class="res-score-track" cx="30" cy="30" r="26"></circle>' +
                 '<circle class="res-score-fill" cx="30" cy="30" r="26" ' +
                   'style="stroke-dasharray:' + circumference + ';stroke-dashoffset:' + offset + ';"></circle>' +
               '</svg>' +
               '<span class="res-score-num">' + pct + '</span>' +
             '</div>' +
             '<div class="res-score-meta">' +
               '<strong>ATS match ' + pct + '/100</strong>' +
               '<span>How much of this posting your resume answers</span>' +
             '</div>' +
           '</div>';
  }

  /**
   * Upgrade a rendered ATS report in place. Safe to call on any container —
   * if the expected table is not present, nothing changes.
   */
  function enhanceAts(container, score) {
    if (!container) { return false; }

    var resolved = score;
    if (resolved == null || resolved === '') {
      var m = /(\d{1,3})\s*\/\s*100/.exec(container.textContent || '');
      resolved = m ? m[1] : null;
    }

    var found = findKeywordTable(container);
    var changed = false;

    if (found) {
      var matchedIdx = found.headers.findIndex(function (h) { return h.indexOf('match') !== -1; });
      var missingIdx = found.headers.findIndex(function (h) {
        return h.indexOf('miss') !== -1 || h.indexOf('gap') !== -1;
      });
      var matched = columnValues(found.table, matchedIdx);
      var missing = columnValues(found.table, missingIdx);

      if (matched.length || missing.length) {
        var panel = document.createElement('div');
        panel.className = 'res-keywords';
        panel.innerHTML = pillGroup('Matched', matched, 'good') +
                          pillGroup('Missing', missing, 'bad');
        var wrap = found.table.closest('.res-table-wrap') || found.table;
        wrap.parentNode.replaceChild(panel, wrap);
        changed = true;
      }
    }

    if (resolved != null) {
      var ring = document.createElement('div');
      ring.innerHTML = scoreRing(resolved);
      container.insertBefore(ring.firstChild, container.firstChild);
      changed = true;
    }
    return changed;
  }

  /**
   * Render `text` into `el`. Pass {ats: true, score: n} to also run the ATS
   * enhancer.
   */
  function render(el, text, options) {
    if (!el) { return; }
    var opts = options || {};
    el.innerHTML = renderMarkdown(text);
    if (opts.ats) { enhanceAts(el, opts.score); }
    return el;
  }

  global.CVAIResult = {
    escapeHtml: escapeHtml,
    renderMarkdown: renderMarkdown,
    enhanceAts: enhanceAts,
    render: render
  };
}(window));
