# Current Plan: Position Identity Refactor

**Initiative owner:** Cowork (strategy) + Claude Code (execution).
**Last update:** 2026-05-07, after the exit-monitor-quote-guard-unification review (Phase 1 shipped + multiple architectural-correctness landings; refresh removes stale architectural-questions block now that operator answered them and the design doc absorbed the answers).

> **Why this initiative supersedes the prior fast-path crypto-scalping plan.** Today (2026-05-04) two automated close paths fired, marking 11 equity Trade rows wrongly closed in DB while the broker still held the positions. The shipped patch (inverse-reconcile, broker-truth-self-heal task) auto-healed 18 of them but its cross-check (`event_count == 0` on `trading_execution_events`) is conservative because **Trade row IDs are ephemeral** — every time a row gets wrongly closed and recreated, fills associated with the prior trade_id orphan. The fast-path scalping initiative depends on a stable position model; building more on this foundation makes things worse, not better. Position-identity refactor goes first. Fast-path resumes after.

> The operator's framing: *"I just vibe coded this so proper data structure and algorithms refactoring must be done for this."* This plan is that refactor.

## Goal of the initiative

Introduce a **persistent, broker-authoritative position identity** as a first-class concept above Trade rows. After the refactor:

- A "position" is identified by `(user_id, broker_source, account, ticker)`. It persists across Trade row generations.
- Every fill recorded in `trading_execution_events` is associated with a position, not just a trade_id. When a Trade row gets recreated for the same physical position, fill history is preserved.
- The "did this position close legitimately" question is answerable from broker-side fact (positions API + fill events) without depending on which trade_id happens to be the current management envelope.
- Trade rows become **management envelopes** — short-lived state machines that hold pattern, entry reason, stop/target, etc. for the *currently-active management instance* of a position. They can be closed and reopened without losing history.
- C2 phantom guard, inverse-reconcile, broker_reconcile_position_gone — all close paths read from position-level state instead of trade_id-level state. Their cross-checks become precise instead of conservative.

**Success criterion for the initiative:** the conservative `event_count == 0` check in inverse-reconcile is replaced by the precise question "is this position closed at the broker AND is there a confirming SELL fill on this position's history?" — and the answer covers all Trade row generations.

## What's broken architecturally (the audit)

From today's investigation. Each of these has a workaround in shipped code; the refactor removes the need.

1. **Trade row IDs are reused as the join key for fills.** `trading_execution_events.trade_id` references whichever Trade row was active when the fill happened. When a Trade row gets wrongly closed (one of the auto-close paths) and broker_sync creates a fresh Trade row, the fresh row has no fill history, even though the underlying broker position never changed.
2. **C2 phantom guard refuses to backfill in this exact scenario.** Today's 11 stuck positions all hit C2 ("position present at broker, no matching filled buy order found in recent history") — the buys WERE recorded, just on dead trade_ids. C2 has no concept of position-level history.
3. **Inverse-reconcile is conservative because of #1.** The check `event_count == 0` is a workaround for not knowing which fills belong to the *position* vs the current trade_id. If a future bug auto-closes a Trade row that DOES have a recorded buy fill on its current trade_id, inverse-reconcile routes to CONTRADICTION and refuses to heal — even when it should heal.
4. **broker_reconcile_position_gone, phantom_after_terminal_reject, emergency_price_monitor_guardrail, zombie_reconcile_orphan, broker_reconcile_no_exit_price** — five independent close paths each writing `exit_reason` strings into `trading_trades`. The diversity is hard to reason about. With position-level state, "is this position closed" is a single query against position_state, not a string-matching exercise across five reasons.
5. **Bracket intents are 1:1 with Trade rows via `trade_id` FK.** Same problem: when a Trade row dies, the bracket_intent attached to it dies. The May-1 broker SELL_STOP placed for trade 1815 (EKSO) is logically for the *position*, not the Trade row — but the bracket_intent FK points at the dead trade_id. When trade 1815 was wrongly closed at noon, intent 223 followed it. Today the EKSO position legitimately closed via that very stop firing — the bracket reconciler had no way to credit the fill to the right management envelope because the envelope was already gone.
6. **`CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` was load-bearing-by-accident.** Today's deploy revealed: when the bracket writer can't see existing covering limit-sells as part of the position's state, its only way to get a stop placed is to cancel the limit. With proper position state, the writer would know about both the limit-sell and the stop, treat them as a bracket pair on the same position, and not need to cancel one to place the other.

## Migration plan

See `docs/DESIGN/POSITION_IDENTITY.md` § 8 for the authoritative phase plan + per-phase exit criteria. CURRENT_PLAN keeps initiative-level orientation; the design doc is the single source of truth for technical specifics. The 6-phase sketch that lived here was concretised into the design doc on 2026-05-04 and revised on the same day with operator answers (direction-for-shorts schema, PaperTrade in Phase 1, Phase 5 soak compressed to 2 weeks).

## Constraints / hard rules

- **No magic numbers anywhere in the refactor.** Every threshold continues to derive from observable system state per the operator's standing principle.
- **No flag-day migrations.** Each phase has to leave the system in a working state with the previous phases' workarounds still functioning. Refactor lands incrementally, not as a bang.
- **No live-money behavior change without explicit operator approval.** Phase 1 is read-only. Phase 2 is backfill + alternate index. Phases 3-6 each ship with a feature flag; flag flip is an operator decision per phase.
- **Backwards-compat with existing close-reason strings.** Reporting code, dashboards, audit queries assume those strings exist. The refactor adds a new authoritative source; it does NOT remove the old one for at least N phases of soak.
- **Tests use `_test`-suffixed DB.** Standard PROTOCOL Hard Rule.

## Architectural questions (closed)

The five open questions on this page were answered by the operator 2026-05-04 and integrated into `docs/DESIGN/POSITION_IDENTITY.md`. Decisions in summary:

1. **Account type granularity** — per-account keys (Robinhood `cash`/`margin`/`ira`, Coinbase `spot`/`portfolio`); position identity tuple is `(user_id, broker_source, account_type, ticker, direction)`.
2. **Cross-broker positions** — `broker_source` is in the identity key; same ticker held at two brokers is two positions. Aggregation lives in reporting, not the position model.
3. **Snapshot vs event-sourced** — event-sourced. `trading_position_events` is the authoritative timeline; `trading_positions` carries derived current state plus a self-healing reconciler.
4. **Trade-row identity layering** — three-layer split. `trading_decisions` (immutable entry intent) → `trading_management_envelopes` (rename of today's `trading_trades`, mutable management state) → `trading_positions` (broker-authoritative identity).
5. **Migration ordering** — Phase 1 drops the schema_version 223 orphan column (`phantom_close_consecutive_zero_qty_sweeps`) as part of the same migration that creates the new tables.

Phase 5 soak duration was also tightened from one quarter to **2 weeks** at operator's instruction.

## Status of the initiative

- **Design doc shipped 2026-05-04.** Locked at `docs/DESIGN/POSITION_IDENTITY.md`. Six phases enumerated with per-phase exit criteria.
- **Phase 1 shipped 2026-05-04.** Migration 224 created `trading_positions` + `trading_position_events`; `sync_positions_to_db` writes shadow-mode (try/except wrapped, never raises, zero readers depend on it); backfill walked both `trading_trades` and `trading_paper_trades`. Audit query post-deploy: 19/19 parity, 0 discrepancies.
- **Phase 1 1-week soak in progress.** Soak window closes 2026-05-11. Passive monitoring via `scripts/audit_position_layer_parity.py`.
- **Phase 2 queues after soak.** `trading_execution_events.position_id` backfill — link every fill row in history to its position. Read-side stays pointed at trade_id; this is the foundation for Phase 3's authority flip.
- **Phases 3–6 sketched in design doc.** Authority flip (3), close-path consolidation (4), envelope-rename + decision-layer split (5, with 2-week soak), bracket_intent re-key + cleanup (6).

## Parallel initiative — Coinbase autotrader enablement

f-coinbase-autotrader-enablement is a parallel multi-phase
initiative orchestrated outside this position-identity refactor
(both touch trading code, but on disjoint surfaces — Coinbase
adds a new venue path; the refactor reshapes the in-process
position model).

**Current status (2026-05-09 24h):**

* Phases 1-5 SHIPPED. See per-phase CC reports under
  `docs/STRATEGY/CC_REPORTS/2026-05-09_f-coinbase-autotrader-enablement-phase-{1-5}-*.md`.
* Phase 6 (paper soak) START — operator flips
  `CHILI_COINBASE_AUTOTRADER_LIVE=1` then runs the soak window.
  Conservative caps in effect: `CHILI_COINBASE_MAX_NOTIONAL_USD=50`
  + `CHILI_COINBASE_MAX_CONCURRENT_POSITIONS=3` + Tier-1 cost-aware
  gate (120bps fee + 30bps buffer = 150bps min projected edge).
  Worst-case exposure ~$152.

**Soak observability:** `scripts/d-cb-phase6-soak-probe.py`
(read-only) summarizes seven sections — routing distribution,
cost-gate decisions, cap-gate decisions, Coinbase fills, bracket
coverage, cash drift vs $2200.01 baseline, anomaly summary.

Operator runs at T+1h, T+12h, T+24h, T+48h:

```
python scripts/d-cb-phase6-soak-probe.py --window-hours 12
```

**Anomaly thresholds:**

* Bracket coverage <100% → RED, queue Phase 6.5.
* Cash drift > $25 → RED.
* Cash drift > $5 → AMBER (monitor).
* Coinbase entry without an intent within 60s → RED.
* 0 Coinbase entries during soak → INFO (path not exercised; not a
  failure — operator may extend the window or seed a synthetic
  alert helper).

**Kill switch ready:** `CHILI_AUTOTRADER_KILL_SWITCH=1` halts BOTH
venues globally in 30s. Coinbase-only halt:
`CHILI_COINBASE_AUTOTRADER_LIVE=0` (~30s; RH unaffected).

**Phase 6 promotion criteria** (to Phase 7 — live with capital
ramp): all of: ≥1 Coinbase route attempt observed (success OR
block — proves path exercised); 100% bracket coverage on any
fills; no silent failures; cash drift ≤ $5; RH equity entries
continue routing+placing identically.

At T+48h operator re-promotes Phase 6 to NEXT_TASK and CC
generates the soak report at
`docs/STRATEGY/CC_REPORTS/2026-05-{N}_f-coinbase-autotrader-enablement-phase-6-paper-soak.md`
with green-light Phase 7 OR Phase 5.5/6.5 fix recommendations.

## What's deferred / parallel

- **Bug 4** (`emergency_close_all` should submit broker SELLs or be retired). No urgency now (no auto-callers); fits within Phase 5 of this refactor naturally — auto-callers are gone, manual usage gets handled via the position-state-transition machinery.
- **Schema hygiene** — Phase 1 dropped migration 223's orphan column (`phantom_close_consecutive_zero_qty_sweeps`) in mig 224. Done.
- **Fast-path crypto scalping initiative.** Paused. The fast-path stack writes to the same `trading_execution_events` and (eventually) `trading_trades` tables; building more on the unstable foundation compounds the problem. Resume after Phase 4 lands.
- **The lying P/L on EKSO trade 1815 ($0 vs actual −$38.80) and ELTX trade 1816 ($0 vs −$33.00).** One-time data backfill, still pending. Operator decision whether to clean now (two SQL UPDATEs) or accept as a one-time scar.
- **Open Q from `f-exit-monitor-quote-guard-unification` review (2026-05-07):** per-ticker volatility-derived implausibility thresholds (`f-implausible-quote-per-ticker-vol`) — surface only when production data shows the structural 0.1x/10x bounds rejecting a real meme-stock move. Until then, the structural constants are correct-by-construction.

## Out of scope right now

- **Live broker-routing changes.** This refactor is data-model only. Order placement code stays as-is; only the way we *think* about positions changes.
- **Non-Robinhood/Coinbase venues.** Forex (OANDA), perps (Hyperliquid/dYdX/Kraken Futures) follow the same pattern but Phase 1 starts with the brokers that have today's audit pain. Generalize to other venues once the model is proven.
- **Dashboards and reporting changes.** Existing reporting queries continue to work via backwards-compat. UI changes can come later.
