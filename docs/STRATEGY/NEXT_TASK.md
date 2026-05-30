# NEXT_TASK: f-position-identity-phase-5l-g-compat-contract-audit

STATUS: PENDING

## Goal

Now that Phase 5L-F emptied the runtime live-reader allowlist, audit and pin the
remaining `trading_trades` compatibility contracts so the system cannot drift
back toward table-name ambiguity.

This is not a physical rename or view drop. It is a contract-hardening pass.

## Current State

Physical relation state:

```text
trading_management_envelopes = physical base table
trading_trades               = legacy compatibility view
```

Fresh safety evidence after Phase 5L-F:

```text
Phase 5K-A: COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:   COMPLETE_POSITIVE, 20 fresh decisions, 20 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
Phase 5L reader allowlist: empty for runtime app raw live-reader SQL
```

## Recommended Work Shape

1. Run a fresh classifier over remaining `trading_trades` references.
2. Split references into explicit buckets:
   - allowed compatibility writers/updates
   - ORM `Trade` symbol compatibility
   - migrations / historical probes / tests
   - docs and runbooks
   - unexpected runtime readers
3. Add or tighten tests so unexpected runtime live-reader SQL remains blocked.
4. Do not touch broker order/close behavior.
5. Re-run Phase 5K-A, Phase 5I, and the Phase 5L reader canary.

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the `Trade` ORM class.
- Do not search-replace writer, broker, order, or reconcile code.
- Do not absorb unrelated dirty worktree files.
- Keep live close/order semantics unchanged.

## Architect Verdict

Phase 5L-F finished the dangerous reader cleanup. The next high-value move is
to freeze the remaining compatibility surface in tests and documentation. Only
after that contract is pinned should we consider a later `Trade` ORM naming
discussion.
