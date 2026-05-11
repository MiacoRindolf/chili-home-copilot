# NEXT_TASK: f-cowork-watcher-truncation-fix

STATUS: PENDING

## Goal

Watcher set 4 false-positive truncation pause flags yesterday using a
buggy line-count heuristic against a stale bash-mount view. Replace
with AST parse against fresh host filesystem reads + 60s re-check
debounce.

## Brief

`docs/STRATEGY/QUEUED/f-cowork-watcher-truncation-fix.md`.

## Next in queue

`f-supervisor-auto-relaunch-investigation` (priority 220) — daemon
supervisor didn't auto-relaunch after the 4h self-restart.

## Hard constraints

- Watcher only. No daemon or trading code changes.
- AST parse oracle, not line counts.
- 60s re-check debounce on false positives.
- Plan-gate active.
