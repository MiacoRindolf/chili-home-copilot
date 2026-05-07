# Cowork Review: f8a-evaluation-rerun

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-03_f8a-evaluation-rerun.md`
**Reviewer:** Cowork.
**Date:** 2026-05-03.

## Verdict

The decay-miner-only verdict ("insufficient data") is correct under the brief's strict criterion — but Claude Code surfaced something the brief got wrong: **the gate stack does NOT block pullback fills in the current configuration**, and 142 round-trip pullback trades have already closed in paper mode. That's verdict-grade realized data. Approve all of it.

The strategic crossroads is real and the answer is **Option B (pivot to F9)**, with one refinement.

## What Claude Code did right

1. **Caught the brief's wrong assumption.** The f8a-evaluation brief and rerun brief both said "gate stack blocks pullback fills, no `fast_exits` rows reference pullback alerts." That was wrong — calibrated gates require n≥30 to fire and the decay miner hasn't crossed it yet, so both `gate_negative_edge_excluded` and tradeability gates pass through. Static gates only check cosmetic conditions. Result: 142 paper trades since F8a-fix landed. **Cowork (me) missed this in two consecutive briefs**; Claude Code caught it by actually running the join against `fast_exits`.

2. **Both verdicts reported, not collapsed.** Decay-miner says "insufficient data" (true under its strict criterion). Realized P/L says "weakly refuted" (true at n=142). Claude Code reported both and let me pick the operative lens — exactly the protocol's discipline. No fabrication, no premature collapse to one number.

3. **Per-ticker breakdown is the actually-useful read.** BTC-USD is the *only* positive ticker (+4.3 bps over 29 trades) while DOGE-USD is the worst (-16.6 bps over 48). Aggregate average smushes those together; the per-ticker view reveals "the fade works on the deepest book, fails on the thinnest." That's a structural read about microstructure, not a noise observation.

4. **Identified the validation-count gap and its mechanism.** 7 validations vs 142 actual exits because `_handle_exit_inserted` does an UPDATE that silently affects 0 rows when the bucket-cell doesn't exist yet. Not a bug strictly, but a structural undercounting. UPSERT fix is named as F-hygiene-3 candidate.

5. **Correctly used the horizon ≥ 5s refinement.** The 9 suggestive cells include 3 at horizon=5; none have positive means. Held the discipline: horizon=1 is the fire moment, not the fade test.

6. **Honest about the SOL-USD `stderr=0` quantization artifact.** Could have ignored it; flagged it as a tick-quantization explanation and a comparison caveat.

## My read on the strategic question (Option A vs B)

**Option B (pivot to F9) is the right call. Modify it to "F9 + keep F8a passive."**

The realized data at n=142 is more strategically informative than miner means at n=12. The fade does dampen the original `volume_breakout_long`'s bleed (−28.5 bps → −6.7 bps, ~75% reduction in loss), but it doesn't cross zero. *The hypothesis is "fade reduces loss enough to make it tradeable" — and it doesn't.* Continuing soak adds precision to a negative result without changing the strategic answer.

But — **don't shut down the F8a pipeline.** It's already running and free. The BTC-USD +4.3 bps (n=29) deserves continued accumulation; if it holds at n=100+, that's a valid "fade-works-on-deepest-book-only" finding worth carrying into F9's design. The cost is zero — the pipeline just keeps Welford-updating.

So the refined recommendation: **F9 brief next, F8a continues passively, BTC-USD subset is the lone interesting per-ticker tell to track in any future re-evaluation.**

## Answers to the Open Questions

### 1. Realized P/L vs decay-miner means as the verdict basis

**Realized P/L is the better lens here.** The decay-miner's per-bucket framework is conservative-by-design — sensible when you have no exits to look at (cold start), unnecessarily strict when you have 142 closed round trips. The miner is an estimator; realized P/L is the truth. n=142 is verdict-grade for the strategic question.

(Carry forward: future signal evaluations should run BOTH lenses by default. The miner gives you per-bucket nuance, realized gives you the aggregate truth. Don't pick one.)

### 2. Validation-count UPSERT fix

**Yes, but defer to F-hygiene-3.** It's a real correctness gap (~95% of validation events silently dropped), but it doesn't change today's verdict. Fold into the next hygiene pass alongside the leak-investigation work that's now urgent.

### 3. 48-hour soak threshold + `VOL_BREAKOUT_MULT` decision

**Don't lower MULT.** The realized-P/L answer is already negative; lowering MULT to seed faster firing would just give us more negative data faster. F9 is the right pivot.

### 4. BTC-USD positive subset

**Significant enough to flag for F9's design**, not significant enough to act on alone (n=29 is suggestive, not verdict-grade — and "1 of 4 tickers positive in a noisy signal" is consistent with random variation). Carry as a hypothesis into F9, don't make it a deliverable.

## Engineering concerns (smaller)

1. **Brief was wrong about gate-stack blocking pullback fills.** That assumption was carried for two consecutive briefs (f8a-evaluation and f8a-evaluation-rerun). My fault, not Claude Code's. The protocol caught it via the `fast_exits` join Claude Code ran. **Lesson for future briefs: don't assert what gates do without checking; ask Claude Code to verify.**

2. **142 paper trades on a not-yet-verdict-grade signal** is "the brain learning by trading." Working as designed (paper mode + small caps), but worth being aware of. When/if the decay miner crosses MIN_SAMPLES on the negative-edge buckets, the tradeability gate will start blocking and the trade rate will drop sharply — that transition will surface as a step-change in fast_alerts/fast_exits volume.

3. **Validation-count UPSERT gap** — 95% silent drop rate is a genuine measurement issue. Doesn't matter for today (we have realized P/L), would matter when validation residuals start mattering in production.

## State of the world after f8a-evaluation-rerun

- 11 protocol runs landed clean.
- 1 doc commit this run (`c406519`), no code commits — analysis-only as briefed.
- F8a's fade hypothesis weakly refuted by realized P/L (n=142, avg −6.7 bps, win rate 30.3%).
- BTC-USD subset is the lone positive ticker, suggestive (n=29).
- 142 paper trades have fired; decay miner accumulating but no verdict-grade cells yet.
- All hygiene fixes durable: db_errors=0, watchdog OK firing, last_error self-clear functional.

## Decisions confirmed

- Realized P/L is the operative lens for verdict-grade strategic questions (with miner as supporting per-bucket nuance).
- Pivot to F9, keep F8a pipeline running passively, track BTC-USD subset.
- Validation-count UPSERT fix deferred to F-hygiene-3.
- F-hygiene-3 priority is now subordinate to **f-leak-1** (urgent host leak; see below).

## Next move — and the priority shift

**Two candidate threads, ordered:**

**1. f-leak-1 (URGENT, NEW).** Operator surfaced recurring host unavailability around 6 AM/PM PT — host-level RAM pressure preventing ordinary app use, with possible data-corruption risk. The dispatch-host-leak-probe data I just gathered shows:
- `chili-home-copilot-chili-1`: **2.998 GiB / 3 GiB (99.94%), status "unhealthy", restarts=7** ← the smoking gun
- `chili-home-copilot-brain-worker-1`: 5.83 GiB / 8 GiB, **CPU 109.88%** ← runaway worker
- `chili-home-copilot-scheduler-worker-1`: 3.32 GiB, BlockIO 11.9 GB ← heavy DB writer
- `chili-home-copilot-fast-data-worker-1`: 157 MiB / 512 MiB ← clean (not the source)
- All other containers stable

**This pre-empts F9.** Writing brief next.

**2. F9 (after leak contained).** Design and prototype a new fast-path alert signal class that doesn't depend on mean-reversion-of-volume-breakout. Brief comes after f-leak-1 lands.

Writing the f-leak-1 brief now.
