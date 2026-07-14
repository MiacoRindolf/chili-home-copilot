# Historical Fable 5 Trading Pilot Untouched Receipt

## Status

The first post-freeze protocol run of `python_short_paper_direction` is complete. CHILI source was frozen at
`ae5c0fb84e991ad93482276a6571738fa93d7d91`; the fixture-only commit was
`28732e80`. No CHILI source or fixture file changed between fixture validation and scoring.

Historical reference commit: `a9e5ea2b70970373fe4af3befd714d4ac05309fc`, `Preserve short direction in
paper auto entry`. The user identifies the source conversation as Fable 5 work. This is user-attested historical
reference evidence, not provider-authenticated same-task Fable 5 output.

## Result

- Score: **25/100**
- Sealed-final functional solve: **0/1**
- Expected diagnosis family: `data`; retained family: `code`
- Expected owners: `trading/paper_trading.py`, `trading/portfolio_risk.py`
- Planned owners: both correct; retained changed files: none
- Public tests: passed
- Repair-feedback tests: failed
- Fresh isolated sealed-final tests: failed
- Patch retained: false
- Live-reasoning-qualified: false
- Local calls: **11 total, 10 successful, 1 case-budget timeout**
- Wall time: **491.8 seconds**
- Premium calls: **0**
- Verdict: `needs_improvement`
- Checkpoint: removed only after atomic report/result writes

The score came only from baseline-final failure, preserved public behavior, prompt-contract closure, and premium
independence. Diagnosis, exact changed-file set, patch retention, and final tests all failed.

## Failure Chain

1. The local reasoner identified the two relevant mechanisms and the correct two owner files, but selected `code`
   and inverted short protective-stop polarity. It claimed a short stop must be below entry; the incident evidence
   and reference contract require a short protective stop above entry and target below entry.
2. The initial coordinated edit changed the already-correct short validation to the wrong polarity and globally
   flipped `risk_per_share` instead of making it direction-aware. It did not normalize and propagate direction
   through default stop, sizing, telemetry, and persisted trade.
3. Feedback then exposed the missing `direction` parameter and zero entered shorts. The next plan correctly stated
   `entry - stop` for long and `stop - entry` for short and again selected both owners.
4. The editor added the parameter and side-aware risk formula, but inserted geometry code outside the function,
   referenced undefined `stop_price` and `entry_price`, emitted an identity replacement, and omitted complete
   normalized direction propagation.
5. Two bounded compiler corrections and one public-regression correction did not restore a valid implementation.
   CHILI rolled the attempt back because syntax/public validation regressed.
6. The second repair-plan call reached the frozen case budget and returned no edit authority. The original source
   was retained, and the sealed final correctly failed.

## Interpretation

This is direct negative evidence against current Fable 5 replacement readiness on a representative historical
trading improvement. Safety behavior was good: no malformed patch survived, public long behavior stayed green,
the final oracle remained sealed until all model calls ended, and no premium or trading runtime path was touched.

Reasoning and editing behavior were not good enough. CHILI failed a side-polarity contract that the prompt made
numerically explicit, could not serialize a coherent two-file Python repair, and exhausted almost the full local
budget. This untouched result must remain authoritative for the pilot even if a later disclosed development replay
passes.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_PILOT_UNTOUCHED.md`
- `project_ws/AgentOps/fable5_historical_trading_pilot_untouched.json`
- `tests/fixtures/autonomy_diagnosis_to_fix_fable5_trading_pilot/`
