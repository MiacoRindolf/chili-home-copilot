# NEXT_TASK: f-phase5o-learning-cycle-architecture-envelope-audit

STATUS: QUEUED

## Goal

Audit `app/services/trading/learning_cycle_architecture.py`, the next Phase 5O
adapter candidate after `learning.py` was reclassified as a realized-evidence
writer future rename blocker.

## Why This Is Next

The remaining adapter candidates are no longer obviously harmless. After the
`learning.py` audit, the next learning-adjacent file is the learning-cycle
architecture/status layer. It may look like orchestration/reporting, but any
step-status mutation that controls learning-cycle progress should be treated as
behavior-bearing until proven otherwise.

Current surface after the `learning.py` audit:

```text
orm_trade_symbol_compat = 66
learning_research_reporting = 6
live_action_broker_reconcile = 20
private_helper_type_only = 5
risk_capital_gate = 21
adapter_candidate = 5
future_rename_blocker = 45
unexpected runtime readers = 0
unexpected runtime mutations = 0
```

## Scope

- Classify every legacy `Trade` ORM reference in
  `learning_cycle_architecture.py`.
- Determine whether the references are passive UI/status reporting,
  learning-cycle step mutation, scheduler orchestration, or action-adjacent
  runtime state.
- If passive and covered by tests, add a narrow helper/adapter conversion.
- If behavior-bearing, add read-only parity evidence and reclassify it as a
  future rename blocker.

## Guardrails

- No live alert cadence, order, stop, close, broker, reconcile,
  risk/capital/PDT, lifecycle, or portfolio behavior change without parity
  evidence.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- Do not touch the dirty root checkout.
- Respect `project_ws` coordination reports; while PM/control-plane governance
  remains frozen, push evidence branches only and do not force a merge/deploy.
- Source posture is currently ALERT because shared app services are mounted
  from dirty root `D:\dev\chili-home-copilot`. Do not restart Postgres or clean
  the dirty root as part of this slice.

## Exit Criteria

- Either a behavior-preserving probe/conversion ships with focused tests, or
  the task closes with a documented deferral and next evidence brief.
- Analyzer reports no unexpected runtime readers/mutations.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.
