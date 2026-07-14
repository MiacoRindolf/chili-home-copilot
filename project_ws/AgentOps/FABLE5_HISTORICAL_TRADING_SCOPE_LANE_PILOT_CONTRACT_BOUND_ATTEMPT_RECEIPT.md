# Historical Fable 5 Candidate-Scope Contract-Bound Attempt Receipt

## Status

This disclosed contract-disabled replay was run from commit `a691addb`. That version accepts a missing explicit
failed-test id only when every generated contract has valid postconditions, one unanimous selected owner, complete
prompt-obligation polarity, and no partially explicit failed-test mapping. Fixture digests were unchanged and no
deterministic contract diagnosis or repair was allowed.

## Result

- Score: **40/100**
- Sealed-final functional solve: **0/1**
- Diagnosis family: expected and retained `data`
- Final planned owner: **correct**, `trading/auto_trader.py`
- Retained changed files: none
- Public tests: **2/2 passed**
- Public plus repair-feedback tests: **2/3 passed**
- Fresh isolated sealed-final tests: **3/6 passed**
- Deterministic contracts disabled: true
- Local model calls: **8**
- Model-call errors: **1** case-budget timeout
- Premium calls: **0**
- Wall time: **487.9 seconds**
- Verdict: `needs_improvement`
- Evaluation verdict: `disclosed_replay_failed`
- Git-normalized Markdown SHA-256: `51891a232a2b0794f7b84fb876956f181c78f3dd220a0c7268d2267d813fe871`
- Git-normalized results SHA-256: `6b32d9785e5d7dbe2167b635ec1e29b470fb40938edc5eb946399cba34ca6903`

## Failure Chain

1. The initial plan and edit selected `trading/auto_trader.py`, but feedback validation made no retained progress.
2. The first repair draft split its contract owners between `trading/query_store.py` and
   `trading/auto_trader.py` under a one-file budget. The new canonicalizer correctly refused to invent unanimity or
   bind the missing failed-test id.
3. An adversarial review consumed 62.3 seconds but left the structurally invalid ownership split unresolved.
4. The second repair plan selected only `trading/auto_trader.py`, covered both prompt obligations and the explicit
   failed test, and skipped redundant review.
5. That valid plan reached the local editor, but only 26.9 seconds of the 480-second case budget remained. The edit
   timed out and the candidate group was rolled back.

## Interpretation

The unanimous multi-contract binding remediation worked as intended: it accepted the later valid plan and continued
to reject an ambiguous one. The remaining failure in this replay is budget scheduling and local edit synthesis, not
another reason to weaken ownership evidence. The next generic remediation reserves a future planning/edit window
instead of spending scarce budget reviewing a structurally invalid draft.

This remains failed disclosed evidence, not Fable 5 parity. No runtime service, database, broker, Docker container,
or live trading state was touched.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_CONTRACT_BOUND_ATTEMPT.md`
- `project_ws/AgentOps/fable5_historical_trading_scope_lane_pilot_contract_bound_attempt.json`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_BUDGET_RESERVED_ATTEMPT_RECEIPT.md`
