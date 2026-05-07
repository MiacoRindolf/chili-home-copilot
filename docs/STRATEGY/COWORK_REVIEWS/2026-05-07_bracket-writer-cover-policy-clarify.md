# Cowork Review: bracket-writer-cover-policy-clarify (Phase 2 of f-thread-tail-2026-05-07-2)

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-07_bracket-writer-cover-policy-clarify.md`
**Reviewer:** Cowork.
**Date:** 2026-05-07.

## Verdict

**Phase 2 SHIPPED via DRAIN-BY-ACKNOWLEDGMENT. APPROVE.** The brief was effectively a no-op: CC's audit confirmed every Step 2.x requirement was already shipped on 2026-05-03 (operator's parallel session) and reinforced by the 2026-05-04 `bracket-writer-respect-upside-targets` rewrite. CC produced a verification report mapping each brief checklist item to the live shipped artifact rather than fake-shipping a duplicate. That's the right CC artifact for this case — the audit trail is preserved and the QUEUED entry is closed honestly.

This is the cleanest shape for a "your brief was already done" outcome: surface it explicitly, verify each requirement against the production code, ship the report, mark the QUEUED placeholder closed-by-acknowledgment.

## What Claude Code did right

1. **Brief-checklist-to-shipped-artifact mapping.** Each Step 2.x sub-item got a paragraph naming the file + line number where the requirement is satisfied. Future readers grep-ing the QUEUED entry find the answer in one hop instead of having to re-derive whether the work landed.

2. **Caught the snapshot-vs-current divergence.** The brief specified renaming `:protected_by_limit` → `:no_stop_coverage`. CC found `protected_by_limit` does not exist anywhere in the production code (zero `grep` matches). The brief's "before" state was a snapshot of the code at brief-write-time that the 2026-05-03 + 2026-05-04 rewrites had already moved past. The actual shipped label is `existing_target_present_no_stop` (more verbose, more accurate). CC named this directly rather than papering over it.

3. **Explained the architectural improvement that supersedes the brief.** Brief asked for clearer comments on the auto-cancel branch. The 2026-05-04 `bracket-writer-respect-upside-targets` rewrite **retired the auto-cancel branch entirely** and replaced it with a `pending_decision` admin-endpoint flow. The clarification work is fully encompassed by the rewrite: the operator now SEES the conflict in a structured pending-decision row and chooses via `POST /api/admin/bracket-decisions/<id>` rather than living with the auto-cancel default. CC named that as the strictly-more-honest framing.

4. **Verified what could be verified cheaply, named what couldn't.** Helper-level startup-warning tests (4 cases) verified PASS in 0.96s. DB-bound tests (4 cases × ~75s/test truncate) were not re-run. CC explained the cost/value tradeoff inline — the source files have been byte-stable since 2026-05-03 and the truncate cycle adds soak cost without information value. That's the right calibration.

5. **Closed all 3 brief Open Questions.**
   - **Replacement label**: brief proposed `:no_stop_coverage`. Shipped value is `existing_target_present_no_stop`. Closed in favor of the shipped label.
   - **Admin route or SQL-only**: shipped Option A (the JSON endpoint). No further action.
   - **broker-sync-worker startup hook**: confirmed broker-sync-worker is a daemon script (not a FastAPI app) and doesn't have its own startup hook; the warning fires from chili main and that's sufficient because chili is the operator's interaction surface.

## What I'd push back on (none, this run)

Zero pushback. Drain-by-acknowledgment is the right outcome for a brief whose work was already shipped.

## CC's open question for me

**QUEUED hygiene** — CC observed this is the second QUEUED entry the thread-tail brief drained where the implementation had already shipped (the f8b verification was the genuinely-needed Phase 1; this Phase 2 was acknowledgment-only). Suggests an audit of `docs/STRATEGY/QUEUED/` to find any other entries whose work landed via parallel-session ships and need to be marked DONE-by-acknowledgment.

**Answer**: agreed. Worth a brief `f-queued-hygiene-audit` next time the operator wants me to drain backlog. The grep pattern is straightforward: for each QUEUED brief, grep for its load-bearing identifiers (renamed labels, new function names, new endpoint paths) in current production code. If they exist as the brief asked, the brief is effectively shipped — promote-and-acknowledge instead of promote-and-re-implement. **Don't promote it as NEXT_TASK proactively** — wait for a quiet moment between substantive briefs.

## Cookbook updates from this run

1. **Drain-by-acknowledgment is a valid CC outcome.** When a QUEUED brief turns out to be already-shipped, the right CC artifact is a verification report that maps the brief's checklist to the shipped artifacts, not a no-op ship or silent skip. Future readers want to know the QUEUED entry was actually closed; silent skipping leaks audit trail. Promote this as protocol-wide guidance.

2. **Re-promotion of QUEUED briefs needs a precondition check.** Before promoting a QUEUED brief (especially older ones from days/weeks back), `grep` for the brief's load-bearing identifiers in production code. If they exist, the brief is shipped — promote it as drain-by-acknowledgment, not as fresh implementation. Cheap to check; saves CC from doing duplicate work.

3. **Brief snapshots can drift from production reality between queueing and promotion.** This brief was queued 2026-05-03; the implementation shipped the same day; the QUEUED entry was promoted 4 days later. The brief's "before" state didn't match production at promotion time. The cookbook fix (above) catches it: precondition grep before promotion.

## Status update

Both phases of `f-thread-tail-2026-05-07-2` are complete and approved.
- Phase 1 (`f8b-verification-soak-3`): genuine analysis ship; recommendation = pivot to F9. Reviewed separately.
- Phase 2 (`bracket-writer-cover-policy-clarify`): drain-by-acknowledgment. Verification map filed.

The `f-thread-tail-2026-05-07-2` jumbo is the cleanest closure of the QUEUED-pre-existing-unblocked backlog. What remains in QUEUED/ now is the placeholder cluster (PROMOTED markers for shipped briefs) plus the deferred-on-trigger follow-ups. No genuine pending work.
