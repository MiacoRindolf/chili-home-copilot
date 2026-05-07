# Cowork Review: f8b-verification-soak

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-03_f8b-verification-soak.md`
**Reviewer:** Cowork.
**Date:** 2026-05-03.

## Verdict

**Honest "inconclusive — too early."** Operator ran ~10 minutes after F8b deploy (current 16:38 UTC vs deploy at 16:29 UTC). Zero distinct closed exits on the post-deploy cohort. Decision tree fires the "more soak" branch correctly.

But the run still surfaced **four substantive findings** that aren't the F8b verdict:

1. **Allowlist gate is verifiably clean.** Zero false rejects on BTC/SOL. ETH/AVAX/DOGE blocked.
2. **DOGE high pullback now auto-blocks via `negative_edge:negative_edge`** — F6.5's calibrated gate fired automatically once DOGE high h=1 crossed n=30. The brain self-pruned without operator action. **Architectural validation.**
3. **F-hygiene-4.2's C fix is verifying empirically.** New post-fix DOGE residuals are 5.66–6.72 bps vs pre-fix 34–40 bps. ~30 bps reduction matches half-spread theory.
4. **6 first-ever verdict-grade decay cells crossed n=30.** First time in F8a's history. Mostly at h=1 (fire moment, not falsifying), but the framework is finally producing verdict-grade data.

Approve the report.

## What Claude Code did right

1. **Held the line on "inconclusive" verdict.** Could have stretched the existing 9 BTC + 14 SOL pre-deploy data points into a "preliminary" verdict. Didn't. Per the brief's pre-window provision, bumped per-ticker minimum to 30 and reported both as below threshold.

2. **Caught the drift signal.** Pre-F8b BTC went +5.66 → +3.65 with one new exit (8→9). Pre-F8b SOL went +3.34 → +1.58 with one new exit (13→14). **Both drifting toward zero as more data lands.** Honest framing: "suggestive that the counterfactual was right and F8a-eval-rerun-2's positive results were small-n noise — but n is still too small to be conclusive."

3. **Caught their own JOIN-cardinality bug.** Initial scratch query showed n=56 on the SOL pre/post split; correct IN-subquery form is n=14. Documented in Surprises #6 with a caveat in the verbatim SQL section to prevent future repetition. **This is exactly the kind of bug that bit f8a-evaluation-rerun's n=142 → 37 inflation.** Catching it pre-publication is the right epistemic move.

4. **The DOGE auto-block observation is strategically important.** F6.5's negative-edge gate fired automatically once DOGE high h=1 crossed n=30. **The system is doing what it was designed to do.** This is validation that the calibrated-gate framework works as advertised — not just a hygiene observation.

5. **Two-checkpoint re-run recommendation.** 18:00 UTC for directional reading + 16:30 UTC May 4 for full 24h verdict. Optional first checkpoint, mandatory second. Lets the operator decide based on patience, not waste.

6. **Cluster-correlation caveat.** *"The 14 catchup paper_fills are time-clustered at 16:29:33 because they all came from snapshot-replay drains. Their P/L outcomes will be highly correlated. If they all close green or all close red, treat that as ONE data point, not 14."* Right call. Snapshot-replay catchup is artifactually correlated.

## Findings

### The drift toward zero on pre-F8b data is the most strategically interesting

| Source | n at F8a-rerun-2 | Avg | n now | Avg | Direction |
|---|---|---|---|---|---|
| BTC-USD | 8 | +5.66 bps | 9 | +3.65 bps | toward zero |
| SOL-USD | 13 | +3.34 bps | 14 | +1.58 bps | toward zero |

**Both tickers drifted toward zero with one new data point each.** The counterfactual on n=69 BTC said uniformly negative; the realized data is now consistent with that read at small-but-growing n.

**This isn't conclusive at n=14 SOL or n=9 BTC.** But the trajectory is consistent with "the F8a-rerun-2 positive results were small-n noise and the counterfactual was right."

### DOGE high auto-block is structural validation

The framework now does this without operator action: as data accumulates, MIN_SAMPLES is reached, calibrated gates evaluate, negative-edge cells get auto-excluded. **This is the system the F6.5 brief envisioned.** The allowlist remains useful as a backstop for less-mature buckets (med/low score), but the long-term equilibrium is the calibrated gates handling everything.

### The C fix delivers as predicted

Pre-fix DOGE residuals: 34–40 bps. Post-fix DOGE residuals: 5.66–6.72 bps. **~30 bps reduction matches the half-spread theory exactly.** F-hygiene-4's diagnosis was correct; the surgical fix works.

### 6 verdict-grade cells is a milestone

First time in F8a's history. Mostly at h=1 (fire moment — structurally not falsifying), but the framework is finally producing verdict-grade data. By the next re-run (24h), more cells should cross at horizons ≥ 5s.

## Answers to the Open Questions

### 1. Both BTC and SOL drifted toward zero

**Suggestive but not conclusive.** Two data points isn't a trend. **f8b-verification-soak-2 at the full 24h target will resolve this.** If both stay near zero, F9 becomes the right pivot.

### 2. DOGE auto-block as architectural validation

**Confirmed.** Worth including in any future architecture review or doc — F6.5's design works. **Not actionable beyond noting it.**

### 3. F-hygiene-4.2's empirical confirmation

**Confirmed.** ~30 bps DOGE residual reduction. The C fix is real.

### 4. Re-run timing — two checkpoints

**Skip the optional 18:00 UTC checkpoint.** The 14 catchup paper_fills are time-clustered (correlated outcomes); intermediate read at 90 min won't reduce noise materially. **Wait for the briefed 2026-05-04 16:30 UTC.** Lower-friction; better-quality verdict.

### 5. Catchup-batch correlation

**Acknowledged.** Treat the 14 catchup fills' aggregate P/L as one data point if they all close green or all red. Detail in next run's brief.

## Engineering concerns (smaller)

1. **The JOIN-cardinality bug appearing in scratch queries** is a recurring pattern (n=142→37 in f8a-eval-rerun-2; this run's n=56→14 in scratch). Worth elevating: the `docs/RUNBOOKS/fast_alerts-microsecond-dup.md` runbook should explicitly call out the SOL pre/post split as a worked example. Future operator-typed scratch queries find the convention quickly. Surface as f-hygiene-5's secondary scope.

2. **Verdict-grade cells crossing.** When more cells cross MIN_SAMPLES at horizons ≥ 5s, F6.5's negative-edge gate will start blocking more aggressively. Watch for the trade rate dropping as a side effect — that's the system tightening, not breaking.

## State of the world after f8b-verification-soak

- 18 protocol runs landed clean.
- 1 doc commit (`41991da`), no code commits — analysis-only as briefed.
- F8b allowlist working as designed: zero false rejects, ETH/AVAX/DOGE blocked.
- Pre-F8b drift signal: BTC +5.66→+3.65, SOL +3.34→+1.58 with n+1 each. Toward zero.
- 6 first verdict-grade decay cells crossed.
- DOGE high pullback auto-blocked via calibrated gate.
- C fix empirically verifying.
- F8a soak continues uninterrupted.

## Decisions confirmed

- f8b-verification-soak verdict: inconclusive (pre-window).
- Re-run target: 2026-05-04 16:30 UTC (briefed time).
- Skip the optional 18:00 UTC intermediate checkpoint.
- f-hygiene-5 (structural B fix) remains queued in parallel.
- F9 candidacy goes up if next re-run shows both BTC and SOL near zero.

## Next move

**f8b-verification-soak-2 brief queued for 2026-05-04 16:30 UTC.** Same shape as this brief; same SQL queries; ≥24h of post-deploy data; per-ticker minimum n=20 floor (the brief's normal threshold).

Drafting next.
