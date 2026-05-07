# Cowork Review: audit-unsupported-crypto-prefilter

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-04_audit-unsupported-crypto-prefilter.md`
**Reviewer:** Cowork.
**Date:** 2026-05-04.

## Verdict

Clean execution. Layered defense (static whitelist + probe fallback at autotrader; static-only at writer), correct minimal scope, ZEC-USD traceback storm closed, ARB-USD caught as a bonus on the first sweep. 21 tests pass. Approving.

This is the third task in a row where CC's discovery step caught something my brief was wrong or out-of-date about (the prior two: the schema-reality `closed_reason → exit_reason` swap, and the `db.commit()` inside `upsert_bracket_intent`). The discovery-before-fix pattern is paying off — without it, CC would have built work the codebase didn't need.

## What I'd flag

- **Brief was outdated about the autotrader.** I described the autotrader as catching `crypto_not_supported_on_robinhood:` *post-broker* and asked CC to move the check upstream. FIX A-3 (2026-04-29) had already done that — the autotrader has been pre-filtering for 5 days. CC's actual contribution was the static-whitelist layer on top of the existing probe + the brand-new writer prefilter. The brief's "Step 2a" became a layering exercise; "Step 2b" (writer) was the real new work. Memory of the autotrader's funnel was stale on my side; correcting in the next brief that touches that area.

- **Layered defense is the right shape.** Layer 1 (static whitelist) is the dominant path — O(1) frozenset membership, no broker call, deterministic. Layer 2 (5-min cached probe via the existing helper) only fires when Layer 1 rejected, so we self-heal if Robinhood adds a pair before the static list catches up. Failure mode is "false unsupported," which is loud and operator-visible, not silent broker-side errors. CC chose this consciously — good judgment on the asymmetric failure-mode trade-off.

- **Writer is single-layer (static only) — also right.** The writer fires on already-mirrored intents (rows that another path created). The autotrader's two-layer is the gate for *new* alerts. By the time we reach the writer, the row exists; this is cleanup. Probe fallback at the writer would add per-sweep latency for a vanishingly rare case. If false-rejects show up in production, adding the fallback is a one-line change.

- **ARB-USD is a real positive signal.** It's another imported position with `broker_source='robinhood'` whose ticker isn't on Robinhood's crypto list. Same root cause as ZEC-USD: somewhere upstream, a non-Robinhood-supported crypto ended up labeled as Robinhood. Out of scope per the brief, but worth surfacing as a follow-up: investigate WHY these tickers are getting `broker_source='robinhood'` in the first place. Until then, the prefilter is the symptom-fix.

- **Sweep duration improvement.** 8-12s → 2.3s is real money for the broker-sync-worker. Less time per sweep = less idle-in-tx pressure (memory has prior incidents around this) = more headroom for other work in the same loop. Quiet operational dividend.

## Open Questions — answers

1. **`pre_broker:` vs `broker:` reason prefix convention.** Don't backfill. Old rows are opaque audit text; pattern-matching queries can `OR` the two prefixes. The new convention reads cleaner and makes the funnel-accounting boundary explicit. Going forward, audit-team queries should `WHERE reason LIKE 'pre_broker:%' OR reason LIKE 'broker:%'`.

2. **Whitelist maintenance cadence.** Skip the formal quarterly review process. The probe-layer self-heal in the autotrader catches Robinhood adding a pair (Layer 2 returns True even when Layer 1 says False; autotrader admits the order). The writer's false-unsupported on a newly-added pair would surface as a SKIPPED log line that an operator notices and prompts a list update. Reactive maintenance is cheaper than scheduled review for a list this stable.

3. **Should the writer get a probe fallback?** Not pre-emptively. The current static list covers all 17 actively-supported pairs; the probe-layer absence at the writer is intentional (one-shot broker latency × every sweep is a worse trade-off than rare manual list update). If a real false-reject shows up at the writer, the fix is a 5-line addition. Don't build it now.

4. **`CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` activation.** Still pending. CC correctly used `docker compose restart` (not force-recreate) to NOT pick up that env var. The operator's plan to activate it via `docker compose up -d --force-recreate --no-deps broker-sync-worker` after Monday 13:30 UTC market open remains the next live-broker step. Unchanged.

## What I'd note for the next briefing

- **Investigate WHY ZEC-USD and ARB-USD have `broker_source='robinhood'`.** Probable root cause: a path that didn't propagate `broker_source` correctly from the original entry venue, OR these positions were manually opened in Robinhood and CHILI mis-labeled the broker_source on import. Worth a 15-minute diagnostic eventually, but not the most pressing thing on the queue.

- **Write a `_pre_broker_audit_reason` helper.** Not urgent. Right now the new `pre_broker:venue_unsupported_crypto:<BASE>` is in-line at one call site. If we add more pre-broker filters (the audit's HIGH #2 on venue-truth wiring will introduce another), having a small constant set of reason-string formatters would keep the audit-row vocabulary clean. Park as background hygiene; don't pull forward.

## Direction for next task

`f8b-verification-soak-3` is the next-up — preserved at `docs/STRATEGY/QUEUED/f8b-verification-soak-3.md` and gated on or after **2026-05-04 16:30 UTC** (the briefed 24h post-F8b-deploy window). At time of writing (~05:10 UTC), that's about 11 hours away. I'll re-promote it to `NEXT_TASK.md` at the window opening, not before — running it at sub-threshold n is explicitly discouraged in the original brief.

In the meantime:

- **Operator restart timing for the pending flag.** The `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` env-var change in `docker-compose.yml` is still pending. After Monday 13:30 UTC equity market open, run `docker compose up -d --force-recreate --no-deps broker-sync-worker`. That triggers the cancel-then-place sequence on the 5 imported positions (AIDX/CCCC/CRDL/TLS/VFS) plus EKSO if it's still in scope, during market hours, atomic-ish. If you do it before market open the cancel might succeed but the SELL_STOP placement could reject outside hours — the partial-state risk window I flagged earlier.

- **The bracket-loop work is finally closed.** Five tasks shipped today (emergency-repair, stale-label-cleanup, cover-policy-clarify, stop-price-live-sync, unsupported-crypto-prefilter). The trading system's bracket-intent path went from "broker stops vanish for 1-2 days, label cache is dead, framing misleads readers, mirror is frozen, unsupported crypto produces tracebacks" to "broker stops repaired, mirror live, framing honest, sync continuous, unsupported tickers gracefully blocked." That's a complete loop on a single audit's findings.

- **After soak-3 lands.** Audit's HIGH #2 (venue-truth shadow log dormancy) is the next residual finding. It's the last big item from the 2026-05-03 audit that hasn't been addressed. After soak-3, recommend queuing `audit-venue-truth-writer-wiring` to close that out.

`CURRENT_PLAN.md` does not need rewriting. The fast-path-stays-paper, prove-edge-before-live posture is intact. The bracket-loop work was hygiene around production trades, parallel to but not affecting the fast-path measurement program.
