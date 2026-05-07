# Cowork Review: f8b-pullback-allowlist-and-calibrate

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-03_f8b-pullback-allowlist-and-calibrate.md`
**Reviewer:** Cowork.
**Date:** 2026-05-03.

## Verdict

One commit (gate + script + wire-up bundled), allowlist working, calibration artifact committed. **Approve.**

But the strategic finding is much bigger than the brief anticipated: **BTC's counterfactual is uniformly negative across all 10 candidate delays.** The +5.66 bps from F8a-evaluation-rerun-2's n=8 actual exits was noise. SOL's edge replicates (n=43, +3.47 bps at 25s). **F8a's "subset-supported on {BTC, SOL}" verdict is now downgraded to "supported on SOL only, BTC is suspect."**

Open Question 4 from the brief — *"if counterfactual shows even the optimum has negative return, the realized P/L pattern was noise at low n"* — fired exactly as written for BTC. That the brief explicitly anticipated this case is good design; that it actually happened is the strategic news.

## What Claude Code did right

1. **Surfaced the BTC refutation immediately and prominently.** First non-header section after "What shipped" is "Major finding — surfaced in Open Q4 territory ⚠". Right place to put it. Per-ticker counterfactual table with n=69/43 is verdict-grade evidence — not buried in an appendix.

2. **Honest about the noise vs signal call.** *"BTC counterfactual REFUTES the F8a-rerun-2 +5.66 bps result. The +5.66 bps from F8a-rerun-2's n=8 actual BTC exits was noise."* Doesn't soft-pedal. Doesn't say "these are inconsistent results, hard to know." Names what the data says.

3. **Multi-modal SOL optimum reported honestly.** 25s=+3.47, 90s=+3.31, 20s=+3.03 are essentially tied. The 25s pick is "defensible but slightly arbitrary." That's the right framing — not "the optimum is 25s," not "the optimum is unstable so we don't know," but a third option that captures both.

4. **Shrinkage estimator (`mean × n/(n+30)`) is well-justified.** Mirrors `MIN_SAMPLES_FOR_CALIB`, prevents high-mean-low-n cells from gaming the optimum. Documented inline. This is the kind of statistical detail that matters when n varies across cells.

5. **Boundary warning logic worked.** BTC=5s tripped the warning; Claude Code didn't expand the search because the uniform-negative pattern doesn't warrant it. Right judgment — expanding to 1s/2s wouldn't change the strategic picture.

6. **Bundled-commit decision honestly framed.** *"I bundled them because they're one logical feature — reverting any one breaks the others."* Standard "one logical feature = one commit" judgment. Acceptable; the brief said "up to 3" not "exactly 3."

7. **Counterfactual vs realized disagreement properly flagged in Open Q3.** Hold-period sampling assumes independence from entry timing — if short delays produce different exit timings, the synthesis is wrong. That's exactly the right caveat to surface; doesn't pretend the counterfactual is gospel.

## Findings

### The counterfactual vs realized disagreement on BTC is the strategic question

Two readings of the same data:

| Source | n | Mean | Read |
|---|---|---|---|
| F8a-rerun-2 actual exits | **8** | +5.66 bps | Suggestive, not verdict-grade by tier definition |
| F8b counterfactual (5s) | **69** | −0.75 bps | Verdict-grade by sample size |

The counterfactual's n=69 is ~9× larger. **By sample size alone the counterfactual wins.** But there are real reasons the realized data could be telling a different truth:

1. **Filtering bias on the realized set.** The 8 actual exits passed all OTHER gates (cooldown, capacity, etc.) at fire-time. Whatever conditions allowed those 8 to fire might correlate with positive return. The counterfactual ignores these conditions — it tests "if we'd entered at delay=5s on every alert" without the gate-stack's contemporaneous filtering.
2. **Hold-period independence assumption.** Counterfactual samples hold periods from empirical distribution without modeling delay→hold dependence (Claude Code's Open Q3). If short-delay entries naturally exit faster (e.g., quicker stop-loss trigger because price hasn't recovered), synthesis under-estimates the actual hold time and thus the actual return.
3. **Plain noise at n=8.** Random variance with mean ≈ 0 will produce some 8-sample windows at +5.66 bps. The counterfactual's n=69 averages out the noise.

**Path A (24h verification soak) is the right way to distinguish.** If BTC realized stays positive on the new allowlist regime, reasons 1 or 2 are operative — the counterfactual is missing something and we shouldn't drop BTC. If BTC realized drifts negative on n=20+, reason 3 is operative and we drop BTC.

### SOL's edge is real and replicates

n=43 counterfactual + n=13 realized + multi-modal positive distribution at 20-25-90s = SOL's edge isn't noise. The 25s calibrated delay is materially better than the 30s default (`-2.45 bps default → +3.47 bps calibrated` on the same n=43 sample). **Just calibrating SOL's delay is a real win** even before any verification soak.

### The "no magic numbers" discipline paid off here

The brief explicitly framed: *"Operator's stated discipline ('no magic numbers'): hard-coding DELAY_S=30 is a magic number."* If we'd skipped the counterfactual and just used `delay=30` for {BTC, SOL}, SOL's actual P/L on the calibrated 25s should be +5-6 bps better than at 30s by Welford expectation. **The calibration pays for itself even if BTC ends up dropped.**

### F8a's "subset-supported" verdict needs a downgrade

After F8b: the strategic position is "supported on SOL, BTC suspect (counterfactual refutes; awaiting realized verification)." If BTC drops, F8a is "supported on 1 of 5 tickers." That's narrow. F9 starts looking more attractive.

## Answers to the Open Questions

### 1. BTC counterfactual refutes its actual-trade edge

**Path B (verify with 24h soak) is the right call. Drafting the brief.**

Three reasons against immediately dropping BTC:
- The realized data is what we actually trade. If realized stays positive on n=20+ post-allowlist, the counterfactual is missing something we should understand before discarding.
- F9 is the major-impact alternative if BTC drops. We should be sure before pivoting that big.
- Cost of waiting 24h is low (paper mode + 8 safety belts; max possible loss is small).

### 2. SOL's multi-modal optimum

**Acceptable for now. 28-day stability check after we have the data.** 25s vs 90s vs 20s are within shrinkage noise; 25s is a defensible middle. If a future re-calibration on a longer history landscape shifts the optimum dramatically, that's a re-tune signal — not a today problem.

### 3. Hold-period independence assumption

**Real concern.** Surface in the verification brief as a hypothesis to investigate if BTC realized stays positive: maybe short-delay entries genuinely produce different exit timings than the empirical distribution implies. Out of scope for the verification soak itself; relevant if Path B's verdict diverges from the counterfactual.

### 4. Shrinkage constant

**Approved.** Mirrors MIN_SAMPLES_FOR_CALIB — same convention.

### 5. Re-calibration cadence

**Manual for now.** Once F8b's verification soak lands a positive verdict (or after F9 is designed), worth setting up a weekly cron. Premature today.

## Engineering concerns (smaller)

1. **The artifact path resolution issue.** Initial run wrote to `/app/services/...` (container-only) instead of `/app/app/services/...` (host-bound). Fixed with `app.__file__` lookup. **The fix is correct, but the convention is fragile** — future calibration scripts could hit the same bind-mount-vs-container-fs ambiguity. Worth a one-line note in the runbook (`docs/RUNBOOKS/`) for next time.

2. **Bundling the 3 subtasks into 1 commit.** Claude Code's reasoning is sound (interdependent change), but the brief's "up to 3 commits" framing implied granular revertability. **Trade-off is real**: bundled-commit means selective revert (e.g., "drop the calibration artifact but keep the gate") requires a more surgical operator action. Acceptable; the components ARE coupled.

3. **Counterfactual analysis quality varies by sample size.** BTC has n=69, SOL has n=43. Future-tickers-of-interest with smaller n might produce unreliable optima. The script has a `min_samples_per_cell=10` floor; if a ticker fails that, it falls back to "best-of-thin with warning." Reasonable behavior.

4. **The script's RNG seed is fixed (RNG=42).** Reproducibility is good; same input produces same output. But for confidence intervals on the optima, we'd want to vary seed and report optimum stability. Not blocking; flag for f-hygiene-5 or wherever the next statistical pass lands.

## State of the world after f8b

- 17 protocol runs landed clean.
- 1 bundled code commit + 1 doc commit, both pushed.
- Allowlist gate firing: BTC/SOL produce paper fills; ETH/AVAX/DOGE blocked with `pullback_ticker_not_allowed:<ticker>`.
- Calibration artifact: BTC=5s (boundary warning, uniformly negative across grid), SOL=25s.
- F8a "subset-supported" verdict downgraded: SOL holds, BTC suspect.
- F8a soak interrupted ~30s for the restart; resumed.
- All hygiene fixes from prior runs continue working.

## Decisions confirmed

- F8b shipped: gate + counterfactual calibration + artifact wire-up.
- BTC counterfactual is uniformly negative; verification soak (Path A) needed before deciding to drop.
- SOL's edge replicates on 5× larger sample; 25s is the calibrated optimum.
- Path A is the next NEXT_TASK.
- f-hygiene-5 (structural B fix from f-hygiene-4) can run in parallel.

## Next move

**Drafting f8b-verification-soak brief next.** Scope:

- Read distinct realized exits per allowlisted ticker over the 24h+ post-deploy window.
- Compute aggregate + per-ticker P/L on the new (post-allowlist + per-ticker calibrated DELAY_S) cohort.
- Compare to F8a-rerun-2 baseline (BTC: +5.66 bps n=8; SOL: +3.34 bps n=13).
- Decision tree:
  - **BTC stays positive on n≥20**: counterfactual was missing something; keep BTC; investigate (Open Q3 hold-period independence is a candidate).
  - **BTC drifts negative on n≥20**: counterfactual correct; drop BTC from allowlist; F9 candidacy goes up.
  - **BTC inconclusive at n<20**: more soak.
  - **SOL stays positive on n≥30**: F8b's calibrated delay is validated; F8b stays in production.
  - **SOL drifts negative**: pivot to F9 immediately; the fade hypothesis isn't holding even on the strongest subset.

Brief incoming.
