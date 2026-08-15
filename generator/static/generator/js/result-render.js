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

  /* ── Shared helpers for the structural enhancers ──────────────────────── */

  var HEADINGS = 'H3,H4,H5,H6';

  /* Split a container's children into [{heading, nodes}] groups, starting a new
     group at every heading whose text matches `test`. Nodes before the first
     match are returned as a leading group with heading null. */
  function groupByHeading(container, test) {
    var groups = [];
    var current = { heading: null, nodes: [] };
    Array.prototype.slice.call(container.children).forEach(function (node) {
      if (node.matches(HEADINGS) && test(node.textContent.trim())) {
        if (current.heading || current.nodes.length) { groups.push(current); }
        current = { heading: node, nodes: [] };
      } else {
        current.nodes.push(node);
      }
    });
    if (current.heading || current.nodes.length) { groups.push(current); }
    return groups;
  }

  function copyButton(label) {
    return '<button type="button" class="res-copy" data-res-copy>' +
             '<i class="fas fa-copy" aria-hidden="true"></i>' + escapeHtml(label) +
           '</button>';
  }

  /**
   * Interview prep → one collapsible card per question.
   *
   * The prompt emits "### Q1: …" followed by "**Why they ask:**",
   * "**Strong answer:**" and "**Tip:**". Rendered flat, ten questions are an
   * unbroken wall of text with no way to work through them one at a time.
   */
  function enhanceInterview(container) {
    if (!container) { return false; }
    var groups = groupByHeading(container, function (text) {
      return /^Q\s*\d+\b/i.test(text);
    }).filter(function (g) { return g.heading; });

    if (groups.length < 2) { return false; }   // not the expected shape

    var wrap = document.createElement('div');
    wrap.className = 'res-qa';

    groups.forEach(function (group, index) {
      var details = document.createElement('details');
      details.className = 'res-qa-item';
      if (index === 0) { details.open = true; }

      var summary = document.createElement('summary');
      summary.className = 'res-qa-q';
      summary.textContent = group.heading.textContent.trim();
      details.appendChild(summary);

      var bodyEl = document.createElement('div');
      bodyEl.className = 'res-qa-a';
      group.nodes.forEach(function (n) { bodyEl.appendChild(n); });
      details.appendChild(bodyEl);

      wrap.appendChild(details);
      group.heading.remove();
    });

    container.appendChild(wrap);
    return true;
  }

  /**
   * Follow-up emails → subject and body in separate copyable boxes.
   *
   * The prompt emits a sequence ("## 1. 3-Day Follow-Up") with "**Subject:**"
   * and "**Body:**" inside each. Flat, the subject line is indistinguishable
   * from the body, and copying meant selecting by hand.
   */
  function enhanceFollowup(container) {
    if (!container) { return false; }

    var groups = groupByHeading(container, function (text) {
      return /follow[-\s]?up|day|email/i.test(text);
    }).filter(function (g) { return g.heading; });

    // A single email with no section headings still has a subject worth lifting.
    if (!groups.length) { groups = [{ heading: null, nodes: Array.prototype.slice.call(container.children) }]; }

    var changed = false;
    var wrap = document.createElement('div');
    wrap.className = 'res-mail-set';

    groups.forEach(function (group) {
      var subjectNode = null;
      group.nodes.forEach(function (n) {
        if (!subjectNode && /^\s*subject\s*:/i.test(n.textContent || '')) { subjectNode = n; }
      });
      if (!subjectNode) { return; }

      changed = true;
      var card = document.createElement('div');
      card.className = 'res-mail';

      if (group.heading) {
        var title = document.createElement('div');
        title.className = 'res-mail-title';
        title.textContent = group.heading.textContent.trim();
        card.appendChild(title);
        group.heading.remove();
      }

      var subjectText = (subjectNode.textContent || '').replace(/^\s*subject\s*:\s*/i, '').trim();
      var subjectBox = document.createElement('div');
      subjectBox.className = 'res-mail-field';
      subjectBox.innerHTML =
        '<div class="res-mail-label">Subject' + copyButton('Copy') + '</div>' +
        '<div class="res-mail-value res-mail-subject">' + escapeHtml(subjectText) + '</div>';
      card.appendChild(subjectBox);
      subjectNode.remove();

      var bodyBox = document.createElement('div');
      bodyBox.className = 'res-mail-field';
      bodyBox.innerHTML = '<div class="res-mail-label">Body' + copyButton('Copy') + '</div>';
      var bodyValue = document.createElement('div');
      bodyValue.className = 'res-mail-value';
      group.nodes.forEach(function (n) {
        if (n === subjectNode) { return; }
        // Drop a bare "Body:" label — the box is already labelled.
        if (/^\s*body\s*:?\s*$/i.test(n.textContent || '')) { n.remove(); return; }
        bodyValue.appendChild(n);
      });
      bodyBox.appendChild(bodyValue);
      card.appendChild(bodyBox);

      wrap.appendChild(card);
    });

    if (changed) { container.appendChild(wrap); }
    return changed;
  }

  /**
   * A stored Studio resume (saved as a JSON string) shown as a readable
   * document rather than as JSON.
   *
   * Lives here because History and the Dashboard both needed it and each had
   * written its own. That is not hypothetical duplication: skills changed
   * shape from [{name}] to [{category, items}], the History copy was updated,
   * the Dashboard copy was not, and every skill badge in the Dashboard modal
   * silently rendered as an empty pill until someone opened one and looked.
   *
   * Both shapes are still accepted — resumes saved before that change are
   * still in these lists.
   */
  function renderResume(data) {
    if (!data || typeof data !== 'object') { return null; }
    var e = escapeHtml;
    var html = '<div class="res-cv">';

    if (data.full_name)   { html += '<h2 class="res-cv-name">' + e(data.full_name) + '</h2>'; }
    if (data.target_role) { html += '<div class="res-cv-role">' + e(data.target_role) + '</div>'; }

    var contact = ['email', 'phone', 'location', 'linkedin', 'github']
      .map(function (k) { return data[k]; })
      .filter(Boolean)
      .map(function (v) { return '<span>' + e(v) + '</span>'; });
    if (contact.length) { html += '<div class="res-cv-contact">' + contact.join('') + '</div>'; }

    if (data.summary) {
      html += '<h3 class="res-cv-h">Summary</h3><p class="res-cv-p">' + e(data.summary) + '</p>';
    }

    function entries(list, title, primary, secondary, meta) {
      if (!Array.isArray(list) || !list.length) { return ''; }
      var out = '<h3 class="res-cv-h">' + e(title) + '</h3>';
      list.forEach(function (item) {
        if (!item) { return; }
        out += '<div class="res-cv-entry">';
        out += '<div class="res-cv-entry-head"><strong>' + e(item[primary] || '') + '</strong>';
        if (meta && item[meta]) { out += '<span class="res-cv-dates">' + e(item[meta]) + '</span>'; }
        out += '</div>';
        if (secondary && item[secondary]) {
          out += '<div class="res-cv-sub">' + e(item[secondary]) + '</div>';
        }
        if (Array.isArray(item.bullets) && item.bullets.length) {
          out += '<ul class="res-cv-bullets">' +
                 item.bullets.map(function (b) { return '<li>' + e(b) + '</li>'; }).join('') +
                 '</ul>';
        }
        out += '</div>';
      });
      return out;
    }

    html += entries(data.experience, 'Experience', 'title', 'company', 'dates');
    html += entries(data.projects, 'Projects', 'title', 'tech_stack', null);

    var groups = Array.isArray(data.skills) ? data.skills : [];
    if (groups.length) {
      var skillsHtml = '';
      groups.forEach(function (group) {
        var names = [];
        if (typeof group === 'string')                 { names = [group]; }
        else if (group && Array.isArray(group.items))  { names = group.items; }
        else if (group && group.name)                  { names = [group.name]; }
        names = names.map(function (n) { return String(n).trim(); }).filter(Boolean);
        if (!names.length) { return; }

        var label = (group && group.category) ? String(group.category).trim() : '';
        if (label) { skillsHtml += '<div class="res-cv-skill-cat">' + e(label) + '</div>'; }
        skillsHtml += '<div class="res-cv-skill-row">' +
          names.map(function (n) { return '<span class="res-cv-skill">' + e(n) + '</span>'; }).join('') +
          '</div>';
      });
      if (skillsHtml) { html += '<h3 class="res-cv-h">Skills</h3>' + skillsHtml; }
    }

    if (Array.isArray(data.education) && data.education.length) {
      html += '<h3 class="res-cv-h">Education</h3>';
      data.education.forEach(function (ed) {
        if (!ed) { return; }
        var line = [ed.degree, ed.school, ed.dates].filter(Boolean).join(' — ');
        if (line) { html += '<div class="res-cv-sub">' + e(line) + '</div>'; }
      });
    }

    if (Array.isArray(data.languages) && data.languages.length) {
      html += '<h3 class="res-cv-h">Languages</h3><div class="res-cv-sub">' +
              e(data.languages.join(', ')) + '</div>';
    }

    return html + '</div>';
  }

  /**
   * Render whatever a Generation row stored: a Studio resume comes back as a
   * JSON string, everything else is markdown.
   */
  function renderStored(text, options) {
    var raw = (text || '').trim();
    if (raw.charAt(0) === '{' && raw.charAt(raw.length - 1) === '}') {
      try {
        var asResume = renderResume(JSON.parse(raw));
        if (asResume) { return asResume; }
      } catch (_) { /* not a resume — fall through to markdown */ }
    }
    return renderMarkdown(raw, options);
  }

  /* One delegated handler for every copy button this file emits. */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-res-copy]');
    if (!btn) { return; }
    e.preventDefault();
    var field = btn.closest('.res-mail-field');
    var value = field && field.querySelector('.res-mail-value');
    var text = value ? (value.innerText || '').trim() : '';
    if (!text) { return; }

    var done = function (ok) {
      var original = btn.innerHTML;
      btn.innerHTML = ok
        ? '<i class="fas fa-check" aria-hidden="true"></i>Copied'
        : '<i class="fas fa-triangle-exclamation" aria-hidden="true"></i>Failed';
      btn.classList.toggle('is-copied', ok);
      window.setTimeout(function () {
        btn.innerHTML = original;
        btn.classList.remove('is-copied');
      }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      done(false);
    }
  });

  /**
   * Render `text` into `el`.
   *
   * `kind` picks the structural enhancer: 'ats', 'interview' or 'followup'.
   * Every enhancer returns without changing anything when the output does not
   * have the shape it expects, so an unexpected wording degrades to plain
   * rendered markdown rather than an empty panel.
   */
  function render(el, text, options) {
    if (!el) { return; }
    var opts = options || {};
    el.innerHTML = renderMarkdown(text);

    var kind = opts.kind || (opts.ats ? 'ats' : '');
    if (kind === 'ats') { enhanceAts(el, opts.score); }
    else if (kind === 'interview') { enhanceInterview(el); }
    else if (kind === 'followup') { enhanceFollowup(el); }
    return el;
  }

  global.CVAIResult = {
    escapeHtml: escapeHtml,
    renderMarkdown: renderMarkdown,
    renderResume: renderResume,
    renderStored: renderStored,
    enhanceAts: enhanceAts,
    enhanceInterview: enhanceInterview,
    enhanceFollowup: enhanceFollowup,
    render: render
  };
}(window));
