# COWORK_REVIEW: position-identity-design-doc

## Verdict

Highest-quality output in this sequence. The design doc is genuinely concrete: 5834 words, 26 source-code citations, full DDL for 4 new tables (not the briefed minimum of 3), complete column-by-column mapping for `trading_trades` + `trading_bracket_intents` + `trading_execution_events`, 6-phase plan with explicit per-phase exit criteria + rollback plans, close-reason mapping covering 7 categories (the 5 the brief named plus 2 the source-code read surfaced), 4 newly-surfaced open questions each with options + recommendation, plus a glossary and a per-phase cost estimate. No "we'll figure it out later" language. No magic numbers proposed for code-time gating. Operator's 5 design answers reflected verbatim with rationale.

The brief said *"the bar is the doc exists, answers the operator's already-stated design questions, surfaces every remaining decision the operator needs to make BEFORE any code change, and leaves no ambiguity that would force Phase 1's implementer to invent semantics."* The doc clears that bar. Phase 1's implementer reads § 7.1 + § 8.1 and writes the migration straight from there — no design conversation needed.

This is what the operator's *"vibe coded, needs proper data structure and algorithms refactoring"* framing called for. The design is an actual design, not a sketch dressed up.

## Algo-trader lens

**What's good.** The three-layer split (decision / envelope / position) is the right structural answer for today's audit pain. The rename-not-replace approach for `trading_trades → trading_management_envelopes` minimizes per-row code churn while gaining the layer separation. The new `trading_decisions` table holds the immutable entry-attribution that today survives a Trade-row rebind only by accident. After Phase 1+4, today's `event_count == 0` workaround retires and inverse-reconcile becomes precise: "is there a SELL fill in this position's history since its most recent open event?" — the answer is unambiguous regardless of how many envelope generations have come and gone.

The event-sourced `trading_position_events` is the operator's stated preference and the natural fit for the broker-truth-driven workflow. The `suspect` event type is a clean response to R32's wholesale-empty-positions case (today's R32 guards against the cascade by refusing to act; post-refactor it writes a `suspect` event so the gap is auditable).

The Phase 7 sketch (§ 10) anticipates the autopilot settings UI: `autopilot_routing_rules` table with `rule_kind` enum supporting the operator's stated "long → RH, scalp → Coinbase" routing as two `strategy_toggle` rows. The data contract is clear enough that whoever builds the UI knows what to integrate against.

The close-reason mapping in § 9 expanded the brief's 5 to 7 — the doc honestly noticed `_finalize_filled_exit` (real envelope closes) and the operator-pre-action `broker_stop_filled_outside_chili` reason. Nothing orphaned by the refactor.

**What's narrow.** Phase 5's soak duration is one full quarter — that's the longest phase by a wide margin (reporting deprecation timeline). Operator should see this on the totals: roughly 2.25 quarters end-to-end soak across all 6 phases (per § 15). That's 5-7 months of staged rollout, plus implementation time. Worth confirming the operator wants to commit to that timeline vs. a faster cutover.

The `direction` handling for short positions is flagged as an open question inside § 6.1 row 3 but not in the formal Open Questions section. Today's data is essentially all `direction='long'` so it doesn't bite Phase 1; if the operator ever opens shorts, the schema needs a small clarification (separate position row or `direction` column on positions). Consider promoting to formal Open Question § 11.5 in the next revision.

**What's deferred and worth tracking.** Fast-path subsystem (§ 11.4) — the doc recommends decoupled-in-v1, integrate after Phase 4 lands. That's correct prioritization but means the fast-path stack continues writing to its own tables without position-keying through Q3+. Worth a sidebar: today's fast-path is paper-mode by default and its volumes are large enough that backfill atomicity is harder there. Operator should confirm the integration timing (after Phase 4) is acceptable.

## Dev-architect lens

**What's good.** Full DDL is concrete enough to migrate from. Column types specified, indexes specified (including the smart partial index on `state='open'` for the dominant query), FK constraints specified with ON DELETE behavior, CHECK constraints on enum-shape strings, NULL semantics explicit, defaults explicit. § 7.1 is the kind of DDL that can be copy-pasted into `_migration_NNN_*` and pass review.

The renamed-table + shim VIEW approach (§ 7.3: `CREATE VIEW trading_trades AS SELECT * FROM trading_management_envelopes;`) is a smart backwards-compat lever — most existing reporting queries continue to work without modification. The doc anticipates this in § 11.3 with the deprecation timeline open question.

Per-phase rollback plans are concrete and honor `docs/PHASE_ROLLBACK_RUNBOOK.md` patterns. Each phase's exit criteria are measurable rather than aspirational ("audit query passes for 1 week" not "we feel good about it").

The doc is INSERT-only on `trading_decisions` enforced at the application layer (no ORM relationships allowing UPDATE; `validates` hooks reject mutations). That's the right discipline for an immutable layer — schema-level CHECK constraints alone don't guarantee immutability, but ORM hooks plus reviewer discipline do.

The magic-number-equivalent audit (§ 36) is the cleanest yet — every literal in the design enumerated and justified. Soak durations and LOC estimates are explicitly flagged as doc-time guesses, not behavioral encoding.

**What's concerning.**

1. **Phase 1's migration ordering is non-trivial.** The DDL in § 7.1 references `trading_management_envelopes(id)` in the FK for `current_envelope_id`. But the rename `trading_trades → trading_management_envelopes` happens in the same Phase 1 migration. The implementer needs to do the rename FIRST, then create the positions table. Worth a one-line note in § 8.1's Code Changes subsection so the order isn't ambiguous.

2. **Shim VIEW doesn't cover writes.** `CREATE VIEW trading_trades AS SELECT * FROM trading_management_envelopes` makes SELECT queries work. But anything that does raw `INSERT INTO trading_trades ...` or `UPDATE trading_trades ...` (operator scripts, dispatch scripts, ad-hoc SQL) will fail post-rename. Phase 1's implementer should grep for raw write paths and either update them or use an UPDATE-trigger on the view to forward writes. Worth surfacing in Phase 1's exit criteria.

3. **`PaperTrade` is mentioned in § 16 references as "parallel; future-phase consideration"** but doesn't appear in the column-by-column mapping (§ 6). PaperTrade has a near-identical shape to Trade. If paper-mode isn't a Phase 1 concern, that's fine — but the doc should explicitly say so or risk surprising someone in Phase 4.

4. **Direction handling for shorts (mentioned above).** Not concerning today; could trip a future operator-introduced short trade.

5. **Phase 6's "multi-leg bracket order where supported"** language is the only place the doc gets vague. Robinhood retail does NOT support OCO/multi-leg. Coinbase has limited support. If Phase 6's success depends on multi-leg support that doesn't exist, the success criterion is unreachable. Worth tightening: "where the venue supports it (currently neither RH retail nor Coinbase Advanced)" so the implementer knows the actual target is the trailing-stop fallback.

## Decisions for the operator

The doc's own § 11 has 4 open questions. Each has options + my recommendation; operator answers each. Cleanly:

1. **§ 11.1 Stale event tolerance window** — recommend B (explicit `sync_gap` event).
2. **§ 11.2 Backfill atomicity** — recommend C (quarantine ambiguous + bulk for clean cases).
3. **§ 11.3 Reporting deprecation timeline** — recommend A+B (dual-write through Q3 + shim view).
4. **§ 11.4 Fast-path dependency** — recommend B (decoupled in v1, integrate after Phase 4).

Plus from this review:

5. **Phase 5 soak duration** — confirm 1 quarter is acceptable, or want shorter? Affects total initiative timeline.
6. **Direction handling for shorts** — promote to formal Open Question 11.5? Schema implications if operator ever shorts.
7. **PaperTrade migration timing** — Phase 1 ignore, or include? Affects backfill scope.

## Recommended next move

The doc is ready for Phase 1 brief. One revision pass at most before queueing implementation:

**Path A — operator answers the 4-7 open questions, Cowork revises § 11 + adds clarifications, then queue Phase 1.** Single revision cycle. Phase 1 brief writes from § 8.1 + § 7.1 directly.

**Path B — operator skims the doc, signals "looks fine," skips formal revision, queue Phase 1 with the open questions answered inline in the Phase 1 brief.** Faster. Risk: Phase 1's NEXT_TASK has to absorb design conversations that should have happened at the doc level.

I lean A. The four new open questions affect concrete Phase code paths (sync_gap event in Phase 1, backfill atomicity in Phase 2, dual-write in Phase 5, fast-path scope in Phase 7). Locking those answers into the doc before Phase 1 starts means each subsequent NEXT_TASK has a clear single-source-of-truth to read. ~1-2 hours of operator review + Cowork revision saves multiple NEXT_TASKs from scope-drift.

## Status of CURRENT_PLAN.md

The 6-phase sketch in CURRENT_PLAN.md (lines 30-103) is now superseded by the design doc's § 8. CURRENT_PLAN.md should reference the design doc as the authoritative source for the initiative going forward. One-line update suggested: replace the 6-phase sketch with `See docs/DESIGN/POSITION_IDENTITY.md § 8 for the authoritative phase plan.` Or leave the sketch as a quick orientation and add a forward pointer to the doc. Cosmetic; not blocking.

## Status of NEXT_TASK.md

Marked DONE for `position-identity-design-doc`. Awaiting operator's answers to § 11's open questions before the next NEXT_TASK (`position-identity-phase-1`) gets staged.
