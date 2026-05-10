# Claude session daemon

A second daemon — separate from `scripts/_claude_daemon.ps1` — that owns
**long-running `claude` sessions** so they don't block the dev daemon's
fast dispatches (docker / git / psql).

## Why two daemons

| | `_claude_daemon.ps1` (dev) | `_claude_session_daemon.ps1` (sessions) |
| --- | --- | --- |
| Command duration | seconds | 2–4 hours |
| Default timeout | 5 min | 4 hours |
| Concurrency | sequential, fast | sequential, with persistent file-lock |
| Output sink | single `_claude_output.txt` | per-session `_claude_session_log/<id>/` |
| Scheduling | execute now | queue + `not_before` timestamp |
| Caller | ad-hoc, mostly Cowork | Cowork queueing CC phases |

If they shared one daemon, every CC session (2–4 h) would freeze every dev
dispatch behind it.

## Start it

```powershell
# In a SECOND side PowerShell window, alongside the existing _claude_daemon.ps1 window:
.\scripts\_claude_session_daemon.ps1
```

It logs to `scripts/_claude_session_daemon.log` and writes machine-readable
state to `scripts/_claude_session_status.json` after every transition.

## Layout

```
scripts/
  _claude_session_daemon.ps1            # the daemon
  _claude_session_queue/                # PENDING sessions, sorted by priority + not_before
    100-foo.session
    200-bar.session
  _claude_session_running/              # CURRENT session (presence == lock)
  _claude_session_done/                 # COMPLETED, prefixed FAILED_ on non-pass
  _claude_session_log/<id>/             # per-session stdout.log + stderr.log + meta.json
  _claude_session_status.json           # machine-readable current state
  _claude_session_stop.flag             # operator: graceful exit
  _claude_session_pause.flag            # operator: idle until removed (also auto-set on failure)
```

## Session file (.session)

Drop a JSON file into `scripts/_claude_session_queue/`. Filename is
arbitrary; convention is `<priority>-<slug>.session`.

```json
{
  "id": "promotion-rebalance-phase2-2026-05-09",
  "description": "human-readable; surfaces in status.json and meta.json",
  "priority": 100,
  "not_before": null,
  "prompt": "Read docs/STRATEGY/PROTOCOL.md and docs/STRATEGY/NEXT_TASK.md, then ...",
  "claude_args": ["--dangerously-skip-permissions"],
  "timeout_min": 240,
  "post_verify": null,
  "on_fail": "pause"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `id` | no | defaults to filename stem; used for log subdir + meta filename |
| `description` | no | echoed in status.json so polling tells you what's running |
| `priority` | no | int; lower runs first; defaults to 1000 |
| `not_before` | no | ISO 8601 timestamp; session won't start before this; null = immediately |
| `prompt` | no | passed via `-p`; defaults to a generic "read NEXT_TASK and execute" |
| `claude_args` | no | extra args; `--dangerously-skip-permissions` is auto-added |
| `timeout_min` | no | wall-clock kill timer; defaults to 240 (4 h) |
| `post_verify` | no | optional `.ps1` path; non-zero exit marks the session FAILED |
| `on_fail` | no | `pause` (default) / `continue` / `abort_chain` |

`pause` writes `_claude_session_pause.flag` so the chain idles until
operator review. `abort_chain` flushes the rest of the queue. `continue`
just records FAILED and moves to the next.

## How Cowork uses it

1. Cowork writes a `.session` file when a phase is ready to ship.
2. Daemon polls every 30s, picks the highest-priority eligible session,
   moves it to `_claude_session_running/`, launches `claude -p "<prompt>" --dangerously-skip-permissions`.
3. CC reads `NEXT_TASK.md`, executes, writes its CC_REPORT, updates
   `NEXT_TASK.md` status, commits, pushes — all per the existing protocol.
4. Daemon captures stdout/stderr to `_claude_session_log/<id>/` and exit
   code to `meta.json`.
5. If `post_verify` is set, runs it and combines exit codes.
6. On pass, session moves to `_claude_session_done/` and the daemon
   advances to the next queued session.
7. On fail with `on_fail: pause`, the daemon writes the pause flag and
   idles. Cowork sees this on next session (queue_depth + last in
   `status.json`), reviews the failed session's logs + CC_REPORT, decides
   whether to fix-and-resume (delete pause flag) or `abort_chain`.

## Operator controls

```powershell
# Pause (chain idles after current session completes)
echo paused-by-operator > scripts/_claude_session_pause.flag

# Resume
Remove-Item scripts/_claude_session_pause.flag

# Stop daemon entirely
echo stop > scripts/_claude_session_stop.flag

# Inspect current state
Get-Content scripts/_claude_session_status.json | ConvertFrom-Json | Format-List

# See running session output
Get-Content scripts/_claude_session_log/<id>/stdout.log -Tail 50 -Wait
```

## Recovery

If the daemon crashes mid-session, the .session file stays in
`_claude_session_running/`. On next startup, the daemon recovers stale
running files by moving them to `_claude_session_done/` with a
`FAILED_RECOVERED_` prefix. Inspect the log dir for that ID to see how
far CC got before the daemon died.

## Hard constraints

- Only ONE session runs at a time (single-host file lock via `running/`).
- The dev daemon is unaffected — keep using `_claude_pending.txt` for
  fast dispatches; they queue independently.
- A `pause` flag set by the daemon on failure stays set until you remove
  it. Don't clear it without reviewing the failed session's CC report.
