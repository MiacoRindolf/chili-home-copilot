# COWORK_REVIEW: broker-truth-self-heal

## Verdict

Cleanest task in this whole sequence. Brief executed faithfully, magic-number audit honest, live evidence shows the inverse-reconcile working exactly as designed. **14 positions self-healed on the first sweep**, including 5 unrelated crypto positions the brief didn't even target — a beneficial overshoot, not a deviation. 7 of the equity positions had real broker SELL_STOPs placed within the same minute, so they're not just back in CHILI's view, they're actively protected at the broker.

Today's bleeding has stopped. The user's "no constants and static decisions" principle is honored end-to-end in the shipped diff. Sub-branch 2 + the entire flap-guard apparatus from the previous task are gone. `emergency_close_all` no longer fires from any automated surface. The system can now see and correct broker-vs-DB drift in either direction. That's the structural shift the user asked for.

## Algo-trader lens

**What's good.** Inverse-reconcile is structurally generic — it doesn't filter by close reason, so it healed 5 unrelated crypto positions that nobody asked about (XRP-USD/XPL-USD/DOT-USD/HBAR-USD/SOL-USD, all closed by the older `broker_reconcile_no_exit_price` path with no execution-event history). Same shape, same fix, no special-case code. That's the principled outcome of "single rule, no allow-lists."

The freeze-instead-of-liquidate response to disconnect/drawdown is the right algo-trading default. If you can't observe prices, you can't responsibly liquidate at unknown prices; freezing entries while leaving existing positions for operator decision matches the discipline of every serious trading desk.

The conservative "ANY execution event = real broker activity → contradiction branch" check is a deliberate over-defer-to-operator. Zero false healings today, zero CONTRADICTION lines. The cost is narrow scope (next paragraph).

**What's narrow.** The `event_count == 0` check only auto-heals positions whose Trade row has zero `trading_execution_events` history. Today's 11 stuck positions all met that bar because their buy fills lived on previous Trade-row generations. But: if a future case occurs where a position's buy fill IS recorded on the current trade_id AND the trade gets wrongly auto-closed by some bug we haven't found yet, the inverse-reconcile will route it to the CONTRADICTION branch and require operator intervention. That's the over-defer cost. Acceptable for shipping; flag it as a known limit. The proper resolution is the position-identity refactor (separate brief) — Trade-row IDs become disposable, broker positions become the persistent identity, and "did this *position* close legitimately" replaces "did this *trade_id* close legitimately."

The bracket reconciler placed 7 broker SELL_STOPs in the same minute as the 14 reopens — broker-side actions on live money in an automated path. That worked because `CANCEL_COVERING_SELL=1` was hot. Without that flag, the writer would have hit `covered_by_existing_sell` and skipped, leaving the 9 reopened equities at the broker's covering-limit-sell prices instead of CHILI's calibrated stops. The flag flip from yesterday's accidental promotion was retrospectively load-bearing for today's outcome to actually be beneficial.

**What's deferred and worth tracking.** EKSO and ELTX. The broker no longer reports those tickers in `_live_tickers`. Two possibilities: (a) the operator manually closed them between noon and deploy, in which case the DB rows are correct (modulo Bug 2's lying exit_price baked in at noon — a now-frozen artifact since `emergency_close_all` won't be called automatically again), or (b) they liquidated some other way (margin, expiration). If (a), no action. If (b), operator needs to investigate. Worth one direct broker-UI check.

## Dev-architect lens

**What's good.** Magic-number audit is genuinely clean. The five `1e-9` tolerances match an existing precedent; the audit-trail string labels are non-behavioural; the `'closed','reconciled','terminal_reject'` list is a state-machine transition guard (the three states a closed-row's bracket_intent could be in), not a tuning parameter. That's the difference the principle requires: literal *categorisation* of state-space is fine; literal *thresholds* gating decisions are not. CC drew the line correctly.

Brief said two commits, two commits shipped. Brief said one fix commit covering all four changes, that's what landed. PROTOCOL Hard Rule 6 (one task = one logical commit) honored. The diff is reviewable as a single coherent change rather than a sequence of partials.

The deletion of yesterday's flap-guard machinery in the same commit as sub-branch 2 retirement is the right move — keeping the dead code around would have invited future drift. CC even noted migration 223's column orphans and explicitly punted the DROP to a separate hygiene ticket per the brief's instruction. Discipline.

The `event_count == 0` cross-check is a single SQL `COUNT(*)` query, not a structured fill-classification routine. That's defensible: when the schema doesn't carry a `side` column, inventing a string-list to guess SELL vs BUY would have been a magic-list violation. The conservative count check is the principled choice given the schema as-is.

**What's concerning.**

1. **The conservative count check leaves a gap that's structurally invisible.** If a future bug auto-closes a Trade row that DOES have a recorded buy fill, the inverse-reconcile will refuse with "CONTRADICTION" and the operator has to manually reconcile. That's safer than over-healing, but the narrow scope is an architectural limitation not just a styling choice. Worth a follow-up brief that introduces a `side` discriminator (or equivalent — even a deterministic SELL-vs-BUY parser of `payload_json`) so the inverse-reconcile can heal the broader case.

2. **Bracket reconciler placed 7 broker orders in the same sweep as inverse-reconcile fired.** Two automated subsystems acting on live money in the same minute, both gated by env flags. This worked. The risk surface is real: if any flag had been wrong (e.g. `MISSING_STOP=0`), the 9 reopened equity positions would have stayed unmanaged. CC verified the four bracket flags pre-deploy in the report. Going forward, post-deploy verification of broker-action-relevant flags should be standard for any task that re-opens or creates trade rows.

3. **The unauthorized `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` from yesterday is now a load-bearing operational invariant.** CC's recommendation to formalize-as-default rather than revert is the right call given today's deploy made the flag's effect visible (broker has CHILI-managed stops now), but the staged-vs-deployed boundary discipline is broken. Recommend the operator add a one-line `printenv` snapshot to the standard deploy verification, and document this flag's expected state in `docs/FAST_PATH_HANDOFF.md` or wherever the bracket flag inventory lives.

4. **EKSO/ELTX silent disappearance.** The brief expected 11/11 self-heals; got 9/11 because broker stopped reporting two tickers. The fix correctly didn't blanket-reopen, but if those positions were silently liquidated (margin call, dividend-related auto-action, etc.) without CHILI seeing the SELL fill, that's a separate observability gap. The DB now thinks they're closed at noon-prices via the lying-exit-price artifact, with no actual exit event recorded.

## Decisions for the operator

1. **`CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1`** — keep hot or revert? CC recommends keep hot. Today's deploy already used it. Reverting only affects future writer behaviour, not the stops already placed. **Recommend: keep hot, formalize in compose default + document in handoff doc.** A separate "formalize the deployed-vs-staged flag inventory" task can clean up the discipline gap.

2. **Kill switch reset.** Still active from noon's trigger; new entries are blocked. Now that 9 positions are reopened with real broker stops, deactivation is operator-judgement: ready to resume new entries from the autotrader, or want a quiet day before resuming?

3. **EKSO/ELTX investigation.** Quick broker-UI check: are those positions actually closed at the broker, or were they just temporarily missing from the API response? If actually closed, what's the realized P/L (vs the lying $0 in the DB)? If still alive but not reported, that's a broker_sync coverage bug worth flagging.

4. **The position-identity refactor.** This is the deep architectural fix. Pragmatic patch shipped today; the patch's narrow scope (event_count == 0 only) is the artifact of *not* having position identity. The proper fix introduces a layer above Trade rows so buy fills associate to (broker, ticker, account) rather than to ephemeral trade_id integers. **Recommend: queue as the next initiative-shaped brief, not as a one-shot NEXT_TASK** — it'll touch enough files that it benefits from a CURRENT_PLAN-level scoping conversation.

## Recommended next move

Operator pre-actions:
- Decide on the cancel-covering-sell flag (recommend keep hot).
- Decide on kill switch reset (recommend reset once you're satisfied with the 9 reopened positions' stops).
- Verify EKSO/ELTX in the broker UI; investigate if they should still be open.

Then the next brief, in priority order:

1. **Bug 4 — `emergency_close_all` should either submit broker SELLs or be removed entirely.** Now that nothing auto-calls it, the urgency dropped. Still worth resolving so a manual operator invocation does what its name says. Smaller brief than today's; can ship in a single tight commit.

2. **Position-identity refactor.** The deep fix that closes today's narrow scope. Initiative-sized. Worth a CURRENT_PLAN scoping conversation before NEXT_TASK.

3. **Schema hygiene batch.** Migration 223's orphan column + any other accumulated-orphan columns. Low priority, batch when convenient.

The fast-path crypto scalping initiative (current `CURRENT_PLAN.md`) can resume after #1 lands or in parallel — your call. Today's safety overhaul didn't touch that work.
