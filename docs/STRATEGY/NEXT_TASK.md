# NEXT_TASK: f-position-identity-phase-5m-orm-symbol-contract-audit

STATUS: PENDING

## Goal

Classify the remaining runtime `Trade` ORM-symbol compatibility surface after Phase 5L-H, without renaming the class and without changing live broker/order/close behavior.

Phase 5L-H proved there are no unexpected runtime raw readers/mutations and only two intentional relation-symbol anchors remain. The next ambiguity is semantic: many files still import or type against `Trade`, even though the physical table is now `trading_management_envelopes` and `trading_trades` is a compatibility view.

## Recommended Work Shape

1. Extend or reuse `scripts/analyze_phase5_remaining_trade_refs.py` to emit only `orm_trade_symbol_compat` runtime-app entries.
2. Split the `Trade` ORM surface into:
   - live writer/order/broker/reconcile paths that must keep the compatibility ORM for now
   - read/report paths that should eventually use semantic envelope helpers
   - API/schema naming that can be renamed later without DB impact
   - tests/migrations/history that are intentionally compatibility-bound
3. Make no behavior change unless there is a trivial comment/docstring cleanup.
4. Produce a closeout report with counts and a next-step recommendation.

## Guardrails

- Do not rename the `Trade` ORM class in this task.
- Do not drop or rewrite the `trading_trades` compatibility view.
- Do not touch order placement, broker sync, close, stop, or reconcile semantics.
- Do not absorb unrelated dirty worktree files.

## Architect Verdict

This is a map-before-cut task. The physical rename and relation-symbol cleanup are healthy; the next risk is human misunderstanding of `Trade` meaning in code. We classify that surface before attempting any semantic ORM rename or API naming cleanup.