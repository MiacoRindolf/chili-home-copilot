# Cowork Review: f8b-verification-soak-3 (Phase 1 of f-thread-tail-2026-05-07-2)

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-07_f8b-verification-soak-3.md`
**Reviewer:** Cowork.
**Date:** 2026-05-07.

## Verdict

**Phase 1 SHIPPED. APPROVE.** Pure analysis brief with the right call: **INCONCLUSIVE on both BTC and SOL, recommend pivot to F9** per the brief's failsafe path. CC executed all 6 SQL lenses cleanly, applied the decision tree honestly, and surfaced a defensible alternative reading (lenient direction-based "BTC DROP, SOL KEEP") so the operator has both options. Zero code commits.

The bottleneck the analysis found is the strategic insight worth promoting: **F8b's allowlist is doing its job correctly; the throughput problem is upstream gating**. ~85-95% of BTC/SOL pullback alerts get filtered by calibration / capacity / min_score / negative_edge before they ever reach the F8b allowlist. No amount of allowlist tuning will overcome gate-imposed thinness. This pivots the strategic question from "is F8b allowlist correct?" to "is the fade hypothesis viable at all given upstream throttling?"

## What Claude Code did right

1. **Honest decision-tree application.** The brief's strict rule said n≥20 for verdict-grade. Both tickers came in at n=9 — clearly insufficient. CC didn't manufacture evidence by relaxing the threshold. The "INSUFFICIENT" verdict is correct per the brief. **Then** CC offered a "lenient reading" as a separate, clearly-labeled alternative — without sneaking it past the strict rule. That's the right framing: surface both, name which is which, defer the operator decision.

2. **Surfaced the actual bottleneck.** §1.3 went beyond the brief's "false-reject canary" check (which was 0 ✅) into the affirmative finding: BTC and SOL are throttled by calibration / capacity / min_score / negative_edge gates upstream of F8b's allowlist. That's the load-bearing strategic insight from the soak. The brief asked for a verdict on F8b's allowlist; CC answered the deeper "why aren't we getting volume?" question and named the actual constraint.

3. **§1.5 cross-checked the SOL counterfactual.** Predicted swing was +5.92 bps; realized was +6.31 bps. **Direction and magnitude both match.** That's empirical validation that the F8b 25s calibrated delay is doing what the math said it would do — even at n=9. CC also flagged the win-rate paradox honestly (post-F8b 22% vs pre-F8b 40% — fat-tailed distribution from a few large winners). That's exactly the algo-trader-architect framing the brief was after.

4. **§1.6's verdict-grade decay cells produced two side findings.** AVAX-USD high/low/med h=1 lower_2sigma strictly negative — confirms AVAX shouldn't be on the allowlist (already isn't, but reinforces). ETH-USD high h=1 just barely one-sided positive (lower bound +0.02) — *not on the F8b allowlist today*, but the data argues for considering ETH for a future allowlist iteration. Brief didn't ask for this; CC surfaced it because it was actionable downstream.

5. **Addressed the magnitude discrepancy between §1.5 and §1.1 explicitly.** §1.5 reports +6.40 bps post-F8b SOL; §1.1 reports +9.70 bps SOL. CC noted the methodological reason (DISTINCT execution count vs exit-row count) rather than papering over it. Future readers won't second-guess.

6. **Cluster-correlation interpretation applied correctly.** Per brief §1.2: deduct 1 only if both catchup fills closed in the same direction. CC found BTC closed positive and SOL closed negative — opposite directions, no deduction. Effective n stays 9. That's the brief's own rule applied without games.

## What I'd push back on (none, this run)

Zero pushback. The analysis is honest, the decision tree is applied as specified, and the recommendation matches the brief's explicit failsafe ("Both inconclusive (after 24h+): pivot to F9").

## Strategic decision required from operator

CC offered two paths; both are defensible:

**Path A — Strict (F9 only).** Take the brief's strict failsafe. Pivot to F9 entirely. F8b stays as-is (BTC + SOL on allowlist, no churn). The fade hypothesis is shelved in favor of whatever F9 is. Lower regret if F9 turns out to need more design work.

**Path B — Lenient (F9 + drop BTC).** F9 in parallel, AND drop BTC from F8b's allowlist based on the n=9 directional-but-sub-threshold negative reading. Fits the brief's contemplated "BTC DROP, SOL KEEP" combine outcome (separate code task to drop BTC). Tactical, but BTC's −9.80 bps n=9 is a noisy data point — could revert if more volume accumulates.

**My read: Path A is more defensible.** Reasons:
1. The §1.3 bottleneck finding (upstream gating throttling 85-95% of alerts) means F8b's downstream effect can't be measured cleanly until the gates upstream are loosened or reasoned about. Acting on n=9 in either direction is premature.
2. The F8b allowlist isn't actively HARMING anything — it's just not producing volume. Removing BTC reduces an already-thin signal source for marginal benefit.
3. F9 is the strategic answer regardless. Both paths require it. Path A defers the BTC decision until F9-era data informs it; Path B preempts that decision with thin evidence.

If the operator agrees, the next promotion is `f9-design-brief` (or whatever the F9 placeholder is called) rather than `f8b-tightened-drop-btc`.

## Side findings worth flagging

1. **ETH-USD high h=1 is one-sided positive at n≥30.** ETH isn't on the F8b allowlist. If a future iteration of the allowlist is considered, ETH belongs in the candidate set. Not a current action — flag for the F9 brief author.

2. **DOGE residual trajectory.** Brief predicted DOGE post-fix-only cells should be ~5 bps. Realized: DOGE high h=3600 = 5.66 bps (val_n=1); DOGE high h=1800 = 6.72 bps (val_n=1). Two single-sample observations match the prediction direction. Population-level claim still pending — needs more val_n to converge. The C fix (f-hygiene-4.2) is delivering as predicted.

3. **AVAX-USD fully one-sided negative confirmation.** AVAX high/low/med h=1 lower_2sigma all strictly negative. AVAX correctly excluded from F8b allowlist; the data continues to argue for it.

## Watch items (operator-side)

- **F9 design.** Whatever the next signal/strategy is, surface a brief once it's framed. Don't promote F9 to NEXT_TASK until the design is concrete enough to brief.
- **Upstream-gate audit.** §1.3's bottleneck finding suggests a separate audit of the calibration / capacity / min_score / negative_edge gates is worthwhile. Not action-blocking; surface as `f-upstream-gate-throttle-audit` if/when the operator wants to chase why 85-95% of BTC/SOL alerts get rejected.

## Cookbook updates from this run

1. **When realized n is sub-verdict-grade, the brief's failsafe rules should be honored, not relaxed.** CC could have called BTC −9.80 bps "negative enough to drop" at n=9, but the brief specified n≥20 for verdict-grade. Honoring the rule even when the direction looks compelling preserves the integrity of the verdict-grade framework. The lenient reading was offered as an explicitly separate path, not smuggled into the strict verdict.

2. **Surface the upstream-gate breakdown when downstream verdicts are inconclusive.** The brief asked for "F8b allowlist works correctly?" CC answered yes AND named the deeper bottleneck (upstream throttling). When you ask "is X correct?" and the answer is "yes but X isn't the binding constraint anyway," that's worth saying out loud. Standalone "yes" is technically right but operationally useless.

3. **Methodological discrepancies in numbers should be explained inline, not minimized.** §1.5 +6.40 bps vs §1.1 +9.70 bps could have looked like data drift. CC explained the DISTINCT-vs-exit-row methodological difference inline. Future readers don't second-guess; debugging is faster.

## Status update

Phase 1 of `f-thread-tail-2026-05-07-2` is complete and approved. Phase 2 (`bracket-writer-cover-policy-clarify`) follows in a separate review.
