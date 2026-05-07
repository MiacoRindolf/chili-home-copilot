# Cowork Review: bracket-intent-stale-label-cleanup

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-03_bracket-intent-stale-label-cleanup.md`
**Reviewer:** Cowork.
**Date:** 2026-05-03.

## Verdict

The code task executed cleanly. **But the CC_REPORT surfaced a finding that requires me to walk back the previous review's risk framing.** That walk-back is the most important thing in this document.

## What shipped (clean execution)

- Mirror writer + auto-transition both correct. 9/9 new tests pass; 7/7 prior emergency-repair tests still pass — no regression.
- Sweep `b68bf08a` populated `broker_stop_order_id` across the entire open-trade population on first post-flip sweep (one-shot backfill effect — exactly the right behavior).
- ELTX 1816 → `state='reconciled'`, mirror = `69f7c5b8…`. ✅
- IMTX 1818 → `state='reconciled'`, mirror = `69f53eaf…` (newly discovered broker stop). ✅
- Authority-contract canary (test #8) enforces no decision-time reads of `broker_stop_order_id` — good defensive measure.
- Two CRITICAL `auto_reconcile` log lines and matching `_g2_event` audit rows produced. Operationally visible.

The "advisory cache, not authority" contract is preserved. That part of the work is done correctly.

## Walk-back: the audit's risk framing was more accurate than I credited

In the previous Cowork review I wrote: **"Real exposure was ~$276, not ~$2,107. Six of seven were stale local labels on positions the broker had already protected."** The CC_REPORT for this task makes clear that conclusion was wrong, and I want to correct it on the record.

The 5 surviving `terminal_reject` rows (AIDX 1812, CCCC 1813, CRDL 1814, TLS 1821, VFS 1822) are NOT auto-transitioned to `reconciled` because the classifier returned `kind=missing_stop`, not `kind=agree`. The CC author traced why: the broker has working **limit-sell** orders (the original take-profit leg of the bracket), not stop-typed orders. From the classifier's perspective, `broker.stop_order_state is None`, so `broker_has_stop = False`. The auto-transition correctly does not fire.

The classifier is right. **A limit-sell at a higher target price is not downside protection.** If price drops, the limit doesn't trigger; the position falls without any defense.

This means:
- The audit's "$2,107 unprotected exposure" framing was **substantively correct** for those 5 positions.
- My previous walk-back ($2,107→$276 based on the "covered_by_existing_sell" finding) **misread the term as a protection signal**.
- The label `covered_by_existing_sell:protected_by_limit` (set in `bracket_writer_g2.py:781`) is misleading — it conflates "broker has a working sell" with "position is protected."
- The comment at `bracket_writer_g2.py:695-696` that says "the position is protected — skip placement entirely. The existing limit IS the exit; we don't need to add a stop on top of it" is the source of the conflation. It is wrong as written.

This is a meaningful retraction. ELTX/IMTX were genuinely repaired today; the remaining 5 are still exposed.

## What this means operationally

The 5 affected positions have:
- ✅ A take-profit limit at the broker (caps upside, locks profit if price rises to target)
- ❌ No stop-loss at the broker (no defense if price falls)

Today is Sunday 2026-05-03; US equity markets are closed. The exposure does not bite until Monday 2026-05-04 13:30 UTC market open. The operator has roughly 14 hours.

The codebase already has an opt-in flag for this exact case: `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1`. Flipping it tells `place_missing_stop` to:

1. Cancel the covering limit-sell.
2. Sleep 2s for the cancel to propagate.
3. Place the SELL_STOP.

This trades upside lock-in for downside protection — for positions trending negative or whose realized return is already disappointing, downside protection is the right call.

**Recommended operator action before Monday market open:**

```powershell
# 1. Add the override to docker-compose.yml broker-sync-worker.environment:
#    CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1
# 2. Restart the worker so the flag is picked up:
docker compose up -d --force-recreate --no-deps broker-sync-worker

# 3. Wait for the 6h emergency-repair throttle to expire (~04:01 UTC Mon),
#    then for market open at 13:30 UTC Mon. The next sweep after market open
#    will cancel the limit-sell and place the SELL_STOP for each of the 5.
# 4. Capture the sweep's `[bracket_writer_g2] cancelled N covering sell order(s)`
#    and `place_missing_stop` log lines as confirmation.
# 5. Optional: flip the flag back to 0 after the 5 are protected, to restore
#    the upside-lock default for future positions where that's the right
#    trade-off. Or leave it ON if you'd rather have stop-loss-by-default.
```

If the operator instead prefers manual broker UI action (cancel the limits + place stops by hand), that's faster and also fine — the live exposure is what matters, not the path.

## Open Questions — answers

1. **Cleanup for the 5 surviving terminal_reject rows.** Operator action via the flag flip above. After they get real stops and the next sweep classifies as `kind=agree`, the auto-transition path we just shipped handles them. No code change needed.

2. **Mirror_write log level for steady state.** Leave at info for now. Steady state is silent (no-op when local matches broker); the only noise is during ticker rotation. Revisit only if it becomes real.

3. **Is `kind=missing_stop` for limit-sell-only-coverage the right signal?** YES. This is the most important answer. The classifier is correct. The downstream writer's `covered_by_existing_sell` guard treating limit coverage as "protection" is the bug. The next task closes that semantic gap.

4. **`f8b-verification-soak-3` re-promotion.** Confirmed queued for on/after 2026-05-04 16:30 UTC. Will queue after the cover-policy clarification task lands.

## What I'd flag

- **The misleading framing in `bracket_writer_g2.py` is now a documented liability.** Future audits, operators, and Claude Code instances reading those comments + that label will repeat the mistake I made. Worth a small task to fix the framing alongside the operator's flag flip.
- **The full picture is consistent across two tasks.** The emergency-repair branch's `covered_by_existing_sell` skip + the stale-label-cleanup's discovery of `kind=missing_stop` is a complete diagnostic chain. The system told us exactly what's going on; we just had to read it correctly. Strategy archive integrity is good.

## Direction for next task

`bracket-writer-cover-policy-clarify` — small, surgical:

1. Rewrite the misleading comments at `bracket_writer_g2.py:680-696` and `:739-755` to accurately describe the trade-off (upside lock-in vs downside protection — NOT "the position is protected").
2. Rename the audit-emit reason from `covered_by_existing_sell:protected_by_limit` to `covered_by_existing_sell:no_stop_coverage` (or similar — the right name is "we deliberately chose to forgo stop-loss to preserve the limit; this is not downside protection").
3. Optional: emit a startup-time WARNING log when `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=1` AND `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=0` simultaneously, since that combination produces the silent "rejection storm avoided, but downside still uncovered" steady state.
4. Optional: add an admin-UI / status query that surfaces `state='terminal_reject' AND last_diff_reason LIKE 'covered_by_existing_sell%'` rows so operators can see them at a glance.

This task does not place broker orders, does not affect live placement decisions — it's documentation, logging, and a status surface. Low risk, high readability dividend.

After this lands, the queue resumes:

- `audit-unsupported-crypto-prefilter` (audit's HIGH #4 — small, ~170 wasted broker calls/day eliminated)
- `f8b-verification-soak-3` (re-promote on/after 2026-05-04 16:30 UTC)
- Other audit findings as appetite allows

`CURRENT_PLAN.md` does not need rewriting. The plan's broader shape is undisturbed.

## Memory update

The previous reference memory I wrote (`reference_bracket_intent_broker_stop_order_id_dead.md`) led with the framing "audit's '$2,107 unprotected' collapsed to $276." That framing is now wrong. Updating the memory after this review.
