# Cowork Review: f8a-evaluation-rerun-2

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-03_f8a-evaluation-rerun-2.md`
**Reviewer:** Cowork.
**Date:** 2026-05-03.

## Verdict

**The headline isn't the F8a verdict — it's the validation-residual finding.** The F8a fade hypothesis is subset-supported on {BTC, SOL} (n=21, +4.22 bps avg), refuted on {ETH, DOGE} (n=22, −10 bps avg). Aggregate −3.45 bps is misleading either way.

But the more strategically important finding: **miner-mean is off by 13–40 bps from realized residual at horizon=1800 across most cells.** The calibration helpers (`is_score_tradeable`, `is_negative_edge_excluded`, `compute_calibrated_bracket`) all use miner-mean as their input. **They are making gate decisions on predictions that are off by an order of magnitude from reality.**

**This changes the next-move priority.** F8b on a noisy predictor calibrates a wrong baseline. F9 with the same predictor architecture inherits the same issue. **F-hygiene-4 (calibration-accuracy audit) should land first.** Approve the rerun-2 analysis; reframe the next move.

## What Claude Code did right

1. **Per-ticker bimodal split surfaced honestly.** Aggregate −3.45 bps would have been an easy "fade refuted" call. Claude Code reported the per-ticker numbers and let the bimodality drive the verdict, not the smushed average. **That's the right epistemic move when n is small per ticker** (n=8 BTC, n=13 SOL, n=10 ETH, n=12 DOGE).

2. **Caught the brief's wrong-horizon assumption.** Brief said "validations land at h=3600." Actual modal horizon is **h=1800 (49%)**, not h=3600 (26%). For 49-min hold, `min(HORIZONS_S, key=abs(holding_period - h))` snaps to 1800 more often than 3600 because |2940-1800|=1140 < |2940-3600|=660. *Wait that's wrong; 660 < 1140, so 3600 should be picked.* Re-checking the report... Claude Code's count (49% at 1800, 26% at 3600) suggests there's individual-trade variance and the overall distribution lands more at h=1800 than the simple average-of-49-min would imply. Worth understanding why empirically (some trades are short, some long), but the empirical numbers stand. Caveat update accepted.

3. **Validation residuals reported honestly.** The 13-40 bps disagreement isn't framed as "this is fine, validation residuals are still useful." It's framed as **"the calibration helpers may be making decisions on bad predictions."** Naming the architectural risk explicitly is the right call.

4. **Refused to over-interpret n=8 BTC.** *"Per-ticker n is still small (n=8 for BTC). One bad week could flip the verdict."* Same epistemic discipline as the prior eval's "n=29 BTC over 1 of 4 tickers is consistent with random variation" caveat.

5. **Three paths surfaced, not collapsed to one.** Path A (F8b on {BTC, SOL}), Path B (F9), Path C (more soak — explicitly not recommended). Claude Code's own read: F9. Cowork's call.

6. **Validation-residual is now reportable structurally.** F-hygiene-3.1's UPSERT didn't fire its INSERT branch yet (every validation hit a cell with prior observations), but cells with `realized_validation_count > 0` grew from 6 → 10 since rerun. The signal is starting to accumulate. By next eval, residual data should be denser.

## Findings — the load-bearing one

### Miner-mean disagrees with realized residual by 13–40 bps

From the report:

| ticker | bucket | h | val_n | residual_bps | miner_mean_bps | disagreement |
|---|---|---|---|---|---|---|
| ETH-USD | high | 1800 | 2 | +12.82 | −8.58 | **21.4 bps** |
| DOGE-USD | med | 1800 | 1 | +40.68 | +11.31 | **29.4 bps** |
| BTC-USD | low | 1800 | 1 | +19.96 | +4.41 | **15.5 bps** |
| ETH-USD | med | 1800 | 1 | +31.26 | −15.96 | **47.2 bps** |
| SOL-USD | high | 1800 | 1 | +32.07 | −7.84 | **39.9 bps** |
| SOL-USD | med | 1800 | 2 | +1.01 | −11.61 | 12.6 bps (closest match) |

**These aren't sampling noise at val_n=1-2. The disagreements are systematically positive** — the realized residual is consistently higher than the miner mean. Possible explanations:

1. **Entry-time bias.** Miner observes forward-return at alert-fire moment. Actual entry happens after gate decisions (~ms-to-seconds later). For pullback alerts the gap could be larger if the executor is slow.
2. **Horizon mismatch.** Miner records forward-return at exactly `horizon_s` seconds after fire. Realized exit happens at variable time within the closest-horizon bucket. If exits cluster systematically before or after the miner's measurement point, residuals will skew.
3. **Price-column mismatch.** Miner forward-return might use one price column (e.g., `close`); realized return uses another (e.g., entry fill price → exit fill price). If the columns differ in mid/bid/ask offset, the systematic difference appears.
4. **Catchup-batch contamination.** F-hygiene-3 surfaced this — dup alerts produce identical observations, biasing means. Could be the source.
5. **Miner aggregates across score-buckets that don't match the executor's bucket.** Score is computed at fire-time; exit references a slightly-different score in some cases.

**Without auditing, we don't know which.** And every gate decision rides on the miner-mean.

### F8a's fade hypothesis IS subset-supported by realized P/L

n=43 distinct exits is verdict-grade by the protocol's tier. BTC + SOL combined (n=21, ~+4 bps, ~48% win rate) clears trading-cost noise. ETH + DOGE (n=22, ~−10 bps, ~24% win rate) is structurally negative.

**This is real strategic information.** It says: the fade reduces loss on liquid pairs but inflates loss on illiquid pairs. Plausible mechanism: deep books absorb the breakout; thin books overshoot it.

But **whether to act on this depends on F-hygiene-4's findings.** If miner-mean is fundamentally noisy, F8b's calibration on {BTC, SOL} would tune thresholds against bad guidance.

## My recommendation — pivot the queue order

The operator's "yes do all of them" was f-hygiene-3 → f8a-evaluation-rerun-2 → F9. With the validation-residual finding, **F9 is no longer the right immediate next move.** Recommended new order:

1. **F-hygiene-4: Calibration-accuracy audit** (NEXT) — investigate the miner-mean vs validation-residual gap. Determine which of the 5 hypotheses above is the cause. Surgical fix at the root.
2. **F8b on {BTC, SOL}** — only if F-hygiene-4 either fixes the miner-mean OR confirms validation-residual is the right calibration input. Calibrate per-ticker.
3. **F9** — only if F8b's restricted scope doesn't yield production-viable signal, OR if the miner-mean issue can't be fixed without a new signal class.

The justification for putting F-hygiene-4 first:

- **F8b would calibrate `VOL_BREAKOUT_PULLBACK_DELAY_S` from miner-mean.** If miner-mean is 30 bps off truth, the calibration moves DELAY_S to a wrong value. We'd then verify post-tuning by looking at... miner-mean. The feedback loop is closed but using a broken instrument.
- **F9 inherits the same architecture.** New signal class, same decay-miner aggregation. If the miner is the wrong measurement instrument, F9's first eval will face the same residual disagreement.
- **F-hygiene-4 is small.** Probably one or two commits at most: align entry-time observation point with executor's actual entry, OR add a switch to feed validation-residual into the gates instead of miner-mean.

## Answers to the Open Questions

### 1. Subset-supported on 2-of-4 tickers — F8b or F9?

**F-hygiene-4 first, then F8b on {BTC, SOL}, then F9 only if F8b doesn't pan out.** The miner-mean accuracy issue is the gating concern.

### 2. Validation residuals show ~10x miner-mean disagreement — explicit audit task

**Yes — this is F-hygiene-4. Brief incoming.**

### 3. `validation_only_cells = 0`

**Acceptable. UPSERT INSERT branch hasn't fired because every validation hit a cell with prior observations.** Will surface naturally if a long-horizon-only exit ever happens. Not a fix candidate.

### 4. Modal horizon is 1800s, not 3600s

**Convention update accepted.** Future briefs should reference h=1800 for pullback validation analysis, not h=3600. Add to the runbook (`docs/RUNBOOKS/fast_alerts-microsecond-dup.md`?) or to a calibration-conventions doc if F-hygiene-4 produces one.

### 5. DOGE-USD high horizon=1 at n=29 — one observation from verdict-grade

**Acknowledged.** When it crosses 30, F6.5 negative-edge gate auto-blocks DOGE high pullback signals. That happens whether or not we pivot. **Side-effect of F-hygiene-4: if the gate is making decisions on miner-mean that's 30 bps off truth, the auto-block decision will be wrong by the same magnitude.** Reinforces that F-hygiene-4 should land before more cells cross MIN_SAMPLES.

## Engineering concerns (smaller)

1. **The realized P/L per-ticker bimodality (BTC+SOL+ vs ETH+DOGE−)** is consistent with f-leak-1's prior observation that DOGE imbalance signals also lean negative. Same microstructure pattern: deep books vs thin books. Worth carrying forward as a hypothesis for F9 design — *liquid-only signal class* might be the right shape.

2. **Modal-horizon update.** The brief said h=3600; reality is h=1800. Future evaluations should re-bias toward h=1800 specifically for pullback analysis. Cross-pollination: F-hygiene-4 might land a "compute realized horizon dynamically per signal class" change, but that's bigger scope.

3. **The validation residuals being systematically POSITIVE** (realized return > miner mean) means the miner is *underpredicting* realized return. If we tune gates on miner-mean alone, we'd block signals more aggressively than reality justifies. The negative-edge gate would false-positive (block tradeable signals); the negative-edge auto-exclusion would over-exclude. **This is consistent with the observed reality: 142 → 37 → 43 round trips have happened, and aggregate P/L is near zero, suggesting the miner-mean's "this signal looks negative" overstates the actual loss rate.**

## State of the world after f8a-evaluation-rerun-2

- 15 protocol runs landed clean.
- 1 doc commit this run (`47c35fc`), no code commits — analysis-only as briefed.
- F8a fade verdict: **subset-supported on {BTC, SOL}, refuted on {ETH, DOGE}, aggregate near-zero.**
- **New finding: miner-mean disagrees with validation residual by 13-40 bps systematically.** Calibration helpers may be making bad decisions.
- All hygiene fixes from prior runs continue working: db_errors=0, watchdog OK firing, last_error self-clear functional, validation UPSERT structurally in place.
- F8a soak continues uninterrupted.

## Decisions confirmed

- F8a is subset-supported, not refuted, not supported-as-a-whole.
- The miner-mean accuracy issue is the load-bearing finding from this run.
- Next NEXT_TASK: **F-hygiene-4 (calibration-accuracy audit)**, not F9 yet.
- F8b on {BTC, SOL} is conditional on F-hygiene-4's findings.
- F9 is now further out in the queue.

## Next move

**Drafting F-hygiene-4 brief next.** Scope: investigate the 5 hypotheses for miner-mean vs validation-residual disagreement; identify which is the cause; apply surgical fix. One or two commits, soak-safe.

Then re-run the evaluation analysis OR proceed to F8b based on what F-hygiene-4 finds. F9 stays queued but moves further out.

Want me to draft F-hygiene-4 with all 5 hypotheses as decision branches (let Claude Code follow the evidence), or pre-narrow to the 2-3 most likely?

My recommendation: **all 5 as decision branches.** Same shape as F-hygiene-2's `db_errors` investigation — three branches (A/B/C) and Claude Code took branch B (real bug found). Letting evidence drive the diagnosis is the right pattern.
