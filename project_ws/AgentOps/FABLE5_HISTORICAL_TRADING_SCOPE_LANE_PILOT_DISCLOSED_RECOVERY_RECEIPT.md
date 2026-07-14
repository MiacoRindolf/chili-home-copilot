# Historical Fable 5 Candidate-Scope Disclosed Recovery Receipt

## Status

The untouched 40/100 result and failure chain were committed before remediation. Generic source and reasoning
changes were then implemented in commit `107a4fa2`, `fix: split asymmetric candidate query scopes`. The sealed
fixture and all fixture digests remained unchanged.

Historical reference commit: `b7afb8f3cd3eb0b86730c8ff73d200164ed51092`, `fix: split autotrader candidate scope
lanes`. The Fable 5 attribution remains user-attested historical evidence, not provider-authenticated same-task
output.

## Result

- Score: **100/100**
- Sealed-final functional solve: **1/1**
- Diagnosis family: expected and retained `data`
- Changed files: exact sole owner, `trading/auto_trader.py`
- Query provider changed: no
- Public tests: **2/2 passed**
- Public plus repair-feedback tests: **3/3 passed**
- Fresh isolated sealed-final tests: **6/6 passed**
- Patch retained: true
- Deterministic diagnosis fast path: true
- Deterministic-only: true
- Live-reasoning-qualified: false
- Local model calls: **0**
- Premium calls: **0**
- Wall time: **5.7 seconds**
- Verdict: `needs_improvement`
- Evaluation verdict: `disclosed_replay_failed`
- Git-normalized Markdown SHA-256: `b1082ef10478dcd335d053ed69267b378586a26bcd375095afff79e4dbf5aa3f`
- Git-normalized results SHA-256: `6b5bc002637ced7964bb0bc094174b1eaacb334d43e8bfb693d0511e57bed938`

## Generic Capability Added

1. A scope-asymmetric query contract is recognized as a `data` boundary: the selector or orchestrator owns two
   scope-pure reads and the global selection policy, while the query provider remains an execution primitive.
2. Explicit-user and system-NULL lanes each receive a local capacity before one in-memory global cap is applied.
   A mixed OR predicate and limit-before-merge are rejected as non-equivalent shapes.
3. Candidate identity is deduplicated across lanes before the final cap. Recent-first mode normalizes naive and
   aware timestamps and uses stable id ties; id-first mode remains distinct.
4. Nonpositive limits short-circuit without issuing either query, preserving the original no-work contract.
5. Both a small provider-call selector and the historical SQLAlchemy AutoTrader selector are supported. The
   historical repair removes only the user-scope OR while retaining independent timestamp predicates and all
   downstream broker, risk, regime, evidence, and order-placement behavior.
6. Ambiguous ownership and partially applied split-lane repairs fail closed. Structural checks require both lanes,
   local limits, deduplication, mode-aware ordering, and one global cap before a proposal can close warnings.

A read-only probe against the complete pre-fix `b7afb8f3^` repository source selected only
`app/services/trading/auto_trader.py`, generated valid Python, left the query provider unchanged, and closed all
contract warnings. No historical source, runtime service, database, broker, or live trading state was modified.

## Validation

- Full affected suites: **336 passed**, with the two existing SQLAlchemy/deprecation warnings.
- Alternate selector, provider, scope, identity, timestamp, and limit names: passed.
- Mixed naive and aware recency ordering with stable ties: passed.
- Historical SQLAlchemy selector source-shape repair and compilation probe: passed.
- Ambiguous-owner and partially repaired fail-closed guards: passed.
- Exact disclosed fixture operator and no-model benchmark paths: passed.
- Sealed final opened only after the zero-call model ledger was frozen.

## Interpretation

This is a fourth non-wrapper system capability: recognized asymmetric candidate-query incidents can be repaired and
verified without calling any model. It is disclosed development evidence. The authoritative first encounter remains
40/100, and the recovery earns no live-model reasoning credit. The evaluation verdict remains failed because no
authenticated same-task Fable 5 run or accepted live causal-reasoning stage exists. It does not prove
unknown-mechanism transfer, Fable 5 parity, or superiority.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_DISCLOSED_RECOVERY.md`
- `project_ws/AgentOps/fable5_historical_trading_scope_lane_pilot_disclosed_recovery.json`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_UNTOUCHED_RECEIPT.md`
- `tests/fixtures/autonomy_diagnosis_to_fix_fable5_trading_scope_lane_pilot/`
