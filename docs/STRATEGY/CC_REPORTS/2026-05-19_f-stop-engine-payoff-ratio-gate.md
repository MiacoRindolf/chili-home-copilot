# CC_REPORT: f-stop-engine-payoff-ratio-gate

**Session type:** Cowork-direct execution via daemon (operator: "continue the tasks we're working on in this chat" after the kill-switch reset).

## What shipped

**Single commit `c07077c`** on `main` (auto-pushed by daemon), 3 files / +260 LOC:

- `app/config.py` — two new flags: `chili_autotrader_payoff_sizing_enabled` (default `False`) + `chili_autotrader_payoff_min_n` (default `5`)
- `app/services/trading/auto_trader.py` — new payoff-ratio scaler block, composes AFTER pilot_promoted multiplier and BEFORE the `qty = int(notional / px)` final computation
- `tests/test_autotrader_payoff_sizing.py` — 6 new pinned tests

## What it does

The Tier A demote-gate (shipped 2026-05-18, commit `23bde18`) protects skew-driven edges like pattern 585 (4.97:1 payoff over 86 trades, WR 35%) from being mis-demoted on win-rate alone. This brief extends the same payoff_ratio signal to the **autotrader entry-sizing path**.

**Composition chain** (in `_execute_broker_buy`):

```
initial notional (risk-per-trade)
  → HRP risk-parity allocation
    → pattern_survival classifier multiplier
      → pilot_promoted Bayesian confidence
        → ★ NEW: payoff-ratio scaler ★
          → qty = int(notional / px)
```

**Tier table** (hardcoded in the function — tune via follow-up brief if needed):

| Condition | Multiplier | Tier label |
|---|---|---|
| `n < min_n` | 1.0x | `insufficient_n` |
| `payoff_ratio >= 5.0 AND n >= 5` | **1.5x** | `very_high` (e.g., pattern 585 @ 4.97:1) |
| `payoff_ratio >= 2.0 AND n >= 5` | **1.25x** | `high` |
| `payoff_ratio >= 1.0 AND n >= 5` | 1.0x | `moderate` (no-op) |
| `payoff_ratio < 1.0 AND n >= 5` | **0.5x** | `low` (sub-1:1 historical record) |

The multiplier is applied to `notional`; the final `qty` falls out as `int(notional / px)`. Floor protection: if scaling drops notional below `_TEMP_MIN_NOTIONAL_USD`, it's floored (and `snap['payoff_sizing_floored_to_min']=True`).

## Observability

Every entry attempt writes to `snap[]` (which becomes the audit row in `trading_autotrader_runs`):

- `payoff_sizing_tier` — which tier fired
- `payoff_sizing_multiplier` — the scalar applied
- `payoff_ratio_observed` — pattern's payoff_ratio at decision time
- `payoff_ratio_n_observed` — pattern's payoff_ratio_n at decision time
- `notional_before_payoff_sizing` — pre-multiplier notional
- `notional_effective` — post-multiplier notional
- `notional_source` — audit chain (e.g., `"...+payoff"`)
- `payoff_sizing_floored_to_min` — set when down-scaling hit the floor

## Verification

**Tests.** 57/57 PASS:
- 6 new payoff-sizing tests
- 5 maker-only tests (existing)
- 4 bracket-fired-stop tests (existing)
- 5 coinbase-exit tests (existing)
- 27 position-identity Phase 2/3/4 tests (existing)
- 10 pattern-demote payoff-ratio tests (existing)

**Compile.** All 3 modified files clean.

**Deploy.** All 5 services force-recreated cleanly. New flag visible in container env (default False).

**Push.** `5e5bf81..c07077c main -> main` ✓.

## Operator promotion path

When ready to paper-soak the scaler:

1. **Capture pre-flip tier distribution** of recent autotrader_runs:
   ```sql
   -- (Run AFTER the flag is on, since the snap fields only populate then.)
   -- Pre-flip, just verify the data is materialized:
   SELECT id, name, payoff_ratio, payoff_ratio_n, lifecycle_stage
   FROM scan_patterns
   WHERE payoff_ratio_n >= 5
   ORDER BY payoff_ratio DESC NULLS LAST
   LIMIT 20;
   ```
2. **Flip the flag:**
   ```
   CHILI_AUTOTRADER_PAYOFF_SIZING_ENABLED=true
   ```
   (Use ASCII WriteAllBytes per `feedback_never_powershell_outfile_env`.)

3. **Restart:**
   ```
   docker compose up -d --force-recreate autotrader-worker
   ```

4. **Audit the first few entry attempts:**
   ```sql
   SELECT created_at, ticker, decision, reason,
          rule_snapshot->>'payoff_sizing_tier' AS tier,
          rule_snapshot->>'payoff_sizing_multiplier' AS mult,
          rule_snapshot->>'payoff_ratio_observed' AS ratio,
          rule_snapshot->>'payoff_ratio_n_observed' AS n
   FROM trading_autotrader_runs
   WHERE created_at > '<flip_ts>'
   ORDER BY created_at DESC LIMIT 20;
   ```

5. **After ~1 week**, compute realized PnL by tier. If `very_high` and `high` tiers out-realize the baseline (or at minimum aren't worse), promote. If they materially underperform → rollback.

## Surprises / deviations

1. **Composition order found cleanly.** The pattern_survival sizing block at line 2017 already established the pattern of (multiplier + observability + try/except wrap). I composed the new block immediately after pilot_promoted (line 2105) and before the qty computation (line 2107), following the same shape.

2. **Hardcoded tier constants.** Originally considered 7 settings (each multiplier as its own knob). Trimmed to 2 (flag + min_n) — the multiplier values + thresholds live in code. If empirical evidence calls for different tiers, a follow-up brief tunes them.

3. **`settings` is module-level at line 12 of auto_trader.py** — direct reference works. (Other places in the file do `from ...config import settings as _local` for shadow imports; the module-level import handles the new block.)

## Deferred

- **Adaptive sizing based on regime** — when the regime classifier is back online (currently blocked by yfinance, per memory `project_regime_classifier_yfinance_block`), payoff_ratio could be regime-conditioned. Out of scope.
- **Per-broker sizing tiers** — Coinbase positions might warrant tighter tiers given their +153 bps avg slippage. Single-tier-set ships first; refinement is a follow-up.
- **Exit-side payoff scaling** — partial-take-profit logic could also key off payoff_ratio. Separate brief.

## Rollback plan

```
CHILI_AUTOTRADER_PAYOFF_SIZING_ENABLED=false
docker compose up -d --force-recreate autotrader-worker
```

Or revert the commit:
```
git revert c07077c
```

Settings stay; scaler block is removed; no schema impact.

## Status

Code shipped + pushed. Flag stays OFF until operator paper-soaks. NEXT_TASK gets the paper-soak procedure (or operator can pick another queued brief).
