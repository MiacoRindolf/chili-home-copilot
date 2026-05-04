# Current Plan: Position Identity Refactor

**Initiative owner:** Cowork (strategy) + Claude Code (execution).
**Last update:** 2026-05-04, after the broker-truth-self-heal review and the EKSO/ELTX investigation.

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

## Open architectural questions for operator decision

1. **Account type granularity.** Robinhood has cash/margin/IRA accounts. Coinbase has spot/portfolio. Should `trading_positions` key on account-type (decoupling per-account positions) or aggregate? Choice affects how the existing single-user, single-account model generalizes.
2. **Crypto positions on Robinhood vs Coinbase.** Same ticker (e.g., BTC-USD) can be held at both. Position identity needs broker_source in the key. Is that already the design intent or do we need an explicit conversation about cross-broker aggregation reporting?
3. **Snapshot table or event-sourced?** `trading_positions.current_quantity` is a snapshot (overwritten each broker_sync). Alternative: event-sourced `position_events` with current state derived. Snapshot is simpler; event-sourcing is more honest about "this is what the broker said at time T." Recommend snapshot for v1, event-sourcing as future enhancement if needed.
4. **What does Trade row identity become?** Today a Trade row IS a position management envelope but also the entry-decision unit (pattern, scan_pattern_id, entry_reason, etc.). Refactor splits these conceptually. Should the Trade row stay the entry-decision unit (and a position can have multiple historical Trade rows representing decisions to enter/manage at different times), or do we introduce yet another layer ("trade_decision" → "management_envelope" → "position")? Three-layer is cleaner; two-layer is less churn.
5. **Migration ordering relative to schema_version 223 orphan column.** That column is unused dead code from the magic-number flap-guard task. Bundle its removal with Phase 1 of this refactor or do separately?

## Expected output of the next strategy step

The next NEXT_TASK should NOT be Phase 1 implementation — it should be a **design doc** that answers the five open questions above, sketches the precise schema for `trading_positions`, and proposes the migration plan. Operator reviews; iterations happen at the doc level (cheap) rather than the code level (expensive); only after the design lands do we queue Phase 1 implementation.

## What's deferred / parallel

- **Bug 4** (`emergency_close_all` should submit broker SELLs or be retired). No urgency now (no auto-callers); fits within Phase 5 of this refactor naturally — auto-callers are gone, manual usage gets handled via the position-state-transition machinery.
- **Schema hygiene** (drop migration 223's orphan column). Bundle with Phase 1 if convenient.
- **Fast-path crypto scalping initiative.** Paused. The fast-path stack writes to the same `trading_execution_events` and (eventually) `trading_trades` tables; building more on the unstable foundation compounds the problem. Resume after Phase 4 lands.
- **The lying P/L on EKSO trade 1815 ($0 vs actual −$38.80) and ELTX trade 1816 ($0 vs −$33.00).** One-time data backfill. Operator decision whether to clean now or accept as a one-time scar.

## Out of scope right now

- **Live broker-routing changes.** This refactor is data-model only. Order placement code stays as-is; only the way we *think* about positions changes.
- **Non-Robinhood/Coinbase venues.** Forex (OANDA), perps (Hyperliquid/dYdX/Kraken Futures) follow the same pattern but Phase 1 starts with the brokers that have today's audit pain. Generalize to other venues once the model is proven.
- **Dashboards and reporting changes.** Existing reporting queries continue to work via backwards-compat. UI changes can come later.
