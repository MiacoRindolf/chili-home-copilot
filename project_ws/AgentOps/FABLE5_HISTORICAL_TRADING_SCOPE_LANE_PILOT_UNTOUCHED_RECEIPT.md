# Historical Fable 5 Candidate-Scope Trading Pilot Untouched Receipt

## Status

The first post-freeze protocol run of `python_split_candidate_scope_lanes` is complete. CHILI source was frozen at
`33a4038ce85be35f8d6a4e6e1a0597a9e5cf949d`; the fixture-only commit was `1317c7b0`. No CHILI source or fixture
file changed between validation and scoring.

Historical reference commit: `b7afb8f3cd3eb0b86730c8ff73d200164ed51092`, `fix: split autotrader candidate
scope lanes`. The user identifies the source conversation as Fable 5 work. This is user-attested historical
reference evidence, not provider-authenticated same-task Fable 5 output.

## Result

- Score: **40/100**
- Sealed-final functional solve: **0/1**
- Expected diagnosis family: `data`; retained family: `data`
- Expected owner: `trading/auto_trader.py`
- Selected files across attempts: `trading/query_store.py`, `trading/auto_trader.py`
- Retained changed files: none
- Public tests: passed
- Repair-feedback tests: failed
- Fresh isolated sealed-final tests: failed
- Patch retained: false
- Live-reasoning-qualified: false
- Local calls: **10 total, 8 successful, 2 timeouts**
- Wall time: **489.4 seconds**
- Premium calls: **0**
- Verdict: `needs_improvement`
- Git-normalized Markdown SHA-256: `2362c96d0fda9eb6a6eea7bc762816b22d88afe2aaac73e7b26cbe71b82e4a65`
- Git-normalized results SHA-256: `469e5d8e5fa36062b94b4acf5af4a39760a1b115d28d08d56896e2df1931a8e7`
- Checkpoint: removed only after atomic report/result writes

## Failure Chain

1. The first investigator call timed out. Its JSON retry identified the mixed predicate, narrow index, separate
   lanes, global merge, dedupe, and cap mechanisms under the correct `data` family, but left the conclusion
   inconclusive. The judge promoted only a provisional broad-scan claim without intervention evidence.
2. The initial plan assigned the split/merge responsibility to `trading/query_store.py`, the query provider, even
   though `trading/auto_trader.py` owned selection orchestration. Its contract table named both owners but granted
   edit authority only to the provider.
3. The editor merely rewrote the provider's existing `or` branch as two local list comprehensions that were still
   called through one `or` scope. Its search was stale, its full-file retry made no effective change, and the atomic
   group was rolled back.
4. Feedback exposed the exact two scope calls. The repair plan selected the correct AutoTrader owner but changed
   the family to `code` and marked one required scope postcondition as `forbidden`.
5. The repair edit issued user and system queries, but removed the zero-limit short circuit, concatenated lanes,
   omitted identity deduplication, ignored `recent_first=False`, and attempted unary minus on `datetime`. Public
   behavior regressed.
6. The compiler/public correction changed the expression to `-timestamp()` but still lacked mode-aware ordering,
   missing-time handling, dedupe, and the zero-limit guard. It remained public-red and was rolled back.
7. The second repair-plan call exhausted the case budget. The original broad-OR selector survived, and the sealed
   final correctly failed id-first merge order, per-lane capacity, and duplicate identity while the baseline mixed
   timestamp case happened to remain green.

## Interpretation

This fourth disjoint result strengthens the negative first-encounter evidence. CHILI's local reasoner could name
most of the mechanism, but ownership and executable synthesis still failed within a practical budget. Safety
behavior remained sound: stale/provider edits and public regressions did not survive, the final stayed sealed, and
no premium or trading runtime path was used.

The untouched score remains authoritative even if disclosed remediation passes. The fixture is current-agent-
authored after source freeze, so it measures post-freeze transfer but does not satisfy the independent-author
promotion gate.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_UNTOUCHED.md`
- `project_ws/AgentOps/fable5_historical_trading_scope_lane_pilot_untouched.json`
- `tests/fixtures/autonomy_diagnosis_to_fix_fable5_trading_scope_lane_pilot/`
