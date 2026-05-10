# Dev daemon v2 — operator handoff (2026-05-10)

The dev daemon (`scripts/_claude_daemon.ps1`) has been hanging repeatedly.
Today I shipped a v2 + supervisor pair. This is the operator's run-once
handoff.

## What changed

- `scripts/_claude_daemon.ps1` — rewritten in place
- `scripts/_claude_daemon_supervisor.ps1` — new outer wrapper

The v1 history is in git; nothing was lost.

## Root-cause hypotheses for the v1 hangs

Three hang surfaces, all addressed:

1. **`Get-Content $pendingFile -Raw` blocking on a writer's lock.**
   When the scheduled-task watcher or session daemon writes to
   `_claude_pending.txt` while v1 is mid-poll, PowerShell's default
   `Get-Content` opens with `FileShare.Read` only — if the writer holds
   FileShare.Read clearance, OK; otherwise the read can stall. v2 uses
   `[System.IO.File]::Open(..., FileShare.ReadWrite)` so we never block
   on a peer.

2. **Pending-file clobber race.** v1 read pending → executed (could take
   5 min) → `Remove-Item pending` at end. If the watcher wrote a NEW
   dispatch during execution, v1's `Remove-Item` nuked it before reading.
   v2 does an atomic rename (`Move-Item pending → _claude_pending_<pid>_<n>.lock`)
   BEFORE reading; the watcher's next write goes into a fresh `pending`
   file that the next loop iteration picks up.

3. **Process-tree zombies holding stdio descriptors.** Already partially
   fixed in v1 (taskkill /T /F instead of $proc.Kill()), but v1 didn't
   verify the tree was actually gone before reading temp files. If a
   docker-compose grandchild kept the descriptor open, v1's
   `Get-Content $tmpStdout -Raw` hung. v2 has `Reap-ProcessTree` that
   polls for the tree to exit (5s max) before reading.

Plus three observability + recovery additions:

- **Heartbeat file** `scripts/_claude_daemon_heartbeat.json` written on
  every state transition (idle → consuming → reading → executing →
  reading_output → idle). External watchdogs (the scheduled-task watcher,
  for instance) can read it and alert if `ts` is more than ~120s old.
- **Periodic self-restart.** v2 exits cleanly after 4 hours OR 1000
  commands, whichever first. The supervisor relaunches it. This puts a
  hard ceiling on hang surface area — even if a new failure mode shows
  up, it's bounded to ≤4h.
- **Restart flag** `scripts/_claude_restart.flag` — `New-Item` it and v2
  exits cleanly; supervisor relaunches.

## How to switch over

Old workflow: `.\scripts\_claude_daemon.ps1` in a side window.

New workflow:

1. In the running daemon's window: **Ctrl+C** (or `New-Item scripts/_claude_stop.flag`).
2. Replace with: `.\scripts\_claude_daemon_supervisor.ps1`

That's it. The supervisor launches the daemon as a child process; if the
daemon exits cleanly (self-restart, restart flag, or unhandled crash) the
supervisor relaunches with exponential backoff (5s → 10s → 20s → … capped
at 5 min).

## Stop/pause/restart cheat sheet

| Goal | Command |
|---|---|
| Stop the whole stack (supervisor + daemon) | `New-Item scripts/_claude_supervisor_stop.flag` |
| Stop daemon only (supervisor relaunches) | `New-Item scripts/_claude_stop.flag` |
| Pause daemon (no relaunch needed) | `New-Item scripts/_claude_pause.flag` |
| Resume from pause | `Remove-Item scripts/_claude_pause.flag` |
| Restart daemon now (no waiting for 4h) | `New-Item scripts/_claude_restart.flag` |

## How Cowork detects a hang going forward

`scripts/_claude_daemon_heartbeat.json` is written every poll iteration
(2s) plus every state transition. Reading it gives:

```json
{
  "ts": "2026-05-10T10:15:42.123Z",
  "pid": 12345,
  "state": "executing",
  "detail": "timeout=600s",
  "counter": 42,
  "last_exit_code": 0,
  "last_timed_out": false,
  "last_duration": 12.5,
  "last_command": "docker compose ps...",
  "started_at": "2026-05-10T08:00:00Z",
  "uptime_sec": 8142
}
```

Watcher rule (already encoded in advisor brief): if `(now - ts) > 120s`
**OR** the `state` field has been the same with the same `counter` across
two successive watcher pulses (5+ min apart), the daemon is hung. The
watcher should:

1. Touch `scripts/_claude_restart.flag` (graceful — daemon picks up next
   loop iteration).
2. If still stuck after 60s, escalate to operator with the heartbeat
   contents.

## Why a supervisor instead of NSSM / Windows Service

NSSM is the textbook answer but it's another moving piece to install +
configure on every machine. The supervisor PS script is dependency-free,
visible (it runs in your terminal, you can see logs), and good enough
for the failure modes we've actually seen. If hangs persist after this,
the next step is NSSM.
