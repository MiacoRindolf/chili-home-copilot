# QUEUED TASK: f-options-exit-monitor-pattern-exit-now-audit (PROMOTED)

**Promoted to `docs/STRATEGY/NEXT_TASK.md` on 2026-05-06 16:45 UTC after the operator agreed it was the highest-priority un-fixed gap on a live exit lane.**

The full brief content now lives in `NEXT_TASK.md`. This file is preserved as a placeholder so the queue history stays linkable; do not edit. If the brief is ever re-queued (e.g., pre-empted by a higher-priority task before execution), restore the body from `docs/STRATEGY/CC_REPORTS/<date>_f-options-exit-monitor-pattern-exit-now-audit.md` once it ships, or from git history.

---

The original body below is preserved verbatim for reference.

# QUEUED TASK: f-options-exit-monitor-pattern-exit-now-audit

**Originally surfaced during the live debug session on 2026-05-06 that fixed `f-crypto-exit-monitor-pattern-exit-now`. The crypto monitor was missing the LLM/pattern-monitor `exit_now` branch the equity lane has. Pre-emptive grep found the SAME gap exists in `app/services/trading/options/exit_monitor.py::run_options_exit_pass` — no `PatternMonitorDecision` import, no monitor-decision consultation, only price/DTE-based triggers. This brief audits + fixes it before an option position pays the same 20-hour cost TRUMP-USD just paid.**

**Promote to NEXT_TASK in the next available slot. Higher priority than the test brief because it's a real un-fixed gap on a live exit lane.**

The body below is the complete brief.

---

# NEXT_TASK: f-options-exit-monitor-pattern-exit-now-audit

STATUS: PENDING

## Goal

Audit `app/services/trading/options/exit_monitor.py::run_options_exit_pass` for the same architectural gap that crypto carried until 2026-05-06: missing consumption of `trading_pattern_monitor_decisions.action='exit_now'`. If confirmed (highly likely — pre-brief grep already shows `PatternMonitorDecision` is not imported anywhere in the options package), wire it in following the equity-lane shape and the crypto fix from 2026-05-06.

## Why now

1. **Same risk shape as the crypto bug.** Pre-brief grep:

   ```
   $ rg -l 'PatternMonitorDecision|monitor_decision|exit_now|_fresh_monitor' app/services/trading/options/
   (no matches)
   ```

   That is the exact signature crypto had before today's fix: a parallel exit lane that reads `Trade.stop_loss/take_profit` (or, for options, `_evaluate_exit_triggers` over premium / DTE) but ignores the LLM/pattern-monitor's "thesis dead" advisory. Any open option position with a fresh `pattern_monitor_decisions.action='exit_now'` that hasn't simultaneously crossed the stop / TP / DTE thresholds will silently sit untouched the same way TRUMP-USD did.

2. **Options positions are time-sensitive.** Unlike spot crypto, options decay on the clock. A "thesis dead" recommendation that the lane ignores costs theta every cycle, not just opportunity. This is more expensive to ignore than the crypto case was.

3. **Refactor opportunity.** With three lanes (equity, crypto, options) about to share the same `_latest_monitor_decisions_by_trade` + `_fresh_monitor_exit_meta` helpers, factor them into a shared module rather than triplicating. Suggested: `app/services/trading/_exit_monitor_common.py`. The equity and crypto copies already drift in subtle ways (the crypto copy uses `_CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS` mirroring the equity `_MONITOR_EXIT_NOW_MAX_AGE_HOURS`); a shared module prevents future drift.

## Phase 1 — Confirm the gap (READ-ONLY)

Phase 1.1 — verify no monitor consultation exists. Concretely re-grep:

```
rg 'PatternMonitorDecision|_latest_monitor_decisions|_fresh_monitor_exit_meta|exit_now' \
   app/services/trading/options/
```

Expected: zero matches. If matches found, brief halts and CC reports what's there — maybe a partial implementation exists.

Phase 1.2 — check production for surfaced cases. Run a one-shot psycopg2 query (no need for a dispatch script — small enough to inline in the CC report):

```sql
SELECT pmd.id, pmd.trade_id, t.ticker, pmd.action, pmd.created_at, t.entry_date, t.status,
       t.pending_exit_order_id, t.exit_date
FROM trading_pattern_monitor_decisions pmd
JOIN trading_trades t ON t.id = pmd.trade_id
WHERE pmd.action = 'exit_now'
  AND t.status = 'open'
  AND (t.indicator_snapshot::text ILIKE '%option%' OR t.tags::text ILIKE '%option%')
  AND pmd.created_at >= NOW() - INTERVAL '7 days'
ORDER BY pmd.created_at DESC;
```

(Adjust the `is_option_trade` predicate to match the actual classifier in `autopilot_scope.py`.) Goal: count how many open option positions have a stale `exit_now` recommendation right now. The number is the operational cost of the gap. If non-zero, the operator may want to manually close them after the fix lands but before the next monitor cycle catches up.

## Phase 2 — Refactor: shared exit-monitor common module

`app/services/trading/_exit_monitor_common.py` (new file). Move the two helpers from `auto_trader_monitor.py` (lines 149-191):

- `_MONITOR_EXIT_NOW_MAX_AGE_HOURS` constant.
- `latest_monitor_decisions_by_trade(db, trade_ids)` — public name (drop the leading underscore so callers across packages can import it cleanly).
- `fresh_monitor_exit_meta(decision)` — same.

In each consumer:
- `auto_trader_monitor.py`: replace local helpers with `from ._exit_monitor_common import latest_monitor_decisions_by_trade, fresh_monitor_exit_meta, MONITOR_EXIT_NOW_MAX_AGE_HOURS`. Keep behavior identical.
- `crypto/exit_monitor.py`: replace the local helpers (added 2026-05-06 in `f-crypto-exit-monitor-pattern-exit-now`) with the same import. Drop `_CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS` since the shared constant is sufficient.
- `options/exit_monitor.py`: import the shared helpers (this is the new consumer).

Acceptance: existing equity tests at `tests/test_auto_trader_monitor.py:338-454` continue to pass without modification. The refactor is behavior-preserving for equity and crypto.

## Phase 3 — Wire the options lane

In `options/exit_monitor.py::run_options_exit_pass`:

1. After the `candidates = [t for t in open_trades if _is_option_trade(t)]` line (~line 263), batch-load:
   ```python
   latest_monitor_decisions = latest_monitor_decisions_by_trade(
       db, [int(t.id) for t in candidates]
   )
   ```

2. Inside the loop, after `_evaluate_exit_triggers` returns (~line 320 area, after the existing trigger-string handling), add the parity branch:
   ```python
   monitor_exit_meta = None
   if not trigger:
       monitor_exit_meta = fresh_monitor_exit_meta(
           latest_monitor_decisions.get(int(t.id))
       )
       if monitor_exit_meta is not None:
           trigger = "pattern_exit_now"
   if not trigger:
       continue
   ```

3. The success log line should carry the audit detail when monitor-driven, mirroring the crypto pattern:
   ```python
   if monitor_exit_meta is not None:
       logger.info(
           "[options_exit] CLOSED trade#%s contract=%s reason=%s "
           "monitor_decision_id=%s monitor_src=%s monitor_age_h=%s monitor_price=%s",
           t.id, contract.get("id"), trigger,
           monitor_exit_meta.get("decision_id"),
           monitor_exit_meta.get("decision_source"),
           monitor_exit_meta.get("decision_age_hours"),
           monitor_exit_meta.get("decision_price"),
       )
   ```

4. The `pending_exit_reason` column on the option Trade row should be set to canonical `"pattern_exit_now"` (no truncation, no audit-detail concatenation) — same rule as crypto: audit metadata goes in the log line, not the 50-char column.

5. Stop-on-tie ordering: existing price/DTE/premium triggers WIN over `exit_now`. The monitor consultation only runs when `_evaluate_exit_triggers` returns None. (Cheaper to evaluate; price/DTE reasons carry stronger semantics for postmortems than "LLM said so.")

## Phase 4 — Test coverage

New file `tests/test_options_exit_monitor_pattern_exit_now.py`. Mirror the five cases from `f-crypto-exit-monitor-pattern-exit-now-test`:

1. Fresh `exit_now` + premium/DTE/stop in safe range → exit fires with `reason="pattern_exit_now"`.
2. Latest `hold` after older `exit_now` → no exit.
3. `exit_now` older than 96h → no exit.
4. Both DTE-trigger and `exit_now` fresh → DTE-trigger wins (`reason="options_dte_proximity"` or whatever the existing literal is).
5. Implausible mark/bid → no exit even with fresh `exit_now`. (Confirm options has the equivalent of crypto's implausible-quote guard; if not, this is a side finding worth flagging.)

Plus: ONE refactor regression test asserting the equity / crypto / options lanes all import the same `latest_monitor_decisions_by_trade` symbol from `_exit_monitor_common.py`. This catches the next time someone re-introduces a local copy.

## Phase 5 — Postmortem note

Add a brief paragraph to `docs/STRATEGY/CC_REPORTS/2026-05-06_f-crypto-exit-monitor-pattern-exit-now.md`'s "Related queued work" section noting that this brief landed and how the broader pattern (asset-class-split exit lanes losing the LLM advisory) was systematically fixed across all three lanes. Optional but useful for future audits.

## Open questions

1. **Does the option Trade row use the same `pending_exit_*` columns as equity/crypto?** The schema check via `psql \d trading_trades` is in CC's job; if options uses a different column shape (e.g., contract-id instead of order-id), the fix needs to set the right fields. Pre-brief reading suggests the columns are uniform — `pending_exit_order_id` etc. — but worth confirming before assuming.
2. **Is there a separate freshness window appropriate for options?** Equity/crypto use 96h. Options theta-decay says a 96h-old recommendation may be stale even if the LLM hasn't re-evaluated. Consider tightening to 24h or 12h for the options lane only — but only if there's a specific signal saying so. Default to 96h for parity unless data argues otherwise. (If the operator has an opinion here, ask before shipping.)
3. **Does any existing options test mock `PatternMonitorDecision` already?** If so, this brief doubles as test coverage for that path — flag and reuse.

## Out of scope

- Adding `PatternMonitorDecision` writers for options (assumes the brain already writes them — verify via Phase 1.2 query). If the brain writes equity-only, that's a separate and larger brief: `f-options-pattern-monitor-coverage`.
- Changing the freshness window for equity / crypto.
- Adding monitor-decision consultation to non-exit lanes (entry, sizing, etc.).

## Acceptance bar

- Phase 1.2 query result included in CC report — N option positions with stale `exit_now`.
- Phase 2 refactor: equity + crypto tests pass unmodified after the shared module lands.
- Phase 3 fix: `options/exit_monitor.py` imports the shared helpers and wires the parity branch.
- Phase 4 tests: 5 cases + 1 refactor regression test, all passing.
- ZERO behavior change for equity / crypto (verified by existing test suites).
- Postmortem note appended to the 2026-05-06 CC report.
