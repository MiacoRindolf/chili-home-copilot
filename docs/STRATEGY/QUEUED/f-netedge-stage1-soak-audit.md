# f-netedge-stage1-soak-audit

> **Type:** Calibration audit gating Phase D Stage 2 promotion
> **Parent:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`
> **Sibling:** `docs/STRATEGY/QUEUED/f-netedge-stage2-allocator-routing.md`
> **Depends on:** `e5a04e5` (Phase D Stage 1 shadow-wiring) merged AND
> ≥ 48 hours of autotrader runtime accumulating `NetEdgeScoreLog` rows.

## Goal

Verify that the NetEdge shadow signal (Phase D Stage 1) has
discriminatory power on the autotrader's actual alert distribution
BEFORE flipping it to authoritative in Stage 2.

This is the safety gate that separates "we wrote the wire" from "we
trust the wire."

## Audit deliverables

A1. **Row-count + freshness check.**
    - Count `NetEdgeScoreLog` rows since `e5a04e5` merge time
    - Count rows with non-null `scan_pattern_id` AND non-null `regime`
      (Stage 1 contract)
    - Hours since the most recent row
    - Fail if: <50 qualified rows OR newest row > 6 hours stale

A2. **Coverage rollup.**
    - Distinct `scan_pattern_id` count across qualified rows
    - Distinct `regime` count
    - Per-asset-class breakdown (stock/crypto)
    - Flag if <5 distinct patterns or single-regime dominance >80%

A3. **Calibration metric.**
    - Join qualified `NetEdgeScoreLog` rows to closed trades via
      `scan_pattern_id` + alert timestamp window
    - Compute: Brier score, AUC of `calibrated_prob` vs realized
      outcome
    - Pool-level + per-asset-class
    - Compare against `brain_net_edge_min_samples` (50) floor

A4. **Disagreement rate vs heuristic gate.**
    - For each alert that passed heuristic gates AND has a
      `NetEdgeScoreLog`: count cases where NetEdge expected-net-edge
      is negative (would have blocked) vs positive (would have
      passed)
    - Surface the implied blocker rate

A5. **Regime-diagnostic health check.**
    - Verify the `_maybe_emit_regime_diagnostic` warning is NOT firing
    - Sample the last 24h of `[autotrader] netedge regime-snapshot
      diagnostic` lines

## Output

`docs/STRATEGY/AUDITS/2026-05-XX_netedge-stage1-soak.md` containing
all five sections with concrete numbers and a sign-off recommendation:
- **PROMOTE** — Stage 2 cleared to run
- **EXTEND SOAK** — need more data, retry in N hours
- **BLOCK** — calibration is bad, NetEdge needs retraining before
  Stage 2

## Hard constraints

- Read-only — no writes to NetEdgeScoreLog, no model updates
- Tests use `_test`-suffixed DB
- Joins must be PIT-correct (no lookahead into future closes when
  computing calibration)

## CONSULT GATE

None. Audit is mechanical.

## After

If PROMOTE: promote `f-netedge-stage2-allocator-routing.md` to
NEXT_TASK and queue its `.session`.
If EXTEND SOAK: re-queue this audit at +24h offset.
If BLOCK: write a NetEdge recalibration brief.
