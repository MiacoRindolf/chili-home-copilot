# Phase 5AE - Alpha Decay Envelope Audit

Date: 2026-05-31

## Summary

Audited `app/services/trading/alpha_decay.py`, the next
learning/research/reporting candidate after Phase 5AD.

Verdict: `alpha_decay.py` is lifecycle-sensitive and should not be converted
blindly. It reads live closed management-envelope evidence plus paper-trade
evidence, then may call `transition_on_decay(...)` to demote promoted patterns.
That makes it an alpha lifecycle path, not a passive report.

No decay behavior was changed. This slice added read-only parity evidence for
the live evidence reads and moved the file out of the adapter-candidate bucket.

## What Changed

- Added `scripts/d-phase5ae-alpha-decay-envelope-parity-probe.py`.
- Added `tests/test_phase5ae_alpha_decay_envelope_parity_probe.py`.
- Reclassified `alpha_decay.py` in the Phase 5O map from
  `adapter_candidate` to `future_rename_blocker` with subtype
  `lifecycle_decay_path`.

## Live Probe Result

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=5 alpha-decay evidence checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
WINDOW_DAYS=30
USER_ID=None
ALPHA_DECAY_CHECKS=5
ALPHA_DECAY_MISMATCHES=0
active_decay_pattern_ids: 4 old = 4 new
decay_live_evidence_ids: 105 old = 105 new
half_life_live_evidence_ids: 112 old = 112 new
```

## Verification

- `python -m py_compile scripts\d-phase5ae-alpha-decay-envelope-parity-probe.py`
- `python -m json.tool docs\STRATEGY\phase5o_remaining_runtime_compat_map.json`
- `pytest tests\test_phase5ae_alpha_decay_envelope_parity_probe.py tests\test_phase5_remaining_trade_refs.py tests\test_alpha_decay_payoff_protection.py -q`
- `python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`
- `python scripts\d-phase5ae-alpha-decay-envelope-parity-probe.py` with live opt-in
- `python scripts\d-phase5k-live-path-parity-probe.py`
- `python scripts\d-phase5i-post-rename-soak-probe.py`

Results:

```text
18 passed, 1 warning
raw reader bucket 0
unexpected runtime mutations 0
orm_trade_symbol_compat remains 69
learning_research_reporting remains 14
adapter_candidate 19 -> 18
future_rename_blocker 34 -> 35
Phase 5AE alpha-decay probe COMPLETE_POSITIVE
Phase 5K COMPLETE_POSITIVE
Phase 5I COMPLETE_POSITIVE
```

## Architect Verdict

Do not convert the `alpha_decay.py` reader in a casual adapter slice. The old
and new evidence scopes match exactly, but this path can demote live promoted
patterns. A future conversion should preserve paper/live blending, payoff-ratio
protection, and transition behavior under a dedicated behavior-parity test
rather than a table-name cleanup.
