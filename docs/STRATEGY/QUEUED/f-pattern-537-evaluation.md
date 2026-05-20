# QUEUED: f-pattern-537-evaluation

> **STATUS: DONE 2026-05-18.** Operator chose Path A (promote now). Mig 247 + commit `2e61287` shipped same session. Pid 537 now in `pilot_promoted`. Watch list at n=15 — see memory entry `project_2026_05_18_pid537_path_a` and CURRENT_PLAN Algo-trader re-eval section for the data-scientist caveats recorded at decision time. Bonus: pattern 585 auto-elevated `pilot_promoted` → `promoted` between probes, confirming the Tier A unblock works end-to-end.

**Origin:** 2026-05-18 post-deploy verification surfaced pid 537 ("Falling Wedge Breakout + Trend Reclaim", currently `lifecycle_stage='challenged'`) as having a **29.6:1 realized payoff ratio** over 7 closed trades (avg winner +4.81%, avg loser −0.16%), with **+$85.96 total realized PnL in 90d** — the #2 contributor after pattern 585. Worth a Cowork strategy decision: is this a second alpha worth promoting, or n=7 noise?

## Goal

A Cowork-level decision on pid 537's lifecycle status, grounded in CPCV evidence + the realized 90d data, with explicit handling of the small-sample-size uncertainty.

## Why now

Tier A protection (`payoff_ratio >= 1.5 AND n >= 5`) just shipped, but pid 537's n=7 is below the protection min_n. So if 537's WR drops further and it qualifies for the WR demote, it's currently unprotected and would die. If it IS a real alpha we should know before that happens.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/promotion_gate.py` — the CPCV / DSR / PBO gate. Pull pid 537's existing CPCV evidence; re-run if stale.
- `app/services/trading/pattern_stats_accessor.py` — for the "what does the brain currently believe about this pattern" read.
- `app/services/trading/lifecycle.py` — if the decision is to promote, use `transition_on_decay`-style transitions (NOT raw UPDATE).
- Diagnostic: `scripts/probe_breaker_arming.py`-style helper is the right shape for the investigation phase.

## Investigation phase (read-only)

1. `SELECT * FROM scan_patterns WHERE id=537` — capture full state.
2. Inspect `rules_json` and `bench_walk_forward_json` — what is the pattern actually triggering on?
3. Look at the 7 realized trades: tickers, dates, entry/exit, holding period.
4. If `cpcv_median_sharpe IS NOT NULL`: report it; promotion_gate verdict.
5. If CPCV is NULL or stale: schedule a CPCV re-run via `trading_backtests` queue.

## Decision path

After investigation:

- **Path A (promote 537 to pilot_promoted)** — if CPCV passes AND realized 7-trade sample is consistent with the CPCV distribution.
- **Path B (queue CPCV re-eval)** — if CPCV is stale or never ran on the current rules_json.
- **Path C (keep in challenged + monitor)** — if CPCV doesn't pass and 7 trades feels too thin.

Default suggestion: Path A IFF `cpcv_median_sharpe >= 1.0 AND promotion_gate_passed=True`. Otherwise Path B.

## Out of scope

- Promoting any other "challenged" patterns proactively. This brief is 537-only.
- Modifying the `chili_pattern_demote_payoff_ratio_min_n` floor below 5. Today's value is the right conservative default; tuning belongs to a separate brief.
- Investigating the other 9 challenged patterns with non-trivial payoff ratios (pid 8, 1066, 1073, 706, etc.). They're recorded in the post-deploy probe for future reference.

## Success criteria

A decision recorded in `docs/STRATEGY/COWORK_DECISIONS_LOG.md` (or equivalent) with:

- Pattern 537's CPCV evidence summary (or note that it's missing).
- The realized 7-trade detail.
- The chosen path (A/B/C) with one-paragraph justification.
- If Path A: one-shot SQL migration N+1 to flip lifecycle, mirroring mig 245's safety-belt pattern.

## Constraints

- **Never write `lifecycle_stage='promoted'` directly.** The convention is `pilot_promoted` for first-stage live promotion; `promoted` is reserved for patterns that have proven out at pilot.
- **No magic numbers.** If CPCV evidence is weak, queue a re-run; don't override with a hardcoded promotion.

## Rollback plan

If Path A is taken and 537 turns out to be n=7 noise:

- A future `run_thin_evidence_demote` cycle with default settings will catch it once n hits 30 (the min-realized-trades floor).
- If urgent: one-line SQL to flip back to `challenged`.

## Estimated complexity

- Investigation: 30 min.
- Decision write-up: 30 min.
- Optional Path A migration: 15 min.

Single Cowork strategy session is sufficient; no CC execution needed unless Path B (CPCV re-eval) is chosen.
