# Historical Fable 5 Trading Pilot Disclosed Recovery Receipt

## Authority

The authoritative first-run result remains **25/100**, 0/1 sealed-final solves, and no retained patch at source
commit `ae5c0fb8`. This receipt describes development after the historical direction-loss mechanism was disclosed.
It does not rewrite the untouched score or establish Fable 5 parity.

Historical reference: user-attested Fable 5-era commit `a9e5ea2b`, `Preserve short direction in paper auto entry`.
No trading runtime, broker, database, container, or premium API was used.

## Generic Repairs

Commit `fb96519b` added a prompt-derived directional position contract and an AST-positioned Python operator:

- normalize direction exactly once, with unknown values preserving established long-compatible behavior;
- long geometry: `stop < entry < target`, loss distance `entry - stop`;
- short geometry: `target < entry < stop`, loss distance `stop - entry`;
- use a side-correct default stop;
- pass the same normalized direction through sizing, NetEdge/sizing telemetry, and persisted paper trade;
- select the long-only sizing function even when an already direction-aware risk helper appears earlier;
- reparse every coordinated source edit and fail closed on unrecognized math or incomplete owner shapes.

Tests cover the sealed historical extraction, alternate function/variable names, `EmitterSignal`/`open_paper_trade`
aliases from the real source, existing direction-bearing telemetry calls, multi-line annotated signatures, and an
unrecognized-risk-math negative case. A read-only probe against the real `a9e5ea2b^` source selected exactly
`app/services/trading/paper_trading.py` and `app/services/trading/portfolio_risk.py`, produced valid Python, and
closed every source invariant warning.

Commit `3e0a8204` added a deterministic diagnosis fast path. It activates only when one causal family and a guarded
source proposal are both provable before model access. The diagnosis stays provisional until pinned public and
repair-feedback tests validate the intervention. It is marked deterministic-only and cannot receive Fable 5 live
reasoning credit.

## Recovery Runs

The first disclosed replay after `fb96519b` reached **100/100** with exact owners and public/feedback/fresh-final
success. Its local judge selected the correct `data` family and described directional normalization, but two of
three diagnosis calls timed out. It took **242.5 seconds**, had 0% live qualification, and remained
`needs_improvement`.

The fast-path replay after `3e0a8204` also reached **100/100**, with:

- exact changed files: `trading/paper_trading.py`, `trading/portfolio_risk.py`;
- public: 2/2 passed;
- repair feedback: 4/4 passed;
- fresh isolated final: 5/5 passed;
- model calls: **0**;
- premium calls: **0**;
- wall time: **5.1 seconds**;
- checkpoint removed only after atomic outputs;
- `deterministic_only=true`, `live_reasoning_qualified=false`, verdict `needs_improvement`.

Artifacts:

- `FABLE5_HISTORICAL_TRADING_PILOT_DISCLOSED_RECOVERY.md`
- `fable5_historical_trading_pilot_disclosed_recovery.json`
- `FABLE5_HISTORICAL_TRADING_PILOT_FAST_SYMBOLIC_RECOVERY.md`
- `fable5_historical_trading_pilot_fast_symbolic_recovery.json`

## Validation

- Focused wording, transfer, fail-closed, real-alias, exact feedback, and isolated-final tests: passed.
- Full affected diagnostic reasoning and diagnosis-to-fix suite: **319 passed**, two pre-existing warnings.
- `py_compile` and `git diff --check`: passed.

## Interpretation

CHILI now has a fast premium-independent system capability for this recognized family and source shape. It is not
a wrapper around Fable 5, Qwen, or any premium model: the final fast replay made no model call at all.

This does not show Fable 5-level unknown-mechanism reasoning. The untouched run failed badly, and the successful
runs occurred only after the mechanism was disclosed and encoded generically. The next meaningful evidence must
use a different historical trading mechanism frozen before any new source work.
