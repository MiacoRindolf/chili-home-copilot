# Historical Fable 5 Queue-Priority Trading Pilot Untouched Receipt

## Status

The first post-freeze protocol run of `python_protected_refresh_queue_priority` is complete. CHILI source was
frozen at `9d4663778bce6ed17f78f7c617874e12e22044f7`; the fixture-only commit was `0cd0d05b`. No CHILI source or
fixture file changed between validation and scoring.

Historical reference commit: `b04ade0678b16daa74fe3491314c185088ea2933`, `fix: protect mesh refresh under queue
pressure`. The user identifies the source conversation as Fable 5 work. This is user-attested historical reference
evidence, not provider-authenticated same-task Fable 5 output.

## Result

- Score: **40/100**
- Sealed-final functional solve: **0/1**
- Expected diagnosis family: `state`; retained family: `state`
- Expected and planned owner: `mesh_queue/repository.py`
- Retained changed files: none
- Public tests: passed
- Repair-feedback tests: failed
- Fresh isolated sealed-final tests: failed
- Patch retained: false
- Live-reasoning-qualified: false
- Local calls: **9 total, 6 successful, 3 timeouts**
- Wall time: **491.2 seconds**
- Premium calls: **0**
- Verdict: `needs_improvement`
- Git-normalized Markdown SHA-256: `5e99a2ec0102d4c7fe7bc449dc8c8dbe82202d4ff9d00072568f6dcd48c80c3b`
- Git-normalized results SHA-256: `f682fef698b581b4c69715ff87c4c16e877e723ba8a1128c7f3fc60ab8bdcb17`
- Checkpoint: removed only after atomic report/result writes

## Failure Chain

1. Both investigator attempts timed out. The judge returned valid JSON and the correct broad `state` family, but
   only the generic claim that state drift caused saturation. It did not establish the exact-capacity replacement,
   eligibility, lifecycle, and rejection-order boundary, so the conclusion remained observational and inconclusive.
2. Contract extraction falsely activated the unrelated immutable request-snapshot family because the domain token
   `brain_market_snapshots` appeared near audit language. The repair plan therefore attached an async
   authorization-snapshot postcondition to the queue owner even though that mechanism was absent.
3. The first plan still chose the correct repository owner and described the main replacement intent. Its edit,
   however, considered every old unlocked pending cause sheddable, allowed every incoming cause to trigger shedding,
   used aware-minus-naive datetime arithmetic, omitted deterministic id tie-breaking, wrote a nonexistent
   `audit_metadata` attribute, and performed the global mutation before the correlation rejection gate.
4. Public validation caught the widening because an ordinary full-queue enqueue could now destroy stale work. CHILI
   rolled the patch back, preserving the original public behavior.
5. Feedback then showed the two protected calls still returned `-1`. The first repair plan misdiagnosed this as id
   allocation and even marked the required `event_id == 4` postconditions as `forbidden`.
6. The repair edit wrote audit data into payload and a processed timestamp, but still omitted protected/sheddable
   cause checks, correlation ordering, exact-full versus over-cap handling, and fail-closed no-eligible behavior. It
   also returned `-1` for every below-cap enqueue through an incorrect `else`. Validation regressed and rollback
   removed it.
7. A second repair plan pivoted narrowly to timezone handling while retaining the unrelated immutable-snapshot
   contract. The final editor call exhausted the case budget, leaving no patch. The sealed final correctly failed
   locked-row replacement while preserving the baseline rejections.

## Interpretation

This is a third disjoint negative result against present Fable 5 replacement readiness. CHILI found the owner and
broad family, and its rollback controls were effective, but it could not maintain a complete mutation contract
across priority, age, cause, lock, capacity, correlation, audit, and time boundaries. It also exposed a source of
reasoning contamination inside CHILI: substring-based contract activation can inject an unrelated mechanism into
the plan.

The untouched score remains authoritative even if disclosed remediation passes. The fixture is current-agent-
authored after source freeze, so it measures post-freeze transfer but does not satisfy the independent-author
promotion gate.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_QUEUE_PRIORITY_PILOT_UNTOUCHED.md`
- `project_ws/AgentOps/fable5_historical_trading_queue_priority_pilot_untouched.json`
- `tests/fixtures/autonomy_diagnosis_to_fix_fable5_trading_queue_priority_pilot/`
