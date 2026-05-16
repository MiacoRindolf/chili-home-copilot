# NEXT_TASK: f-phase3-stop-bleed (Phase 3 stop-bleed from 2026-05-15 quant audit)

STATUS: DONE

## Goal

Ship the four "stop-the-bleeding" items from the Phase 3 roadmap of the
2026-05-15 quant audit:

1. Monthly-realized-drawdown gate inside the existing
   `check_drawdown_breaker` (so April-2026-class single-month bleeds
   trip the kill switch).
2. Five rejection-histogram code defects (NameError diagnostic
   improvement, INVALID_ARGUMENT product_id normalizer, Insufficient-balance
   pre-flight, stop-not-below-entry producer fix, model-layer scan_pattern_id
   guard).
3. BNB-USD zombie row cleanup (migration 243).
4. Verification: re-run discovery probe; confirm rejection counts drop.

## Brief

`docs/STRATEGY/QUEUED/f-phase3-stop-bleed.md`

## Context

- `docs/AUDITS/audit-discovery-stats-output.txt`
- `docs/AUDITS/audit-no-pattern-timing-output.txt`
- Re-runnable via `.\scripts\dispatch-audit-discovery.ps1` and
  `.\scripts\dispatch-audit-no-pattern-timing.ps1` through the
  `_claude_daemon` dispatch loop.

## Deliverables (per brief)

D1. Monthly DD extension in `portfolio_risk.py:check_drawdown_breaker`,
    threshold computed empirically (Gaussian lower-bound on 30-day
    realized PnL from CHILI-attributed history; no hardcoded dollar
    amount). Flags: `chili_monthly_dd_breaker_enabled` (default off),
    `chili_monthly_dd_breaker_lower_bound_sigmas` (default 2.0 = 95%).
D2. NameError diagnostic improvement at `auto_trader.py:1602`
D3. Product-ID normalizer (`_normalize_product_id`) in `coinbase_spot.py`
D4. Pre-flight cash-check in three Coinbase placement methods
D5. Upstream producer fix for `stop_not_below_entry` alerts
D6. `@validates("scan_pattern_id")` guard at `app/models/trading.py:Trade`
D7. Migration 243: BNB-USD zombie row (id=1861) cleanup
D8. `tests/test_phase3_stop_bleed.py` — one test per deliverable
D9. Post-deploy: re-run audit-discovery-stats probe + record deltas

## Hard constraints

- One commit per deliverable (clean audit trail).
- All tests pass before deploy.
- Drawdown breaker default OFF; operator flips ON after walk-forward
  shows it would have tripped ~2026-04-22.
- No alpha-generation code touched (no autotrader entry-logic changes,
  no pattern miner changes, no LLM cascade changes — only gates).
- TEST_DATABASE_URL ends in `_test`.
- D5 (producer fix) may be deferred to a follow-up brief if non-obvious;
  ship D1–D4, D6–D9 regardless. The existing rule at
  `auto_trader_rules.py:915` already rejects the bad orders.

## Result

CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-15_phase3-stop-bleed.md`,
covering: 7 commits, walk-forward DD-breaker simulation (would have
tripped on/around 2026-04-22), tests passing, post-deploy histogram
confirming initial movement on the four bug counts.
