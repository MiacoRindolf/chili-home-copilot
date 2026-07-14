# Historical Fable 5 Candidate-Scope Compact-Owner Attempt Receipt

## Status

This disclosed contract-disabled replay was run from commit `d32de6d0`. It replaced repeated full caller/callee
profiles with one compact structural owner hint and seeded the heuristic fallback from that hint. The earlier
40/100 live ablation and 25/100 verbose-graph attempt were committed before this run. Fixture digests were unchanged.

## Result

- Score: **25/100**
- Sealed-final functional solve: **0/1**
- Diagnosis family: `state`, expected `data`
- Planned owner: **correct**, `trading/auto_trader.py`
- Context-only primitive: **correct**, `trading/query_store.py`
- Retained changed files: none
- Public tests: **2/2 passed**
- Public plus repair-feedback tests: **2/3 passed**
- Fresh isolated sealed-final tests: **3/6 passed**
- Deterministic contracts disabled: true
- Deterministic repair attempted/applied: false/false
- Local model calls: **7**
- Model-call errors: **5** timeouts
- Premium calls: **0**
- Wall time: **488.5 seconds**
- Verdict: `needs_improvement`
- Evaluation verdict: `disclosed_replay_failed`
- Git-normalized Markdown SHA-256: `0fd98eb5396eea56ea09ed321ed16267a565561ace12caa15d457685e50af9e1`
- Git-normalized results SHA-256: `12676ec76e8f29d551d76fd36fbb80c2dc0ac82b3ebbbeca4f8d4719335a754b`

## Improvement And Remaining Failure

1. Investigator and judge calls still timed out, but the grounded fallback selected `trading/auto_trader.py` as
   owner and `trading/query_store.py` as context. The planner preserved that assignment.
2. The first edit correctly replaced the composite `or` call with one `user` call and one `system` call, each
   locally bounded, without changing the provider.
3. The edit remained incomplete: tuple-level deduplication and concatenation did not provide identity dedupe,
   timezone-safe global recent ordering, stable id ties, or distinct id-first mode before the global cap.
4. Four diagnosis call timeouts consumed 300 seconds. The first repair-plan call then exhausted the remaining case
   budget before it could use feedback to complete the merge semantics.

## Interpretation

Compact boundary preflight fixed the observed ownership error, but not end-to-end correctness or latency. The next
general change is stage-level early stopping: if investigator output is absent and a source-grounded fallback exists,
skip the judge retry path and reserve budget for executable repair feedback. This result remains failed disclosed
development evidence, not Fable 5 parity.

No runtime service, database, broker, Docker container, or live trading state was touched.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_COMPACT_OWNER_ATTEMPT.md`
- `project_ws/AgentOps/fable5_historical_trading_scope_lane_pilot_compact_owner_attempt.json`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_CALLER_CALLEE_ATTEMPT_RECEIPT.md`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_LIVE_REASONING_ABLATION_RECEIPT.md`
