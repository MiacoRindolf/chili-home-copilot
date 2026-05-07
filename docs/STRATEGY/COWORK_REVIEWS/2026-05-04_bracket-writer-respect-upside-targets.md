# COWORK_REVIEW: bracket-writer-respect-upside-targets

## Verdict

Brief executed faithfully. The auto-cancel code path is gone (not just gated — the orphan ~50 LOC was deleted), the env flag is flipped and verified live, the pending-decision surface is wired through the writer + reconciler + admin endpoint, and 6/6 new test scenarios pass. Zero new broker actions during deploy, the 9 SELL_STOPs from yesterday remain in place. Magic-number audit is clean: the only literals added are float-equality tolerances inherited from prior code conventions and audit-trail strings.

The discipline is the strongest of the four tasks shipped today. CC honestly surfaced two design judgement calls in the Surprises section (state-machine token choice, evaluator silence on the 5 affected tickers) instead of papering over them.

**Operator's "satisfy me first" bar:** writer no longer cancels operator-authored profit targets. Confirmed at code level (auto-cancel path deleted), env level (flag is 0), runtime level (zero new cancellations in 30-min post-deploy window). The structural failure mode that produced today's 5 cancellations cannot recur.

## Algo-trader lens

**What's good.** The pending-decision surface is the right mechanism. It captures the conflict at observation time (broker state, brain state, current price all snapshotted into one JSONB row) and waits for operator input. The reconciler emits the discrepancy on every sweep so it stays visible. Three resolution choices map to three real strategies: keep upside (accept no downside), swap for stop (accept no upside), trailing-stop (compromise). Honest enumeration.

The Step 5 viability evaluator's gate is structural — `kind=='agree'` AND `broker has stop AND no target AND no pending decision yet`. No frozen list of tickers, no allow-list of close-reasons. Any future trade in this state gets the same evaluation. That generalization matters.

The conservative-defer pattern (return None when `fetch_quote` returns None or when brain target ≤ current price) honors the operator's principle: no signal ≠ negative signal. The cost is that the 5 cancelled-limit tickers got zero pending-decision surfaces — see Concerns.

**What's narrow.** `compute_bracket_intent` runs with default brain context (`regime='cautious'`, no ATR) inside the evaluator. The brain's output is conservative by default; for post-rally tickers that have already moved against the original target, the conservative-cautious target may already be at-or-below current price. CC's Open Question #2 flags this and recommends a follow-up that exposes `regime` / `atr` from the trade's `indicator_snapshot` to the evaluator. Worth queuing as a small follow-up if operator wants the evaluator to surface more candidates.

**What's deferred and worth tracking.** The 9 reopened equity positions have downside protection (SELL_STOPs alive) but no upside take-profit on the broker's books. The pending-decision surface is the mechanism for surfacing replacement candidates; it has not surfaced any yet. Operator can either (a) accept this as the conservative default, (b) hand-craft pending_decision rows via the admin endpoint to force a replacement consideration, or (c) wait for follow-up that enriches the evaluator's brain context.

## Dev-architect lens

**What's good.** Two-commit boundary respected (one fix, one CC report). Magic-number audit is the cleanest of any task in this sequence — every literal is enumerated and justified, and every new threshold genuinely derives from observable system state. The `_has_trailing_stop_placement_helper()` probe is a clean way to make the option list dynamic instead of hardcoded — when a future task adds a `place_trailing_*` helper, the option appears automatically.

The pending-decision data model uses existing `payload_json` JSONB column, no migration needed. Forward-compatible with Phase 1 of the position-identity refactor (the `pending_decision` shape will move to `position`-keyed instead of `bracket_intent`-keyed but the JSON structure stays).

Async admin endpoint is the right call given `await request.body()` — even though existing admin routes are sync, the new code's correctness wins over surface-style consistency. Future autopilot-settings UI integrations should follow this pattern.

50 LOC of orphan auto-cancel code DELETED rather than just gated. That's the level of cleanup the operator's "vibe-coded, needs proper refactoring" framing calls for.

**What's concerning.**

1. **`accepted_no_stop` state was specified in the brief but `reconciled` was used instead.** CC's reasoning is reasonable — introducing a new state-machine token mid-task carries regression risk, and the consequence is encoded in `last_diff_reason='pending_decision_resolved:keep_target'`. Forward defensive check for `accepted_no_stop` exists in the Step 5 evaluator's skip gate. But: the audit trail now mixes "keep_target operator decision" and "agree-after-broker-truth" under the same `intent_state='reconciled'` value. Reporting / dashboard queries that want to know which positions have explicitly opted into "no stop" need to grep `last_diff_reason`. Acceptable; would be cleaner with the new state. Future hygiene.

2. **The Step 5 evaluator produced zero pending-decision rows for the 5 cancelled-limit positions.** The brief expected at least one. CC's Surprise #2 honestly attributes this to brain-context conservatism + possible yf-breaker fetch_quote None. It's the no-magic-numbers principle in action (silent defer rather than false-positive surface), but it means the operator's "replace if viable" intent is currently expressed as "don't replace anything." Visibility gap: there's no telemetry on how many evaluator runs deferred vs ran-to-completion.

3. **No upside take-profit orders at the broker for any of the 9 reopened equity positions.** Today's auto-cancel cascade removed the 5 covering limits; 4 of the 9 (EKSO/ELTX/IMTX/PED) had earlier cancellations. The pending-decision surface is the mechanism to address it; it hasn't surfaced anything. Whether that's "correct conservative behavior" or "needs richer brain context" is operator judgement.

4. **Async/sync mismatch in admin routes** is minor but a real maintenance signal. If the autopilot-settings UI introduces several more JSON-body endpoints, they'll all want `async def`. Worth noting in the Phase 7 design that this is now the convention.

## Decisions for the operator

1. **Are you satisfied?** Operator's stated bar: "fix the stuff above and satisfy me first" before kill switch reset. The bug is fixed (writer no longer cancels profit targets). The remediation surface (pending-decision) is live but currently silent. If "satisfied" means "the bug can no longer recur" → yes, ship the kill switch reset whenever ready. If "satisfied" means "the 5 cancelled targets get replacement candidates" → not yet, the evaluator's silence needs a follow-up.

2. **Kill switch reset.** Two paths:
   - **Reset now.** Autotrader resumes new entries. The 9 existing positions are protected on the downside (stops alive). Upside is unprotected on the broker; brain manages exits via stop_engine if/when it fires.
   - **Hold reset until evaluator surfaces candidates.** Queue a follow-up that enriches the evaluator's brain context (consume `indicator_snapshot.regime` / `atr` from the trade row instead of defaulting). After that, if pending_decisions appear, you choose `keep_target` / `replace_with_stop` per ticker. Then reset.

3. **The 5 cancelled limits.** Operator already accepted as one-time loss. The evaluator's silence means CHILI is honoring "don't force replacement" — which IS the principled outcome. If operator wants explicit visibility into "why didn't a candidate appear for AIDX," that's the follow-up evaluator-context task.

4. **EKSO/ELTX P/L cleanup.** Still outstanding from the earlier review. -$71.80 combined of misreported P/L on trades 1815 and 1816. One-time SQL update if you want clean books.

## Recommended next move

Two paths, operator picks:

**Path A — minimal additional work, ship to next initiative.**
- Operator decides on EKSO/ELTX P/L (clean or accept).
- Operator resets kill switch.
- Cowork drafts the position-identity refactor design doc (next NEXT_TASK).
- Fast-path scalping initiative resumes after Phase 4 of the refactor lands.

**Path B — improve the evaluator's signal-to-noise first, then ship.**
- One small NEXT_TASK: `bracket-evaluator-richer-brain-context` — pass `indicator_snapshot.regime` + ATR into `compute_bracket_intent` from the Trade row, add deferral-count telemetry, optionally surface a "manual force re-evaluate" admin endpoint.
- Operator confirms pending-decision rows appear (or honestly don't) for the 5 affected positions.
- Then operator resets kill switch + Cowork drafts the position-identity refactor doc.

I lean toward Path A. The evaluator's conservative silence is correct given today's market context (post-rally tickers, conservative default brain context, possibly-broken yfinance). Operators usually want fewer surfaces, not more. If silence persists past a couple sessions, Path B becomes obvious; if it doesn't, no work needed.

## Status of CURRENT_PLAN

`docs/STRATEGY/CURRENT_PLAN.md` already reflects the position-identity refactor as the active initiative (updated after broker-truth-self-heal review). No changes needed from this review. The 5 design questions in CURRENT_PLAN now have operator answers (captured in the prior chat turn, propagating to the design doc).

## Status of NEXT_TASK.md

CC marked DONE. File can be left as-is until the next NEXT_TASK is staged.
