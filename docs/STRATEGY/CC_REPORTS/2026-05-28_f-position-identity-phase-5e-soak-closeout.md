# f-position-identity-phase-5e-reporting-reader-soak

Date: 2026-05-28
Status: COMPLETE
Branch: `main`

## Executive Summary

Phase 5E soak is green. The Phase 5B/5C reporting reader has now seen fresh
post-mig-275 data and still has no hard linkage issues or pattern attribution
drift.

This clears the gate to prepare the rename plan. It does not mean the physical
rename should be executed blindly in the same step.

## Latest Probe

Manual run at 2026-05-28 09:35 PT:

```text
VERDICT_STATUS=READY_FOR_RENAME_BRIEF
VERDICT_REASON=fresh data clean: decisions=3, envelopes=3, closes=7
MIG275_APPLIED_AT=2026-05-26 16:03:52.455658
FRESH_DECISIONS=3
FRESH_ENVELOPES=3
FRESH_CLOSES=7
FRESH_CLOSE_MISMATCHES=0
HARD_LINKAGE_ISSUES=0
CLOSED_ROWS=310
MISMATCHED_ROWS=0
MISMATCHED_PNL=0.0000
```

The scheduled run on 2026-05-27 at 18:20 PT also emitted
`READY_FOR_RENAME_BRIEF`.

## Reporting Compare

30-day live-vs-research Phase 5C compare:

```text
envelope_pattern_groups: 26
decision_pattern_groups: 26
envelope_closed_envelopes: 310
decision_closed_envelopes: 310
mismatched_pattern_groups: 0
mismatched_closed_envelopes: 0
absolute_group_pnl_delta: $0.0000
null_decision_pattern_envelopes: 0
```

## Architect/Data-Science Read

The position identity read model is now semantically stable enough to plan the
rename. The data says the remaining Phase 5B/5C/5D repair chain worked:

- Hard linkage stayed at zero after fresh closes.
- Decision-pattern attribution matches legacy envelope-pattern attribution.
- New fresh data did not reintroduce bridge drift.

The next step is a rename dependency audit and dry-run plan, not the physical
rename itself. The repo still contains substantial legacy `trading_trades` and
`Trade` ORM surface area; the table rename must preserve old raw-SQL and ORM
consumers during transition.

## Acceptance

- At least one post-mig-275 entry/close cycle represented: yes.
- Reporting compare remains clean with fresh data: yes.
- No writer path changed: yes.

## Follow-Up

Proceed to Phase 5F rename audit and dry-run brief.
