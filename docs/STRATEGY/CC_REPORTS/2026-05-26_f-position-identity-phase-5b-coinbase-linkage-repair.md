# CC_REPORT: f-position-identity-phase-5b-coinbase-linkage-repair

Date: 2026-05-26

## Summary

Phase 5B soak found a hard linkage gap after fresh Coinbase activity:
open Coinbase management envelopes had matching `trading_positions` rows whose
`current_envelope_id` pointed at the envelope, but the inverse
`trading_trades.position_id` was still NULL.

This report closes that gap.

## What Shipped

- Mig 273: backfilled open Coinbase envelope `position_id` values from
  `trading_positions.current_envelope_id`.
- `coinbase_service._ensure_coinbase_position_identity(...)` now writes both
  sides of the link:
  - `trading_positions.current_envelope_id = trade.id`
  - `trading_trades.position_id = position.id`
  - `trading_bracket_intents.position_id = position.id`
- Import-order repair in `app/config.py`: moved
  `PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_LIFECYCLE_STAGES` above the
  stock-fastlane default that references it. This pre-existing branch bug was
  exposed by force-recreating the workers after the Coinbase patch.
- Test guard updated in `tests/test_coinbase_position_lineage.py` so the
  Coinbase sync sidecar must include the `UPDATE trading_trades` inverse write.

## Verification

Focused tests:

```text
python -m pytest tests/test_coinbase_position_lineage.py tests/test_position_identity_phase5b.py -q
12 passed
```

Migration verifier:

```text
OK: 273 migrations, 0 retired; no ID collisions.
```

Live DB after mig 273:

```text
linked                                      525
historical_broker_envelope_missing_position 110
hard_linkage_issues                          0
open_broker_trades_missing_position          0
orphan_decisions                             0
```

Open Coinbase envelopes now all have position links:

```text
TRUMP-USD  -> position 259
COOKIE-USD -> position 260
HFT-USD    -> position 261
ALCX-USD   -> position 262
NMR-USD    -> position 263
YFI-USD    -> position 264
DIEM-USD   -> position 258
```

The live Coinbase sync path was exercised once after restart:

```text
{'created': 0, 'updated': 7, 'reopened': 0, 'closed': 0, 'deduped': 0, ...}
```

Workers were force-recreated after the config import-order repair and stayed
up. No `NameError`, `Traceback`, or `PATTERN_IMMINENT_DEFAULT...` crash lines
remained in the post-restart log check.

## Remaining Phase 5C Question

Phase 5B's hard linkage gate is now green. The remaining old-vs-new 30d PnL
report difference is semantic attribution, not broken linkage:

- `decision.scan_pattern_id` is NULL on a few bridge-created decisions while
  the management envelope has `scan_pattern_id` populated.
- The current measured old-vs-new diff is 4 groups / about $42.53 absolute.
- Phase 5C should migrate one reporting reader deliberately and decide whether
  each report groups by immutable decision pattern, mutable envelope pattern,
  or both.

## Recommendation

Move to Phase 5C reader adoption. Do not physically rename
`trading_trades` yet.
