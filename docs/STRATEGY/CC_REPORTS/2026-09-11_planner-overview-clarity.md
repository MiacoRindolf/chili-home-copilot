# Planner overview and readable task details

The planner now opens on an Overview with four task counts, a compact completion
indicator, and priority queues. Open, Completed, and All scopes carry through the
existing List, Board, and Timeline views. Counts use unique task IDs, including
subtasks; attention counts the union of blocked and overdue open tasks.

Task descriptions have a safe, formatted reading view and a separate editor.
Source text, code whitespace, and numbered steps are preserved. Chat and the
coding cockpit collapse to keep the task readable. The layout supports narrow
screens, light/dark themes, keyboard focus, and embedded mode.

Task updates accept only the fields actually changed. Creating a task still
requires a title. Pending saves retain newer typing, stale detail reads cannot
restore older content, failed field changes restore the persisted value, and
closing a task waits for saves and keeps a newer draft open. Viewer controls
cannot issue task mutation requests. Initial JSON, descriptions, status classes,
and Timeline project colors are escaped or validated at their rendering sinks.

Validation:

- 19 Node tests passed for presentation, dates/DST, input safety, partial saves,
  failed saves, stale reads, later typing, viewer guards, and pending-close races.
- 40 Python tests passed across presentation contracts, planner schemas, and
  service performance. These use fake persistence; no live schema or task data
  is changed by the tests.
- Independent adversarial reviews A and B passed. Review A rechecked the final
  pending-close guard and all 19 Node cases.
- Browser checks covered desktop light/dark, a 390 px viewport, embedded mode,
  filters, long stored descriptions, failed field restoration, and focus after
  closing/refetching a task. The preview used a labelled read-only project 9
  snapshot and rejected writes.
- `git diff --check` passed. Full repository CI is tracked separately; this
  report does not claim a green full-suite run.

The change does not alter strategy decisions, task status/progress semantics,
or stored project evidence. Deployment and merge receipts belong in the program
handoff and planner, not in this pre-merge report.
