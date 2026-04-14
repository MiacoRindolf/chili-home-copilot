var _tbaMessages = [];

function _tbaPct(v) {
  var n = Number(v);
  if (!isFinite(n)) return 'n/a';
  if (n <= 1) n = n * 100;
  return Math.round(n) + '%';
}

function _tbaText(v) {
  return v == null ? '' : escHtml(String(v));
}

function _tbaRenderRecommendations(m) {
  if (!m || !Array.isArray(m.recommendations) || !m.recommendations.length) return '';
  var html = ['<div class="tba-rec-grid">'];
  m.recommendations.forEach(function(rec) {
    var rationale = Array.isArray(rec.rationale) ? rec.rationale : [];
    var missing = Array.isArray(rec.missing_context) ? rec.missing_context : [];
    html.push('<div class="tba-rec-card">');
    html.push('<div class="tba-rec-top"><div class="tba-rec-action">' + _tbaText(rec.action || 'wait') + ' Â· ' + _tbaText(rec.symbol || rec.market || 'market') + '</div><div class="tba-rec-conf">Confidence ' + _tbaPct(rec.confidence) + '</div></div>');
    html.push('<div class="tba-rec-thesis">' + _tbaText(rec.thesis) + '</div>');
    if (rationale.length) {
      html.push('<ul class="tba-rec-list">');
      rationale.forEach(function(item) { html.push('<li>' + _tbaText(item) + '</li>'); });
      html.push('</ul>');
    }
    html.push('<div class="tba-rec-meta">');
    html.push('<div><strong>Entry</strong>' + _tbaText(rec.entry || 'Wait for trigger') + '</div>');
    html.push('<div><strong>Invalidation</strong>' + _tbaText(rec.invalidation || rec.risk_note || 'Monitor risk gates') + '</div>');
    html.push('<div><strong>Exit logic</strong>' + _tbaText(rec.exit_logic || 'Adaptive exit') + '</div>');
    html.push('<div><strong>Timeframe</strong>' + _tbaText(rec.timeframe || 'Active desk') + '</div>');
    html.push('<div><strong>Sizing</strong>' + _tbaText(rec.sizing_guidance || 'Use policy-bound sizing') + '</div>');
    html.push('<div><strong>Change trigger</strong>' + _tbaText(rec.what_would_change || 'New contrary signal or readiness change') + '</div>');
    html.push('</div>');
    if (rec.execution_readiness) {
      html.push('<div class="tba-rec-fidelity"><strong style="color:var(--text-muted)">Execution readiness:</strong> ' + _tbaText(rec.execution_readiness.status || rec.execution_readiness.reason || JSON.stringify(rec.execution_readiness)) + '</div>');
    }
    if (missing.length) {
      html.push('<div class="tba-rec-missing"><strong>Missing context:</strong> ' + _tbaText(missing.join(', ')) + '</div>');
    }
    if (rec.source_of_truth_provider || rec.source_of_truth_exchange) {
      html.push('<div class="tba-rec-fidelity"><strong style="color:var(--text-muted)">Market truth:</strong> ' + _tbaText(rec.source_of_truth_provider || 'provider') + (_tbaText(rec.source_of_truth_exchange) ? ' Â· ' + _tbaText(rec.source_of_truth_exchange) : '') + '</div>');
    }
    html.push('</div>');
  });
  html.push('</div>');
  return html.join('');
}

function _tbaRenderMessages() {
  var container = document.getElementById('tba-messages');
  if (!container) return;
  container.innerHTML = '';
  _tbaMessages.forEach(function(m) {
    var wrap = document.createElement('div');
    wrap.className = 'tba-msg ' + m.role;
    var roleEl = document.createElement('div');
    roleEl.className = 'tba-msg-role';
    roleEl.textContent = m.role === 'user' ? 'You' : 'Assistant';
    var bubble = document.createElement('div');
    bubble.className = 'tba-msg-bubble';
    if (m.role === 'assistant' && typeof marked !== 'undefined') {
      try { bubble.innerHTML = marked.parse(m.content || ''); } catch(e) { bubble.textContent = m.content || ''; }
    } else {
      bubble.textContent = m.content || '';
    }
    if (m.role === 'assistant') {
      bubble.innerHTML += _tbaRenderRecommendations(m);
      if (Array.isArray(m.missing_context) && m.missing_context.length) {
        bubble.innerHTML += '<div class="tba-rec-missing"><strong>Snapshot gaps:</strong> ' + _tbaText(m.missing_context.join(', ')) + '</div>';
      }
    }
    wrap.appendChild(roleEl);
    wrap.appendChild(bubble);
    container.appendChild(wrap);
  });
  container.scrollTop = container.scrollHeight;
}

function _tbaSend() {
  var input = document.getElementById('tba-input');
  var sendBtn = document.getElementById('tba-send');
  if (!input || !sendBtn) return;
  var text = (input.value || '').trim();
  if (!text) return;
  _tbaMessages.push({ role: 'user', content: text });
  input.value = '';
  _tbaRenderMessages();
  sendBtn.disabled = true;
  var loadingEl = document.createElement('div');
  loadingEl.className = 'tba-msg assistant';
  loadingEl.innerHTML = '<div class="tba-msg-role">Assistant</div><div class="tba-msg-bubble tba-loading">Thinking...</div>';
  document.getElementById('tba-messages').appendChild(loadingEl);
  document.getElementById('tba-messages').scrollTop = document.getElementById('tba-messages').scrollHeight;

  var payload = { messages: _tbaMessages.slice(0, -1).concat([{ role: 'user', content: text }]), include_pattern_search: true, refresh: false };
  fetch('/api/brain/trading/assistant/chat', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    var container = document.getElementById('tba-messages');
    var loading = container && container.querySelector('.tba-loading');
    if (loading && loading.closest('.tba-msg')) loading.closest('.tba-msg').remove();
    if (d.ok && (d.reply || (Array.isArray(d.recommendations) && d.recommendations.length))) {
      _tbaMessages.push({
        role: 'assistant',
        content: d.reply || 'Structured recommendation ready.',
        recommendations: d.recommendations || [],
        missing_context: d.missing_context || [],
        snapshot_at: d.snapshot_at || null
      });
      _tbaRenderMessages();
    } else {
      _tbaMessages.push({ role: 'assistant', content: d.error || 'Sorry, I could not reply. Check that the LLM is configured in settings.' });
      _tbaRenderMessages();
    }
  })
  .catch(function() {
    var container = document.getElementById('tba-messages');
    var loading = container && container.querySelector('.tba-loading');
    if (loading && loading.closest('.tba-msg')) loading.closest('.tba-msg').remove();
    _tbaMessages.push({ role: 'assistant', content: 'Network error. Please try again.' });
    _tbaRenderMessages();
  })
  .finally(function() { if (sendBtn) sendBtn.disabled = false; });
}

function _tbaClear() {
  _tbaMessages = [];
  var container = document.getElementById('tba-messages');
  if (container) container.innerHTML = '';
  var input = document.getElementById('tba-input');
  if (input) input.value = '';
}

function initTradingBrainAssistant() {
  var chips = document.querySelectorAll('.tba-chip');
  chips.forEach(function(chip) {
    chip.onclick = function() {
      var input = document.getElementById('tba-input');
      if (input && chip.dataset.msg) input.value = chip.dataset.msg;
    };
  });
  var sendBtn = document.getElementById('tba-send');
  var clearBtn = document.getElementById('tba-clear');
  if (sendBtn) sendBtn.onclick = _tbaSend;
  if (clearBtn) clearBtn.onclick = _tbaClear;
  var input = document.getElementById('tba-input');
  if (input) {
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _tbaSend(); }
    });
  }
}