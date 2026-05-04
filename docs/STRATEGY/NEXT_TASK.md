# NEXT_TASK: position-identity-design-doc

STATUS: DONE

## Goal

Author a design document — not implementation — that specifies how CHILI moves from today's "Trade row IS everything" model (entry decision + management envelope + broker position all squashed into one ephemeral row) to a three-layer model with persistent broker-authoritative position identity.

The output is a single markdown document at `docs/DESIGN/POSITION_IDENTITY.md` that the operator and Cowork can iterate on at the doc level (cheap) before any code lands (expensive). The doc must be specific enough that subsequent NEXT_TASKs (Phase 1 implementation, Phase 2 backfill, etc.) can be written from it without further design conversations.

After this task, the conversation moves from "what should the model look like?" to "let's queue Phase 1." That's the bar — the doc exists, it answers the operator's already-stated design questions, it surfaces every remaining decision the operator needs to make BEFORE any code change, and it leaves no ambiguity that would force Phase 1's implementer to invent semantics.

## Why now

Today (2026-05-04) shipped four coordinated patches that worked but exposed the architectural ceiling:

1. **broker-truth-self-heal**'s inverse-reconcile uses a conservative `event_count == 0` check because Trade row IDs are ephemeral. If a future bug auto-closes a Trade row that DOES have fills on its current trade_id, inverse-reconcile refuses to heal — even when the underlying broker position is the same physical position with the same broker truth.
2. **bracket-writer-respect-upside-targets**'s pending-decision surface lives in `trading_bracket_intents.payload_json` because the bracket_intent FK points at the trade_id, which dies when the trade_id dies. The forward design wants pending decisions keyed on the persistent position, not the ephemeral envelope.
3. **bracket-emergency-repair-flap-guard** introduced a counter column (mig 223) that became dead code one task later because the path it guarded was retired. The orphan column is a symptom of the same identity problem — workarounds at the trade_id layer instead of fixes at the position layer.
4. **The 5 cancelled covering limit-sells** today were a strategy decision the writer made on its own. The pending-decision surface fixes that policy gap, but the deeper "operator wants both upside and downside protection" question hits the one-sell-per-share constraint at Robinhood — which is fundamentally about position-level state, not envelope-level state.

The operator's framing — *"I just vibe coded this so proper data structure and algorithms refactoring must be done"* — is the right one. Today's patches stopped today's bleeding; this initiative is the structural fix that makes future patches unnecessary.

The operator has already given five design-question answers (in the prior chat turn, captured below). The brief takes those as load-bearing inputs and writes the doc around them.

## Operator pre-actions (independent of this task)

Two small operator-side housekeeping items are decoupled from the design doc and can happen any time:

1. **Kill switch reset.** Operator chose Path A: reset now. The 9 reopened equity positions have live broker SELL_STOPs (verified at 20:12 UTC); upside management defers to the brain's `stop_engine`. After reset, autotrader resumes new entries. **Action:** `docker compose exec -T broker-sync-worker python -c "from app.services.trading.governance import deactivate_kill_switch; deactivate_kill_switch()"` — or hit whatever existing admin endpoint deactivates the kill switch. If neither path works smoothly, the next NEXT_TASK after the design doc could include a tiny "operator-grade kill switch reset endpoint" as a follow-up.

2. **EKSO/ELTX P/L data backfill.** Trade 1815 (EKSO) and 1816 (ELTX) currently show `exit_price = entry_price, pnl = 0` — the lying-fallback artifact from noon's `emergency_close_all`. Actual broker fills were ELTX `$10.70 × 25` (loss `-$33.00`) and EKSO `$10.76 × 40` (loss `-$38.80`). One-time SQL update if operator wants clean books:

```sql
UPDATE trading_trades SET exit_price=10.70, pnl=-33.00, exit_reason='broker_stop_filled_outside_chili'
 WHERE id=1816;
UPDATE trading_trades SET exit_price=10.76, pnl=-38.80, exit_reason='broker_stop_filled_outside_chili'
 WHERE id=1815;
```

Or accept as one-time data scar. Operator's call.

Neither blocks this NEXT_TASK.

## Brain integration / source material the doc must read and cite

- `app/models/trading.py` — the existing `Trade`, `BracketIntent`, `PaperTrade`, `TradingExecutionEvent` ORM models. The doc has to map every column on these to its post-refactor home (decision / envelope / position / unchanged).
- `app/services/broker_service.py::sync_positions_to_db` — the mirror function. Doc specifies how it changes to write `trading_positions` + emit `position_events` instead of (or in addition to, during the staged migration) writing `trading_trades` directly.
- `app/services/broker_service.py::get_positions` and the order-fetching helpers — broker-truth source. The position-event stream's source-of-truth for "did this position open / close / change quantity" is broker observation, not local DB writes.
- `app/services/trading/bracket_reconciliation_service.py` and `bracket_writer_g2.py` — the bracket layer that currently reads from `bracket_intent.trade_id`. Doc specifies how it migrates to read from `bracket_intent.position_id`.
- The five existing close-reason strings (`broker_reconcile_position_gone`, `phantom_after_terminal_reject`, `emergency_price_monitor_guardrail`, `zombie_reconcile_orphan`, `broker_reconcile_no_exit_price`) and their respective writers — doc must show how each maps to a `position_state` transition reason in the new model. No string left orphaned.
- `docs/STRATEGY/CURRENT_PLAN.md` — already has the 6-phase rough sketch. The design doc takes that sketch as a starting point but adds full schemas, migration ordering specifics, and phase exit criteria.
- The yf-breaker, R32 wholesale guard, C2 phantom guard, inverse-reconcile path, bracket flap guard (now-deleted) — the doc has to explain how each existing guard moves to the new model OR retires when the model makes it unnecessary.

## Path

**Design principle for the doc itself: every claim about future behavior is grounded in a specific cross-reference to existing code or operator-stated intent.** No "we'll figure it out later" wording. If a question is genuinely open, it goes in the Open Questions section with two-or-three options + Cowork's recommendation, not in the body as ambiguous prose.

### Step 1 — Read the source

Before writing prose, the executor reads (at minimum):

- All five files in Brain Integration section above
- `docs/STRATEGY/CURRENT_PLAN.md` — the 6-phase sketch
- `docs/STRATEGY/COWORK_REVIEWS/2026-05-04_*.md` — today's review files for the architectural pain captured there
- `docs/STAGING_DATABASE.md`, `docs/PHASE_ROLLBACK_RUNBOOK.md` — protocol docs for migration discipline

Cite specific file:line references throughout the doc body when describing existing behavior.

### Step 2 — Write `docs/DESIGN/POSITION_IDENTITY.md` per the structure below

```
# Design: Position Identity Refactor

## Architectural problem
  (1-2 page; cite specific 2026-05-04 incidents)
## Operator-stated design answers (load-bearing inputs)
  (verbatim quotes from operator + their interpretation)
## Three-layer model
  ### Decision layer
  ### Management envelope layer
  ### Position layer
  ### Why three layers, not two — concrete examples of operations each layer owns
## Event-sourced position state
  ### Event taxonomy (open / qty_change / close / suspect / corrected)
  ### State derivation
  ### Backfill strategy
## Schema specifics
  ### `trading_positions` (full DDL — not pseudocode)
  ### `trading_position_events` (full DDL)
  ### `trading_management_envelopes` (or whatever the renamed Trade row becomes)
  ### `trading_decisions` (the entry-intent-immutable table; fields that exist today on Trade row that belong here)
  ### `trading_execution_events` migration shape (add `position_id` column; backfill plan)
  ### `trading_bracket_intents` migration shape (FK retarget)
## How existing tables map (column-by-column)
  ### Every column on today's `trading_trades` → its new home
  ### Every column on today's `trading_bracket_intents` → its new home
  (Tables, not prose.)
## Migration ordering — 6 phases with explicit exit criteria
  Each phase: scope, schema changes, code changes, soak duration, exit criteria, rollback plan
## Compatibility surfaces
  Reporting queries, dashboards, audit logs that reference the old strings/columns — what stays / what gets deprecated / on what timeline
## Close-reason mapping table
  Today's 5 strings → new `position_state.transition_reason` values
## Phase 7 — autopilot settings UI
  Just enough sketch to show the data model supports the operator's stated UI requirements
  (per-broker enable/disable, per-strategy toggles, per-broker trade-kind allowlists)
## Brain context flow
  Where does `compute_bracket_intent` plug in? Stop engine? Stop monitor?
  How does the new model feed brain inputs vs receive brain outputs?
## What this initiative does NOT change
  Order placement code, broker auth, TP/SL primitive, etc.
## Open Questions for operator
  Each question: framing, two-three options, Cowork's recommendation, what the answer affects
## Estimated cost (pre-implementation)
  Per phase: lines of diff, test count, soak window in operator-hours
## Glossary of new terms
  position, decision, envelope, position event, transition_reason, etc.
```

The DDL must be writable directly to migration code in Phase 1 — i.e., column types, indexes, FK constraints, NULL semantics, default values, all specified.

### Step 3 — Cross-check against operator's design answers

The doc must internally validate against these operator answers (verbatim quotes from the chat):

- **Account granularity:** *"I'm not sure, what can make the more profit without losing quality?"* → Cowork's recommendation: aggregate by `(user_id, broker_source, ticker)` for now. Explain WHY (no quality loss for a single-cash-account-per-broker setup, simpler, easier to refactor in `account_type` enrichment when/if multiple-account-types-per-broker becomes real).
- **Cross-broker same-ticker:** *"for long trades, just use rh, for scalping just coinbase. it would be nice if you can just create like an autopilot settings page so I can enable/disable there the flags and checkboxes for the auto trading system including enabling and disable kind of trades per broker"* → separate position rows per broker_source (the natural key already includes it). Add Phase 7 sketch showing the autopilot settings page consumes `trading_positions` + a new `autopilot_routing_rules` table.
- **Snapshot vs event-sourced:** *"event-driven then"* → event-sourced. Snapshot fields (`current_quantity`, `current_avg_price`) on `trading_positions` are derived materializations of the event stream, NOT primary truth. Rebuild from events any time.
- **Three-layer split:** *"I think separating them is a cleaner approach"* → three layers (decision / envelope / position). Doc justifies WHY (fixed today's "Trade row dies, decision history dies, envelope dies" cascade by separating concerns) and shows what each layer owns concretely.
- **Mig 223 orphan column:** *"I give you the decision for this basing on your investigation"* → bundle DROP into Phase 1's coordinated migration. Single schema change rather than separate hygiene ticket.

If the doc reads back differently from any of those, surface it as an Open Question — don't quietly diverge.

### Step 4 — Surface 2-4 NEW open questions that emerged from the deeper read

Beyond the five operator-answered questions, the source-code read in Step 1 will turn up new design choices the operator hasn't seen yet. Likely candidates:

- **Stale event tolerance.** When broker_sync misses a sync cycle (broker auth flap, network blip), how does the event stream record the gap? Best guess vs explicit "unknown" event vs no-write?
- **Cross-account position aggregation in reporting.** Today's reporting queries don't distinguish accounts; if Phase 1 splits at `(broker_source, ticker)` aggregate, what do dashboards do with the aggregated view?
- **Backfill atomicity for the `position_id` column on `trading_execution_events`.** Bulk update with FK enforcement at the end? Online-rolling? Some events may have ambiguous mapping (orphaned trade_id) — strategy?
- **Fast-path subsystem dependency.** Fast-path code in `chili-brain/` and `app/services/trading/fast/` writes to `fast_executions`, `fast_orderbook`, etc. Do those tables migrate to position-keying in this initiative or stay decoupled?

The doc lists these and proposes options. Operator answers them in the review pass.

### Step 5 — Commit

Two commits:

1. `docs(strategy): position-identity design doc — three-layer model, event-sourced, full schema specs` — the design doc itself.
2. `docs(strategy): position-identity-design-doc CC report + mark NEXT_TASK done` — the standard CC report.

CC report includes: a copy of the source-material list actually read with citation, the magic-number-audit equivalent for the doc (zero numeric thresholds proposed, all schemas use observable-data-derived values where applicable), and the 2-4 newly-surfaced open questions.

## Constraints / do not touch

- **Doc only.** This task ships ZERO code, ZERO migrations, ZERO tests. The output is markdown.
- **No magic numbers in the design.** If the doc proposes a threshold (e.g., "stale event tolerance window"), it has to derive from observable system state OR be explicitly flagged as an Open Question for operator decision.
- **No premature schema commitments.** The DDL in the doc must be specific enough to implement, but Phase 1's actual migration script is a separate task. The doc doesn't prescribe migration ordering inside Phase 1; it prescribes Phase 1's exit criteria.
- **Cite, don't speculate.** Every "today the system does X" claim must reference a specific file:line in the codebase. Every "the operator wants Y" claim must reference a specific quote or operator-stated intent in `docs/STRATEGY/COWORK_REVIEWS/*.md` or `docs/STRATEGY/CURRENT_PLAN.md`.
- **No dependencies on the Phase 7 UI being built.** The data model must stand on its own; UI is consumer.
- **Backwards compatibility plan must be concrete.** Existing reporting queries that grep `exit_reason='broker_reconcile_position_gone'` need a deprecation timeline, not handwaving.
- **Tests use `_test`-suffixed DB.** Standard PROTOCOL Hard Rule (vacuous for this doc-only task; included because it'll matter for Phase 1).
- **No `git push --force` to main.** PROTOCOL Hard Rule 4.

## Out of scope

- **Implementation of any phase.** Phase 1 is the next NEXT_TASK after this doc lands and operator+Cowork have iterated.
- **Operator pre-actions** (kill switch reset, EKSO/ELTX P/L cleanup) — those are decoupled and operator-driven; not part of this brief's success criteria.
- **Fast-path subsystem refactor.** Surfaced as a NEW open question (Step 4 candidate); but the doc doesn't dictate that fast-path migrates in this initiative. Operator decides on review.
- **Forex / perps / non-Robinhood-Coinbase venues.** Phase 1 starts with Robinhood + Coinbase (today's pain); generalization to other venues happens after the model proves out. Doc mentions this as future direction; doesn't design for it now.
- **The autopilot settings UI itself.** Phase 7 sketch in the doc describes the data model the UI will consume; building the UI is a later initiative.

## Success criteria

1. **`docs/DESIGN/POSITION_IDENTITY.md` exists** with the structure outlined in Step 2 above. Sections all populated, no TODO placeholders.
2. **Two commits, both pushed:**
   - `docs(strategy): position-identity design doc — three-layer model, event-sourced, full schema specs`
   - `docs(strategy): position-identity-design-doc CC report + mark NEXT_TASK done`
3. **Operator's five design answers** are reflected verbatim or paraphrased-with-attribution in the doc, with rationale.
4. **2-4 newly-surfaced open questions** at the bottom of the doc, each with options + Cowork-recommended answer.
5. **Full DDL** for at least three of the new tables (`trading_positions`, `trading_position_events`, `trading_management_envelopes` or its renamed equivalent). Column types, indexes, FK constraints, NULL semantics, default values all specified.
6. **Column-by-column mapping table** for today's `trading_trades` and `trading_bracket_intents` to their new homes. Every column accounted for.
7. **6-phase migration plan** with explicit exit criteria per phase. Each phase reads as something Claude Code could execute as a NEXT_TASK without further design.
8. **Close-reason mapping table** for the 5 existing strings to new `transition_reason` values. No string orphaned.
9. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-04_position-identity-design-doc.md` with source-material citation list + new open questions.

## Rollback plan

- **Code rollback:** `git revert <doc-commit>`. Doc disappears; no live system effect.
- **No migration to roll back.**
- **No live broker rollback needed.**

## Verification commands (for the executor)

```bash
# After commits land, doc exists
ls -la docs/DESIGN/POSITION_IDENTITY.md

# Word count sanity check (doc should be substantial, not a stub)
wc -w docs/DESIGN/POSITION_IDENTITY.md
# Expect: > 4000 words for the structure outlined

# Citations sanity check
grep -c "app/services\|app/models\|docs/STRATEGY" docs/DESIGN/POSITION_IDENTITY.md
# Expect: > 15 cross-references

# Open questions present
grep -c "## Open Question\|### Open Question" docs/DESIGN/POSITION_IDENTITY.md
# Expect: at least 2-4
```

## Open questions for Cowork (surface in your CC_REPORT)

1. **Fast-path subsystem.** Does it migrate to position-keying in this initiative or stay decoupled? Cowork's lean: stay decoupled for v1, integrate after Phase 4 lands and the model is proven. Surface for operator decision.
2. **Backfill atomicity.** What's the chosen strategy for backfilling `position_id` on `trading_execution_events`? Cowork's lean: bulk update per-(user, broker_source, ticker) batch with explicit `unmapped` event for orphan trade_ids; FK gets enforced after backfill completes. Surface alternatives.
3. **Stale event handling.** When a broker_sync cycle misses entirely, the event stream has a gap. Cowork's lean: emit explicit `sync_gap` event covering the missed window so the derivation logic doesn't quietly assume continuity. Operator may want stricter (alert on gap) or looser (best-effort continuity).
4. **Reporting deprecation timeline.** Existing dashboards grep `exit_reason` strings. New `transition_reason` values are different. Cowork's lean: maintain both columns for one full quarter of soak; add a view that maps old-to-new for legacy queries. Surface alternatives.

## Forward pointer

After this doc lands and operator+Cowork iterate (review pass + ~1-2 revision cycles, all at doc level), the next NEXT_TASK is Phase 1 implementation: the `trading_positions` table, the event-stream insert path in `broker_sync`, and shadow-mode operation (no readers depend on it yet). The doc's Phase 1 exit criteria become Phase 1's success criteria directly.
