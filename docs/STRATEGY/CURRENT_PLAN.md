# Current Plan: Position Identity Refactor

**Initiative owner:** Cowork (strategy) + Claude Code (execution).
**Last update:** 2026-05-18, after f-evaluation-function-fix Tier A shipped (commit `23bde18`) restoring pattern 585 + adding payoff-ratio demote protection + composite n≥5 floor. Position-identity Phase 2 is the next priority. See "Algo-trader re-eval 2026-05-18" section at bottom.

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
- **Phase 1 1-week soak passed.** Closed 2026-05-11. GRT-USD's 13-cycle close/reopen pattern surfaced as the marquee evidence for why position-identity is needed.
- **Phase 2 SHIPPED 2026-05-18.** Mig 248. `trading_execution_events.position_id` added + indexed + quarantine view; Option A backfill seeded 168 historical closed positions (33 → 201 in `trading_positions`); 8,358/8,358 with_trade_id events resolved (100%); 6,797 null_trade_id events sit in quarantine (expected). Double-write live at `record_execution_event` via `_resolve_position_id_for_event`. 11/11 tests pass. Reader canary pinned. Single commit in this Cowork session. CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-2.md`.
- **Phase 3 SHIPPED 2026-05-18.** Mig 249. `trading_bracket_intents.position_id` added + partial index + 422/422 rows resolved (100%). Resolver extracted to shared `app/services/trading/position_resolver.py`. Same-session ship with mig 250 (Coinbase account_type='spot' retrofit) + mig 251/252/253 + auto_trader.py TCA wiring. CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-3-and-tca-and-account-type.md`.
- **TCA wiring shipped 2026-05-18.** Mig 251/252/253 + `auto_trader.py:2233` now writes `tca_reference_entry_price=px`. Result: 285/638 trades populated; **avg entry slippage = +102 bps**. This is the single most-actionable finding from the session: pattern 585's 168 bps gross edge is being eroded by ~60% on entry slippage alone. Architectural follow-ups queued (maker-only Coinbase, tighter entry-price gating, reference re-snap).
- **Phase 4 SHIPPED 2026-05-18 (flag-gated).** Commit `cdf65fe`. New helper `position_has_recorded_sell(db, position_id)` + flag-gated reader in `broker_service.sync_positions_to_db` inverse-reconcile branch. CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-4.md`.
- **Sell-side recording SHIPPED 2026-05-18.** Mig 254 + `robinhood_exit_execution.py` writer hooks at 2 sites. Live: **450 sell events recorded** (was 0); **107 of 196 closed positions have recorded sell**. The Phase 4 flag-flip is now safe. CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-18_f-execution-events-sell-side-recording.md`.
- **Phase 4 flag-flip NEXT.** `f-position-identity-phase-4-flag-flip-paper-soak` — operator paper-soak of the Phase 4 reader path with `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=true`. Audit → flip → 24h soak → promote or rollback. No code change.
- **Phases 5–6 sketched in design doc.** Envelope-rename + decision-layer split (5, with 2-week soak), bracket_intent re-key + cleanup (6). Wait for Phase 4 to be operationally enabled before starting Phase 5.

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

## Parallel initiative — Promotion-pipeline rebalance (Phases 1-4 SHIPPED)

f-promotion-pipeline-rebalance is a parallel multi-phase initiative
addressing the brain's promotion pipeline — orchestrated outside the
position-identity refactor and the Coinbase autotrader rollout
(disjoint surfaces; this initiative reshapes lifecycle transitions and
quality scoring on `scan_patterns`, no broker code touched).

**Final architecture (post-Phase-4)**:

* **Two-ladder lifecycle** with new `shadow_promoted` stage (Phase 3,
  mig 236). Patterns at `shadow_promoted` fire imminent alerts but
  autotrader routes their alerts to shadow-log only. `promoted/live`
  remains the trade-eligibility ladder; `shadow_promoted` is the
  alert-eligibility-only ladder.
* **Gate-noise-free directional signal** (Phase 2, mig 235). New table
  `pattern_alert_directional_outcome` + view
  `pattern_directional_quality_v` measure "did price move ≥1.5% in
  predicted direction within 24h" on every imminent alert (not just
  gate-survivors). Rolling-30 per-pattern WR is the clean signal.
* **AND-logic auto-demote with sample-size floor** (Phase 1).
  Patterns with `trade_count < 30` are protected from realized-stat
  demotes; patterns with `cpcv_median_sharpe ≥ 1.0` are protected even
  at higher n (CPCV must agree before demote). Settings:
  `chili_pattern_demote_min_realized_trades` (=30),
  `chili_pattern_demote_require_cpcv_degrade` (=True).
* **Composite quality score** (Phase 4, mig 237). Per-pattern
  `quality_composite_score ∈ [0,1]` =
  0.30·clip(cpcv_sharpe/2.0) + 0.20·clip(deflated_sharpe/1.0)
  + 0.15·(1−pbo) + 0.25·directional_wr + 0.10·(1−decay),
  computed nightly at 23:30 PT.
* **Weekly cohort auto-promote** (Phase 4). Sunday 22:00 PT job
  selects top-N candidates (capped at 10/rolling-7-day) by composite
  score and advances them to `shadow_promoted`. Eligibility requires
  `promotion_gate_passed=True`, `cpcv_median_sharpe≥1.0`,
  `rolling_sample_n≥30` (decay computable), and excludes
  already-promoted/shadow_promoted/live patterns. **Ships DORMANT**:
  `chili_cohort_promote_enabled=False` until operator opts in.

**Status (2026-05-10)**:

* Phase 1 SHIPPED (commit `b00edec`): sample-size floor + CPCV
  protection. 16/16 tests PASS.
* Phase 2 SHIPPED (commit `e480d9f`): directional outcome + view +
  evaluator + 30-min scheduler. 19/19 tests PASS.
* Phase 3 SHIPPED (commit `ba05195`): shadow_promoted lifecycle +
  byte-identical autotrader parity gate held.
* Phase 4 SHIPPED (commit `893e73c`): composite scoring + cohort job
  (dormant by default). 21 tests written; some deferred to operator
  pre-opt-in run due to DB contention.
* Phase 5 DEFERRED: per-pattern universe via `scope_tickers`. Session
  errored at daemon launch; no commit. Brief preserved at
  `docs/STRATEGY/QUEUED/f-promotion-pipeline-rebalance.md` for future
  re-queue.
* Phase 6 DOC-AND-VERIFY (this section + CC_REPORT
  `docs/STRATEGY/CC_REPORTS/2026-05-10_f-promotion-pipeline-rebalance-phase6-final-summary.md`).

**Calibration evidence** (the data point the brief was betting on):

Pattern 585 was the marquee case. Pre-Phase-1 it was auto-demoted on
n=8 gate-laundered trades (realized WR 25%); after the rebalance:

| Metric                  | Value | Source                            |
|-------------------------|-------|-----------------------------------|
| CPCV median Sharpe      | 1.405 | pre-existing                      |
| Deflated Sharpe         | 1.0   | pre-existing                      |
| PBO                     | 0.0   | pre-existing                      |
| Directional WR (rolling-30) | 0.967 | Phase 2 view, captured 2026-05-10 |
| Composite score (calibration) | 0.843 | Phase 4 formula             |

The realized WR (gate-laundered noise) and the directional WR (clean
signal) diverge by ~70 percentage points on this pattern. That is the
quantified justification for the entire initiative.

**Operator opt-in (when ready)**:

1. Run pytest on the cohort suite:
   `pytest tests/test_pattern_cohort_promote.py -v -p no:asyncio`.
2. Set `CHILI_COHORT_PROMOTE_ENABLED=true` in `.env`.
3. `docker compose up -d --force-recreate chili scheduler-worker
   brain-worker autotrader-worker broker-sync-worker`.
4. Wait for next Sunday 22:00 PT cohort job; inspect:
   `SELECT id, name, lifecycle_stage, quality_composite_score FROM scan_patterns WHERE quality_composite_score IS NOT NULL ORDER BY quality_composite_score DESC LIMIT 20;`

**Kill switch**: `CHILI_COHORT_PROMOTE_ENABLED=false` halts the weekly
job at the flag check; nightly score refresh continues
(non-destructive). Code revert: `git revert` Phase 4 commit; mig 237
(`ADD COLUMN IF NOT EXISTS`) intentionally left in place — harmless.

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

## Parallel initiative — Phase 3 stop-bleed SHIPPED 2026-05-16 (Path A on DD breaker)

f-phase3-stop-bleed (out of the 2026-05-15 quant audit) shipped 9 commits
(`0fa783f` → `67c330e`) covering D1 (empirical monthly DD breaker, default
off), D2 (NameError diagnostic), D3 (`_normalize_product_id`), D4
(pre-flight Coinbase cash check + R1/R2 settings), D6 (`@validates
scan_pattern_id`), D7 (mig 243 BNB-USD cleanup), D8 (41/41 tests), D9
(walk-forward sim). D5 (stop_not_below_entry producer fix) deferred; the
existing rule at `auto_trader_rules.py:915` continues to reject the bad
orders so capital is not at risk while it sits in queue.

**Walk-forward finding.** The breaker never trips across K = 1.5σ /
2.0σ / 2.5σ / 3.0σ over 2026-03-10 → 2026-05-16. Reason: only **20
distinct CHILI-attributed close-days** in 67 calendar days; the
helper's `n >= 30` floor refuses to compute. CC handed Cowork the
A-or-B decision (keep floor / lower floor); **operator chose Path A**
on 2026-05-16.

**Path A arm-up protocol** (per `COWORK_REVIEWS/2026-05-16_phase3-stop-bleed.md`):

1. Daily scheduled task reports `n_distinct_close_days` from
   CHILI-attributed history.
2. When n ≥ 30, helper returns a non-None threshold; Cowork updates
   CURRENT_PLAN.md noting breaker is data-ready.
3. Operator flips `CHILI_MONTHLY_DD_BREAKER_ENABLED=1` only after
   reviewing the first non-None output and the K-sigma value at that
   point. Default remains `false`.
4. ETA at ~5 distinct close-days/week: mid-June 2026.

**Deploy state (2026-05-16).** Commits on disk. Container restart
daemon-dispatched 2026-05-16 to make D2/D3/D4/D6 live in running
processes. Post-restart audit-discovery probe scheduled for T+24h to
isolate D2/D3/D4 rejection-histogram deltas from pre-fix bleed.

**Next strategic priority.** The walk-forward result surfaced a
larger architectural issue than the breaker itself: the `no_pattern`
cohort (210 trades / −$1,560 / 47% of trade volume) is leaking past
the attribution pipeline. The `alerts.py:_scan_pattern_id_from_proposal`
extractor and the family-backfill work from
`project_2026_05_16_evidence_fidelity_activations.md` are the
upstream chokepoints. Two QUEUED briefs to write next:

- `f-attribution-leak-extractor`: walk
  `strategy_proposal_id → strategy_proposals` to recover pattern
  context when `signals_json` lacks it. Acceptance:
  `no_pattern` share of new trades drops from ~47% to <20%.
- `f-stop-not-below-entry-d5`: producer-side fix for the stop≥entry
  defect. Start with `scanner.py` + `pattern_imminent_alerts.py`.

## Algo-trader re-eval 2026-05-16 (Cowork)

Cowork did a full algo-trader hat re-eval after Phase 3 stop-bleed
shipped. Key findings (probes captured in chat):

**The no_pattern bleed is throttling.** Last-7d shows 167 attributed
trades vs only 3 no_pattern trades ($0 PnL). The D6 validator + the
family-backfill activation on 2026-05-16 are doing what they should.
The bleed has migrated, not stopped: 7d attributed PnL is −$74.

**The composite quality score is inversely correlated with realized
PnL.** Diagnostic #1 (run 2026-05-16):

- Spearman(score, total_pnl) = **−0.757** at p = 0.0044 (n=12)
- Top-half by composite: total PnL −$118.63
- Bottom-half by composite: total PnL +$597.80
- Pattern 585 (only proven alpha, +$554 over 85 trades) sits rank 10/12

Mechanism: DSR pegged at 1.0 for all 12 scored patterns, PBO pegged at
0.0 for all 12 — DSR + PBO contribute **0.35 as a dead constant** to
every score. Of the remaining discriminating weight, CPCV Sharpe
(in-sample overfit) dominates at 0.30/0.65 = 46%.

**Target distance is fine.** Diagnostic #2 (run 2026-05-16): clean-exit
target-hit-rate is 9.8% (n=51), not the 1.3% the all-trades figure
suggested. 83% of trades exit via opaque paths (NULL exit_reason 33% +
reconciler-driven 50%); the system is losing decisional content about
"did our hypothesis pay off?" before the brain can learn from it. But
the trades that DO get clean exits are reaching stops, not getting
mis-targeted.

**Sequence chosen.** Composite-reweight (this brief, NEXT_TASK) ships
first because:
1. It's the active landmine — flipping `CHILI_COHORT_PROMOTE_ENABLED`
   today would promote losers and dilute 585.
2. It's the highest-leverage change with the smallest blast radius
   (formula + flag + one-shot mig; no autotrader/broker touched).
3. Position-identity Phase 2 (next after composite-reweight) addresses
   the NULL-exit-reason observability problem at its root.

**What's working, don't break:** pattern 585 alpha (statistically sound
at sign-test p ≈ 0.003), promotion-pipeline rebalance Phases 1–4
infrastructure, the May 1/May 7 connection-hygiene work, Phase 3
stop-bleed deploys.

**Strategic-debt items flagged but not in flight:** ATR-multiple stops
(structural premature-stop driver), Coinbase 120bps round-trip without
maker routing (Phase 7 live-flip blocker), HMM regime classifier
yfinance-blocked (regime conditioning blocked), monthly DD breaker
data-starved (~mid-June arms organically per Path A).

## Algo-trader re-eval 2026-05-18 (architect/data-scientist audit + Tier A ship)

The 2026-05-18 architect/data-scientist pass identified that the demote
gate uses WR alone, which systematically punishes skew-driven edges.
Pattern 585 (only proven alpha: CPCV 1.41, deflated 1.0, PBO 0.0,
+\$547 over 86 trades, WR 35%, payoff ratio 4.97:1) had been demoted
to `decayed`. Without 585 the 90d cumulative was −\$1,718; the
"decayed" tier was outperforming the "promoted" tier by an order of
magnitude. The dominant pain point was the evaluation function, not
the trading. Tier A surgical fixes shipped same session.

**Tier A shipped (commit `23bde18`):**

* Mig 245 — pattern 585 restored to `pilot_promoted` (safety-belted,
  idempotent).
* Mig 246 — `scan_patterns.{avg_winner_pct, avg_loser_pct,
  payoff_ratio, payoff_ratio_n, payoff_ratio_updated_at}` added +
  backfilled. Refreshed nightly by `realized_stats_sync`.
* Payoff-ratio gate in `_matches_thin_evidence_criteria` AND
  `run_live_pattern_depromotion` — symmetric protection across both
  demote paths. Default: `payoff_ratio >= 1.5 AND n >= 5` short-
  circuits demote regardless of WR.
* Composite-score n≥5 floor — `chili_composite_min_realized_trades`
  (default 5) makes the composite NULL when realized n is below
  floor. Cohort-promote landmine closed structurally.
* 25/25 tests pass; live DB verified post-deploy.

**Hidden alpha surfaced — decision made same session.** Pid 537
("Falling Wedge Breakout + Trend Reclaim") had realized 29.6:1 payoff
ratio over 7 trades (+\$86 90d). Operator chose **Path A** (promote
now); mig 247 + commit `2e61287` flipped it to `pilot_promoted`.
Data-scientist caveats recorded: effective sample is ~3 distinct
ideas (ACHC, PFSI, WDCX) over 10-day window; CPCV Sharpe 0.626 is
BELOW the brain's 1.0 floor; promotion was operator override of
the brain's own 2026-05-16 demote. Watch list at n=15; if WR drops
below 50% or payoff_ratio below 3, re-demote. Bonus same-session
finding: pattern 585 auto-elevated `pilot_promoted` → `promoted`
between probes, confirming the Tier A unblock works end-to-end.

**Tier B (next priority, queued):**

* `f-position-identity-phase-2-execution-events-position-id-backfill`
  — closes the 80% opaque-exits gap at its root. Already queued.
* `f-tca-writer-wiring` — slippage is currently invisible (zero TCA
  rows in last 90d). Newly queued 2026-05-18.

**Tier C (strategic, queued):**

* `f-pattern-537-evaluation` — second-alpha promotion decision.
* `f-composite-reweight-no-renormalize` — softer cap-at-0.65
  alternative to Tier A #3, partially superseded; operator decides if
  still useful.
* Momentum-continuation family demote (391 patterns of one family, 0
  in live ladder, persistent bleed). No brief yet.
* Coinbase maker-only routing — required before Phase 7 live-flip.
  Already in memory as `f-fastpath-maker-only`.

**What's working, don't break (updated):** pattern 585 alpha (now
restored AND protected), promotion-pipeline rebalance Phases 1–4
infrastructure, the May 1/May 7 connection-hygiene work, Phase 3
stop-bleed deploys, Tier A payoff-ratio gate (just shipped).
