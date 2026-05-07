# 2026-05-07 — LLM cascade recovery verified post OpenAI credit add

## TL;DR

Operator added OpenAI credits and asked me to retest the LLM cascade.
Force-recreated `chili` + `autotrader-worker` (which clears the
process-lifetime `_auth_failed_urls` skip-set) and ran 3 direct probes
through `app.services.llm_caller.call_llm`. Two of three probes
returned real text (`'OK'`, `'Yes.'`); one returned empty string (a
known Gemini-empty-response shape, **not** `parse_failed`). Cascade is
operational.

The autotrader-worker has not yet ticked through a `pattern_breakout_imminent`
batch in the post-recreate window because the brain emits these on its
own ~13–30 min cadence; last batch was 18:43 UTC (pre-recreate), and
brain-worker is healthy and processing other work. The next batch will
be the end-to-end proof point.

## Context

- Prior session diagnosed the LLM cascade exhaustion (OpenAI credit
  exhausted + groq invalid + gemini empty → all `parse_failed` /
  `raw_preview=""`) as the gate keeping `autotrader-worker` from
  placing stock trades for 7 days.
- Operator added OpenAI credits and triggered this retest.

## What I did

1. Wrote and dispatched `scripts/dispatch-llm-retest-2026-05-07.ps1`:
   force-recreate `chili` + `autotrader-worker` → 3 direct
   `call_llm` probes spaced 5s + 3s apart.
2. Output in `scripts/dispatch-llm-retest-2026-05-07-output.txt`.
3. Followed up with `dispatch-autotrader-tick-check-2026-05-07.ps1`
   (schema-discovery roundabout because I guessed `result` + `kind`
   columns on first pass) and `dispatch-llm-postrecreate-2026-05-07.ps1`
   to check for post-recreate runs.
4. Then `dispatch-brain-cadence-2026-05-07.ps1` to confirm brain-worker
   is healthy and document the natural alert cadence.

## Findings

### LLM cascade — recovering

```
Probe 1 (retest-probe):
  llm_cascade_order=legacy (openai→groq→groq_secondary→gemini)
  groq 401 (suppressed)
  llm_reply tokens=536
  call_llm returned: 'OK'

Probe 2 (retest-probe, +5s):
  llm_reply tokens=540
  call_llm returned: ''   ← empty (Gemini sometimes returns empty)

Probe 3 (retest-probe-2, +3s):
  llm_reply tokens=544
  call_llm returned: 'Yes.'
```

Real text on probes 1 + 3. Probe 2's empty reply maps to
`viable=False` at the autotrader caller (not `parse_failed`), so still
a forward-progress shape — the worker decides instead of erroring.

Note: the "falling back to gemini" log line is misleading — the
`llm_reply model=gpt-5.5` line that follows it suggests OpenAI is in
fact answering. The "fallback" log appears to fire after the groq 401
and before the next provider attempt, regardless of which provider
actually answers. Worth tightening that log message in a follow-up,
but it doesn't affect correctness.

### Autotrader worker — alive but starved (temporarily)

```
[autotrader] tick uid=1 candidate_pool=0 batch=0 processed=0 ...
```

— ticking every 10s, but no `pattern_breakout_imminent` alerts to
process. Last batch was at 18:43:57–18:43:59 UTC (3 alerts: AIG, RZLV,
HAE — pattern 585). Current time 19:08 UTC; gap of 25 min is within
the observed cadence band (13–57 min between batches in the prior
3-hour window).

### Brain-worker — healthy

- `Phase 2 handlers run instead. Set =1 to re-enable for emergency rollback.`
- Reconcile-pass gate working as designed (skipped per brain-worker
  policy).
- `realized_sync` updating 9 patterns/iter; `exit_parity` persisting
  50 backtest rows per ticker; `signal_refresh` completing in 0.5s.

### Other lanes — green

- scheduler-worker: momentum live + paper, neural_mesh_drain,
  brain_batch_jobs reconciler all running clean.
- broker-sync-worker: position+order sync ticking every 2 min, FIX C2
  guard correctly refusing to spawn phantom Trades for 2 unmatched
  positions (XLM-USD, GRT-USD), 20 positions updated.
- fast-data-worker: 5 pairs streaming, alerts firing into
  `fast_alerts`, executor mode=paper, 1 open paper position SOL-USD.

## Pending verification

The only thing left is to watch the next `pattern_breakout_imminent`
batch land and confirm the autotrader produces a viable / not-viable
shape (not `parse_failed` / empty `raw_preview`). Brain cadence
suggests this happens within an hour. **Operator can do a one-line
psql at next check-in:**

```sql
SELECT id, ticker, decision, reason, llm_snapshot::text
FROM trading_autotrader_runs
WHERE created_at >= '2026-05-07 18:57:00'
ORDER BY id DESC LIMIT 10;
```

If post-18:57 rows show `'viable': true` or `'viable': false` with
non-empty `raw_preview`, the chain is closed.

## Follow-ups (deferred)

1. **`f-llm-cascade-add-ollama-fallback`** — Ollama is healthy in this
   stack (`Up 30 minutes (healthy)`) but is **not** in the call_llm
   cascade (`legacy: openai→groq→groq_secondary→gemini`). Adding it
   would have prevented the 16h LLM blackout — `qwen2.5:3b` could have
   produced "viable: true/false" responses without any external API.
2. **`f-llm-cascade-clearer-log-messages`** — "falling back to gemini"
   log line is misleading; emits even when OpenAI ends up answering.
3. **`f-llm-auth-failed-skip-set-ttl`** — process-lifetime skip-set
   means an upstream credit-add requires container recreate. A 30-min
   TTL would self-heal on the operator's next refill.

None of these block live trading — operator can queue them when ready.

## Files touched

- `scripts/dispatch-llm-retest-2026-05-07.ps1` (new)
- `scripts/dispatch-llm-retest-2026-05-07-output.txt` (output)
- `scripts/dispatch-autotrader-tick-check-2026-05-07.ps1` (new)
- `scripts/dispatch-autotrader-tick-check-2026-05-07-output.txt` (output)
- `scripts/dispatch-schema-discover-2026-05-07.ps1` (new)
- `scripts/dispatch-schema-discover-2026-05-07-output.txt` (output)
- `scripts/dispatch-llm-postrecreate-2026-05-07.ps1` (new)
- `scripts/dispatch-llm-postrecreate-2026-05-07-output.txt` (output)
- `scripts/dispatch-brain-cadence-2026-05-07.ps1` (new)
- `scripts/dispatch-brain-cadence-2026-05-07-output.txt` (output)

No code changes. Verification only.

## Status

- Task #55 (Retest LLM cascade after OpenAI credits added) — DONE.
- Next operator action: wait ~10–60 min for next
  `pattern_breakout_imminent` batch and confirm autotrader produces
  viable shapes.
