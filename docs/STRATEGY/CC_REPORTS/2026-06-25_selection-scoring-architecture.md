# Selection / scoring / ranking architecture — research + redesign (the PLSM #15 failure)

Triggered 2026-06-25 by the operator's question: would CHILI catch the names Ross traded (PLSM +400% blue-sky, FRTT, ROC) — and "ganyan ba talaga ang tamang design?" Diagnosed via replay + live data: CHILI **selects** PLSM (live-eligible) but ranked it **#15 of 471** (mega-cap MU outranked it) → slot-starved → armed at 11:17, an HOUR after the 10:07 move.

## Verdict: the ARCHITECTURE (backbone) is correct — do NOT rewrite. The bug is the scoring MATH.
The pipeline shape is sound + must be kept: scan → per-(symbol, variant) Ross pillars → **adaptive batch-relative** normalize → viability_score → read-time **argmax-per-symbol dedupe** → arm top-N + rank-displacement (#767). One aggregator is broken, not the lane.

## The PRIMARY bug — `ross_momentum.score_universe()` double compression
1. **`_percentile_rank`** maps every top-of-batch value to ~1.0 → 15,000× RVOL and 6× RVOL are **indistinguishable** (magnitude destroyed by design).
2. **Linear weighted-average** (`sum(pct·wt)/wsum`) is **fully compensatory** → an extreme on one axis is averaged toward the batch mean by mid pillars.
→ Scores collapse into **[0.65, 0.74]**; non-explosive mega-cap MU (0.665) outranks +400%/15,000×-RVOL PLSM (0.651). **A math bug, not an architecture failure.**
- Compounding A — **vendor float wrong on recent IPOs**: PLSM stamped 50M (shares-outstanding) vs **3.5M real free float**; the `-log10(float)` low-float pillar then DEMOTES the most explosive name + makes float_rotation read 14× too slow.
- Compounding B — **slot latency**: rank-displacement inherits the compressed score (the 0.02 margin = ~20% of a [0.65,0.74] band → never clears), caps at 1 eviction/pass, only considers the single best newcomer → a #15 PLSM is never a displacement candidate. **Fix scoring first → this largely self-heals.**

## The FIX — a 3-layer scorer (batch-relative, no magic; building behind `chili_momentum_explosive_scoring_enabled`)
- **Layer 1 — LEXICOGRAPHIC EXPLOSIVENESS TIER** (outer sort key; the decisive guarantee). Batch-relative cuts: tier 3 (rvol ≥ ~10× batch-median AND change ≥ ~10× median → PLSM), tier 2 (≥3×), tier 1 (Ross floor 5×/10%), tier 0 (MU). Sort `(tier DESC, score DESC, rvol DESC)`. Non-compensatory: a tier-3 STRICTLY outranks every tier-0/1 → fills the 9-12 slots with the explosive cohort first → kills slot-starvation.
- **Layer 2 — log-MULTIPLICATIVE core × bounded quality**: `rvol_norm = log10-min-max(rvol)` (on the RAW signal, not percentile, so 15,000× stays separated from 6×); `core = rvol_norm^0.6 · mom_norm^0.4`; `score = core·(0.5 + 0.5·quality_blend)` (secondary pillars modulate only ±50%, never average an explosive down). **Same shape as the existing `curl_score`** — a trusted in-codebase pattern, not novel risk.
- **Layer 3 — tiebreak**: tier, then score, then raw rvol (the current tiebreak never fires because score is the first key).
- **Validated target:** PLSM tier=3 core≈0.92 → **top-3**; MU tier=0 → does not outrank. Flag-OFF = byte-identical (parity-proven).

## What to KEEP (do not throw away)
The whole backbone; the batch-relative/adaptive philosophy; the **per-(symbol, variant) + argmax dedupe** data model (variants are entry-style families → best-per-symbol is the right reducer, NOT ensemble/matview); the `float_rotation` self-correcting machinery (fix its denominator); the eligibility floors (become tier-1); ALL the displacement safety guards (inert-only victims, in-flight/orphan veto, reap-cooldown, deterministic upsert — the deadlock fix); the graceful-degrade/fail-open discipline.

## Migration (incremental, additive, kill-switched, parity-tested — NO big-bang)
- **Step 0** parity harness (lock PLSM=0.651#15 / MU=0.665#9 baseline). **Step 1** 3-layer scorer behind `chili_momentum_explosive_scoring_enabled` (off=byte-identical, on=PLSM top-3; default-ON once parity passes). **Step 2** float-trust gate (recent-IPO low-trust → drop low-float penalty + self-correct rotation). **Step 3** displacement hardening (relative-margin / express-lane / eviction-budget sub-flags). **Step 4** query index (DISTINCT ON + partial index; one DB migration). **Step 5-6** EDGAR float enrichment + LTR weight-tuning (last). Each: parity-test → flip live → observe → instant per-sha rollback.

**Bottom line:** the lane's profitability lever isn't more edges — it's this **ranking fix** so the +400% PLSM-class movers reach a top slot at 10:07, not 11:17. (Step 1 building now: wf w5g1xy1z5.) Related: replay caught E1 over-veto + the agentic `live_error` exit-qty strand (separate fixes).
