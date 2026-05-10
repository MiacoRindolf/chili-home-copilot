# f-coinbase-bracket-coverage-fix

## Background

After operator's force-recreate at 2026-05-10 ~10:53 PT, env propagated:
`BRAIN_LIVE_BRACKETS_MODE=authoritative`,
`CHILI_BRACKET_SWEEP_WRITER_ENABLED=1`. broker-sync-worker fires
bracket_reconciliation every 60s with `mode=authoritative`. Yet 9 open
Coinbase trades remain unprotected at the venue.

```
trade  ticker      stop_loss     intent  broker_stop_order_id
1846   RARE-USD    0.015542      none    -
1845   ACX-USD     0.040748      none    -
1844   1INCH-USD   0.092324      none    -
1843   AERGO-USD   0.051784      none    -
1842   ACS-USD     0.00019208    239     NULL
1841   ALEPH-USD   0.01458       none    -
1840   ACH-USD     0.00713391    none    -
1839   COTI-USD    0.011963      none    -
1838   FIDA-USD    0.015779      none    -
```

Phase 6 LIVE soak has been running since 2026-05-09. Phase 4 wired
`coinbase` into `bracket_writer_g2._SUPPORTED_VENUES` (line 367) and
shipped `place_stop_limit_order_gtc`. Phase 5 shipped cost_aware_gate.
Yet none of the 9 trades have a GTC stop at Coinbase. The bugs are not
in the new code — they're in how the new code is *wired* into the
existing entry-time and reconcile-time flows.

## Bugs identified (from probe data)

### Bug A — Intent emission only fires on alert events, never at entry

`stop_engine.py:_maybe_emit_bracket_intent()` is called from a single
site at line 938:

```python
if result.alert_event and result.alert_event != "DATA_STALE":
    _record_stop_decision(db, trade.id, result)
    _apply_stop_to_trade(db, trade, result)
    _maybe_emit_bracket_intent(db, trade, brain)
```

For a freshly-entered trade with `stop_loss` set by autotrader, no alert
event is generated until price approaches the stop. So the bracket_intent
is never created at entry. Trade 1842 ACS-USD was likely the first of
the 9 to trigger an alert path (price moved enough to fire trail/move-to-
breakeven logic) — that's why it has an intent and the others don't.

**Evidence**: probe Stage F shows zero `stop_engine` log lines in
autotrader-worker over the last 10 minutes despite 4 fresh Coinbase
entries today. The emitter would have logged at line 739 if it ran.

**Fix path**: emit at trade-entry time (autotrader path) AND on every
sweep when stop_loss > 0 and no intent row exists. Whichever path lands
first wins; idempotent upsert handles concurrent calls. Do not gate this
on `result.alert_event` — that's an exit-path concern, not an
entry-time concern.

### Bug B — Reconciler doesn't backfill intents for trades with stop_loss

`bracket_reconciliation_service.py` scans existing `trading_bracket_intents`
rows. For trades with `stop_loss > 0` but NO intent row, the reconciler
does nothing. Combined with Bug A, this means a trade entered while the
stop_engine doesn't fire an alert path is permanently invisible to the
bracket system.

**Evidence**: sweep_summary shows `trades_scanned=25 brackets_checked=11`.
Of 25 open trades, only 11 have intent rows that the reconciler can
process.

**Fix path**: in the reconciler, for each open trade with `stop_loss > 0`
that has no intent row, call the same upsert path stop_engine would.
This is a no-op for trades that already have intents and a recovery
mechanism for trades that don't. Same idempotency semantics.

### Bug C — Writer attempt on Coinbase trade 1842 produces zero log lines

Trade 1842 ACS-USD has `bracket_intents.id=239`, `intent_state=intent`,
`broker_stop_order_id=NULL`. The reconciler classified it as
`missing_stop` in multiple sweeps:

```
[bracket_reconciliation_ops] event=discrepancy mode=authoritative
trade_id=1842 bracket_intent_id=239 ticker=ACS-USD broker_source=coinbase
kind=missing_stop severity=warn
```

But no corresponding `[bracket_writer_g2] place_missing_stop` log line
exists for this trade. The 20 place_missing_stop log lines in the
window are 100% Robinhood crypto pairs hitting
`venue_unsupported_crypto_path`. Either:

(i) The reconciler skips Coinbase trades silently before invoking the
    writer (pre-filter we don't see in stdout).
(ii) The writer is invoked but an early-return path doesn't log.
(iii) A throttle / cooldown blocks placement and doesn't surface the
      reason.

**Fix path**: trace the call from `event=discrepancy` → writer entry-
point. Find why no log lines appear. Add explicit logging at every
short-circuit so silent skips become visible. If a real bug is found,
fix it. The Phase 4 wire-up to `place_stop_limit_order_gtc` should be
exercised, not bypassed.

## Plan-gate protocol

CC writes `scripts/_claude_session_consult/<session_id>/plan.request.md`
covering:

(a) Files to modify with absolute paths, current `wc -l` for each, and
    Write-vs-Edit strategy per file.
(b) For Bug A: where to add the entry-time emission (auto_trader.py
    fill-handler vs stop_engine sweep vs bracket_reconciliation
    pre-pass). Reasoning for the chosen site.
(c) For Bug B: the reconciler-side change. Be careful: the reconciler
    sweeps `chili-broker-sync-worker-1`, runs every 60s. A bug here
    that creates spurious intents will propagate fast.
(d) For Bug C: the investigation path. Read the actual code from
    discrepancy → writer to identify the silent-skip site, instrument
    it, and fix.
(e) Tests in `tests/test_coinbase_bracket_coverage.py` (NEW file, use
    Write from start). At minimum:
      - new Coinbase trade with stop_loss creates intent at entry-time
        (or sweep-time, depending on chosen site)
      - reconciler with stop_loss + no intent backfills the intent
      - place_missing_stop on Coinbase trade attempts
        place_stop_limit_order_gtc and emits a log line either way
(f) Hot-fix SQL for ACS-USD #1842: operator manually closed at the
    broker yesterday but DB row is still status=open. The fix should
    UPDATE this row to closed using the actual exit_price the operator
    sold at (operator will need to provide the exit_price; CC writes
    the SQL skeleton).
(g) Verification queries to include in CC_REPORT:

```sql
-- All open Coinbase trades should have an intent row after fix:
SELECT t.id, t.ticker, bi.intent_state, bi.broker_stop_order_id
  FROM trading_trades t
  LEFT JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.status='open' AND t.broker_source='coinbase';

-- Sweep should now report missing_stop ≈ 0 for Coinbase if writer works:
-- (look in broker-sync-worker logs for sweep_summary events after deploy)
```

## CC step-by-step

1. **Read in this exact order**:
   - `CLAUDE.md`
   - `docs/STRATEGY/PROTOCOL.md`
   - `docs/STRATEGY/COWORK_ADVISOR_BRIEF.md`
   - `docs/STRATEGY/NEXT_TASK.md`
   - This brief
   - Memory: `project_2026_05_10_naked_coinbase_positions.md`
   - `app/services/trading/stop_engine.py` (focus 685-740, 920-960)
   - `app/services/trading/bracket_intent_writer.py` (focus
     `upsert_bracket_intent` and idempotency semantics)
   - `app/services/trading/bracket_reconciliation_service.py` (focus
     the missing_stop branch and how trades without intent rows are
     filtered out)
   - `app/services/trading/bracket_writer_g2.py:place_missing_stop`
   - `app/services/trading/auto_trader.py` (find the fill-handler that
     writes trade.stop_loss; this is a candidate Bug A fix site)

2. **Plan**. Write `plan.request.md`. Cover sections (a) through (g)
   above.

3. **Wait** for `plan.response.md`. Watcher should auto-approve given
   high real-money urgency, but humans may intervene.

4. **Read the response**. APPROVED → proceed; REVISE → re-plan; ABORT →
   stop.

5. **Implement**. wip-commit after each meaningful piece. Run pytest
   for the new test file. Force-recreate workers + verify settings
   propagate.

6. **Final commit**. CC_REPORT at
   `docs/STRATEGY/CC_REPORTS/2026-05-10_f-coinbase-bracket-coverage-fix.md`.
   Update NEXT_TASK to STATUS: DONE.

## Hard constraints

- Crypto-side only. **Do not** touch options/equity entry code or
  any non-crypto exit code.
- Edit-tool truncation discipline: use `Write` for any file >500 lines.
  After each modification: `wc -l <file>`, `git diff --stat -- <file>`,
  `python -c "import ast; ast.parse(open('<file>').read())"`. If
  `wc -l` drops by more than your edit added, STOP and
  `git checkout HEAD -- <file>` then retry with Write.
- Coinbase Phase 6 LIVE soak active. Do not disable, weaken, or
  bypass existing safety gates (cap flag, side check, post-place
  verify, etc.). Phase 4's `_SUPPORTED_VENUES = {"robinhood",
  "coinbase"}` is a green light to USE the new path, not to bypass it.
- No magic-fallback values for missing measurements. NULL/None
  propagate; gate decisions explicitly.
- Plan-gate protocol active.
