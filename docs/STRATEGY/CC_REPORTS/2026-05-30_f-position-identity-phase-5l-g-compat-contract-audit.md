# Phase 5L-G: Compatibility Contract Audit

**Date:** 2026-05-30
**Status:** SHIPPED
**Branch:** `codex/brain-work-done-marker-recovery`

## What Changed

Phase 5L-G hardened the remaining trade-surface classifier so the post-rename
compatibility layer is explicit rather than fuzzy.

The classifier now separates:

- unexpected runtime raw readers (`FROM/JOIN trading_trades`)
- unexpected runtime mutations
- allowed compatibility-view writer/update paths
- literal `trading_trades` relation-symbol contracts
- legacy ORM `Trade` symbol contracts
- migrations / probes / tests / historical scripts
- docs and runbooks

It also has a `--fail-on-unexpected-runtime` mode so future CI or operator
checks can fail if raw app readers creep back in.

## Clean-Branch Audit

Verified from a clean temporary worktree at commit `a7ecd6c`:

```text
OK=True
FILE_COUNT=497
RAW_SQL_FILE_COUNT=113
BUCKETS={
  'allowed_compatibility_writer_update': 4,
  'compatibility_migration_test_history': 203,
  'compatibility_relation_symbol': 17,
  'docs_runbooks': 176,
  'orm_trade_symbol_compat': 97
}
RAW_READER_BUCKETS={
  'compatibility_migration_test_history': 82,
  'docs_runbooks': 31
}
UNEXPECTED_READERS=[]
UNEXPECTED_MUTATIONS=[]
UNCLASSIFIED=[]
```

This is the desired Phase 5L-G shape: no runtime app raw readers and no
unowned references.

## Verification

```text
tests/test_phase5_remaining_trade_refs.py              PASS
tests/test_phase5l_reader_allowlist.py                 PASS
tests/test_alert_refresh_churn_audit.py                PASS
py_compile                                             PASS
Phase 5K live-path parity probe                        COMPLETE_POSITIVE
Phase 5I post-rename soak probe                        COMPLETE_POSITIVE
```

Combined clean-worktree focused tests:

```text
18 passed, 1 warning
```

Live probes after this slice:

```text
Phase 5K-A: 6 live-path aggregate checks matched, 0 mismatches
Phase 5I:   20 fresh decisions, 20 fresh envelopes, 10 fresh closes,
            0 hard linkage issues, 0 attribution drift
```

## Architect Verdict

Phase 5L is now doing what it was meant to do: the runtime reader surface is
clean, and the remaining compatibility surface is owned. The compatibility view
must stay for now. The next step should be a small relation-symbol contract
slice that turns the 17 literal `trading_trades` references into clearer helper
or constant usage where that reduces ambiguity, without touching broker
behavior or renaming the ORM class.
