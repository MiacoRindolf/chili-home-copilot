# Historical Fable 5 Candidate-Scope Caller/Callee Attempt Receipt

## Status

This disclosed contract-disabled replay was run from commit `6540f484`, after adding a bounded caller/callee source
graph and ownership challenge. The prior clean live-reasoning ablation was already sealed at 40/100 before this
remediation. The fixture and all fixture digests remained unchanged, and no deterministic contract diagnosis or
repair path was allowed.

## Result

- Score: **25/100**
- Sealed-final functional solve: **0/1**
- Diagnosis family: `clock`, expected `data`
- Planned owner: wrong, `trading/query_store.py`
- Retained changed files: none
- Public tests: **2/2 passed**
- Public plus repair-feedback tests: **2/3 passed**
- Fresh isolated sealed-final tests: **3/6 passed**
- Deterministic contracts disabled: true
- Deterministic repair attempted/applied: false/false
- Local model calls: **7**
- Model-call errors: **5** timeouts
- Premium calls: **0**
- Wall time: **486.2 seconds**
- Verdict: `needs_improvement`
- Evaluation verdict: `disclosed_replay_failed`
- Git-normalized Markdown SHA-256: `cd359362ad84019708baf2fed8a06bea2a438e815dc0de9c35708bd98912d175`
- Git-normalized results SHA-256: `577d68f065c7f52387ca453a98f5f562c5b705394a5a36ba7e87b80db75b6de9`

## Failure Chain

1. The full structural profile increased the investigator prompt from about 19.5K to 20.6K characters and the judge
   prompt from about 25.8K to 25.9K characters.
2. Investigator, compact investigator retry, judge, and compact judge retry all timed out. The heuristic fallback
   selected `clock` rather than the candidate-scope `data` boundary.
3. The planner then proposed a timezone change in `trading/query_store.py`; the edit added the UTC offset twice to
   an already timezone-safe timestamp path.
4. Feedback remained red, and the first repair plan exhausted the remaining case budget.

## Interpretation

The ownership graph is structurally useful and passed its caller-versus-primitive unit contracts, but serializing
the full graph into repeated local-model prompts made the end-to-end system worse. The next remediation must keep
the graph local, derive one compact owner hint, omit the verbose profile from model prompts, and seed the heuristic
fallback with that hint when the investigator times out.

This attempt is negative development evidence, not a recovery and not a Fable 5 parity result. No runtime service,
database, broker, Docker container, or live trading state was touched.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_CALLER_CALLEE_ATTEMPT.md`
- `project_ws/AgentOps/fable5_historical_trading_scope_lane_pilot_caller_callee_attempt.json`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_LIVE_REASONING_ABLATION_RECEIPT.md`
