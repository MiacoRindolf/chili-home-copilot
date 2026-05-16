# 2026-05-15 — evidence-fidelity arc follow-up activations (Cowork-direct)

> Author: Cowork (interactive, acting directly — not via the
> Cowork/CC plan-execute loop)
> Trigger: Operator decisions on all 4 deferred follow-ups from the
> Phase A–E commit chain.

## What shipped

Three config-default flips + one release-blocker inversion. No code
behavior changes beyond what's gated on these flags.

### 1. `chili_family_fdr_enabled` False → True

`app/config.py:1020`. Phase E's brief defaulted this OFF for a 7-day
soak; operator decided to flip it on now.

**Why safe:** the BH-adjusted DSR threshold is computed and logged
into `pattern_family_trial_log` regardless of the flag; what changes
when True is the `use_bh` choice in
`cpcv_adaptive_gate._evaluate_adaptive`. For single-variant families
(`family_size == 1`) the path is unchanged — BH only activates when
`fam_m > 1 and thr_bh is not None`. Multi-variant families now get
a stricter pool threshold, matching the research-correct discipline.

**Bounded risk:** tighter threshold → potentially fewer promotions.
But the adaptive gate's `chili_cpcv_target_promotion_pool_pct = 0.05`
ceiling (5% of pool) caps the floor on admission rate; drought
cannot exceed `1 - (max_pool_size / total_active_patterns)`
asymptotically.

### 2. `brain_execution_cost_mode` "shadow" → "authoritative"

`app/config.py:332-335`. Phase B of evidence-fidelity wired
`record_fill_observation` into the 3 close-hook paths
(`on_paper_trade_closed`, `on_live_trade_closed`,
`on_broker_reconciled_close`). The writer was already shadow-active;
flipping to authoritative permits downstream consumers (NetEdge,
sizing) to read `trading_execution_cost_estimates` as truth.

**Today's reality check:** no consumer logic reads
`m == 'authoritative'` and gates differently from `m == 'shadow'`
yet. The flip primarily updates the `mode=` field on
`[execution_cost_ops]` log lines and signals intent. Real consumer
wiring is a separate brief.

### 3. `brain_venue_truth_mode` "shadow" → "authoritative"

`app/config.py:341-346`. Same wire-shape as exec-cost. Writes to
`trading_venue_truth_log` go via the same close-hook surface from
Phase B.

**Required companion change** — the release-blocker:
`scripts/check_venue_truth_release_blocker.ps1` previously fired on
any `[venue_truth_ops] mode=authoritative` log line as a phase-F
shadow-lockdown invariant. Inverted: now fires on `mode=shadow` or
`mode=off` (regression detector). Legacy semantics preserved behind
`-LegacyShadowLockdown` switch for rollback windows.

### 4. NetEdge Stage 2 + soak audit briefs

Two new briefs in `docs/STRATEGY/QUEUED/`:

- `f-netedge-stage1-soak-audit.md` — read-only calibration audit
  that runs after Stage 1 (commit `e5a04e5`) has accumulated ≥48h of
  shadow-log data. Outputs a PROMOTE / EXTEND-SOAK / BLOCK decision.

- `f-netedge-stage2-allocator-routing.md` — the actual cutover
  brief: splice `portfolio_allocator.evaluate(...)` between LLM
  revalidation and execution paths; flag-gated under
  `chili_autotrader_route_via_allocator_enabled` (default False);
  flips `brain_net_edge_ranker_mode` from `"shadow"` to
  `"authoritative"` only after soak audit passes.

**Architect call:** I did NOT queue a `.session` for either brief
yet. The soak-audit must wait for runtime data to accumulate; the
operator can promote it (or its `.session`) once 48–72h have
elapsed since `e5a04e5`. Stage 2 must wait on the soak audit's
sign-off. Auto-queueing would either fail (insufficient rows) or
ship Stage 2 without evidence.

## Hypothesis-family backfill — architect decision

Operator: "I'm not sure. Make your best decision as an algo trader
architect."

**Decision: run the existing backfill.** Justification:

- `app/services/trading/pattern_family_backfill.py` exists and is
  idempotent (two-pass: parent_chain inheritance → keyword
  classifier from name + description; priority-ordered).
- Migration 185 ran historically; new patterns since then inherit
  family from parent via `learning.py` insert paths, so NULL today
  is a narrow tail (orphans whose parents were also NULL, plus
  hand-seeded patterns).
- The Phase E BH-adjustment SILENTLY BYPASSES NULL-family patterns
  (helper falls back to `n_hypotheses_tested=1` which matches legacy
  behavior). Without backfill, BH discipline only fires on the
  already-tagged subset — defeating the purpose.
- Cost is one container exec + a few hundred UPDATEs. Risk is
  bounded by the keyword classifier's curated priority list and the
  fact that updates only fire on NULL or 'unknown'.

Dispatched via `scripts/dispatch-hypothesis-family-backfill.ps1`
(staged for daemon pickup). Runs:
1. Pre-coverage SELECT (NULL / unknown / tagged counts)
2. By-family distribution
3. `_smoke_family_backfill.py` (dry-run inside → applies → re-checks)
4. Post-coverage SELECT

Operator can re-promote the dispatch script if it doesn't fire on
the next daemon pass.

## Hard constraints honored

- [x] No autotrader / venue / broker behavior change
- [x] No migrations added (flag flips + comment updates only)
- [x] Phase E BH math safe for single-variant families (unchanged)
- [x] Release-blocker preserved via `-LegacyShadowLockdown` switch
- [x] No code in autotrader/sizing reads `m == 'authoritative'` yet;
      flipping the flag is forward-compatible
- [x] Stage 2 NetEdge brief documented; NOT auto-queued (gated on
      soak audit)

## Open items for operator

1. **Run the family-backfill dispatch script** (or it will run on
   next daemon pass if queued). Verify the output file in
   `scripts/dispatch-hypothesis-family-backfill-output.txt`.
2. **Container restart** required for the 3 flag flips to take
   effect at runtime. `docker compose up -d --force-recreate chili
   autotrader-worker brain-worker scheduler-worker broker-sync-worker`.
3. **Promote `f-netedge-stage1-soak-audit.md` to NEXT_TASK** in 48h
   when shadow-log data has accumulated.
4. **Watch `pattern_family_trial_log`** for the first week — the BH
   adjustment will now apply to any family that hits the gate with
   `>1` active variants. Surface to operator if drought signal
   shifts.
