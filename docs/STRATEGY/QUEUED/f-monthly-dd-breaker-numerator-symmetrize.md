# f-monthly-dd-breaker-numerator-symmetrize — close the open-loop in `check_drawdown_breaker`

## Context

f-phase3-stop-bleed (commit `0fa783f`, 2026-05-15) shipped the
data-driven monthly-DD breaker default-OFF. It computes the threshold
from CHILI-attributed daily PnL (`scan_pattern_id IS NOT NULL AND != -1`)
in `_monthly_dd_threshold` (`app/services/trading/portfolio_risk.py:909`).

But the **numerator** the breaker compares against the threshold —
`monthly_pnl` at `portfolio_risk.py:1088–1101` — has no
`scan_pattern_id` filter. It sums realized PnL across **all closed
trades** in the last 30d, including the no_pattern bleed bucket.

Two problems result:

1. **Statistical incoherence.** K·σ is calibrated on the
   CHILI-attributed daily-PnL distribution; comparing against a
   numerator drawn from a different (all-closed) distribution makes
   the 2σ label meaningless. Per 2026-05-15 quant audit and
   2026-05-16 composite-anti-correlation diagnostic, the no_pattern
   bucket and the CHILI-attributed bucket have wildly different
   means and variances — they are not the same distribution.

2. **Open-loop control bug.** The breaker's only lever is "halt
   CHILI-attributed entries." If a no_pattern bleed trips it, the
   gated channel is *not* the bleed source, so the trip cannot fix
   the cause. Worst case: the system enters an absorbing state where
   no_pattern keeps bleeding → breaker stays tripped → CHILI cannot
   resume → operator must manually reset → repeat.

Today (2026-05-16) the system is **9 close-days** from auto-arming
(n=21 of 30 required; projected arm date ≈ 2026-05-28). On day-1 of
arming, if uncorrected, the breaker would trip immediately: monthly
all-closed PnL = **−$1,216** ≤ preview threshold **−$34** at K=2σ,
because the no_pattern bucket alone lost ~$1,560 in the trailing 30d.
We have ~12 calendar days to land this fix before the breaker arms.

## Goal

Make the numerator and denominator come from the **same distribution**
so the K·σ math is coherent and the breaker's trip is actionable by
its lever.

This brief does **not** address whether an account-level (all-closed)
breaker is also wanted — that is a separate, larger architecture
question handled by `f-portfolio-vs-pattern-breaker-separation`. Ship
this one first; treat the larger one as the follow-up.

## Deliverable

### D1. Add `scan_pattern_id` filter to `monthly_pnl` numerator

**Where:** `app/services/trading/portfolio_risk.py`, the `monthly_pnl`
SELECT at approximately lines 1088–1101 inside `check_drawdown_breaker`.

**Change:** add the same two predicates the threshold query uses:

```sql
AND scan_pattern_id IS NOT NULL
AND scan_pattern_id != -1
```

so the numerator and denominator are sampled from the same population.

**Comment header** on the change must reference this brief and the
control-loop argument — the reader needs to understand that the
asymmetry was the bug, not the symmetry.

### D2. Update the log line

The log line at `portfolio_risk.py:1107–1112` currently reads:

```
monthly_dd_breaker: 30-day realized PnL $X.XX <= empirical Gaussian
lower-bound $Y.YY (K=Nσ, computed from Md CHILI history)
```

Add `(CHILI-attributed only)` next to the `30-day realized PnL` figure
so on-call can immediately tell which population the trip was scored
against. This matters because operator instinct will be to look at the
account-level cum PnL, which is a different number.

### D3. Regression test

Add to `tests/test_portfolio_risk.py` (or wherever the existing
breaker tests live):

- Seed 30 CHILI-attributed close-days with `mean=$0, std=$10` (light
  noise → threshold around `−$2·sqrt(30)·10 ≈ −$110`).
- Seed an additional **non-attributed** loss of `−$2000` in the
  trailing 30d (e.g. `scan_pattern_id IS NULL`).
- Pre-fix expectation (would fail today): breaker trips even though
  the CHILI-attributed monthly PnL is roughly zero.
- Post-fix expectation: breaker does **not** trip — non-attributed
  loss is excluded from the numerator.

Add a second sub-test where the CHILI-attributed bucket genuinely loses
`−$500` and the breaker correctly trips.

### D4. Re-run the arming-watch probe

After the fix lands, re-run `scripts/dispatch-monthly-dd-arming-watch.ps1`
and confirm the "current 30-day realized PnL" line in the watch's report
template is also CHILI-attributed-filtered, so the daily watch and the
runtime breaker see the same number. Update the watch script's
`monthly_sql` to match.

## Out of scope

- Whether to also add an account-level (all-closed) breaker. That is
  `f-portfolio-vs-pattern-breaker-separation`. Do not anticipate it
  here.
- K-sigma re-tuning. Stays at 2.0σ. Tuning is a separate axis.
- Default-OFF stays OFF. Operator still controls the flag flip.

## Acceptance

1. Code change in `portfolio_risk.py` matches D1 + D2.
2. Regression test from D3 passes; pre-fix test (gated by a
   `@pytest.mark.skipif` referencing a `git rev-parse HEAD` sentinel,
   or just deleted post-merge) demonstrates the bug existed.
3. `scripts/dispatch-monthly-dd-arming-watch.ps1` updated per D4 and
   re-run produces a "current 30-day realized PnL (CHILI-attributed)"
   line that is no longer `−$1,216` but the smaller CHILI-only figure.
4. Operator can now flip `chili_monthly_dd_breaker_enabled=True` on
   arm-day without an immediate trip from the no_pattern bleed.

## Risk

Low. The change is additive (two predicates on one SELECT), behind a
flag that is still default-OFF, and immediately covered by regression
tests. Reverting is a one-line diff.

## Timing

Ship before **2026-05-26** (2 calendar days of soak before projected
arm-date 2026-05-28).
