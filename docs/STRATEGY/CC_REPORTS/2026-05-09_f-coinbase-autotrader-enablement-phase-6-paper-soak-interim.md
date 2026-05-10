# CC_REPORT: f-coinbase-autotrader-enablement (Phase 6: paper soak — interim)

**STATUS: PART 1 DONE — AWAITING T+48h OPERATOR PROMOTION FOR PART 2.**

This is the interim Phase 6 report. CC's deliverable for Phase 6
splits into two parts:

* **Part 1 (now)** — observability tooling + soak documentation +
  operator runbook. Shipped in this commit.
* **Part 2 (T+48h)** — CC generates the soak completion report
  from probe output + DB state, recommends Phase 7 OR
  Phase 5.5/6.5 fix. Triggered when operator re-promotes Phase 6
  in NEXT_TASK.md after the wall-clock window.

No autotrader edits in this brief — Phase 6 is read-only
observability per the brief's hard constraint.

## Part 1 deliverables

### 1. Soak observability probe shipped — `scripts/d-cb-phase6-soak-probe.py`

Read-only Python script that pulls + summarizes seven sections
from the chili DB + Coinbase API:

1. **Routing distribution** — selector decisions (rh / coinbase /
   skip-with-reason) in the soak window.
2. **Cost-gate distribution** — `cost_gate:*` reasons in
   `trading_autotrader_runs`.
3. **Cap-gate distribution** — `coinbase_cap:*` reasons.
4. **Coinbase fills** — Trade rows with
   `broker_source='coinbase'` + ticker, qty, notional, status.
5. **Bracket coverage** — for each Coinbase Trade in window: is
   there a `trading_bracket_intents` row created within 60s of
   `entry_date` AND a `broker_stop_order_id` populated within 5min?
   Per acceptance criterion #4 (100% required).
6. **Cash drift** — current Coinbase `portfolio.cash` vs $2200.01
   baseline. Per acceptance criterion #5 (≤ $5 required).
7. **Anomaly summary** — green/amber/red verdict per the brief's
   thresholds.

CLI:
```
python scripts/d-cb-phase6-soak-probe.py --window-hours 12
python scripts/d-cb-phase6-soak-probe.py --json
```

Defaults: 48h window, pretty-print output. Operator runs at T+1h /
T+12h / T+24h / T+48h.

### 2. Soak documentation in `CURRENT_PLAN.md`

Added a "Parallel initiative — Coinbase autotrader enablement"
section that documents:

* Phases 1-5 status pointer.
* Phase 6 conservative caps + worst-case exposure (~$152).
* Probe runbook + cadence.
* Anomaly thresholds (RED / AMBER / INFO).
* Kill switch references (global vs Coinbase-only).
* Phase 6 promotion criteria to Phase 7.
* Trigger for CC's Part 2 report at T+48h.

## Sanity-test result (T+0)

```
$ python scripts/d-cb-phase6-soak-probe.py --window-hours 1
# d-cb-phase6-soak-probe @ 2026-05-10T00:17:36Z
# window: trailing 1h (since 2026-05-09T23:17:36Z)

=== 1. ROUTING DISTRIBUTION (selector decisions) ===
(no autotrader runs in window)

=== 2. COST-GATE DISTRIBUTION ===
(no cost-gate decisions in window)

=== 3. CAP-GATE DISTRIBUTION (Coinbase per-venue cap) ===
(no cap-gate decisions in window)

=== 4. COINBASE FILLS (Trade rows broker_source='coinbase') ===
(no Coinbase Trades in window)

=== 5. BRACKET COVERAGE (Coinbase entries) ===
(no Coinbase entries to check coverage on)

=== 6. CASH DRIFT (current vs baseline) ===
  baseline cash: $2200.01
  current cash:  $2200.01
  drift:         $+0.00
  verdict:       GREEN — within tolerance

=== 7. ANOMALY SUMMARY (acceptance criteria) ===
  [INFO] no Coinbase entries this window — path not exercised yet
  [GREEN] cash drift $+0.00
```

The probe runs end-to-end against production DB + Coinbase API
without errors. Cash baseline matches operator's funded amount.

## Operator-side actions (load-bearing — Phase 6 SOAK START)

1. **Flip `CHILI_COINBASE_AUTOTRADER_LIVE=1`** in `.env`.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker broker-sync-worker`.
3. Verify env pickup across all 4 workers:
   ```bash
   for c in chili autotrader-worker scheduler-worker broker-sync-worker; do
     docker exec chili-home-copilot-${c}-1 python -c \
       "from app.config import settings; \
        print('${c}:', settings.chili_coinbase_autotrader_live)"
   done
   ```
   Expected: `True` in every container.
4. **Run probe at T+1h / T+12h / T+24h / T+48h**:
   ```bash
   python scripts/d-cb-phase6-soak-probe.py --window-hours 12
   ```
5. **At T+48h**: re-promote Phase 6 to NEXT_TASK PENDING and CC
   generates the soak completion report (Part 2 of this brief).

## Real-money risk

Conservative caps from Phase 5:

* `CHILI_COINBASE_MAX_NOTIONAL_USD=50` per position.
* `CHILI_COINBASE_MAX_CONCURRENT_POSITIONS=3`.

→ Worst-case Coinbase exposure: $50 × 3 = $150.
→ + Tier-1 round-trip fees (120bps × $150) = ~$1.80 per round-trip.
→ Maximum cash drift if every position hits stop AND stop-limit
  fills 0.5% below stop: ~$0.75 per position × 3 = ~$2.25 worst-case
  realized loss + $1.80 fees ≈ $4 total. Well within the $5 amber
  threshold.

If anything goes catastrophically wrong, kill switches are 30s away:

* `CHILI_AUTOTRADER_KILL_SWITCH=1` in `.env` + force-recreate →
  halts BOTH venues globally.
* `CHILI_COINBASE_AUTOTRADER_LIVE=0` in `.env` + force-recreate →
  halts Coinbase routing only; RH unaffected.

## What CC will do at T+48h (Part 2)

When operator re-promotes Phase 6:

1. Run the probe with `--window-hours 48`.
2. Cross-check: query `trading_autotrader_runs` + `trading_trades`
   + `trading_bracket_intents` for full-window aggregates.
3. Pull final cash from Coinbase API.
4. Apply acceptance-criteria checks:
   - ≥1 Coinbase route attempt (block OR success).
   - ≥1 RH pass + ≥1 Coinbase decision in cost-gate.
   - 100% bracket coverage on Coinbase entries.
   - Cash drift ≤ $5.
   - No silent failures.
   - RH equity entries identical to pre-Phase-3.
5. Recommend ONE of:
   - **Phase 7 GREEN-LIGHT** (all criteria met).
   - **Phase 5.5** (USDC-aware buying-power wiring) if cost-gate
     blocks via "Insufficient balance in source account" surfaces.
   - **Phase 6.5** (hygiene fix for any anomaly).
   - **Phase 6 EXTEND** (if 0 Coinbase entries observed; path not
     exercised).

## Constraints honored (Part 1)

* ✅ **No code changes during soak window.** Probe is a script,
  not a runtime change. CURRENT_PLAN edit is doc-only.
* ✅ **No autotrader edits.** Read-only observability.
* ✅ **Conservative caps stay conservative.** Probe references
  but does not change `CHILI_COINBASE_MAX_*` defaults.
* ✅ **Kill-switch references documented** — operator has both
  switches at hand.
* ✅ **Edit-tool truncation discipline.** New script (single
  file, AST clean). CURRENT_PLAN edit is small + bounded.

## Rollback plan

* Probe script is read-only — no rollback needed.
* If CURRENT_PLAN soak section is wrong: `git revert` the edit;
  CURRENT_PLAN reverts to position-identity-only focus.
* If operator decides to abort soak before T+48h:
  `CHILI_COINBASE_AUTOTRADER_LIVE=0` in `.env` + force-recreate,
  then queue Phase 6.5 hygiene brief OR re-queue Phase 6 with
  observed-anomaly notes.

## What's NEXT

* Operator-side: flip LIVE=1, force-recreate, run probe at T+1h.
* T+48h: re-promote Phase 6 → CC ships Part 2 (soak completion
  report).
* Post-Part-2: Phase 7 (live + capital ramp) OR Phase 5.5 / 6.5
  fix.
