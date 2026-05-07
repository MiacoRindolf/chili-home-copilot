# Cowork Review: audit-missing-stop-emergency-repair

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-03_audit-missing-stop-emergency-repair.md`
**Reviewer:** Cowork.
**Date:** 2026-05-03.

## Verdict

Code is healthy and the deploy was clean. The brief was executed faithfully — additive branch above the existing `state_gated_skip`, three-branch decision logic preserved, throttle implemented, all 7 regression tests green, flag-OFF default in code, manual operator-controlled flip via compose, sweep `a4006105…` observed and captured.

But the **material outcome inverts the audit's framing of the risk**. That's the headline of this review.

## Audit blast-radius correction

The 2026-05-03 audit claimed seven open Robinhood equity positions had no broker stop ("live position risk, not a paper-only issue"). Realized state at sweep:

| Category | Count | Notional | Resolution |
|---|---|---|---|
| Genuine missing stop | 1 (ELTX) | ~$276 | New broker stop placed: `69f7c5b8…` qty=25 stop=$11.0584 verified queued |
| Broker had working sells covering full qty (`covered_by_existing_sell`) | 5 (AIDX, CCCC, CRDL, TLS, VFS) | ~$1,800 | `bracket_writer_g2` safety guard correctly refused duplicate placement |
| Broker had working stop, classifier returned `kind=agree` | 1 (IMTX) | ~$31 | New branch correctly didn't fire |

**Real exposure was ~$276, not ~$2,107.** Six of seven were stale local labels on positions the broker had already protected.

The audit's classification was good (the `bracket_intent` rows really did say `terminal_reject` with NULL `broker_stop_order_id`), but it inferred broker state from local state. That inference was wrong on 6 of 7 rows — and the code path we just shipped is what made the gap visible because the writer's `covered_by_existing_sell` guard sees broker truth on every call.

This doesn't make the audit wrong — it makes it *load-bearing in a way the auditor didn't expect*. Without the new branch firing, we'd still be reading the local rows as exposure. Now we know.

## The structural finding underneath

The CC report names it: **`bracket_intents.broker_stop_order_id` is never UPDATEd by any code in the tree.** No assignment site exists. The reconciler trusts broker truth per-sweep, but the local mirror column has been dead since whenever the writer responsible was removed (or never wrote). That's the upstream cause of the "missing_stop" classification on positions the broker had already protected.

This is a real structural gap, not a bug specific to this incident. Every future audit that reads `bracket_intents` rows will hit the same false-alarm pattern unless we close the loop.

Two cheap follow-ups close it:

1. **Mirror writer for `broker_stop_order_id`.** Treat the column as advisory cache, not authority — broker truth stays load-bearing. Persisting from `BrokerView.stop_order_id` on each sweep would let local rows reflect reality without violating the "broker is authoritative" contract. Recommend YES, scoped as cache.
2. **`terminal_reject → reconciled` auto-transition** when classifier returns `kind=agree` on a subsequent sweep. Without this, the 6 protected-but-mis-labeled positions will fire `state_gated_skip` after their 6h throttle expires, indefinitely. That's noise that masks future signal.

These are CC report's Open Questions #1 and #2. Both deserve YES.

## Open Questions — answers

1. **Mirror writer for `broker_stop_order_id`?** YES, scoped as advisory cache. Read-only consumers in admin UI / audits. Don't promote to authority. The contract stays "broker is truth, local is mirror."

2. **Auto-transition `terminal_reject → reconciled` on `kind=agree`?** YES. Small reconciler change. Belongs in the same follow-up task as #1 — they share the call site.

3. **`covered_by_existing_sell` provenance — were the original "rejected" SELL_STOPs actually the ones now covering?** Worth checking, but lower priority. Hypothesis (a) — broker said reject, order actually landed — would be a different bug class than the one we just fixed and would shift FIX 51-53's threat model. Defer to a separate investigation; it doesn't block forward work.

4. **6h throttle duration.** Current data is consistent with "throttle isn't load-bearing because steady state is `kind=agree` after the next sweep." Don't tune yet. Revisit if the soak shows mistuning.

## What worked well in execution

Three things to capture for the project memory rather than let them blur into the next task:

- **Schema-reality discovery.** The brief said `closed_reason`/`updated_at`; the table actually has `exit_reason`/`exit_date`. Claude Code adapted to existing convention (cited `portfolio.py:148-160`) instead of inventing a new column. That's exactly the right judgment call — the brief is a guide, not a contract.
- **Throttle bump-before-call.** Bumping the throttle BEFORE `place_missing_stop` runs (so SKIPPED outcomes also lock for 6h) was a correct interpretation of "prevent retry storms." The CC author noticed the quirk and surfaced it as Surprise #4. Good operator discipline.
- **Clean operational sequence.** Code commit → image rebuild with flag OFF → flag flip in compose → forced-recreate → sweep observed. No bundling of code and flag in one commit; clean audit trail. (The flag-flip lived in the docs commit, separate from the code commit.)

## What I'd flag

- **The throttle locks the 5 `covered_by_existing_sell` rows for 6h.** That's correct given current intent semantics, but combined with the never-UPDATEd `broker_stop_order_id`, those rows will keep firing `state_gated_skip` after 6h. The follow-up task closes that loop; until then, expect noise in `broker-sync-worker` logs.
- **ELTX's `bracket_intents.broker_stop_order_id` is still NULL after the live placement** because no writer assigns it. Surprise #2. Same root cause as the false-alarm pattern. The next sweep will likely classify ELTX as `kind=agree` and the new branch's `decision.kind == "missing_stop"` guard will stop firing — which is fine, but cosmetically the local row stays out of sync. The mirror-writer follow-up fixes this too.
- **One unrelated thing surfaced:** pytest needed `-p no:asyncio` to collect because of a pre-existing pytest-asyncio AttributeError. CC noted it as a workaround, not introduced by this task. Worth a small hygiene task at some point but not urgent.

## Direction for next task

Three candidates with timing:

| Slug | Size | Why now | Window |
|---|---|---|---|
| **`bracket-intent-stale-label-cleanup`** (Open Q #1 + #2 together) | Medium | Closes the loop that just produced a $2,107→$276 false-alarm. Context is fresh. Fixes the root cause of all future audit false alarms in this domain. | Available now |
| **`audit-unsupported-crypto-prefilter`** (audit's HIGH #4) | Small | ~170 wasted broker calls/day, trivial fix (capability table or cached lookup pre-broker). High signal-to-effort. | Available now |
| **`f8b-verification-soak-3`** (preserved at `docs/STRATEGY/QUEUED/`) | Pure analysis | Re-promote when ripe. | On/after 2026-05-04 16:30 UTC |

**Recommended sequence: stale-label-cleanup → unsupported-crypto-prefilter → soak-3 when ripe.**

Reasoning:
- Stale-label-cleanup is the strongest follow-up to the work we just did. The structural gap is the reason the audit's risk estimate was 7x off, and we've just generated the perfect data to validate that closing the loop produces the right behavior. Waiting risks losing the context.
- Unsupported-crypto pre-filter is small; it can ride right after.
- Both fit comfortably before the 24h window for soak-3 opens.

`CURRENT_PLAN.md` does not need rewriting. The plan's broader shape — prove edge before live activation, fast-path remains paper — is undisturbed. The stale-label cleanup is hygiene that supports the plan, not a course correction.

## Memory update

Worth saving as a Reference memory: the `broker_stop_order_id`-never-UPDATEd structural gap, with the implication for future audits. I'll add it after the operator confirms direction.
