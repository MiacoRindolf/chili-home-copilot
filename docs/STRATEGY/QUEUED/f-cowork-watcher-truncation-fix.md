# f-cowork-watcher-truncation-fix

## Background

The `cowork-watcher-chili` scheduled task set four false-positive
"POST-SESSION TRUNCATION DETECTED" pause flags on 2026-05-10, each
claiming `stop_engine.py` and `bracket_reconciliation_service.py` were
truncated on disk. Every time the host files were actually clean
(verified via PowerShell `wc -l` + `python ast.parse`). The watcher's
view came from the bash sandbox mount, which serves stale snapshots of
the Windows host filesystem.

Each false-alarm halted the autonomous loop until interactive Cowork
manually cleared the pause flag. That happened ~four times in one day.

## Root cause

The watcher uses a line-count comparison heuristic that is BOTH:
1. Sourced from a stale view (bash mount), and
2. Compared against CC's reported `wc -l` from the CC_REPORT,
   which can itself be a misread of the BEFORE state rather than AFTER.

Line-count delta is the wrong oracle for truncation. AST parse
result is the right one — if the file parses, it's not truncated,
period.

## Goal

Replace the watcher's truncation-detection logic so it:
1. Uses AST parse (not line counts) as the truncation oracle.
2. Reads from a fresh view of the host filesystem (not stale bash
   mount). Possible approaches: shell out to `python -c "import ast;
   ast.parse(open(...).read())"` via PowerShell against host paths,
   or use Get-FileHash, or read via `[System.IO.File]::ReadAllText`.
3. Does NOT compare against CC's CC_REPORT-stated line counts (those
   can be wrong without indicating truncation).
4. Only sets the pause flag if AST parse genuinely fails AND a
   subsequent re-check 60 seconds later also fails (transient I/O
   should self-resolve).

## Scope

The watcher is invoked via the scheduled-task framework. The actual
logic lives in the watcher's prompt + helper scripts. Need to:
- Locate the watcher's truncation-detection code (likely in the
  scheduled-task prompt or a helper PS script).
- Replace line-count comparison with AST parse.
- Add the 60s re-check to filter transient false positives.
- Document the new heuristic so future maintainers don't revert.

## Hard constraints

- Watcher only. Don't touch the daemon or any trading code.
- The fix should ONLY change the pause-flag trigger; do not change
  what the watcher does WITH the pause flag (still surfaces to
  operator, still logs).
- Test by manually triggering a watcher run on a known-good file
  and confirming no false flag.

## Deliverables

- Updated watcher prompt or helper script
- Documentation of the new truncation-detection heuristic
- CC_REPORT covering the diagnostic root-cause + the fix
- NEXT_TASK → STATUS: DONE
