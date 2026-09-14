# Task [9]: preserve protection while refreshing an unsent exit limit

When the bid falls between freezing an exit intent and cancelling its protective stop, the frozen sell limit can be above the current bid. The literal-post guard then correctly rejects that limit after protection has already been cancelled. Task [9]'s planner records BJDX 20293 and LBGJ 22135 examples, and a rejected replacement stop followed by repeated handoff blocks. Those historical counts are recovered Claude measurements; this continuation did not re-query that cohort.

The recovered implementation supersedes only a never-finalized limit intent, while the same deadman remains the durable owner transport, then freezes the same close identity and quantity at the current ladder price. A later pulse revalidates broker truth before cancelling or posting. Marketable frozen limits remain unchanged. Emergency authority records the actual frozen price passed to the broker.

The aftermath path permits retirement of an exact broker-inert handoff after a successor or replacement stop was proven not transmitted or rejected before acceptance. Existing claim, account, identity and quantity conditions still apply. Broker partial fills are not converted into no-transport proofs.

During author verification, a forced failed price-supersession write reproduced a protective cancel. The continuation fixes that fall-through: `deadman_frozen_limit_reprice_not_certified` defers the pulse before cancel/POST. Its regression test verifies an unchanged durable handoff, no cancellation, no order submission, and a later successful repricing under the same close identity.

| Binding | Value / derivation |
| --- | --- |
| Stale frozen sell limit | Above this pulse's executable bid, using the same comparison as the literal-post guard |
| Replacement limit | Existing exit ladder and sell-price formatter; no new ladder coefficient |
| Numeric equality | Existing `max(1e-9, bid * 1e-8)` floating-point comparison epsilon |
| Durable refusal | No protective cancellation and no successor POST during this pulse |
| Retirement | Exact inert transport proof and broker/local quantity agreement |

Validation on base `4f75f0a0203083ac096d35a3519fa2348c87e438`, reserved PostgreSQL database `chili_rossbench26_test`:

- Recovered baseline: 80 tests passed.
- Negative test before the fix: failed because the scripted broker received one deadman cancellation.
- Final focused and direct-neighbor set: **141 passed**, 489.43 seconds. Files: `test_deadman_close_handoff_price_supersession.py`, `test_alpaca_deadman_close_handoff.py`, `test_exit_verdict_f_review_fixes.py`, `test_deadman_close_handoff_verb_supersession.py`, `test_exit_pre_place_handback.py`.
- Existing configuration escape-sequence and SQLAlchemy table-cycle warnings were reported. The scripted broker exercised submission/cancellation behavior; no PAPER lane or bridge was changed or restarted.

This is author verification, not either required independent adversarial review or program verification. Review must include the two-commit supersede/refreeze boundary and continuing downward bids across successive pulses. The patch retains that continuation boundary; it does not establish an exit-latency bound or solve task [10]. No profitable-backside classifier, parent-wave selection or replacement of ordinary G's 255-print window is included. No merge or deployment is claimed.

Provenance: recovered Claude task-9 snapshot, followed by isolated current-main integration, failed-write regression and correction by Astra/Codex. Original Claude worktree was preserved.
