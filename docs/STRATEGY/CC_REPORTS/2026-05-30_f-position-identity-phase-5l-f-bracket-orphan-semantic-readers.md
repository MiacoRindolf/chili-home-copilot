# Phase 5L-F: Bracket/Orphan Semantic Readers

**Date:** 2026-05-30
**Status:** SHIPPED
**Branch:** `codex/brain-work-done-marker-recovery`

## What Changed

Phase 5L-F moved the last raw runtime live-reader SQL references to the legacy
`trading_trades` compatibility view behind explicit management-envelope helper
contracts:

- bracket reconciliation local readback now loads through
  `load_bracket_reconciliation_scope`
- missing-stop watchdog candidate lookup now loads through
  `load_stale_bracket_watchdog_candidates`
- Coinbase orphan-stop adoption now loads through
  `load_coinbase_orphan_adoption_candidates`
- `tests/test_phase5l_reader_allowlist.py` now has an empty allowlist for raw
  runtime live-reader SQL against `trading_trades`

The physical relation contract remains unchanged:

```text
trading_management_envelopes = physical base table
trading_trades               = legacy compatibility view
```

## Verification

Verified from a clean temporary worktree at commit `ee2ae94`:

```text
python -m py_compile ...                                    PASS
tests/test_phase5l_reader_allowlist.py                      PASS
tests/test_bracket_reconciliation_envelope_readers.py       PASS
tests/test_coinbase_orphan_envelope_readers.py              PASS
Phase 5K live-path parity probe                             COMPLETE_POSITIVE
Phase 5I post-rename soak probe                             COMPLETE_POSITIVE
```

Live probes after this slice:

```text
Phase 5K-A: 6 live-path aggregate checks matched, 0 mismatches
Phase 5I:   20 fresh decisions, 20 fresh envelopes, 10 fresh closes,
            0 hard linkage issues, 0 attribution drift
```

The current shared pytest database had unrelated active pytest jobs during the
run, so the slow DB-backed bracket/orphan integration tests were not all rerun
in the primary dirty worktree. The committed branch was verified in a clean
worktree with the new helper contract tests and live parity probes.

## Architect Verdict

This closes the Phase 5L runtime reader cleanup. Runtime app code should no
longer add raw live-reader SQL against the legacy `trading_trades` view without
failing the canary. The compatibility view stays in place for writers, old
contracts, migrations, tests, and the still-legacy `Trade` ORM symbol.

Do not drop `trading_trades` yet. The next move is a compatibility-contract
audit of remaining non-reader references, not a blind rename.
