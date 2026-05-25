# COWORK_REVIEW: f-position-identity-phase-5b-soak-and-reader-parity

**Date:** 2026-05-23
**CC_REPORT:** [`2026-05-23_f-position-identity-phase-5b-soak-and-reader-parity.md`](../CC_REPORTS/2026-05-23_f-position-identity-phase-5b-soak-and-reader-parity.md)
**Session duration:** ~7 min (45 min budget). One commit (`e2dc1ff`), docs-only.

## TL;DR

Approved. All three probes are green on the live linkage path; pattern-585 PnL pinned at +$521.11 vs the 2026-05-22 baseline (matches to the cent); the future-insert trigger held through three new entries today. CC went one step further than the brief asked and operationalized the brief's prose "boring soak" criterion into a multi-day measurable bar. Concur with the recommendation to extend the soak by 2–3 days before flipping to Phase 5C.

## What's good

**Plan-gate discipline.** CC produced a plan that was more detailed than my brief required: probe order + rationale, triangulation extras (row counts, recency timestamps, regclass check), and a concrete "boring soak" operationalization with multi-day + close-event criteria. That extra effort is exactly the right insurance for the next decision point (Phase 5C cutover) where being wrong is costly.

**Direct answer to the marquee question.** Pattern 585 holds at $521.11 with 87/87 closed envelopes — bit-identical to the 2026-05-22 CC report's baseline. Pattern 537 moved $82.09 → $81.28 with one new closed envelope ($−0.81 ≈ expected single-trade contribution), which is the proof-of-life signal the Phase 5B read model is exercising the close-path properly. CC explicitly called the drift out in the Surprises section per my plan-response ask. Good follow-through.

**Triangulation data is the right shape.** The 7-day decision-activity histogram (3/6/4/18/22/4/2 entries 2026-05-17 → 2026-05-23) and the matched `MAX(entry_date)` between `trading_decisions` and `trading_trades` together prove the future-insert trigger from Phase 5A is still active and locking new entries onto the decision bridge. That's the load-bearing question for Phase 5B's longevity — and it's been answered with concrete numbers, not assertions.

**Docs-only commit.** 2 files changed; report added; NEXT_TASK marked DONE; zero code touched. Brief said no code change, no migration, no behavior change — CC respected all three. The 67-row dust-row gap (688 trades − 621 decisions) is correctly characterized as intentional legacy debt (`entry_price <= 0 OR quantity <= 0`), not a Phase 5B miss.

## Answers to CC's open questions

**Q1 — Soak duration before Phase 5C.** Agree with the report's recommendation: extend 2–3 more daily probe runs before Phase 5C. Today's single-cycle confirmation is strong but a one-day sample can hide intermittent failures in the close path. The cost of two more probes is ~7 min CC time × 2 days = trivial; the cost of a Phase 5C cutover that hits a regression two weeks later is significant. Concrete trigger for Phase 5C re-promotion: three consecutive days where all three hard linkage counters stay at 0 AND at least one envelope closes during each window. CC explicitly recorded that criterion; it's now the binding gate.

**Q2 — First Phase 5C reader candidate.** Start with the **P&L dashboard's pattern-by-pattern aggregation** — whatever `loadPerfDashboard` (in `brain-trading-desk.js`) is currently pointing at against `trading_trades`. Reasoning: (1) its output shape is closest to `trading_phase5b_pattern_decision_performance` (per-pattern PnL + count), so the parity comparison is one column-mapping table not a multi-row join debugging exercise; (2) the dashboard is operator-facing but read-only, so a bad migration would show up immediately at the dashboard layer without affecting any decisioning or execution path; (3) the brain runtime tab redesign that just shipped already routes that section through the Research drill-down, so any UX-side debug iterations are now isolated to one tab. Secondary candidates if (1) turns out to be too aggressive on shape change: the "tradeable patterns" surface (`brain-tradeable-section`) and the cycles-tab activity timeline. I'll name the specific reader in the Phase 5C brief when it gets written.

**Q3 — Historical debt threshold.** Agree, set a soft +10/day alert. Implementation: in the next soak-day probe brief, add a "delta vs prior day" column to Probe B's output. If `historical_broker_envelope_missing_position` increases by more than +10 in a 24h window, flag YELLOW in the report's "Boring soak interpretation" section and do not recommend Phase 5C promotion. Absolute level (currently 106) is not the gate; growth rate is. If we ever decide to actually clean up the historical debt — close out the prior envelopes' position links via a one-shot backfill migration — that's a separate task and probably waits until after Phase 5C has stabilized.

## What I'd flag

Nothing material. The session was uneventful in the best way — which is what a soak should be.

One observation worth noting for the file-discipline side: the `corrupt_dust_rows = 67` figure (entry_price ≤ 0 or quantity ≤ 0) has been stable across the 2026-05-21 → 2026-05-23 window. If at some point an operator considers a data-hygiene pass to retire those rows, the cleanest moment is right after Phase 5C cutover stabilizes — the new readers will already be ignoring them via the helper API's `valid_trades_missing_decision` filter, so the visual gap (688 − 621 = 67) becomes invisible to any reporting surface and only the raw `trading_trades` row count carries the legacy. No urgency; flagging only because today's report makes the issue legible.

## Recommended next moves

1. **Day-2 soak probe** (next NEXT_TASK): re-run the same three probes plus the new "delta vs 2026-05-23 baseline" column for historical-debt. Should ship as a thin brief that points at this report as the prior-day baseline.
2. **Day-3 soak probe**: same shape; only re-promote Phase 5C when both day-2 and day-3 stay green AND at least one envelope closes during one of those windows.
3. **Draft Phase 5C reader brief in parallel** (no rush): identify the exact `loadPerfDashboard` call path + its current `trading_trades` query so the migration brief is ready to ship the moment the soak passes the multi-day bar.
4. **Two queued follow-ups from the runtime-tab redesign** remain in `docs/STRATEGY/QUEUED/` (`f-brain-runtime-drawer-reduced-motion`, `f-chili-env-pin-pytest-asyncio`) — both small, both green-lit by the operator in this session's question, both can be picked up by CC opportunistically between soak days.

## Verdict

Ship. The Phase 5B semantic layer is reading boring exactly as designed; the multi-day soak window the brief implies is now the only remaining gate before Phase 5C.

— Cowork (interactive)
