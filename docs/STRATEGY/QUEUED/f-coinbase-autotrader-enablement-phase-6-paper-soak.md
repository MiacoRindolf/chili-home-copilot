# f-coinbase-autotrader-enablement-phase-6-paper-soak

**Owner**: Cowork → Claude Code (light) + Operator (load-bearing)
**Status**: PENDING
**Risk**: LOW-MEDIUM (real money in play but capped at $150 total
exposure: $50 × 3 concurrent positions). Real downside is a sloppy
broker-side fill due to thin Coinbase liquidity, not a code defect
class.
**Time budget**: ≥48h wall-clock soak; CC scope is the
observability + analysis tooling (~2h CC).

## Goal

Phase 6 of the Coinbase enablement initiative. Run a **≥48h paper-
soak** with `CHILI_COINBASE_AUTOTRADER_LIVE=1` and the conservative
caps from Phase 5 ($50 notional / 3 concurrent / Tier-1 fee gate).
Verify the full chain — selector → cost-gate → cap-check →
Coinbase BUY → bracket writer stop-limit → reconciler → exit —
behaves as designed under live (paper-sized) production load.

This is the **last gate before Phase 7** (live with capital ramp).

## Why now

Phases 1-5 shipped. All hard prerequisites met:
- Auth verified (Phase 2)
- Selector routes correctly (Phase 3)
- Stop primitive + bracket writer wired (Phase 4)
- Cost-aware gate + per-venue caps live (Phase 5)
- USD wallet has buying power: cash=$2200.01

The only remaining unknown is **production behavior under
real-time load**: do alerts that fire actually route correctly,
do brackets land within seconds, does the reconciler key on
`broker_source='coinbase'` correctly when a position closes,
does the cost-gate's 12%-rule-floor / 1.5%-Coinbase-fee-floor
layering actually allow any Coinbase entries through?

## The change (3 components, all observability — no autotrader edits)

### Component A — Soak observability dashboard / probe script

A single script `scripts/d-coinbase-soak-probe.ps1` (or .py — CC
chooses) that pulls and summarizes:

1. **Routing distribution** over the soak window:
   - `selector:rh_whitelist_match` count
   - `selector:fast_path_active` count
   - `selector:coinbase_routing_shadow_log` count (when LIVE=0
     before flip)
   - `selector:no_venue_supports` count (should be 0)
2. **Cost-gate distribution**:
   - `cost_gate:rh_fee_free` count (RH passes)
   - `cost_gate:coinbase_clears_fee_threshold` count
   - `cost_gate:coinbase_below_fee_threshold` count (blocks)
3. **Cap-gate distribution**:
   - `coinbase_cap:no_cap_breach` count
   - `coinbase_cap:notional_cap_exceeded` count (clip / block)
   - `coinbase_cap:position_count_cap_exceeded` count
4. **Coinbase fills**:
   - List of `Trade` rows with `LOWER(broker_source)='coinbase'`
     and `created_at >= soak_start_ts`.
   - Per row: ticker, qty, notional_usd, entry price, current
     price, P&L %, has-bracket-intent (yes/no), bracket-stop-
     placed (yes/no with broker order_id).
5. **Bracket coverage rate**:
   - Of the Coinbase entries, what fraction have an associated
     `bracket_intent` row with non-null `broker_stop_order_id`?
6. **Broker-side residual orders**:
   - `coinbase_service.get_recent_orders(limit=100)` filtered
     to status='OPEN'/'PENDING'. Should match the Coinbase
     entries × 1 stop each.

### Component B — Daily check-in alerts (passive)

If anything goes obviously wrong, surface it. The script should
flag (not auto-fix):
- Any Coinbase entry without a bracket within 60s of fill.
- Any cost-gate decision that contradicts the rule
  (e.g., `coinbase_clears_fee_threshold` for an edge < 1.5%).
- Any open Coinbase order that's been resting > 24h without
  fill or cancel (paper-soak window is 48h; orders shouldn't
  stale-out).
- Cash drift > $5 from $2200.01 baseline (would suggest an
  unexpected fill or fee charge).

### Component C — Soak completion report

After ≥48h, a CC-generated `docs/STRATEGY/CC_REPORTS/<DATE>_f-
coinbase-autotrader-enablement-phase-6-paper-soak.md` summarizing:
- Routing distribution (counts + %s).
- Cost-gate decisions (counts).
- Coinbase entries (full list with realized P&L).
- Bracket coverage rate (target: 100%).
- Any anomalies surfaced.
- **Decision**: green-light Phase 7, OR queue Phase 5.5 / 6.5
  fixes first.

## Acceptance criteria (8-item list)

1. **Soak dashboard / probe script shipped**. Operator can run
   it on demand and get a clean snapshot of the soak's state.
2. **Routing distribution captured**: at least 1 valid Coinbase
   route attempt during the window (success or block — just
   needs to prove the path was exercised).
3. **Cost-gate distribution captured**: at least 1 RH pass + 1
   Coinbase decision (pass or block).
4. **Bracket coverage on Coinbase entries**: 100% (every
   Coinbase entry has a bracket intent within 60s + a placed
   broker stop within 5min).
5. **No cash drift > $5** from $2200.01 baseline (modulo
   actual fills + fees, which are documented).
6. **No silent failures**: no Coinbase entries without bracket,
   no orphan stops, no `selector:no_venue_supports` for known
   long-tail tickers.
7. **All RH equity entries continue to route + place
   identically**: the existing RH path is byte-identical
   under live load (verifiable via Trade row count + cost
   profile pre-Phase-3 vs Phase-6).
8. **CC report at canonical path** with green-light or
   queue-fix recommendation.

## Brain integration (read-only EXCEPT the operator's LIVE flip)

**Operator-side actions** (CC does NOT do these):
- Set `CHILI_COINBASE_AUTOTRADER_LIVE=1` in `.env`.
- `docker compose up -d --force-recreate chili autotrader-worker
  scheduler-worker broker-sync-worker`.
- Watch the system over ≥48h.
- Run the soak probe at intervals (T+1h, T+12h, T+24h, T+48h).
- At T+48h, trigger CC to write the soak report by promoting
  this brief.

**CC-side actions**:
- Write the probe script.
- After 48h, read DB + Coinbase API + logs and produce the
  soak report.
- Recommend Phase 7 OR Phase 5.5/6.5 fixes.

**Read-only:**
- All Phase 1-5 modules (no edits).
- DB tables: `Trade`, `bracket_intent`, `pattern_imminent_alerts`.
- Coinbase: `get_portfolio()`, `get_positions()`,
  `get_recent_orders()`.
- Container logs.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **No code changes** during the soak window. If a bug surfaces,
  surface it; fix is a Phase 6.5 brief, not in-flight edits.
- **Operator-controlled LIVE flip.** Phase 6 brief does NOT
  auto-flip the env var.
- **Conservative caps stay conservative**: $50 / 3 positions
  during soak. Operator may raise after Phase 7 lands.
- **Kill switch ready**: if anything looks wrong during soak,
  operator flips `CHILI_AUTOTRADER_KILL_SWITCH=1` to halt all
  entries (RH AND Coinbase). 30-second mitigation.

## Out of scope (Phase 6 — covered by later phases)

- Live with capital ramp + cap raises (Phase 7).
- Coinbase fee-tier optimization (volume-based; future).
- Maker-only routing for Coinbase (separate brief).
- USDC-quoted (`-USDC`) ticker support.
- Phase 5.5 buying-power-into-gate wiring (queue if soak
  shows it matters).

## Sequencing

### Phase 6.0 — soak start
1. CC writes probe script.
2. CC writes this brief's "soak start" note in
   `docs/STRATEGY/CURRENT_PLAN.md` documenting the start
   timestamp.
3. **Operator flips `CHILI_COINBASE_AUTOTRADER_LIVE=1`** in
   `.env` + force-recreate workers.
4. Soak window begins.

### Phase 6.1-6.3 — interval check-ins (operator)
5. T+1h: run probe script. Confirm at least one autotrader
   cycle has passed; selector decisions logged.
6. T+12h: run probe script. Confirm cost-gate + cap-gate firing
   under load.
7. T+24h: run probe script. If a Coinbase entry has fired,
   verify bracket coverage.
8. T+48h: run probe script + queue Phase 6 promotion to CC for
   final report.

### Phase 6.4 — soak completion (CC)
9. CC reads DB + Coinbase API + logs + probe outputs.
10. CC generates the soak report.
11. CC recommends Phase 7 OR Phase 5.5/6.5.
12. Commit + push.

## Operator-side after Phase 6 ships

1. Read the soak report.
2. **Decide**: queue Phase 7 (live with capital ramp) OR queue
   Phase 5.5 / 6.5 if anomalies surfaced.
3. (Optional) raise `CHILI_COINBASE_MAX_NOTIONAL_USD` from
   $50 to $100-200 if Phase 6 shows clean operation.
4. (Optional) raise `CHILI_COINBASE_MAX_CONCURRENT_POSITIONS`
   from 3 to 5-10 same conditions.

## Rollback plan

- **Anomaly detected mid-soak**:
  `CHILI_COINBASE_AUTOTRADER_LIVE=0` halts new Coinbase entries;
  RH unaffected. Existing Coinbase positions continue under
  bracket coverage. Operator manual-cancels via Coinbase UI if
  desired.
- **Catastrophic**: `CHILI_AUTOTRADER_KILL_SWITCH=1` halts ALL
  entries (both venues). 30-second mitigation.
- **Operator wants to abort early**: same as above; queue a
  Phase 6.5 hygiene brief documenting what surfaced.

## What CC should do if it's unsure

1. **Probe script encounters DB failure**: STOP. Surface for
   operator. Do NOT continue with stale data.
2. **Coinbase entry without bracket** found post-soak: surface
   in report as CRITICAL. Don't auto-fix; recommend Phase 6.5
   brief.
3. **Cost-gate decision contradicts the rule** (e.g.,
   coinbase_clears_fee_threshold for edge < 1.5%): surface as
   CRITICAL with the offending row's full context.
4. **Cash drift > $5** without obvious explanation: surface as
   HIGH. Could be unexpected fee charge or partial fill.
5. **0 Coinbase entries during soak**: NOT a failure mode —
   means alerts didn't route to Coinbase during the window.
   Document as "soak completed but Coinbase path not exercised
   end-to-end"; recommend extending soak OR forcing a synthetic
   alert via Phase 6.5 helper.

## Notes on real-money risk

The conservative caps ($50 × 3 = $150 max exposure) keep this
soak cheap. Worst case: every Coinbase entry takes a 100% loss
overnight (extremely improbable for any liquid token, even
long-tail crypto), max loss is $150 + 120bps × 3 fills = ~$152.
This is well within the operator-acceptable risk envelope for
a paper-soak verification.
