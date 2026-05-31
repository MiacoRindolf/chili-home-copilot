# NEXT_TASK: f-phase5o-scanner-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/scanner.py`, the next Phase 5O adapter candidate
after `pattern_imminent_alerts.py` was reclassified as a live selection gate.

## Why This Is Next

The Phase 5O map now has 12 remaining adapter candidates. `scanner.py` is the
highest-leverage next slice because scanner output can feed candidate
generation, scoring, and downstream alert surfaces. It is currently classified
as `learning_research_reporting / adapter_candidate`, but it needs evidence
before any rename/conversion pressure.

Current surface after the pattern-imminent audit:

```text
orm_trade_symbol_compat = 69
learning_research_reporting = 10
live_action_broker_reconcile = 19
private_helper_type_only = 5
risk_capital_gate = 21
adapter_candidate = 12
future_rename_blocker = 41
raw reader bucket = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in `scanner.py`.
- Determine whether the references are passive scoring/history reads, signal
  generation, candidate selection, or live-gate-adjacent.
- If passive and covered by tests, add a small safe helper/adapter conversion.
- If behavior-bearing, add read-only parity evidence and reclassify it as a
  future rename blocker.

## Guardrails

- No live alert cadence, order, stop, close, broker, reconcile,
  risk/capital/PDT, or portfolio behavior change without parity evidence.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- Do not touch the dirty root checkout.
- Respect `project_ws` coordination reports; while PM/control-plane governance
  remains frozen, push evidence branches only and do not force a merge/deploy.

## Exit Criteria

- Either a behavior-preserving probe/conversion ships with focused tests, or
  the task closes with a documented deferral and next evidence brief.
- Analyzer stays clean: raw reader bucket 0, no unexpected runtime mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
- Source posture remains `COMPLETE_POSITIVE`; if it drifts to the dirty root,
  correct only app services from the clean Phase5AB-D runtime worktree and do
  not restart Postgres.
