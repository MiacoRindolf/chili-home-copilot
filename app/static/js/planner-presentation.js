/* Pure presentation helpers shared by the planner and its DB-free checks. */
(function(root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PlannerPresentation = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function(c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function localDate(date) {
    var d = date || new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  function color(value) {
    return /^#(?:[a-f0-9]{3}|[a-f0-9]{6})$/i.test(String(value)) ? value : '#6366f1';
  }

  function flatten(tasks) {
    var result = [], seen = new Set();
    function visit(items) {
      (items || []).forEach(function(t) {
        var key = t.id == null ? t : String(t.id);
        if (seen.has(key)) return;
        seen.add(key);
        result.push(t);
        visit(t.subtasks);
      });
    }
    visit(tasks);
    return result;
  }

  function overdue(task, today) {
    return task.status !== 'done' && !!task.end_date && task.end_date < (today || localDate());
  }

  function needsAttention(task, today) {
    return task.status !== 'done' && (task.status === 'blocked' || overdue(task, today));
  }

  function summarize(tasks, today) {
    var all = flatten(tasks), date = today || localDate();
    var result = { total: all.length, done: 0, open: 0, inProgress: 0, blocked: 0, overdue: 0, attention: 0, todo: 0, percent: 0 };
    all.forEach(function(t) {
      if (t.status === 'done') result.done++;
      else result.open++;
      if (t.status === 'todo') result.todo++;
      if (t.status === 'in_progress') result.inProgress++;
      if (t.status === 'blocked') result.blocked++;
      if (overdue(t, date)) result.overdue++;
      if (needsAttention(t, date)) result.attention++;
    });
    result.percent = result.total ? Math.round(result.done / result.total * 100) : 0;
    return result;
  }

  function sortByPriority(tasks) {
    var ranks = { critical: 0, high: 1, medium: 2, low: 3 };
    return tasks.slice().sort(function(a, b) {
      var priority = (ranks[a.priority] == null ? 2 : ranks[a.priority]) - (ranks[b.priority] == null ? 2 : ranks[b.priority]);
      if (priority) return priority;
      var due = (a.end_date || '9999').localeCompare(b.end_date || '9999');
      return due || ((a.sort_order || 0) - (b.sort_order || 0)) || (Number(a.id) - Number(b.id));
    });
  }

  function matches(task, filters, scope, today) {
    var f = filters || {};
    if (scope === 'open' && task.status === 'done') return false;
    if (scope === 'done' && task.status !== 'done') return false;
    if (scope === 'attention' && !needsAttention(task, today)) return false;
    if (f.assignee && task.assigned_to !== parseInt(f.assignee, 10)) return false;
    if (f.status && task.status !== f.status) return false;
    if (f.priority && task.priority !== f.priority) return false;
    if (f.label && !(task.labels || []).some(function(l) { return String(l.id) === String(f.label); })) return false;
    if (f.search) {
      var term = f.search.toLowerCase();
      if ((task.title || '').toLowerCase().indexOf(term) < 0 && (task.description || '').toLowerCase().indexOf(term) < 0 && String(task.id).indexOf(term) < 0) return false;
    }
    return true;
  }

  // Escape every source token before adding our own fixed formatting tags.
  // Deliberately no raw HTML, images, or active links in task descriptions.
  function inline(text) {
    return String(text).split(/(`[^`]+`)/g).map(function(piece) {
      if (/^`[^`]+`$/.test(piece)) return '<code>' + escapeHtml(piece.slice(1, -1)) + '</code>';
      return escapeHtml(piece).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/__([^_]+)__/g, '<strong>$1</strong>');
    }).join('');
  }

  function description(text) {
    var lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
    var output = [], paragraph = [], list = null, code = null;
    function flushParagraph() {
      if (paragraph.length) output.push('<p>' + paragraph.map(inline).join('<br>') + '</p>');
      paragraph = [];
    }
    function closeList() { if (list) output.push('</' + list + '>'); list = null; }
    lines.forEach(function(line) {
      if (/^\s*```/.test(line)) {
        flushParagraph(); closeList();
        if (code !== null) { output.push('<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>'); code = null; }
        else code = [];
        return;
      }
      if (code !== null) { code.push(line); return; }
      var heading = line.match(/^\s*#{1,6}\s+(.+)$/);
      var item = line.match(/^\s*(?:([-*+])|(\d+)[.)])\s+(.+)$/);
      if (heading) { flushParagraph(); closeList(); output.push('<h3>' + inline(heading[1]) + '</h3>'); }
      else if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { flushParagraph(); closeList(); output.push('<hr>'); }
      else if (item) {
        flushParagraph();
        var tag = item[1] ? 'ul' : 'ol';
        if (list !== tag) { closeList(); list = tag; output.push('<' + tag + '>'); }
        output.push('<li' + (item[2] ? ' value="' + Number(item[2]) + '"' : '') + '>' + inline(item[3]) + '</li>');
      } else if (!line.trim()) { flushParagraph(); closeList(); }
      else { closeList(); paragraph.push(line); }
    });
    flushParagraph(); closeList();
    if (code !== null) output.push('<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>');
    return output.join('');
  }

  return { escapeHtml: escapeHtml, color: color, localDate: localDate, flatten: flatten, overdue: overdue,
    needsAttention: needsAttention, summarize: summarize, sortByPriority: sortByPriority,
    matches: matches, description: description };
});
