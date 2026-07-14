# Historical Fable 5 Queue-Priority Disclosed Recovery Receipt

## Status

The untouched 40/100 result and failure chain were committed before remediation. Generic source and reasoning
changes were then implemented in commit `54043ffd`, `fix: preserve protected refreshes at queue capacity`. The
sealed fixture and all fixture digests remained unchanged.

Historical reference commit: `b04ade0678b16daa74fe3491314c185088ea2933`, `fix: protect mesh refresh under queue
pressure`. The Fable 5 attribution remains user-attested historical evidence, not provider-authenticated same-task
output.

## Result

- Score: **100/100**
- Sealed-final functional solve: **1/1**
- Diagnosis family: expected and retained `state`
- Changed files: exact sole owner, `mesh_queue/repository.py`
- Telemetry distractor changed: no
- Public tests: **3/3 passed**
- Public plus repair-feedback tests: **6/6 passed**
- Fresh isolated sealed-final tests: **8/8 passed**
- Patch retained: true
- Deterministic diagnosis fast path: true
- Deterministic-only: true
- Live-reasoning-qualified: false
- Local model calls: **0**
- Premium calls: **0**
- Wall time: **4.9 seconds**
- Verdict: `needs_improvement`
- Git-normalized Markdown SHA-256: `12f2c741c0c900d77ca420e579d0b062d97f0e4217e7e44a250194e78bfe09e4`
- Git-normalized results SHA-256: `fe056dd96884321cad4bdef09200627f9166c4989e10899acff85fa672ad0c26`

## Generic Capability Added

1. Contract activation now distinguishes natural request-policy snapshot semantics from underscore-delimited domain
   identifiers. `brain_market_snapshots` plus audit language no longer injects the immutable-request-snapshot
   family, while genuine policy reload/generation requests remain recognized.
2. Protected and sheddable snake-case causes plus the age threshold are extracted from the incident rather than
   fixed to one fixture's variable names.
3. The state operator recognizes queue-cap, correlation-cap, pending-depth, correlation-count, and enqueue roles.
   It requires exactly one structural owner and leaves telemetry-only files untouched.
4. The repair moves correlation rejection before any mutation, replaces only at exact capacity, selects the oldest
   unlocked eligible row with stable id ties, preserves the payload, records audit/processed state, rechecks the
   fixed cap, and then uses the original enqueue path.
5. Both in-memory repositories and SQLAlchemy owners are supported. The ORM path uses ordered
   `with_for_update(skip_locked=True)` selection; the in-memory path models the same lock exclusion explicitly.
6. Ambiguous owners and partially applied queue-pressure repairs fail closed. Structural post-repair checks verify
   eligibility, ordering, exact equality, lock behavior, audit lifecycle, and cause/age policy before the proposal
   can close contract warnings.

A read-only probe against the complete pre-fix `b04ade06^` repository source selected only
`app/services/trading/brain_neural_mesh/repository.py`, generated valid Python, retained the fixed queue cap,
inserted the correlation-first and `SKIP LOCKED` boundaries, and closed all contract warnings. No historical source,
runtime service, database, broker, or live trading state was modified.

## Validation

- Full affected suites: **331 passed**, with the two existing SQLAlchemy/deprecation warnings.
- Natural policy-reload snapshot recognition and domain-identifier collision guard: passed.
- Alternate repository, cause, parameter, cap, and 45-minute policy names: passed.
- SQLAlchemy ordered `SKIP LOCKED` source shape: passed.
- Ambiguous-owner and partially repaired fail-closed guards: passed.
- Exact disclosed fixture operator and no-model benchmark paths: passed.
- Sealed final opened only after the zero-call model ledger was frozen.

## Interpretation

This is another non-wrapper system capability: recognized queue-state incidents can be repaired and verified without
calling any model. It is disclosed development evidence. The authoritative first encounter remains 40/100, and
the recovery earns no live-model reasoning credit. It does not prove unknown-mechanism transfer, authenticated
Fable 5 parity, or superiority.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_QUEUE_PRIORITY_PILOT_DISCLOSED_RECOVERY.md`
- `project_ws/AgentOps/fable5_historical_trading_queue_priority_pilot_disclosed_recovery.json`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_QUEUE_PRIORITY_PILOT_UNTOUCHED_RECEIPT.md`
- `tests/fixtures/autonomy_diagnosis_to_fix_fable5_trading_queue_priority_pilot/`
