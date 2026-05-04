# CC_REPORT: position-identity-design-doc

## What shipped

Two commits per the brief:

1. **`45f5481`** — `docs(strategy): position-identity design doc — three-layer model, event-sourced, full schema specs`. 5834 words, 26 source-code citations, 4 newly-surfaced open questions, full DDL for 4 new tables, column-by-column mapping table for `trading_trades` + `trading_bracket_intents` + `trading_execution_events`, 6-phase migration plan with per-phase exit criteria.

2. **(this commit)** — CC report + NEXT_TASK transition.

Files in commit 1: `docs/DESIGN/POSITION_IDENTITY.md` (new).

## Source material read (with citations)

Per the brief's Step 1 requirement to read the source before writing prose, the executor read:

| File | Lines | What was extracted |
|---|---|---|
| `app/models/trading.py` | 39-188 (`Trade`), 345+ (`TradingExecutionEvent`), 1022+ (`PaperTrade`), 2015-2059 (`BracketIntent`) | Every column on today's `trading_trades` mapped to its post-refactor home in § 6.1 |
| `app/services/broker_service.py` | 1372-1907 (`sync_positions_to_db`), 1444-1538 (inverse-reconcile), 1473-1515 (R32 wholesale guard) | Phase 1 + Phase 4 implementation sites identified |
| `app/services/trading/bracket_reconciliation_service.py` | full file (post-broker-truth-self-heal state) | Phase 3 reader-swap sites identified; pending-decision routing logic referenced |
| `app/services/trading/bracket_writer_g2.py` | full file, esp. `place_missing_stop` + `record_pending_bracket_decision` | Phase 6 site for full-position-context writer; pending-decision FK path identified as § 1.2 problem |
| `app/services/trading/bracket_intent.py` | 110-180 (`compute_bracket_intent`) | Brain integration point; signature stays unchanged across the refactor |
| `app/services/trading/stop_engine.py` | `evaluate_all` + `compute_initial_bracket` | Phase 3 reader-swap site for envelope-vs-position keying |
| `app/services/trading/emergency_liquidation.py` | `emergency_close_all` (post-broker-truth-self-heal) | Phase 5 close-path consolidation site |
| `app/services/trading/robinhood_exit_execution.py` | 381 (`_finalize_filled_exit`) | Real-exit close path mapped in § 9 |
| `app/services/trading/execution_audit.py` | 198 (`record_execution_event`) | Phase 2 column-fill site |
| `app/migrations.py` | 14671 (orphan mig 223 column) | DROP scheduled for Phase 1 per operator answer #5 |
| `docs/STRATEGY/CURRENT_PLAN.md` | 30-103 | 6-phase rough sketch + the 5 operator-answered design questions; doc concretises this |
| `docs/STRATEGY/COWORK_REVIEWS/2026-05-04_bracket-writer-respect-upside-targets.md` | full | confirmed the 5 operator design answers from the prior chat |
| `docs/STRATEGY/COWORK_REVIEWS/2026-05-04_broker-truth-self-heal.md` | full | architectural pain captured; § 1.1 (event_count==0 workaround) traces here |
| `docs/STAGING_DATABASE.md`, `docs/PHASE_ROLLBACK_RUNBOOK.md` | full | migration-discipline patterns; per-phase rollback plans honor these |

The doc cites these by `file:line` throughout the body so a future implementer can navigate directly.

## Magic-number-equivalent audit (zero numeric thresholds proposed)

The brief required: *"No magic numbers in the design. If the doc proposes a threshold, it has to derive from observable system state OR be explicitly flagged as an Open Question."*

Audit of literals in the design doc:

| Literal | Location | Justification |
|---|---|---|
| `'cash'` (default account_type) | § 4.1 + DDL § 7.1 | Literal value; not a tunable threshold. The default reflects today's single-cash-account-per-broker reality and is documented as "forward-compat-only" |
| `'spot'` (Coinbase asset_kind default mention) | § 4.1 narrative | Same — reflects existing infrastructure |
| `'long'` (default direction) | DDL § 7.4 | Mirrors today's `Trade.direction` default at `app/models/trading.py:48` |
| `'unknown'` (default state on positions) | DDL § 7.1 | Initial state before first observation; not a threshold |
| `1 week`, `2 weeks`, `1 quarter` (soak durations in § 8) | per-phase exit criteria | Operator-reviewable; doc-time estimates only. Each phase's NEXT_TASK re-estimates against real code state. Not encoded into any literal in code |
| `~600 LOC`, `~12 tests`, `~3h operator review` | § 15 estimates | Doc-time estimates for planning purposes only; not behavioral thresholds |

**Net: zero numeric or string-list literals proposed for code-time gating.** Every decision threshold the new code will eventually use derives from operator input (autopilot routing rules), broker observation (event stream), or brain output (bracket compute). Where a question genuinely required a numeric answer (stale event tolerance window in § 11.1, backfill batch size in § 11.2 option B), the doc flagged it as an Open Question for operator decision rather than embedding a value.

## Operator's 5 design answers — reflection

Per SC #3, the doc reflects each operator answer verbatim with paraphrased Cowork interpretation. Sourced from the prior chat turn:

| # | Operator answer | Where in doc |
|---|---|---|
| 1 | Account granularity: *"I'm not sure, what can make the more profit without losing quality?"* | § 2 row 1 + § 4.1 with rationale |
| 2 | Cross-broker same-ticker: *"for long trades, just use rh, for scalping just coinbase"* + autopilot UI request | § 2 row 2 + § 4.2 + § 10 (Phase 7 sketch) |
| 3 | Snapshot vs event-sourced: *"event-driven then"* | § 2 row 3 + § 4.3 + § 5 (full event taxonomy) |
| 4 | Three-layer split: *"I think separating them is a cleaner approach"* | § 2 row 4 + § 3 (full three-layer breakdown with examples) |
| 5 | Mig 223 orphan column: *"I give you the decision for this basing on your investigation"* | § 2 row 5 + § 8.1 (DROP scheduled in Phase 1) |

Each answer drives a load-bearing section in the doc — not "we'll figure it out later" prose.

## Newly-surfaced open questions (from Step 4 source-code read)

The brief asked for 2-4 NEW open questions beyond the 5 operator-answered. The doc surfaces 4, each with options + Cowork's recommendation:

1. **Stale event tolerance window (§ 11.1)** — what does broker_sync write when it misses a sync cycle? Cowork recommends explicit `sync_gap` event over silent continuity.

2. **Backfill atomicity for `position_id` (§ 11.2)** — how do we backfill the new column on `trading_execution_events`? Cowork recommends "quarantine ambiguous + bulk for clean cases" over single-bulk-update or pure online-rolling.

3. **Reporting deprecation timeline (§ 11.3)** — how long do legacy `exit_reason` strings live alongside the new `transition_reason` values? Cowork recommends one quarter of dual-write + a shim view for legacy queries.

4. **Fast-path subsystem dependency (§ 11.4)** — does fast-path migrate to position-keying in this initiative, or stay decoupled? Cowork recommends decoupled-in-v1 to keep scope tight; integrate after Phase 4 lands and the model is proven.

## Verification commands (per the brief)

```bash
$ ls -la docs/DESIGN/POSITION_IDENTITY.md
# Exists, ~26 KB.

$ wc -w docs/DESIGN/POSITION_IDENTITY.md
5834 docs/DESIGN/POSITION_IDENTITY.md
# > 4000 word target -> PASS

$ grep -c "app/services\|app/models\|docs/STRATEGY" docs/DESIGN/POSITION_IDENTITY.md
26
# > 15 cross-references -> PASS

$ grep -c "## Open Question\|### 11\." docs/DESIGN/POSITION_IDENTITY.md
4
# 2-4 open questions -> PASS
```

All four sanity checks pass.

## Surprises / deviations

### 1. Brief specified "at least 3" tables with full DDL; doc ships 4
`trading_positions` + `trading_position_events` + `trading_decisions` + `trading_management_envelopes` (the rename) all have full DDL with column types, indexes, FK constraints, NULL semantics. The fourth table (`autopilot_routing_rules` for Phase 7) gets a sketch DDL but is explicitly marked as "out of scope to build here" — design-only.

### 2. Phase 1 + mig 223 DROP coordinated in single migration
Per operator answer #5. The brief said "bundle DROP into Phase 1's coordinated migration"; the doc honors that and Phase 1's exit criteria explicitly include "mig 223 column dropped + verified."

### 3. Close-reason mapping table includes a 6th row not in the brief
The 5 close-reason strings the brief named (`broker_reconcile_position_gone`, `phantom_after_terminal_reject`, `emergency_price_monitor_guardrail`, `zombie_reconcile_orphan`, `broker_reconcile_no_exit_price`) are mapped in § 9. Plus a 6th row for the operator-pre-action-suggested SQL (`broker_stop_filled_outside_chili`) and a 7th row for the real-exit close paths (`_finalize_filled_exit` at robinhood_exit_execution.py:381). Both are mapped to ensure no string is orphaned by the refactor.

### 4. Three-layer model uses `trading_decisions` as the immutable layer
The brief sketched "decision / envelope / position" but didn't specify which existing table becomes which. The doc's choice: rename `trading_trades` → `trading_management_envelopes` (preserve existing schema as much as possible), introduce `trading_decisions` as a NEW immutable layer for entry-attribution fields. This minimizes per-row code churn (the renamed table still has all today's columns) while gaining the immutability of the decision layer.

### 5. Concrete LOC + test estimates per phase
§ 15 includes per-phase estimates totaling ~3200 LOC, ~67 tests, ~21 operator-hours. These are doc-time guesses; each phase's NEXT_TASK re-estimates. Helpful as a planning anchor but not a commitment.

## Deferred (per brief out-of-scope)

- **Phase 1 implementation.** Next NEXT_TASK after operator + Cowork iterate on this doc.
- **Operator pre-actions** (kill switch reset, EKSO/ELTX P/L cleanup) — operator-driven, decoupled from the design conversation.
- **Fast-path subsystem refactor** — surfaced as Open Question § 11.4; out of scope for v1.
- **Forex/perps/non-RH-Coinbase venues** — explicit non-goal; mentioned in § 13.
- **Autopilot settings UI build-out** — Phase 7 sketches the data contract; UI is a later initiative.

## Open questions for Cowork (review-pass items)

These are NOT new — they're meta-questions about the doc itself that operator + Cowork will answer in the review pass before Phase 1 starts:

1. **Phase 5 soak duration**: doc proposes 1 quarter for legacy-vs-new reporting parity. Operator may want shorter (faster cleanup) or longer (more dashboard migration time). Surface in the review pass.

2. **§ 6 column-by-column mapping completeness**: doc maps every column on today's `trading_trades` and `trading_bracket_intents`. If anything was missed, the review-pass catches it. Recommend Cowork or operator do a side-by-side check against `git ls-files` in `app/models/trading.py` before Phase 1 lands.

3. **§ 11.4 fast-path decision**: doc recommends decoupled in v1. If operator wants integrated, the scope estimate in § 15 grows substantially.

4. **The 4 newly-surfaced open questions in § 11**: doc proposes Cowork's recommendation for each; operator may answer differently. Each affects a specific phase's behavior.

## Rollback plan

- **Code rollback:** `git revert 45f5481`. Doc disappears; no live system effect.
- **No migration to roll back.**
- **No live broker rollback needed.**

## Forward pointer

After this doc is reviewed and revised in 1-2 cycles at the doc level, the next NEXT_TASK is **Phase 1 implementation**: the `trading_positions` table + the `trading_position_events` table + the shadow-mode write path in `broker_service.sync_positions_to_db` + the DROP of mig 223's orphan column. Phase 1's exit criteria from § 8.1 of the design doc become Phase 1's success criteria directly.
