# COWORK_REVIEW: f-phase3-stop-bleed

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-16_phase3-stop-bleed.md`
**Operator decision captured this review:** Path A on the monthly DD breaker (keep `n >= 30` floor; wait for attributed-history bake; flip `CHILI_MONTHLY_DD_BREAKER_ENABLED=1` only after the helper returns a non-None threshold).
**Reviewer:** Cowork (Senior Quantitative Trading Architect)
**Date:** 2026-05-16

## What's good (algo-trader lens)

1. **The brief's "no magic constants" rule held under pressure.** The walk-forward result was uncomfortable — the breaker doesn't trip on the April-22 trough — and the easiest escape was to lower the `n` floor or paste a static dollar threshold. CC flagged the methodology conflict back instead. That's the protocol working.
2. **R1 + R2 settings-sourced fee slack and staleness threshold** (commit `6553a1a`) closed the obvious magic-number temptation in the pre-flight cash check. Tests (`test_d4_fee_slack_uses_settings`, `test_d4_stale_cache_allows_through_with_warning`) prove the helper actually reads from settings, not from a hidden default.
3. **D6 conflict-resolution shows the right instinct.** The sentinel substitution (commit `69de691`) was rejected at test-run when the FK violated — and instead of papering over it (e.g., inserting an id=-1 row in `scan_patterns`), CC pivoted to widening the validator's allow-set conditionally on `strategy_proposal_id IS NOT NULL` (commit `1f1cbd2`). That's a structurally correct fix: NULL is allowed when there's a proposal context that justifies missing pattern attribution; NULL is still rejected when the autotrader's own placement path forgets to set it.
4. **Test coverage is real, not theatre.** 41/41 tests pass, the CRDL-style update regression case is in there, and CC discovered + fixed two test-infra issues (missing `scan_patterns` seed, exit_price validator sign bug) without smuggling them into the production diff.

## What's concerning (algo-trader lens)

1. **The 7d rejection histogram still carries pre-fix data.** The 05:48Z snapshot today shows `54 NameError`, `48 INVALID_ARGUMENT`, `830 Insufficient balance`, `41 stop_not_below_entry` — all pre-fix because no container restart has happened yet. We cannot claim D2/D3/D4 are validated until a post-restart histogram bakes for ≥24h.
2. **D5 (stop_loss producer fix) is still 41/week.** The 26-hits-across-12-files producer-grep tells me this isn't one bug — it's a structural fragility. `auto_trader_rules.py:915` rejects the bad orders so capital isn't at risk, but every rejection is a missed entry and a noise source in our diagnostics. Surfacing this as its own brief next.
3. **Walk-forward findings are bigger than just the breaker.** The fact that we have **only 20 distinct CHILI-attributed close-days** in 67 calendar days tells us something about cadence: the attribution gap (`no_pattern` cohort = 210 trades / −$1,560) is dominating CHILI's own emission rate. Path A is the right call for the breaker, but the underlying signal is that **the attribution pipeline is leaking more than half our trade volume into untyped territory**. That belongs in the next strategic priority, not just a breaker watch.

## What's concerning (dev-architect lens)

1. **The CC_REPORT itself was excellent — but the "Next steps (operator)" block at the bottom is the kind of thing that should have been a daemon dispatch.** Per the standing feedback ("deploy via daemon, don't defer to operator"), the container restart is daemon-runnable. Cowork (me) is queuing it now, not flagging it back to the operator. Calling this out so the protocol stays consistent: CC reports next steps; Cowork dispatches them.
2. **The `_scan_pattern_id_from_proposal` extractor open question is worth a small brief.** CC's note: "Currently it parses only the JSON keys. Making the extractor smarter would mean better attribution downstream." This is exactly the kind of work that would chip away at the `no_pattern` cohort, which is item 3 above. Cross-reference: this is upstream of the same problem the monthly-DD breaker hits when it filters out the legacy-cleanup rows.

## Path A — decision and arm-up protocol

**Decision:** Path A. Keep the `n >= 30` floor in the empirical lower-bound helper. No methodology change to the breaker. No flip of `CHILI_MONTHLY_DD_BREAKER_ENABLED=1` until the helper returns a non-None threshold.

**Why:**

- The 30-day floor isn't arbitrary; it's the smallest sample where a Gaussian lower-bound is even loosely defensible without additional structure (heavy-tail correction, EVT, etc.). Lowering to 20 would force us to choose between a fragile σ-estimate (small-sample Gaussian with no adjustment) or a heavier methodology that needs its own validation.
- Path A is data-conservative. Arming the breaker on 20 days of history that bridges the **pre-Phase-3 + post-Phase-3 boundary** would be borrowing pre-fix bleed data to set a post-fix risk gate. Bad practice.
- Per the operator brief's "no magic constants" working principle, this is the principled call.

**Arm-up watch protocol:**

1. Daily scheduled task (`phase3-monthly-dd-breaker-arming-watch`) reports `n_distinct_close_days` from CHILI-attributed history. Will be queued separately.
2. When n_distinct ≥ 30, the helper will start returning a non-None threshold. At that point I'll write a one-paragraph CURRENT_PLAN update flagging the breaker is data-ready.
3. Operator decides whether to flip `CHILI_MONTHLY_DD_BREAKER_ENABLED=1`. The breaker still ships disabled — the flip is your call after seeing the helper's first non-None output and the proposed K-sigma value at that point.
4. ETA at current cadence (~5 distinct close-days/week × 10 more needed): mid-June 2026.

## Open items for next Cowork session

1. **Surface the alerts.py `_scan_pattern_id_from_proposal` extractor improvement** as a QUEUED brief. Goal: chase pattern context through `strategy_proposal_id → strategy_proposals` link when `signals_json` doesn't carry it. Acceptance: reduce `no_pattern` cohort share of new trades from today's ~47% (210/444) to <20% over a soak window.
2. **D5 producer fix** as its own QUEUED brief. Start with `scanner.py` + `pattern_imminent_alerts.py` per CC's recommendation; expand only if those don't account for the bulk of the 41/week.
3. **NULL-cohort retroactive demotion question.** Memory `project_2026_05_16_evidence_fidelity_activations.md` flagged that the NULL cohort lost $1710 in 30d. With D6 in place, future NULL-attributed trades are blocked at the model layer. But what about the 210 historical no_pattern rows that already lost us $1,560? Should we demote any patterns that were silently feeding them? Needs a discovery probe before action.

## Verdict

PASS. Phase 3 shipped clean. Path A confirmed. The interesting work moved upstream into the attribution layer; that is now the next strategic priority.
