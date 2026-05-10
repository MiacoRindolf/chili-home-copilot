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

## Start it

```powershell
# In a SECOND side PowerShell window, alongside the existing _claude_daemon.ps1 window:
.\scripts\_claude_session_daemon.ps1
```

Logs to `scripts/_claude_session_daemon.log`, writes machine-readable
state to `scripts/_claude_session_status.json` after every transition.

## Layout

```
scripts/
  _claude_session_daemon.ps1            # the daemon
  _claude_session_launcher.ps1          # bridge that runs `claude` (handles .cmd shim)
  _claude_session_queue/                # PENDING sessions, sorted by priority + not_before
  _claude_session_running/              # CURRENT session (presence == lock)
  _claude_session_done/                 # COMPLETED, prefixed FAILED_ on non-pass
  _claude_session_log/<id>/             # per-session stdout.log + stderr.log + meta.json + args.json
  _claude_session_consult/<id>/         # plan-gate consultation files (request/response)
  _claude_session_status.json           # machine-readable current state
  _claude_session_stop.flag             # operator: graceful exit
  _claude_session_pause.flag            # operator: idle until removed
```

## Session file (.session)

Drop a JSON file into `scripts/_claude_session_queue/`. Filename convention:
`<priority>-<slug>.session`.

```json
{
  "id": "promotion-rebalance-phase3-2026-05-10",
  "description": "...human readable...",
  "priority": 100,
  "not_before": null,
  "prompt": "...full prompt text...",
  "claude_args": ["--dangerously-skip-permissions"],
  "timeout_min": 240,
  "post_verify": null,
  "on_fail": "pause"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `id` | no | defaults to filename stem |
| `description` | no | echoed in status.json |
| `priority` | no | int; lower runs first; defaults to 1000 |
| `not_before` | no | ISO 8601; null = immediately |
| `prompt` | no | passed via `-p` |
| `claude_args` | no | extra args; `--dangerously-skip-permissions` is auto-added |
| `timeout_min` | no | wall-clock kill timer; defaults to 240 (4 h) |
| `post_verify` | no | optional `.ps1` path; non-zero exit marks the session FAILED |
| `on_fail` | no | `pause` (default) / `continue` / `abort_chain` |

## Plan-gate consultation protocol

Sessions can include a "review checkpoint" where CC pauses, posts an
implementation plan, and waits for Cowork's approval before coding.
This catches design errors at the highest-leverage moment.

**How it works at the daemon level:**

1. Daemon creates `scripts/_claude_session_consult/<id>/` at session start.
2. Daemon sets `$env:CHILI_SESSION_ID = <id>` so the spawned CC inherits it.
3. While CC runs, daemon polls the consult dir every 5 seconds for `*.request.md` files lacking matching `*.response.md`.
4. When a pending request is detected, status.json flips to `state: "awaiting_review"` with the request file path. The daemon log notes it.
5. When all requests have responses, status.json reverts to `state: "running"`.

**How CC participates (only when the prompt opts in):**

Including the plan-gate in a session prompt looks like:

> Step 1: Read CLAUDE.md, docs/STRATEGY/PROTOCOL.md, docs/STRATEGY/COWORK_ADVISOR_BRIEF.md, docs/STRATEGY/NEXT_TASK.md, and the QUEUED brief.
>
> Step 2: Develop your implementation plan covering: file paths, migration ID, test cases, edge cases, deviations from the brief.
>
> Step 3: Write the plan to `scripts/_claude_session_consult/$env:CHILI_SESSION_ID/plan.request.md`.
>
> Step 4: Poll for `plan.response.md` in the same directory every 30s, up to 2h. Use Bash: `while [ ! -f scripts/_claude_session_consult/$CHILI_SESSION_ID/plan.response.md ]; do sleep 30; done`
>
> Step 5: Read the response. It will contain one of:
>   - `APPROVED` — proceed with the plan exactly as written
>   - `REVISE: <feedback>` — revise the plan, overwrite plan.request.md, delete the response, wait again
>   - `ABORT: <reason>` — write a brief CC_REPORT explaining and exit code 7
>
> Step 6: After APPROVED: implement, test, commit, push, write CC_REPORT, update NEXT_TASK.

**How Cowork (operator-mediated) responds:**

When operator notices `state: "awaiting_review"` in status.json (or pings
the assistant explicitly), the assistant:
1. Reads the request file directly (Read tool with absolute path).
2. Reviews the plan against the brief, codebase context, and lore.
3. Writes the response file (Write tool).

Daemon detects the response within 5 seconds and CC's polling loop sees it.

**Pre-Phase-3 sessions (legacy):** sessions whose prompts don't reference
the consult dir simply ignore it. The daemon creates the dir but stays
out of the way.

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

# Watch log
Get-Content scripts/_claude_session_daemon.log -Tail 30 -Wait
```

## Recovery

If the daemon crashes mid-session, the .session file stays in
`_claude_session_running/`. On next startup, the daemon recovers stale
running files by moving them to `_claude_session_done/` with a
`FAILED_RECOVERED_` prefix.

## Hard constraints

- Only ONE session runs at a time (single-host file lock via `running/`).
- Dev daemon is unaffected — keep using `_claude_pending.txt` for
  fast dispatches; they queue independently.
- Pause flag set by daemon on session failure stays until you remove
  it. Don't clear it without reviewing the failed session's CC report.
- Plan-gate stalls the chain when neither operator nor Cowork is
  reachable. That's by design — better a stall than wrong code.
