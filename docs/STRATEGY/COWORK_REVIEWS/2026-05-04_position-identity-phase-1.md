# COWORK_REVIEW: position-identity-phase-1

## Verdict

Cleanest implementation task in this entire sequence. Five commits (the planned three plus two honest fix-during-execution commits), every brief success criterion met, magic-number audit clean, live verification 19/19 positions mirrored with zero discrepancies on first audit run, zero tracebacks in the shadow-mode write path, all 11 test scenarios passing, all three embedded operator-confirmation items answered with recommendations.

The brief said *"the bar is the new tables exist, broker_sync writes to them, backfill seeds them, audit query verifies parity, ZERO READERS depend on the new tables for decisions."* The CC delivered exactly that. Live behavior unchanged. Position layer accumulating data. Ready to soak.

## Algo-trader lens

**What's good.** First post-deploy audit is 19/19. Every active Robinhood position the broker reports is mirrored to a `trading_positions` row with a matching `opened` event, and the snapshot fields (current_quantity, current_avg_price) match broker truth exactly. That's the strongest possible signal that the shadow-mode write path covers the full broker-position surface — not a sample, the whole live universe.

The "natural broker_sync pre-populated 19 rows before the explicit backfill" surprise (Surprise #2 in the report) is actually the system working correctly. Mig 224 applied at 03:29:20 UTC, broker_sync cron ran on its 2-min schedule, hit the new shadow-mode write path, populated everything. By the time the explicit backfill ran, it found everything already-present and the `ON CONFLICT DO NOTHING` semantic kept things idempotent. **The backfill and the live write path can race without producing duplicates** — that's exactly the design intent for a system that has to handle deploys mid-cycle.

The shadow-mode invariant is verified by Test J — a static-grep canary that confirms zero readers consult `trading_positions` or `trading_position_events` in any decision path. Nothing in stop_engine, bracket_writer, bracket_reconciliation, or inverse-reconcile reads from the new tables. Live behavior is structurally guaranteed unchanged.

The sync_gap detection is correctly silent so far (no missed broker_sync cycles in the post-deploy window). When auth flap or network blip eventually causes a missed cycle, a `sync_gap` event will appear and the audit trail will be intact rather than silently assuming continuity.

**What's narrow.** Zero paper-mode positions in the live system right now (no open `trading_paper_trades` rows). Test H proves the structural support exists, but the live verification can't show paper-mode mirroring until the operator runs paper trades. That's a gap in *empirical* verification, not in *code* verification. If paper-mode trading resumes during the soak window, the next audit run will surface it; if it doesn't, the next phase that touches paper-mode (the eventual rename or Phase 5 transition_reason mapping) will be the first real exercise.

The backfill skips closed historical trades (CC Open Q #3) — only walks `WHERE status='open'`. This keeps the migration window short but means the position-event stream won't contain pre-refactor history. Whether that matters depends on whether Phase 5's reporting consumers need historical-event continuity. Probably no, but worth tracking through Phase 4.

## Dev-architect lens

**What's good.** Three planned commits + two honest fix commits during execution (`27bd414` text import hoist, `a6c3174` test fixture seeding). The "honest fix during execution" pattern is the right discipline — the bug was caught at test time, fixed in a small follow-up commit, and surfaced in the report's Surprises section rather than buried.

Magic-number audit is the cleanest yet. The two numeric literals introduced (`_BROKER_SYNC_CRON_INTERVAL_SECONDS = 120`, `_SYNC_GAP_TOLERANCE_MULTIPLIER = 2`) are both:
- Derived (the 120 cites `trading_scheduler.py:3317` `minute="*/2"` directly; the 2× cites design doc § 11.1 Decision B)
- Documented at definition site with the source-line citation that justifies them
- Operator-confirmed (the 2× via your earlier "go per recommendation" answer)

Every other literal added is categorical (state values, event types, audit-trail strings). Zero tunable thresholds. The "no magic numbers" principle held end-to-end through the largest implementation task of the sequence.

The migration scope discipline is right: ONLY the position layer ships in Phase 1. The brief explicitly said the rename, decisions table, bracket_intent retarget, and reader changes all wait for later phases — and the CC honored that. The new FK columns target today's `trading_trades(id)` for now. Smaller blast radius, cleaner rollback.

The shadow-mode write path uses a try/except wrapper that logs and continues on any exception — the `[phase1_position_event] write failed; shadow-mode continues` pattern. If the new path EVER throws, it can't break today's `trading_trades` writes. Zero such failures in the post-deploy window so far.

**What's concerning.**

1. **Soak is partial.** "Less than 5 sweeps captured in this report" — the brief's exit criterion is 1 week of clean audit-query parity before Phase 2 queues. We're in hour-1, not day-7. The audit-query result is encouraging (19/19) but not yet sufficient to declare Phase 1 complete in the soak sense. NEXT_TASK is marked DONE for the implementation work, but the gating-into-Phase-2 needs continued audit-query monitoring.

2. **The text-import fix (`27bd414`)** was caught at test time, not at code-review time. That's fine — tests are the safety net — but it's a reminder that module-level helpers added to a file with function-local imports don't inherit those imports. Worth a one-line note in `app/services/broker_service.py`'s top docstring or a contributing convention to put SQL-related imports at module level by default.

3. **The two follow-up commits (`27bd414` + `a6c3174`) didn't go through the CC report's planned-commit list.** They're real commits with semantic messages, but they happened during execution rather than being pre-listed. CC honestly mentioned them in the report header. For a Phase 1 task at this scope (~700+ LOC + 11 tests + scripts) two small fixup commits is well within tolerance; just worth noting that the "Three commits" plan ended up "Three planned + two fixes." Phase 2's brief should expect similar fixup-commit tolerance.

4. **The `current_envelope_id` FK targets `trading_trades(id)` per the brief.** When Phase 3/4 renames the table, the FK has to be updated to target `trading_management_envelopes(id)`. PostgreSQL handles this via `ALTER TABLE ... DROP CONSTRAINT ... ADD CONSTRAINT ...` after the table rename — Phase 3's brief should call this out explicitly.

## Decisions for the operator

1. **Phase 1 is implementation-complete; soak is in progress.** Watch the audit query for the next several days. If 19/19 stays at 19/19 (or grows-with-broker-changes appropriately), Phase 1's gating exit criterion is satisfied and Phase 2 queues. If discrepancies appear, surface them — that's the kind of finding Phase 1's shadow-mode soak is designed to catch.

2. **Operator confirmation items: all three resolved per Cowork's recommendations.**
   - `pnl_pct` deferred to rename phase ✓
   - sync_gap multiplier stays at 2× (240s threshold) ✓
   - 2-week Phase 5 soak carries forward ✓

3. **Operator pre-actions still outstanding from earlier today:**
   - Kill switch reset (your Path A choice — reset whenever).
   - EKSO/ELTX P/L cleanup (-$71.80 of misreported P/L; backfill or accept as one-time scar).
   Neither blocks Phase 1's soak.

4. **Direction default for crypto positions** stays `'long'` for v1. Will need a per-broker resolver when perps venues integrate. Surface to me when perps integration enters the queue.

5. **The next NEXT_TASK is NOT immediately Phase 2.** It's the soak window. NEXT_TASK can stay marked DONE for `position-identity-phase-1` until you signal soak-clean readiness. When you do, I stage `position-identity-phase-2`.

## Recommended next move

Three workstreams, parallel and operator-driven:

1. **Watch the soak.** Run `scripts/audit_position_layer_parity.py` periodically (hourly cron is reasonable; manual once a day if you prefer). When 1 week of clean audits has accumulated, signal me and Phase 2 queues.

2. **Resolve the outstanding operator pre-actions** at your convenience (kill switch reset + EKSO/ELTX cleanup). Independent of Phase 1 soak.

3. **(Optional) Tighten Phase 6 multi-leg-order language in the design doc** — flagged in the prior review as a follow-up doc revision before Phase 6 lands. Could happen any time during the next several phases of soak; not urgent.

Once Phase 1 soaks clean, Phase 2 (`trading_execution_events.position_id` backfill per design doc § 8.2) is the next implementation task.

## Status of NEXT_TASK.md

CC marked DONE for `position-identity-phase-1`. Implementation is done; Phase 2 staging awaits operator soak-clean signal.

## Status of CURRENT_PLAN.md

Forward pointer to design doc § 8 still accurate. The "Open architectural questions for operator decision" section (lines 45-50) is now historically inaccurate — operator answered all 5 + 4 doc-internal opens. Cosmetic; can be cleaned up in a future doc-revision pass alongside the Phase 6 language tightening, or now if you want a clean read for the next operator who picks up the file. Not blocking.
