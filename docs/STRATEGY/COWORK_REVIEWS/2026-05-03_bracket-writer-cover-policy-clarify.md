# Cowork Review: bracket-writer-cover-policy-clarify

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-03_bracket-writer-cover-policy-clarify.md`
**Reviewer:** Cowork.
**Date:** 2026-05-03.

## Verdict

Clean execution. The CC author caught one more rewrite site than I named in the brief, surfaced the operator's mid-task flag flip respectfully, and explicitly held back from self-authorizing the worker restart. 8/8 new tests pass, 16/16 prior tests still pass. Approving.

The task ran via the daemon during a brief window when this was the active `NEXT_TASK.md` (between when I queued it and when the operator picked option 2 for the stop-price diagnostic, which led me to overwrite NEXT_TASK with `bracket-intent-stop-price-live-sync`). The work shipping out of order isn't a problem — the cover-policy-clarify and stop-price-live-sync tasks are independent. The strategy archive is consistent.

## Open Questions — answers

1. **Replacement label `:no_stop_coverage`.** Accept. Reads well in `last_diff_reason LIKE 'covered_by_existing_sell:%'` queries and pairs with the precondition. No follow-up rename.

2. **Admin route under `/api/admin/...`.** Accept. JSON for diagnostic data, paired-context guard matching 13 prior precedents. Right call.

3. **Warning hook at `scripts/scheduler_worker.py:main()`.** Accept. The broker-sync-worker is the process that exercises the writer's covered-by-sell branch, so the boot-log signal is operationally relevant there.

4. **Flag-state-only warning vs row-count.** Accept the flag-state-only choice. Warning's job is to flag the combo, not enumerate exposure; the new admin endpoint is the right surface for per-row data. Don't couple boot-time warning to DB connectivity.

5. **Restart timing.** This is the only Open Q I'd push back on with a recommendation. Operator should NOT restart `broker-sync-worker` tonight (Sun 2026-05-03 evening UTC). Sequence:
   - Emergency-repair throttle on the 5 stuck intents expires ~04:01 UTC Monday.
   - Between 04:01 UTC and 13:30 UTC (US equity market open), each sweep would fire `place_missing_stop` on the 5. With `cancel_covering_sell=1` ON, that runs: cancel limit → sleep 2s → place SELL_STOP.
   - The cancel will likely succeed off-hours (broker typically accepts cancels during the close). The SELL_STOP placement is more uncertain — Robinhood may reject equity stops outside market hours, in which case the limit-sell is gone but no stop is in place. ~9.5 hours of partial-state exposure overnight.
   - Restarting after Monday 13:30 UTC collapses that window: cancel-then-place runs entirely within market hours, atomic-ish.

   The new code is cosmetic-only (label rename, warning, admin endpoint). Nothing is bleeding waiting on it. Wait until Monday post-13:30 UTC to restart.

## Surprises worth carrying forward

- **Surprise #1 (third rewrite site).** The misleading "the position is protected" framing existed in three places in `bracket_writer_g2.py`, not the two the brief enumerated. The CC author caught the third (the inline persist-side comment) and rewrote it. This is the kind of judgment call that's exactly what we want from the executor. Brief was a guide, code was the contract.
- **Surprise #3 (zero rows carry old label).** The persistence concern in the brief's Step 1.3 was moot because `bump_last_observed` overwrites `last_diff_reason` every sweep. Worth knowing for future label-rename tasks: persisted-label propagation in `bracket_intents.last_diff_reason` is naturally short-lived, so renames are forward-only without backfill.

## What I'd flag

- **Unpushed commits.** Local is 4 commits ahead of `origin/main` (today's emergency-repair, stale-label-cleanup, this clarify task, and their flag-flip docs commits). Daemon committed but didn't push. Not urgent if the operator is the only consumer of the remote; surface for awareness.
- **The QUEUED file `docs/STRATEGY/QUEUED/bracket-writer-cover-policy-clarify.md` is now redundant.** The work is shipped. I'll leave it for the strategy archive — it's a true historical record of what was queued — but flagging that future readers may double-take if they only see the queued copy without the CC_REPORT alongside.

## Direction for next task

`NEXT_TASK.md` already holds `bracket-intent-stop-price-live-sync` (the structural sync fix triggered by today's diagnostic that `bracket_intents.stop_price` was frozen at entry-time while `trade.stop_loss` had moved). That task is independent of the worker restart — touches `stop_engine.py` and `bracket_intent_writer.py`, no broker calls, no worker dependency.

Order of operations:

1. Operator runs `claude` to ship the sync fix. Independent of restart timing.
2. Operator restarts `broker-sync-worker` Monday after 13:30 UTC. Brings up cover-policy-clarify code AND sync fix code AND the cancel-covering-sell flag in one motion. Cancel-then-place on the 5 runs during market hours.
3. After both deploys settle, the queue resumes: `audit-unsupported-crypto-prefilter`, then `f8b-verification-soak-3` re-promotion at the 16:30 UTC Monday window.

`CURRENT_PLAN.md` does not need rewriting.
