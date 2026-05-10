# NEXT_TASK: f-coinbase-autotrader-enablement-phase-6-paper-soak

STATUS: PART_1_DONE_AWAITING_T+48 (re-promote to PENDING after T+48h for Part 2 soak report)

## Goal

Phase 6 of the Coinbase enablement initiative. Run a **≥48h paper
soak** with `CHILI_COINBASE_AUTOTRADER_LIVE=1` and the conservative
Phase 5 caps ($50 notional / 3 concurrent / Tier-1 fee gate).
Verify the full chain end-to-end under live (paper-sized) load
before Phase 7 (live with capital ramp).

The full brief is at
`docs/STRATEGY/QUEUED/f-coinbase-autotrader-enablement-phase-6-paper-soak.md`
— **read it first.** ≥48h wall clock; ~2h CC scope (observability
tooling + soak report).

## Why now

Phases 1-5 shipped:
- ✅ Auth verified (Phase 2; commits 6cce057 + 74b907b)
- ✅ Selector routes correctly (Phase 3; bcf9ea0 + 9c02e37)
- ✅ Stop primitive + bracket writer (Phase 4; e70e80f + aca780d)
- ✅ Cost-aware gate + per-venue caps (Phase 5; 4ad554b + 458b36d)
- ✅ USD wallet has buying power: cash=$2200.01

**All hard prereqs met.** Phase 6 is the last gate before Phase 7.

## The change (3 components — all observability)

1. **Soak observability probe script** — pulls + summarizes
   routing distribution, cost-gate decisions, cap-gate decisions,
   Coinbase fills, bracket coverage rate, broker-side residuals.
2. **Daily check-in alerts (passive)** — surface anomalies
   (no-bracket entries, cost-gate rule violations, stale orders,
   cash drift > $5).
3. **Soak completion report** — CC-generated post-48h with
   green-light Phase 7 or queue-fix recommendation.

**No autotrader edits.** Phase 6 is read-only observability.

## Real-money risk

Conservative caps: $50 × 3 = $150 max exposure. Worst case
~$152 with fees. Within operator-acceptable envelope for soak
verification.

## Acceptance criteria (8-item list)

See full brief. Headlines:

1. Probe script shipped; operator runs on demand for clean
   snapshot.
2. ≥1 valid Coinbase route attempt during window (success or
   block — proves path exercised).
3. ≥1 RH pass + ≥1 Coinbase decision in cost-gate.
4. **100% bracket coverage** on Coinbase entries (within 60s of
   fill + broker stop placed within 5min).
5. No cash drift > $5 from $2200.01 baseline.
6. No silent failures.
7. RH equity entries continue routing + placing identically.
8. CC report at canonical path with green-light or queue-fix
   recommendation.

## Operator-side actions (load-bearing)

1. **Flip `CHILI_COINBASE_AUTOTRADER_LIVE=1`** in `.env`.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker broker-sync-worker`.
3. Run probe at T+1h, T+12h, T+24h, T+48h.
4. At T+48h, queue Phase 6 promotion to CC for final report.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **No code changes during soak window.** Bugs surface as Phase
  6.5 briefs, not in-flight edits.
- **Conservative caps stay conservative.** $50 / 3 positions
  during soak.
- **Kill switch ready**: `CHILI_AUTOTRADER_KILL_SWITCH=1` halts
  both venues in 30s if anything looks wrong.

## Out of scope (Phase 6 — later phases)

- Live with capital ramp + cap raises (Phase 7).
- Coinbase fee-tier optimization.
- Maker-only routing.
- USDC-quoted (`-USDC`) ticker support.
- Phase 5.5 buying-power-into-gate wiring (queue if soak shows
  it matters).

## Sequencing

1. CC writes probe script.
2. CC documents soak start in CURRENT_PLAN.md.
3. Operator flips LIVE=1 + force-recreate.
4. Operator runs probe at T+1h, T+12h, T+24h, T+48h.
5. At T+48h, CC generates soak report from DB + Coinbase API +
   logs.
6. CC recommends Phase 7 OR Phase 5.5/6.5.
7. Commit + push.

## Rollback plan

- Anomaly mid-soak → `CHILI_COINBASE_AUTOTRADER_LIVE=0` halts
  new Coinbase entries.
- Catastrophic → `CHILI_AUTOTRADER_KILL_SWITCH=1` halts both
  venues.
- Abort early → flip both flags + queue Phase 6.5 hygiene
  brief.

## What CC should do if unsure

1. Probe DB failure → STOP. No stale data.
2. Coinbase entry without bracket → CRITICAL surface; recommend
   Phase 6.5.
3. Cost-gate decision contradicting rule → CRITICAL surface
   with full row context.
4. Cash drift > $5 unexplained → HIGH surface.
5. 0 Coinbase entries during soak → NOT a failure mode; document
   as path-not-exercised; recommend extending OR synthetic alert
   helper.
