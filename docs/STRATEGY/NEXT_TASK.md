# NEXT_TASK: f-position-identity-phase-5n-semantic-envelope-helper-slice

STATUS: PENDING

## Goal

Reduce the remaining `Trade` ORM-symbol semantic debt by moving low-risk read/report callers behind management-envelope helper APIs, without renaming the ORM class and without changing live broker/order/close behavior.

Phase 5M found 105 runtime `Trade` ORM-symbol files. That is not a table-rename blocker, but it is still a human-modeling problem: the database object is now a management envelope while many callers still speak "trade".

## Recommended Work Shape

1. Start with analytics/learning/reporting files from the Phase 5M candidate pool, not broker/order/reconcile or capital-gate paths.
2. Add narrow helper functions to `app/services/trading/management_envelopes.py` when a repeated read pattern exists.
3. Convert one small slice of callers to the helper API.
4. Avoid behavior changes. This is a naming/contract slice, not an alpha or execution change.
5. Re-run:
   - `tests/test_phase5_remaining_trade_refs.py`
   - `tests/test_phase5l_reader_allowlist.py`
   - `scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime`

## Candidate Pool

Best first candidates from Phase 5M:

- analytics / learning / reporting services
- attribution and execution-quality reports
- pattern performance readers
- AI context / journal / daily playbook surfaces

Avoid in this slice:

- broker sync
- bracket writers
- stop/exit execution
- autotrader order placement
- PDT / capital / portfolio-risk gates
- ORM class rename
- API/UI product rename

## Guardrails

- Do not rename `Trade`.
- Do not rewrite `trading_trades` compatibility view usage in writer/ORM paths.
- Do not touch order placement, broker sync, close, stop, or reconcile semantics.
- Do not absorb unrelated dirty worktree files.

## Architect Verdict

Phase 5M says the rename is semantically safe only if we keep cutting by behavior surface. Phase 5N should make the read/report brain speak the new entity language first. The live-money paths stay boring until each one has an explicit parity gate.
