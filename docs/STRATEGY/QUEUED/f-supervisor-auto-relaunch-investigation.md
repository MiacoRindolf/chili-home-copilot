# f-supervisor-auto-relaunch-investigation

## Background

The dev daemon v2 has a designed 4h self-restart limit. When it
exited cleanly at 2026-05-10 15:08:04 with `state=exited
detail=clean_exit uptime_sec=14402`, the outer
`_claude_daemon_supervisor.ps1` was supposed to detect the clean exit
and relaunch within 2-5 seconds (per the supervisor's exponential-
backoff logic for non-fast exits, OR the immediate-relaunch path for
expected clean restarts).

It didn't. The daemon was dead for 16 minutes (15:08 → 15:24) until
operator manually re-ran the supervisor script. The pending dispatch
sat unread during that window. The dev loop was completely paused.

## Why this matters

- Defeats the whole point of the supervisor wrapper. The 4h self-
  restart is a designed safety hatch against pathological hangs; the
  supervisor exists to make it transparent. Operator's manual
  intervention shouldn't be required.
- The autonomous loop can sit dead for arbitrary periods. If operator
  is asleep / away when the 4h cycle hits, the next CC session can't
  run + any production-monitoring dispatches won't fire.

## Hypotheses

1. **Supervisor process was killed externally** (Ctrl+C from operator
   at some prior point, OS Task Manager, Windows update reboot, etc.)
   — supervisor never ran past the FIRST daemon exit. The "auto-
   relaunch" only works if supervisor outlives the daemon.
2. **Supervisor crashed silently** — unhandled exception in the
   relaunch path, e.g. Start-Process throwing on some specific state.
3. **Supervisor exited intentionally on its own stop flag** —
   `scripts/_claude_supervisor_stop.flag` got created (maybe by an
   earlier dispatch that thought it was helping).
4. **Supervisor log file isn't rotating + filled disk** — write
   failures abort the relaunch loop.

## Scope

Investigation + minimal hardening of `_claude_daemon_supervisor.ps1`:

(a) Add a heartbeat file for the supervisor itself (analogous to
    `_claude_daemon_heartbeat.json`). External watchdog (or operator)
    can detect supervisor death within ~30s instead of waiting for
    something to break.

(b) Make supervisor crash-resilient: wrap the main while-true loop
    in try/catch that logs any unhandled exception and continues
    rather than dying silently.

(c) Add a check for "is there a daemon process running per the last
    known PID file?" on supervisor startup — if YES, skip relaunch
    (don't double up). If NO, relaunch.

(d) Install the supervisor as a Windows scheduled task that auto-
    starts at login, so a Windows-update reboot doesn't permanently
    leave the loop dead.

(e) Investigate today's specific incident: check
    `scripts/_claude_supervisor.log` for the last entry. Look for any
    unhandled exception, stop-flag presence, or PowerShell host crash.
    If supervisor process pid 13184 (the new one) is still alive
    when we investigate, see if the PRIOR pid (the one running 11:08+
    today) left a death-row entry in any log.

## Hard constraints

- `_claude_daemon_supervisor.ps1` only (and maybe a new tiny
  heartbeat file for it).
- Don't change daemon v2 behavior.
- Don't introduce Windows-service installation as a hard requirement
  (operator's choice when to install NSSM); just document the
  scheduled-task alternative.
- Keep the supervisor as a single PowerShell script (no compiled
  binaries, no new dependencies).

## Deliverables

- Investigation findings in CC_REPORT (what likely happened today)
- Hardened `_claude_daemon_supervisor.ps1` with (a) and (b) at
  minimum, optionally (c) and (d)
- A short operator-facing doc explaining how to install the
  supervisor as a scheduled task on login (optional but recommended)
- 1-2 tests if practical (PS scripts are hard to unit-test; smoke
  test by killing a fake daemon process and confirming relaunch fires)

## Priority

Medium. Operator's manual restart unblocked the deploy today, but
this WILL bite again on the next 4h cycle if not fixed.
