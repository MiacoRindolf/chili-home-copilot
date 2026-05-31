# Current Plan: Position Identity Refactor

**Initiative owner:** Cowork (strategy) + Claude Code (execution).
**Last update:** 2026-05-30, after Phase 5Z-B converted stop-position reads.

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
- **Phase 4 flag-flip PROMOTED 2026-05-19.** `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=true` in `.env`; broker-sync-worker force-recreated cleanly. Soak window: zero tracebacks, zero `[phase4_*]` re-opens (RH session is currently dead so the branch hasn't been exercised — code + data verified end-to-end). Position-identity refactor is **operationally complete through Phase 4**. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-19_f-position-identity-phase-4-flag-flip-paper-soak.md`.
- **Phase 5A SHIPPED 2026-05-20.** Migs 256-258. Additive bridge only: new immutable `trading_decisions`, nullable `trading_trades.decision_id` + `trading_trades.position_id`, historical backfill, future-insert trigger, residual race-window backfill, and `trading_phase5a_envelope_parity`. Live parity: 602/602 valid trade rows have decisions, all open broker trades have position links, orphan decisions = 0. The 67 missing-decision rows are corrupt legacy dust rows with entry_price<=0 or quantity<=0 and are intentionally skipped. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-20_f-position-identity-phase-5a-decision-bridge.md`.
- **Phase 5B SHIPPED 2026-05-22.** Migs 264-265. Read-only semantic layer: `trading_management_envelopes` compatibility view over `trading_trades`, `trading_phase5b_decision_envelope_position`, `trading_phase5b_pattern_decision_performance`, and helper module `app/services/trading/management_envelopes.py`. Mig 265 separates hard live linkage issues from closed historical broker-envelope debt. Live verification: 506 linked decisions, 106 historical broker envelopes without position links, 0 hard live linkage issues. No live trading behavior changed and no physical rename happened. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-21_f-position-identity-phase-5b-read-models.md`.
- **Phase 5B Coinbase linkage repair SHIPPED 2026-05-26.** Mig 273 backfilled open Coinbase `trading_trades.position_id` from `trading_positions.current_envelope_id`, and `coinbase_service._ensure_coinbase_position_identity` now writes both sides going forward. Live verification after 23 fresh envelopes + 17 fresh closes: 525 linked, 110 historical closed broker-envelope debt, **0 hard linkage issues**, **0 open broker trades missing position**, 0 orphan decisions. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-26_f-position-identity-phase-5b-coinbase-linkage-repair.md`.
- **Phase 5C reporting-reader adoption SHIPPED 2026-05-26.** Mig 274 appends `decision_scan_pattern_id` and `envelope_scan_pattern_id` to `trading_phase5b_decision_envelope_position`, and `/api/trading/attribution/live-vs-research?phase5b_compare=true` now reports legacy envelope-pattern attribution beside Phase 5B decision-pattern attribution. Live 30d compare: 320 closed envelopes, 25 envelope groups, 25 decision groups, 4 mismatched closed envelopes across 3 envelope patterns, all caused by `decision_scan_pattern_id=NULL` while the envelope pattern is populated; mismatch net PnL $21.2641 and absolute group drift $42.5282. No writer or live trading behavior changed. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-26_f-position-identity-phase-5c-reporting-reader-adoption.md`.
- **Phase 5D decision-pattern attribution repair SHIPPED 2026-05-26.** Mig 275 backfilled only NULL `trading_decisions.scan_pattern_id` from the linked envelope's non-null `scan_pattern_id`, appending a provenance marker to `trading_decisions.notes` and never overwriting existing decision attribution. Live: 5 decisions repaired total (IDs 604, 613, 623, 624, 625); the 30d Phase 5C compare now reports 320 closed envelopes, 25 envelope groups, 25 decision groups, **0 mismatched closed envelopes**, and **$0.0000 attribution drift**. No writer or live trading behavior changed. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-26_f-position-identity-phase-5d-decision-pattern-attribution-repair.md`.
- **Phase 5E soak COMPLETE 2026-05-28.** The daily soak watcher and a manual re-run both emitted `READY_FOR_RENAME_BRIEF`. Fresh post-mig-275 data is represented in the read model: 3 fresh decisions, 3 fresh envelopes, and 7 fresh closes. Hard linkage issues = 0, fresh close mismatches = 0, 30d mismatched rows = 0, and attribution drift = $0.0000. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5e-soak-closeout.md`.
- **Phase 5F rename audit SHIPPED 2026-05-28.** Read-only audit script `scripts/d-phase5f-rename-audit.py` counted 35 runtime files with literal `trading_trades` references and 101 runtime files with `Trade` ORM-symbol references. Architect call: the data-science/read-model gate is green, but the production physical rename should be compatibility-first and dry-run before live. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5f-rename-audit.md`.
- **Phase 5G dry-run SHIPPED 2026-05-28.** New script `scripts/d-phase5g-rename-dry-run.py` ran the physical rename shape transactionally on `chili_test`, then rolled it back. Results: old SQL insert through `trading_trades` compatibility view = true, new SQL insert through `trading_management_envelopes` base table = true, SQLAlchemy `Trade` flush through compatibility view = true, Phase 5B view survived, hard linkage unchanged, rollback restored `trading_trades` as table and `trading_management_envelopes` as view. `STAGING_DATABASE_URL` is not configured locally, so production-shaped staging rehearsal did not run. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5g-physical-rename-dry-run.md`.
- **Phase 5H production physical rename SHIPPED 2026-05-28.** Mig 283. `trading_management_envelopes` is now the physical base table (`relkind='r'`) and `trading_trades` is now the legacy compatibility view (`relkind='v'`). Live smoke: old SQL via `trading_trades`, new SQL via `trading_management_envelopes`, and SQLAlchemy `Trade` flush all worked inside a rollback transaction; Phase 5A insert trigger created/linked 3/3 decisions; row count stayed 705 before/after rollback; Phase 5E compare stayed clean (`READY_FOR_RENAME_BRIEF`, 0 linkage issues, 0 attribution drift). Test harness hardened so full pytest cleanup truncates the physical table after the rename. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5h-production-physical-rename.md`.
- **Phase 5I post-rename soak SHIPPED 2026-05-30.** The watcher reached `COMPLETE_POSITIVE`: 20 fresh decisions, 20 fresh envelopes, 10 fresh closes, 0 fresh close mismatches, 0 hard linkage issues, 0 30d attribution mismatches, and 0 rename-path schema log hits. Mig 288 (`position_identity_phase5i_pattern_sync`) repaired the only drift found during soak: decisions created before Coinbase/sync paths finalized envelope `scan_pattern_id`. The physical rename remains healthy: `trading_management_envelopes` is the base table and `trading_trades` remains the compatibility view. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5i-post-rename-soak-closeout.md`.
- **Phase 5J selective reader cleanup slice 1 SHIPPED 2026-05-30.** Converted the brain health KPI/manual-book reader, management-envelope health helper, and three read-only probes (`d-cb-phase6-soak-probe`, `d-maker-only-tca-probe`, `d-imminent-silence-audit`) from `trading_trades` to `trading_management_envelopes`. No live writer/order/broker path changed. Verification: Phase 5J guard tests passed, direct brain KPI smoke returned `ok=True`, and Phase 5I remained `COMPLETE_POSITIVE` with `LOG_SCHEMA_ERRORS=0`. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-1.md`.
- **Phase 5J selective reader cleanup slice 2 SHIPPED 2026-05-30.** Converted `decision_packet_coverage.py` and `divergence_service.py` read-only analytics joins from `trading_trades` to `trading_management_envelopes`. No live writer/order/broker path changed. Verification: Phase 5J guard tests passed, Phase 5I remained `COMPLETE_POSITIVE`, and the scheduled wrapper reported `LOG_SCHEMA_ERRORS=0`. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-2.md`.
- **Phase 5J selective reader cleanup slice 3 SHIPPED 2026-05-30.** Converted `dynamic_priors.py`, `ticker_scope_autotune.py`, and `pattern_stats_recompute.py` learning/reporting source reads from `trading_trades` to `trading_management_envelopes`. No live writer/order/broker path changed. Verification: Phase 5J guard tests passed, Phase 5I remained `COMPLETE_POSITIVE`, scheduled wrapper reported `LOG_SCHEMA_ERRORS=0`, and live dynamic-prior/ticker-autotune reader smokes succeeded. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-3.md`.
- **Phase 5J selective reader cleanup slice 4 SHIPPED 2026-05-30.** Converted `realized_stats_sync.py` and `hrp_sizing.py` reader/source queries from `trading_trades` to `trading_management_envelopes`. `net_edge_ranker.py` was skipped because it already had unrelated local edits. Verification: Phase 5J guard tests passed, Phase 5I remained `COMPLETE_POSITIVE`, scheduled wrapper reported `LOG_SCHEMA_ERRORS=0`, HRP reader smoke returned a list, and realized-stats dry-run returned `updated=40`, `skipped=0`. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-4.md`.
- **Phase 5J selective reader cleanup slice 5 SHIPPED 2026-05-30.** Converted `admin.py`, `trading_sub/ai.py`, `brain_work/handlers/quality_score.py`, `d-pid537-watcher.py`, and `walkforward_monthly_dd_breaker.py` reader queries from `trading_trades` to `trading_management_envelopes`. No live writer/order/broker path changed. Verification: Phase 5J guard tests passed, Phase 5I remained `COMPLETE_POSITIVE`, scheduled wrapper reported `LOG_SCHEMA_ERRORS=0`, and live smoke queries succeeded. Bonus: pid 537 watcher now reports `COMPLETE_POSITIVE` with n=17, WR=0.6471, payoff=13.0411, stage=`promoted`; close/retarget separately. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-5.md`.
- **Pid 537 watcher CLOSED 2026-05-30.** The special post-Path-A watcher reached `COMPLETE_POSITIVE` (`n=17`, WR=0.6471, payoff=13.0411) and pid 537 is already `promoted`. Local scheduled task `CHILI-pid537-watcher` was disabled to stop redundant prompts. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-pid537-watcher-closeout.md`.
- **Phase 5J remaining-reference audit CLOSED 2026-05-30.** Remaining literal `trading_trades` references are now classified as compatibility contracts, migration/test/history, live writer/order/broker/reconcile paths, live capital/promotion readers, or dirty local candidates. No additional reader-only conversion is safe without moving into live-system contract work. Phase 5I remains `COMPLETE_POSITIVE` with `LOG_SCHEMA_ERRORS=0`. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-remaining-reference-audit-closeout.md`.
- **Phase 5K live-path cutover brief SHIPPED 2026-05-30.** Recommendation: do not cut live paths over yet. Remaining references are live capital, promotion, broker, order, stop, reconcile, repair, compatibility, or dirty-file surfaces. Next implementation should be a read-only old-vs-new live-path parity probe, not a behavior change. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-live-path-cutover-brief.md`.
- **Phase 5K-A live-path parity probe SHIPPED 2026-05-30.** Added `scripts/d-phase5k-live-path-parity-probe.py` and focused tests. Live result: `COMPLETE_POSITIVE`, six old-vs-new aggregate checks matched (Coinbase cap, PDT, promotion realized, pattern quality, portfolio risk, position integrity), `PARITY_MISMATCHES=0`. Phase 5I still `COMPLETE_POSITIVE` and wrapper reports `LOG_SCHEMA_ERRORS=0`. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-a-live-path-parity-probe.md`.
- **Phase 5K-B Coinbase cap reader flag SHIPPED 2026-05-30.** `cost_aware_gate.py` now has a default-OFF `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES` hook. OFF reads `trading_trades`; ON reads `trading_management_envelopes`; filters and conservative failure behavior are unchanged. Flag was not flipped and no service was restarted. Focused tests passed; full DB-backed cost-aware file hit a pre-body test DB deadlock in the shared truncate fixture. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-b-coinbase-cap-reader-flag.md`.
- **Phase 5K-C Coinbase cap flag-soak PROMOTED 2026-05-30.** The initial flag attempts exposed runtime instability, not a Phase 5K code defect: the `CHILI-live-runtime-watchdog` task was registered against a stale worktree root and project-autonomy/Codex pytest jobs were colliding with the live DB as soon as Postgres recovered. Recovery actions: disabled the stale watchdog during Postgres fsync, waited for Postgres to become healthy, disabled `PROJECT_AUTONOMY_AGENT_SCHEDULER_ENABLED`, stopped stale pytest jobs, flipped `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true`, recreated only `autotrader-worker`, and re-registered the watchdog from `D:\dev\chili-home-copilot`. Post-flip validation: Phase 5K-A `COMPLETE_POSITIVE` (`PARITY_MISMATCHES=0`), Phase 5I `COMPLETE_POSITIVE`, autotrader sees the flag `true`, and the watchdog reports `runtime_ok=true`, `services_wrong_worktree=[]`, `action=noop`. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-c-coinbase-cap-flag-soak-attempt.md`.
- **Phase 5K-D PDT reader flag PROMOTED 2026-05-30.** `pdt_guard.py` now has `CHILI_PHASE5K_PDT_USE_ENVELOPES=true` live in `autotrader-worker`. OFF reads the `trading_trades` compatibility view; ON reads the `trading_management_envelopes` base table. Broker-confirmed PDT filters, crypto exclusion, reconcile-artifact exclusion, and true 5-business-day cutoff are unchanged. Verification: 13 focused tests passed, Phase 5K-A remains `COMPLETE_POSITIVE`, direct live PDT counts match (`PDT_COMPAT_COUNT=3`, `PDT_ENVELOPE_COUNT=3`), Phase 5I remains `COMPLETE_POSITIVE`, and the short post-flip log soak showed no PDT query/relation errors. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-d-pdt-reader-flag.md`.
- **Phase 5K-E promotion/pattern-quality reader flags PROMOTED 2026-05-30.** `CHILI_PHASE5K_COHORT_PROMOTE_USE_ENVELOPES=true` and `CHILI_PHASE5K_PATTERN_QUALITY_USE_ENVELOPES=true` are live in the consumer workers. OFF reads the `trading_trades` compatibility view; ON reads `trading_management_envelopes`. Verification: 14 focused tests passed, Phase 5K-A remains `COMPLETE_POSITIVE`, Phase 5I remains `COMPLETE_POSITIVE`, direct pattern-quality realized maps match (`30/30`), direct cohort-promote candidate IDs match (`9/9`), and the short post-flip log soak showed no reader/relation errors. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-e-promotion-pattern-quality-reader-flags.md`.
- **Phase 5K-F portfolio-risk drawdown reader flag PROMOTED 2026-05-30.** `CHILI_PHASE5K_PORTFOLIO_RISK_USE_ENVELOPES=true` is live in `chili`, `autotrader-worker`, `scheduler-worker`, and `broker-sync-worker`. OFF reads `trading_trades`; ON reads `trading_management_envelopes`; filters, formulas, K-sigma thresholds, attribution scope, and breaker semantics are unchanged. Verification: 15 focused tests passed, Phase 5K-A remains `COMPLETE_POSITIVE`, Phase 5I remains `COMPLETE_POSITIVE`, direct old/new drawdown helper comparisons match exactly for global account and user 1, and the post-flip log soak showed no portfolio-risk/relation errors. The earlier brief called this "open exposure"; implementation deliberately targets the concrete raw SQL surface in this module. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-f-portfolio-risk-reader-flag.md`.
- **Phase 5K-G position-integrity reader flag PROMOTED 2026-05-30.** `CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=true` is live in `chili`, `broker-sync-worker`, and `autotrader-worker`. OFF reads `trading_trades`; ON reads `trading_management_envelopes`; report/repair predicates and verdict semantics are unchanged. Verification: 13 focused tests passed, Phase 5K-A remains `COMPLETE_POSITIVE`, Phase 5I remains `COMPLETE_POSITIVE`, direct old/new position-integrity audit and dry-run repair checks match exactly, the live sidecar cleanup path ran with `position sidecars closed=0`, and the post-flip log soak showed no position-integrity/relation errors. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-g-position-integrity-reader-flag.md`.
- **Phase 5K-H alpha-portfolio gate reader flag PROMOTED 2026-05-30.** `CHILI_PHASE5K_ALPHA_PORTFOLIO_GATE_USE_ENVELOPES=true` is live in `chili`, `scheduler-worker`, and `brain-work-dispatcher`. OFF reads `trading_trades`; ON reads `trading_management_envelopes`; scoring math, recert rules, lifecycle staging behavior, and write paths are unchanged. Verification: 5 focused tests passed, Phase 5K-A remains `COMPLETE_POSITIVE`, Phase 5I remains `COMPLETE_POSITIVE`, direct old/new gate-reader rows match exactly (`446/446`), and the scheduled alpha portfolio maintenance job completed successfully after the flip (`updates_written=446`, `audit_rows_written=5`, no relation/query errors). CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-h-alpha-portfolio-gate-reader-flag.md`.
- **Phase 5K-I live-path closeout audit CLOSED 2026-05-30.** `trading_management_envelopes` is confirmed as the physical table and `trading_trades` as the compatibility view. Fresh Phase 5K-A and Phase 5I probes are both `COMPLETE_POSITIVE`; extra old-vs-new checks for attribution aggregates, TCA usable-sample counts, autotrader open-by-lane counts, probation counts, and bracket open-reconcile summaries all matched. Architect verdict: Phase 5K is complete; remaining references are compatibility contracts, writer/order/broker/reconcile paths, trade-id semantic readers, reporting candidates, or migration/test/history. Next phase is Phase 5L contract hardening, not another blind table-name cutover. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5k-i-live-path-closeout-audit.md`.
- **Phase 5L-A contract-reader slice SHIPPED 2026-05-30.** Added shared management-envelope relation constants and converted two low-risk readers from the legacy compatibility name to the semantic base-table name: attribution closed-pattern live stats and Coinbase TCA usable-sample backing counts. No broker/order/reconcile writer path changed. Verification: targeted canaries passed, py_compile passed, Phase 5K-A remains `COMPLETE_POSITIVE`, Phase 5I remains `COMPLETE_POSITIVE`, live attribution/TCA smokes succeeded, and `chili` + `autotrader-worker` restarted cleanly. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5l-a-contract-reader-slice.md`.
- **Phase 5L-B reader allowlist canary SHIPPED 2026-05-30.** Added a runtime-app canary that fails when new raw live-reader SQL uses `FROM trading_trades` or `JOIN trading_trades` outside the exact known compatibility lines. Also preserved remote commit `2b0db55` (remaining trade-surface classifier) via merge `3ce746c`. Verification: reader allowlist + classifier tests passed (`4 passed`), Phase 5K-A remains `COMPLETE_POSITIVE`, and Phase 5I remains `COMPLETE_POSITIVE`. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5l-b-reader-allowlist-canary.md`.
- **Phase 5L-C evidence-reader slice 2 SHIPPED 2026-05-30.** Converted `crypto/pattern_miner.py` and `options/portfolio_budget.py` reader SQL to `MANAGEMENT_ENVELOPES_RELATION`, reduced the Phase 5L allowlist, and fixed boolean option greeks so `True`/`False` are invalid numeric greeks. Verification: focused reader/options tests passed (`22 passed`) in the current worktree and a clean temporary checkout, Phase 5K-A remains `COMPLETE_POSITIVE`, Phase 5I remains `COMPLETE_POSITIVE`, live reader smokes succeeded, and `chili`, `autotrader-worker`, and `brain-work-dispatcher` restarted cleanly. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5l-c-evidence-reader-slice-2.md`.
- **Phase 5L-D dirty evidence-reader slice SHIPPED 2026-05-30.** Converted `pattern_regime_ledger.py` and `pattern_survival/features.py` reader SQL to `MANAGEMENT_ENVELOPES_RELATION` while using isolated staging so unrelated dirty local edits in those files were not absorbed. Verification: Phase 5L reader allowlist passed (`1 passed`) in current worktree and clean checkout, py_compile passed, Phase 5K-A remains `COMPLETE_POSITIVE`, Phase 5I remains `COMPLETE_POSITIVE`, and `chili`, `scheduler-worker`, and `brain-work-dispatcher` restarted cleanly. Architect verdict: safe reader-reduction is now done; remaining references need semantic contracts, not mechanical table-name cleanup. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5l-d-dirty-evidence-reader-slice.md`.
- **Phase 5L-E autotrader semantic-reader contracts SHIPPED 2026-05-30.** Added management-envelope helper APIs for autotrader open-by-lane exposure counts, synergy retry candidate lookup, and probation recert daily quota counts, then routed the autotrader callers through those helpers. Verification: focused rule-gate/canary tests passed (`2 passed`), synergy retry test passed, probation quota test passed, py_compile passed, Phase 5K-A remains `COMPLETE_POSITIVE`, Phase 5I remains `COMPLETE_POSITIVE`, live open-lane smoke returned `{'equity': 1, 'crypto': 4, 'options': 0}`, and `autotrader-worker` restarted cleanly. The Phase 5L canary now only allows bracket reconciliation and Coinbase orphan-adoption raw reader lines. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5l-e-autotrader-semantic-reader-contracts.md`.
- **Phase 5L-F bracket/orphan semantic-reader contracts SHIPPED 2026-05-30.** Added management-envelope helper APIs for bracket reconciliation scope, stale missing-stop watchdog candidates, and Coinbase orphan-stop adoption candidates, then routed the live callers through those helpers. The Phase 5L raw-reader allowlist is now empty for runtime app code: new `FROM trading_trades` / `JOIN trading_trades` live-reader SQL fails the canary. Verification from a clean worktree at `ee2ae94`: py_compile passed, reader allowlist + new bracket/orphan helper contract tests passed (`7 passed`), Phase 5K-A remains `COMPLETE_POSITIVE`, and Phase 5I remains `COMPLETE_POSITIVE`. The legacy `trading_trades` compatibility view intentionally remains for writer/ORM/migration/test contracts. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5l-f-bracket-orphan-semantic-readers.md`.
- **Phase 5L-G compatibility-contract audit SHIPPED 2026-05-30.** Hardened `scripts/analyze_phase5_remaining_trade_refs.py` so the remaining surface is explicit: unexpected runtime raw readers/mutations, allowed compatibility-view writer/update paths, relation-symbol contracts, legacy ORM `Trade` symbols, tests/migrations/probes/history, and docs/runbooks. Clean-worktree audit at `a7ecd6c`: `OK=True`, 497 files with trade-surface references, 113 raw SQL files, 4 allowed writer/update paths, 17 relation-symbol contracts, 97 ORM symbol contracts, 203 compatibility/test/history files, 176 docs/runbooks, **0 unexpected runtime readers**, **0 unexpected mutations**, **0 unclassified**. Verification: classifier/canary/alert-refresh tests passed (`18 passed`), py_compile passed, Phase 5K-A remains `COMPLETE_POSITIVE`, and Phase 5I remains `COMPLETE_POSITIVE`. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5l-g-compat-contract-audit.md`.
- **Phase 5L-H relation-symbol contracts SHIPPED 2026-05-30.** Reduced runtime app-side `trading_trades` relation-symbol contracts from 17 to 2 intentional anchors (`app/models/trading.py` and `management_envelopes.py`) while preserving compatibility writers and live broker/order/close semantics. Also declared the already-migrated Project Autonomy Agent OS ORM models so clean monitor/API imports pass. Clean-worktree verification: py_compile passed, monitor/regime/classifier/canary tests passed (`29 passed`), classifier reports **0 unexpected runtime readers**, **0 unexpected runtime mutations**, **0 unclassified**, Phase 5K-A remains `COMPLETE_POSITIVE`, and Phase 5I remains `COMPLETE_POSITIVE`. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5l-h-relation-symbol-contracts.md`.
- **Phase 5M ORM-symbol contract audit SHIPPED 2026-05-30.** Extended the Phase 5 classifier with a focused `--bucket` audit mode and classified the remaining runtime `Trade` ORM-symbol surface. Result: 105 runtime ORM-symbol files, 0 unexpected raw readers, 0 unexpected mutations, 0 unclassified. Architect call: do not one-shot rename the ORM class yet; the remaining risk is semantic coupling across broker/order/reconcile, risk/sizing, API/UI, analytics, and helper surfaces. Next slice should reduce low-risk analytics/reporting imports through semantic management-envelope helpers while leaving live broker/order/reconcile paths unchanged. Verification: Phase 5 classifier/allowlist tests passed (`9 passed`) and the focused bucket audit exited cleanly. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5m-orm-symbol-contract-audit.md`.
- **Phase 5N semantic-envelope helper slice 1 SHIPPED 2026-05-30.** Added closed-envelope helper contracts and converted daily playbook recent-performance plus execution-quality/implementation-shortfall reports off direct `Trade` ORM reads. No broker/order/close/reconcile or capital-gate path changed. ORM-symbol compatibility count dropped 105 -> 103, with 0 unexpected raw readers, 0 unexpected mutations, and 0 unclassified entries. Verification: py_compile passed for the touched services; helper/execution-quality/classifier/allowlist tests passed (`16 passed`). CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5n-semantic-envelope-helper-slice.md`.
- **Phase 5O attribution-review helper slice SHIPPED 2026-05-30.** Added closed-pattern and closed-review envelope helper contracts, then converted `performance_attribution.attribute_pattern_trades` and `attribution_service.post_trade_review` off direct `Trade` ORM reads. No live broker/order/close/reconcile/capital-gate path changed. ORM-symbol compatibility count dropped 103 -> 101, with 0 unexpected raw readers, 0 unexpected mutations, and 0 unclassified entries. Verification: py_compile passed for touched attribution/helper files; helper/attribution/performance/classifier/allowlist tests passed (`26 passed`). CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5o-attribution-review-helper-slice.md`.
- **Phase 5P context/report helper slice SHIPPED 2026-05-30.** Added a recent ticker management-envelope helper, converted `ai_context.build_ai_context` off direct `Trade` ORM reads, and removed type-only `Trade` imports from journal and close-attribution report helpers. No live broker/order/close/reconcile/PDT/capital-gate path changed. ORM-symbol compatibility count dropped 101 -> 98, with 0 unexpected raw readers, 0 unexpected mutations, and 0 unclassified entries. Verification: py_compile passed for touched context/helper files; management-envelope/context/classifier/allowlist tests passed (`21 passed`). CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5p-context-report-helper-slice.md`.
- **Phase 5Q report/type cleanup slice SHIPPED 2026-05-30.** Removed the legacy `Trade` ORM symbol from execution feedback hook annotations and cleaned two brain-work close-event text surfaces that were being counted as compatibility symbols. No runtime query or live broker/order/close/reconcile/PDT/capital-gate behavior changed. ORM-symbol compatibility count dropped 98 -> 95, with 0 unexpected raw readers, 0 unexpected mutations, and 0 unclassified entries. Verification: py_compile passed for touched brain-work files; type-cleanup/emitter-coverage/classifier/allowlist tests passed (`20 passed`). CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5q-report-symbol-type-cleanup.md`.
- **Phase 5R router/schema contract audit SHIPPED 2026-05-30.** Classified remaining router/schema/UI `Trade` terminology into public contracts, private helper internals, live/API parity-gated surfaces, and user-facing product copy. No behavior changed. Architect verdict: do not public-rename yet; next safe slice is `ai.py` private pattern-evidence helper conversion while preserving response key `trades`. Focused analyzer remains `orm_trade_symbol_compat | 95`, with 0 unexpected raw readers, 0 unexpected mutations, and 0 unclassified entries. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5r-router-schema-contract-audit.md`.
- **Phase 5S private router helper slice SHIPPED 2026-05-30.** Added `load_pattern_tagged_envelope_rows(...)` and converted `ai.py::_api_pattern_evidence_response(...)` off direct `Trade` ORM reads while preserving public response key `trades` and row shape. No public API field names and no live broker/order/close/reconcile/PDT/capital-gate path changed. ORM-symbol compatibility count dropped 95 -> 94, with 0 unexpected raw readers, 0 unexpected mutations, and 0 unclassified entries. Verification: py_compile passed for touched helper/router files; management-envelope/private-router/classifier/allowlist tests passed (`19 passed`). CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5s-private-router-helper-slice.md`.
- **Phase 5T audit export helper slice SHIPPED 2026-05-30.** Added `load_audit_export_envelope_rows(...)` and converted `trades.py::api_audit_export(...)` off direct `Trade` ORM reads for the audit export trade section while preserving the public `trades` response key, CSV `# TRADES` section label, and field/header order. No public API field names and no live broker/order/close/reconcile/PDT/capital-gate path changed. ORM-symbol compatibility count remains 94 because `trades.py` still owns public `/trades` live paths; raw reader bucket remains 0. Verification: py_compile passed for touched helper/router files; management-envelope/audit-export/classifier/allowlist tests passed (`21 passed`). CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5t-audit-export-envelope-helper-slice.md`.
- **Phase 5U router/monitor contract audit CLOSED 2026-05-30.** Audited remaining router/schema/UI `Trade` ORM-symbol surfaces after Phase 5T. Verdict: no public rename and no one-shot ORM class rename. Remaining router-facing uses are public compatibility contracts (`/trades`, `trade_id`, schema names, UI labels), live behavior contracts (sell/close/monitor-run/stop surfaces), or parity-gated read-only candidates. Focused analyzer remains `orm_trade_symbol_compat | 94`, with raw reader bucket 0. Next is a read-only Phase 5V monitor-read parity probe before any conversion. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5u-router-monitor-contract-audit.md`.
- **Phase 5V monitor-read parity probe SHIPPED 2026-05-30.** Added `scripts/d-phase5v-monitor-read-parity-probe.py`, a read-only old-vs-new parity gate for monitor decisions and imminent-alert actioned exclusion. Live result: `COMPLETE_POSITIVE`, 20 checks matched, `PARITY_MISMATCHES=0`, relation kinds healthy (`trading_management_envelopes='r'`, `trading_trades='v'`). Stop-decision parity remains optional behind `PHASE5V_INCLUDE_STOP_DECISIONS=1` because the old compatibility-view stop-decision join exceeds the read-only gate timeout in live data. Verification: py_compile passed; Phase 5V/classifier/allowlist tests passed (`14 passed`); focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5v-monitor-read-parity-probe.md`.
- **Phase 5W monitor-read helper conversion SHIPPED 2026-05-30.** Converted the two Phase 5V parity-proven monitor read surfaces (`api_monitor_decisions(...)` and `api_monitor_imminent_alerts(...)`) to management-envelope helpers while preserving public response keys and payload shape. No active setup card, `api_monitor_run`, sell/close, stop execution/rendering, broker/order/reconcile/PDT/capital-gate, `/trades`, `trade_id`, schema, or UI-label behavior changed. Verification: py_compile passed; management-envelope/Phase5W/Phase5V/classifier/allowlist tests passed (`28 passed`); Phase 5V live probe remained `COMPLETE_POSITIVE` with 20 checks matched and 0 mismatches. Focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5w-monitor-read-helper-conversion.md`.
- **Phase 5X stop-decision read helper conversion SHIPPED 2026-05-30.** Added `load_stop_decision_envelope_rows(...)` and converted `/api/trading/stops/decisions` off the direct `StopDecision` + `Trade` ORM join while preserving response fields (`id`, `trade_id`, `as_of_ts`, `state`, `old_stop`, `new_stop`, `trigger`, `reason`, `executed`). No stop execution, stop-position rendering, monitor-run, sell/close, broker/order/reconcile/PDT/capital-gate, `/trades`, schema, or UI-label behavior changed. Live profiling caught and fixed a planner trap: `NULLS LAST` defeated the `(trade_id, as_of_ts DESC)` index; final plain-`DESC` helper reads 50 all-trade rows in ~468 ms cold / ~11 ms warm and single-trade rows in ~2 ms. Verification: py_compile passed; management-envelope/Phase5X/classifier/allowlist tests passed (`24 passed`); focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5x-stop-decision-read-helper-conversion.md`.
- **Phase 5Y stop-position contract audit CLOSED 2026-05-30.** Audited `/api/trading/stops/positions` and decided not to convert it directly yet. The endpoint is a risk-display surface, not a passive read: it combines open envelope fields with broker-stale filtering, broker-position truth overlays, option detection, broker/market quotes, stop-engine brain context, and UI state computation. A direct dict-row conversion would introduce an unproven runtime object contract across helpers still typed/tested around `Trade`-like objects. No behavior changed. Focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. Next: build a read-only runtime-envelope adapter parity probe before any endpoint swap. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5y-stop-position-contract-audit.md`.
- **Phase 5Z-A stop-position runtime-adapter probe SHIPPED 2026-05-30.** Added `scripts/d-phase5z-stop-position-runtime-adapter-probe.py`, a read-only old-vs-new parity probe comparing current `Trade` ORM stop-position serialization with a candidate runtime object built from `trading_management_envelopes` rows. Live result: `COMPLETE_POSITIVE`, matched=true, 5 old positions, 5 new positions, 0 suppressed-stale drift, 10 cached quote entries, relation kinds healthy (`trading_management_envelopes='r'`, `trading_trades='v'`). Verification: py_compile passed and Phase5Z/classifier/allowlist tests passed (`13 passed`). Next: Phase 5Z-B narrow endpoint conversion using the proven runtime-envelope object. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5z-stop-position-runtime-adapter-probe.md`.
- **Phase 5Z-B stop-position endpoint conversion SHIPPED 2026-05-30.** Added `load_open_stop_position_envelope_objects(...)` and converted `/api/trading/stops/positions` to load open runtime objects from `trading_management_envelopes`. The serializer and live helper chain remain unchanged: broker-stale filtering, broker-position truth overlays, option detection, broker/market quote routing, stop-engine brain context, and UI state computation all still run. Public payload fields are preserved. No stop execution/evaluation/dispatch, sell/close, broker/order/reconcile/PDT/capital-gate, `/trades`, schema-name, or UI-label behavior changed. Verification: py_compile passed; management-envelope/Phase5Z tests passed (`22 passed`); existing stop-position option/broker-truth integration tests passed (`2 passed`); live Phase 5Z probe remains `COMPLETE_POSITIVE`; focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5z-b-stop-position-endpoint-conversion.md`.
- **Phase 5AA-A active-setup runtime-adapter probe SHIPPED 2026-05-30.** Added `scripts/d-phase5aa-active-setup-runtime-adapter-probe.py`, a read-only old-vs-new parity probe comparing current `Trade` ORM active-setup card serialization with candidate runtime objects loaded from the physical `trading_management_envelopes` table. Live result: `COMPLETE_POSITIVE`, matched=true, 5 old setups, 5 new setups, 0 suppressed-stale drift, relation kinds healthy (`trading_management_envelopes='r'`, `trading_trades='v'`). Verification: py_compile passed, focused Phase5AA/classifier/allowlist tests passed (`13 passed`), and focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. Next: Phase 5AA-B narrow active-setup endpoint conversion using the proven runtime-envelope object; monitor-run/sell/close/stop execution/broker/order/reconcile/PDT/capital gates stay untouched. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5aa-active-setup-runtime-adapter-probe.md`.
- **Phase 5AA-B active-setup endpoint conversion SHIPPED 2026-05-30.** Added `load_open_active_setup_envelope_objects(...)` and converted only `api_monitor_active` / `/api/trading/active-setups` display loading to read runtime objects from `trading_management_envelopes`. The active-card serializer and helper chain stayed intact: broker-stale filtering, broker-position truth overlays, option detection, broker/market quote routing, alert/pattern enrichment, monitor decisions, and execution-state metadata. `api_monitor_run` intentionally remains on the old `Trade` helper path because it is a live action surface. No sell/close, stop execution/evaluation/dispatch, broker/order/reconcile/PDT/capital gates, `/trades`, schema names, UI labels, or response fields changed. Verification: py_compile passed, focused active-setup/management-envelope/monitor integration tests passed (`54 passed`), live Phase 5AA probe remains `COMPLETE_POSITIVE` (5 old setups = 5 new setups), focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5aa-b-active-setup-endpoint-conversion.md`.
- **Phase 5AB AutoTrader desk contract audit CLOSED 2026-05-30.** Audited `autotrader_desk.py::list_pattern_linked_open_positions(...)` and did not convert it. The surface mixes live envelope display, broker-stale suppression, broker-truth metrics, option/crypto quote routing, paper-trade rows, per-position overrides, UI control flags, and close capability flags. Safe next step is a read-only runtime-adapter parity probe for the live `trades` list only; paper rows and mutation/control endpoints stay unchanged. Verification: py_compile passed, AutoTrader desk API/classifier/allowlist tests passed (`19 passed`), focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5ab-autotrader-desk-contract-audit.md`.
- **Phase 5AB-A AutoTrader desk runtime-adapter probe SHIPPED 2026-05-30.** Added `scripts/d-phase5ab-autotrader-desk-runtime-adapter-probe.py`, a read-only old-vs-new parity probe for the live AutoTrader desk `trades` list. It compares current `Trade` ORM runtime rows with candidate objects loaded from physical `trading_management_envelopes`, then feeds both through broker-stale filtering, position-identity suppression, broker-truth overlays, option/crypto quote routing, override lookup, control flags, and unrealized-PnL enrichment. Live result: `COMPLETE_POSITIVE`, matched=true, 5 old trades, 5 new trades, 0 suppressed-stale drift, relation kinds healthy (`trading_management_envelopes='r'`, `trading_trades='v'`). Verification: py_compile passed, focused probe/desk/classifier/allowlist tests passed (`25 passed`), focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. Next: narrow live desk `trades` loader conversion; paper rows and close/override mutation endpoints stay unchanged. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5ab-a-autotrader-desk-runtime-adapter-probe.md`.
- **Phase 5AB-B AutoTrader desk live loader conversion SHIPPED 2026-05-30.** Added `load_autotrader_desk_live_envelope_objects(...)` and converted only the live `trades` loader inside `list_pattern_linked_open_positions(...)` to runtime objects from `trading_management_envelopes`. The enrichment loop stayed intact: broker-stale filtering, extra position-identity suppression, broker-truth overlays, option/crypto quote routing, override lookup by `("trade", id)`, `controls_supported` / `close_supported`, and unrealized PnL. Paper rows and all close/override mutation paths stayed unchanged. Verification: py_compile passed, focused loader/probe/desk/management-envelope/classifier/allowlist tests passed on rerun (`45 passed`), live Phase 5AB probe remains `COMPLETE_POSITIVE` (5 old trades = 5 new trades), focused analyzer remains `orm_trade_symbol_compat | 94`, raw reader bucket 0. CC report: `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5ab-b-autotrader-desk-live-loader-conversion.md`.
- **Side queues:** ~~`f-coinbase-exit-side-recording`~~ ~~`f-bracket-fired-stop-recording`~~ ~~`f-coinbase-maker-only-routing`~~ ~~`f-stop-engine-payoff-ratio-gate`~~ ALL SHIPPED 2026-05-19. Updated 2026-05-20: the autotrader ENTRY-sizing path now uses posterior-smoothed payoff sizing instead of raw threshold cliffs (HRP -> survival -> pilot -> payoff -> qty). Pattern 585 is `high` with a smoothed multiplier between 1.25x and 1.5x, not a hard very-high 1.5x cliff. Coinbase maker-only routing + payoff-aware sizing stay behind their existing watchers/flags. NEXT_TASK = `f-runtime-docker-postgres-recovery-then-phase5k-c-retry`. The maker-only soak weekly watcher fires Sundays 18:00 PT.

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
## Position Identity Phase 5AC - Compatibility Boundary Audit (2026-05-30)

Phase 5AC shipped as an audit/tooling slice. No live trading behavior changed.

The remaining trade surface is now explicitly classified rather than treated as
generic cleanup debt. The analyzer still reports zero unexpected runtime raw
readers and zero unexpected runtime mutations. The remaining 94 `Trade` ORM
symbol references are compatibility contracts grouped as:

- learning/research/reporting: 39
- live-action/broker/reconcile: 15
- risk/capital gates: 18
- public UI/schema contracts: 14
- private helper/type-only: 8

Architect verdict: **do not do the full ORM/view rename now**. The useful Phase
5 reader conversion work is done; `trading_trades` remains a deliberate
compatibility view and `Trade` remains a legacy ORM symbol until a separate
alias/facade plan proves safe. The next safe slice is audit-only:
`f-position-identity-phase-5ad-orm-alias-plan`.

Report:
`docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5ac-live-action-boundary-audit.md`.

## Position Identity Phase 5AD - ORM Alias Plan (2026-05-30)

Phase 5AD shipped as a plan/canary slice. No live trading behavior changed.

Decision: keep `Trade` as the legacy compatibility ORM mapper for now. Do not
introduce a separate live `ManagementEnvelope` ORM class and do not broadly
rename `Trade` imports. The semantic helper layer in
`management_envelopes.py` remains the correct boundary for new envelope reads.

Rationale:

- `Trade` is still public route/schema/UI vocabulary.
- `trade_id` remains a stable external/business key.
- live writer, broker, reconciliation, risk, and capital paths still depend on
  the compatibility mapper contract.
- a broad rename would not improve alpha, execution, slippage, or risk today.

Added `tests/test_phase5ad_orm_alias_plan.py` to pin the current relation
contract: physical table `trading_management_envelopes`, compatibility relation
`trading_trades`, and `Trade` mapped to the compatibility relation until a
future compatibility migration says otherwise.

Report:
`docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5ad-orm-alias-plan.md`.

## Position Identity Phase 5AE - Trades API Shadow Canary (2026-05-30)

Phase 5AE shipped as a passive canary slice. No public `/trades` behavior
changed.

Added `load_trades_api_envelope_rows(...)` and a passive shadow compare inside
`api_get_trades(...)`. The route still returns the current `Trade` ORM response;
the canary separately loads stable database-backed fields from the physical
`trading_management_envelopes` table and logs
`[phase5v] /trades envelope shadow mismatch` only if the two shapes drift.
Broker-truth display overlays are intentionally excluded from the comparison by
checking `local_entry_price` and `local_quantity`.

Live/read-only validation is green: the Phase 5AE `/trades` parity probe
reported `COMPLETE_POSITIVE` across all/open/closed rows with 0 mismatches;
Phase 5K-A and Phase 5I remain `COMPLETE_POSITIVE`; classifier output still has
0 unexpected runtime readers, 0 unexpected runtime mutations, and 0
unclassified entries.

Architect verdict: keep observing the shadow canary. Do not cut over `/trades`
or rename public trade vocabulary without a fresh feature-flagged plan and clean
shadow evidence.

Report:
`docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5ae-trades-api-shadow-canary.md`.

## Position Identity Phase 5AF - Trades API Cutover Flag (2026-05-31)

Phase 5AF shipped the reversible `/api/trading/trades` cutover flag without
changing default behavior.

Added typed setting `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES`, default `false`,
plus a management-envelope response renderer for the public `/trades` shape.
When enabled, the route can serve management-envelope rows, but it deliberately
falls back to the compatibility path for `status=open` or any mixed response
containing open rows. That preserves broker-truth overlays and stale-open
suppression until a dedicated open-row runtime adapter probe proves parity.

Verification: py_compile passed; focused route/helper/config/canary tests
passed (`38 passed`); Phase 5AE `/trades` parity remains
`COMPLETE_POSITIVE`; Phase 5K live-path parity remains `COMPLETE_POSITIVE`;
Phase 5I post-rename soak remains `COMPLETE_POSITIVE`; the classifier still
reports raw reader bucket 0.

Architect verdict: safe to merge as a default-off switch. Do not flip broadly
yet. The next useful slice is Phase 5AF soak plus an open-row runtime adapter
probe before full `/trades` route cutover.

Report:
`docs/STRATEGY/CC_REPORTS/2026-05-31_f-position-identity-phase-5af-trades-api-cutover-flag.md`.

## Position Identity Phase 5AG - Trades Open-Row Runtime Adapter Probe (2026-05-31)

Phase 5AG shipped the missing read-only proof for `/api/trading/trades` open
rows.

Added `scripts/d-phase5ag-trades-open-runtime-adapter-probe.py`, which compares
the current `Trade` ORM open-row path with runtime objects loaded from the
physical `trading_management_envelopes` table. Both sides run through the same
broker-truth display overlay and stale-open suppression chain before comparing
the public `/trades` row shape.

Live evidence is green: `VERDICT_STATUS=COMPLETE_POSITIVE`, 5 old open rows =
5 new open rows, 0 suppressed-stale drift, relation kinds healthy
(`trading_management_envelopes='r'`, `trading_trades='v'`). Phase 5AE, Phase
5K, and Phase 5I probes remain `COMPLETE_POSITIVE`.

Architect verdict: the Phase 5AF flag can now be safely expanded in the next
slice to use the management-envelope runtime-object path for open/all rows when
explicitly enabled. Keep the flag default off; do not public-rename.

Report:
`docs/STRATEGY/CC_REPORTS/2026-05-31_f-position-identity-phase-5ag-trades-open-runtime-adapter-probe.md`.

## Position Identity Phase 5AH - Trades API Open Cutover Flag Path (2026-05-31)

Phase 5AH expanded the existing default-off
`CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES` flag path so
`/api/trading/trades` can render open and mixed responses from
`trading_management_envelopes` runtime objects when explicitly enabled.

Default behavior is unchanged. The route now shares one Trade-like public
serializer across the legacy compatibility path and the envelope runtime-object
path, so both use the same broker-truth display overlay and stale-open
suppression chain. `status=closed` keeps the simple envelope renderer;
`status=open` and mixed responses can use runtime objects behind the flag.

Live/read-only evidence is green. The Phase 5AH cutover probe reports
`COMPLETE_POSITIVE`: open rows match exactly (5 old = 5 new), closed rows match
exactly (50 old = 50 new), and the mixed/all response has identical row content
with only tie-order drift among rows sharing the same `entry_date`. Phase 5AG,
Phase 5AE, Phase 5K, and Phase 5I probes remain `COMPLETE_POSITIVE`.

Architect verdict: safe to merge as a default-off route path. Next step is a
short Phase 5AI route trial with the flag enabled, watching the live API/UI and
then reverting or promoting based on observed behavior. Do not public-rename.

Report:
`docs/STRATEGY/CC_REPORTS/2026-05-31_f-position-identity-phase-5ah-trades-api-open-cutover-flag-path.md`.

## Position Identity Phase 5AI - Trades API Flag Route Trial (2026-05-31)

Phase 5AI started the controlled live route trial for
`CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true`.

Because the live root is heavily dirty and behind the merged branch, the `chili`
web container was recreated from the clean Phase 5AH worktree rather than
pulling or overwriting `D:\dev\chili-home-copilot`. Postgres and trading
workers were not restarted. The running web container mounts the clean Phase
5AH worktree's `app` and `docs` directories, and its container environment
shows `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true`.

Live route evidence is clean for both unauthenticated and user-1 requests.
`/api/trading/trades?status=open` used the Phase 5AH runtime-object path and
returned 5 rows for user 1 with zero stale suppressions. Closed rows used the
simple Phase 5AF envelope renderer. No fallback, traceback, or route exception
lines were observed during the short soak.

After the web startup broker sync created one fresh Coinbase envelope, Phase
5AH, Phase 5AG, Phase 5K, and Phase 5I probes all remained
`COMPLETE_POSITIVE`; Phase 5I now reports 21 fresh decisions, 21 fresh
envelopes, 10 fresh closes, 0 hard linkage issues, and 0 mismatched rows.

Architect verdict: healthy enough to leave the route flag on for a short soak.
Next slice should remove the remaining mixed/all tie-order caveat so the Phase
5AH probe can require exact all-route parity instead of accepting
`tie_order_only=true`. Also keep the operational caveat visible: recreating
`chili` from the dirty live root would roll this route code back.

Report:
`docs/STRATEGY/CC_REPORTS/2026-05-31_f-position-identity-phase-5ai-trades-api-flag-route-trial.md`.

## Position Identity Phase 5AJ - Trades API Tie-Order Hardening (2026-05-31)

Phase 5AJ removed the final soft parity caveat from the flagged
`/api/trading/trades` envelope runtime path.

The legacy `Trade` ORM reader now uses a deterministic secondary `id DESC`
tie-breaker after `entry_date DESC`, matching the management-envelope
runtime-object reader for rows with identical timestamps. The Phase 5AH cutover
probe no longer accepts `tie_order_only=true`; all status modes must match
exactly.

Verification is green. Focused route/helper/probe tests passed (`19 passed`).
Live probes are all `COMPLETE_POSITIVE`: Phase 5AH reports exact parity for
all, open, and closed responses; Phase 5AG, Phase 5AE, Phase 5K, and Phase 5I
remain green.

Architect verdict: the `/trades` route cutover evidence is now exact under the
flag. The next practical risk is operational source-of-truth drift: the live
web container is running from a clean worktree because the live root remains
dirty. Resolve that deployment posture before treating the route flag as a
boring permanent default.

Report:
`docs/STRATEGY/CC_REPORTS/2026-05-31_f-position-identity-phase-5aj-trades-api-tie-order-hardening.md`.
