# Historical Fable 5 Candidate-Scope Live-Reasoning Ablation Receipt

## Status

This disclosed replay was run from implementation commit `c38cdd30`, after the causal-ownership packet and
budget-aware thinking changes but with every recognized-contract diagnosis and repair path disabled. A regression
test proved that both the initial fast path and the later repair-feedback fallback remained disabled. The sealed
fixture and all fixture digests were unchanged.

Historical reference commit: `b7afb8f3cd3eb0b86730c8ff73d200164ed51092`, `fix: split autotrader candidate scope
lanes`. The Fable 5 attribution remains user-attested historical evidence, not provider-authenticated same-task
output. This is a post-remediation disclosed ablation, not an untouched promotion-gate holdout.

## Result

- Score: **40/100**
- Sealed-final functional solve: **0/1**
- Diagnosis family: expected and retained `data`
- Planned owner: wrong, `trading/query_store.py`
- Expected owner: `trading/auto_trader.py`
- Retained changed files: none
- Public tests: **2/2 passed**
- Public plus repair-feedback tests: **2/3 passed**
- Fresh isolated sealed-final tests: **3/6 passed**
- Patch retained: false
- Deterministic contracts disabled: true
- Deterministic repair attempted: false
- Deterministic repair applied: false
- Live-reasoning-qualified: false
- Local model calls: **8**
- Model-call errors: **1** timeout
- Premium calls: **0**
- Wall time: **486.7 seconds**
- Verdict: `needs_improvement`
- Evaluation verdict: `disclosed_replay_failed`
- Git-normalized Markdown SHA-256: `4f68e41b03530e36f2d9c9fcff2a51ac6d76db2b3593545d4f6e81afb71c1748`
- Git-normalized results SHA-256: `493b3d414a9edd3bf81bad1a108f7535330000ba7a595799cfa1821ff4c9cadd`

## What Improved

1. The investigator returned one valid visible JSON response in **91.6 seconds** with hidden thinking disabled. The
   old untouched run spent 105 seconds in a failed hidden-thinking call and another 37.4 seconds on its compact
   retry.
2. Total local calls dropped from **10 to 8**, and model-call errors dropped from **2 to 1**.
3. The packet made the ownership error auditable: its leading hypothesis explicitly marked
   `trading/query_store.py` as owner and `trading/auto_trader.py` as context, while a competing hypothesis contained
   the reverse assignment.
4. The ablation switch prevented deterministic source operators from rescuing the generative path. The final
   result therefore measures the local reasoner, planner, editor, feedback loop, and sealed adjudication only.

## What Did Not Improve

1. End-to-end score and final solve did not improve over the authoritative untouched result: both were **40/100**
   and **0/1**.
2. The judge retained the wrong boundary owner. `QueryStore.query_scope` already exposed `or`, `user`, and `system`
   execution primitives, while `select_candidate_refs` in `auto_trader.py` chose the bad composite `or` mode. The
   caller owned selection policy, but the reasoner treated the callee as mutation owner.
3. The initial plan and first feedback repair both mutated only `trading/query_store.py`. No source proposal survived
   validation.
4. The saved diagnosis budget shifted downstream: an edit retry used 84.5 seconds, repair planning used 72.8
   seconds, and the second repair plan exhausted the case budget after 67.7 seconds.
5. Overall latency was effectively unchanged: **486.7 seconds** versus **489.4 seconds** untouched.

## Interpretation

The ownership packet and thinking policy improved observability and call efficiency, not complex-diagnosis
correctness. This result disproves any claim that the current live local stack is already Fable 5-class on this
mechanism. It also identifies the next general bottleneck: distinguish the component executing an existing
primitive from the upstream component choosing the wrong mode, ordering, multiplicity, merge, or lifecycle.

The caller/callee ownership-graph remediation was started only after this result was sealed and is not part of this
score. No runtime service, database, broker, Docker container, or live trading state was touched.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_LIVE_REASONING_ABLATION.md`
- `project_ws/AgentOps/fable5_historical_trading_scope_lane_pilot_live_reasoning_ablation.json`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_UNTOUCHED_RECEIPT.md`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_DISCLOSED_RECOVERY_RECEIPT.md`
- `tests/fixtures/autonomy_diagnosis_to_fix_fable5_trading_scope_lane_pilot/`
