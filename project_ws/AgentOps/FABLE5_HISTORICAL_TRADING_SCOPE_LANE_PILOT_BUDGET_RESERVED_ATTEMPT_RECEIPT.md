# Historical Fable 5 Candidate-Scope Budget-Reserved Attempt Receipt

## Status

This disclosed contract-disabled replay was run from commit `a4c69bd3`. That version could stop after an unusable
investigator when a grounded boundary fallback existed, reserving judge budget for repair. In this run the local
investigator and judge both returned usable packets, so early stop correctly did not activate. Fixture digests were
unchanged and no deterministic contract repair was allowed.

## Result

- Score: **40/100**
- Sealed-final functional solve: **0/1**
- Diagnosis family: expected and retained `data`
- Planned owner: **correct**, `trading/auto_trader.py`
- Retained changed files: none
- Public tests: **2/2 passed**
- Public plus repair-feedback tests: **2/3 passed**
- Fresh isolated sealed-final tests: **3/6 passed**
- Deterministic contracts disabled: true
- Local model calls: **7**
- Model-call errors: **1** budget timeout
- Premium calls: **0**
- Wall time: **488.2 seconds**
- Verdict: `needs_improvement`
- Evaluation verdict: `disclosed_replay_failed`
- Git-normalized Markdown SHA-256: `39eb5a7475087627110a1c8a97cb6d90e70b6cb723397d9903ea0dc346008bbc`
- Git-normalized results SHA-256: `e88bd49920633c4fed0c27357983e29890c8df661e50317063bdcf52b78188d2`

## Failure Chain

1. Investigator and judge still proposed `query_store.py`, but the evidence gate marked that owner `challenged` and
   the planner correctly selected only `auto_trader.py`.
2. The initial edit issued two lanes and deduplicated by identity, but split the global capacity between lanes and
   omitted mode-aware global ordering. It was rolled back after feedback made no validated progress.
3. Repair planning and adversarial review produced one unanimous `auto_trader.py` plan covering both prompt
   obligations and the semantic source guard.
4. The generic contract canonicalizer only attached an explicit failed test ID when the draft had exactly one
   contract entry. Because this valid draft had three unanimous entries, the adapter rejected it without editing.
5. A second repair plan then exhausted the remaining case budget.

## Interpretation

Caller/callee ownership is now consistently corrected before editing. The remaining failure in this run was partly
repair synthesis and partly a generic harness restriction that discarded an evidence-backed plan. The subsequent
remediation allows unanimous multi-entry ownership to bind a missing failed-test ID while still refusing to
auto-complete any partially explicit failed-test mapping.

This remains failed disclosed evidence, not Fable 5 parity. No runtime service, database, broker, Docker container,
or live trading state was touched.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_BUDGET_RESERVED_ATTEMPT.md`
- `project_ws/AgentOps/fable5_historical_trading_scope_lane_pilot_budget_reserved_attempt.json`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_COMPACT_OWNER_ATTEMPT_RECEIPT.md`
