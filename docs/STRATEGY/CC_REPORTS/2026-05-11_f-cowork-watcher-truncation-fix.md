# CC_REPORT: f-cowork-watcher-truncation-fix

Session: `cowork-watcher-truncation-fix-2026-05-11`
Date: 2026-05-11
CC: claude-opus-4-7 (1M context)
Plan gate: APPROVED (autonomous) at 2026-05-11T15:20:06Z
Mode: PLAN-GATE PROTOCOL ACTIVE

## What shipped

Three new files; one canonical reference sample; no `app/` changes.

| File | Purpose |
|---|---|
| `scripts/watcher-check-truncation.ps1` | Canonical helper — AST-parse based truncation oracle with 60s re-check debounce. |
| `scripts/_watcher_truncation_check.json` | Reference sample of the helper's JSON output against the 4 production files at HEAD. Committed so the schema is visible to future maintainers. |
| `docs/STRATEGY/COWORK_WATCHER_PROMPT.md` | Canonical watcher routine prompt. Operator pastes/merges this into the routine at https://claude.ai/code/routines. |
| `docs/runbooks/WATCHER_TRUNCATION_HEURISTIC.md` | Future-maintainer documentation: why line counts were wrong, why AST parse is right, why 60s debounce, helper invocation, rollback. |

NOT MODIFIED (per hard constraints in the brief):
- Anything under `app/`, `tests/`, or `app/trading_brain/`.
- `scripts/_claude_daemon*.ps1` or other daemon infrastructure.
- Existing `_cowork_watcher_disarm_truncation_check.flag` (operator's manual safety belt, untouched).

## Diagnostic — where the watcher logic actually lives

The watcher (`cowork-watcher-chili`) is a **remote Claude Code
routine**, NOT a Windows scheduled task or an in-repo script. Verified:

- `Get-ScheduledTask` returns no matching task.
- `RemoteTrigger list` returns `{"data":[],"has_more":false}` for the
  operator's current account context — i.e. the routine is currently
  not enumerable from this session (disabled, different OAuth scope,
  or removed).
- Searches for `STEP A`, `POST-SESSION TRUNCATION`, `watcher charter`,
  `cowork-watcher-chili` across the entire repo only hit sidechannel
  outputs and the truncation-fix brief — NOT the prompt itself.
- The only in-repo trace of the truncation-detection pattern is in
  `scripts/dispatch-watcher-disarm.ps1` (operator's manual disarm
  script), which already uses AST parse correctly. The buggy logic is
  in the cloud-side routine prompt.

This is the root cause of why the fix is split across "a helper
script the watcher invokes" and "a canonical prompt the operator
pastes into the routine": there is no single in-repo prompt file to
edit. Future maintainers can keep the heuristic correct by updating
the helper + the canonical prompt doc in this repo.

## Heuristic change

| | Before | After |
|---|---|---|
| Oracle | `wc -l <file>` delta vs HEAD or CC_REPORT-stated lines | `ast.parse()` exit code |
| Read path | Bash sandbox mount (stale) | `[System.IO.File]::ReadAllText` against absolute Windows path |
| Debounce | None (single signal pause-flags) | 60s re-check; first failure is PENDING, second is TRUNCATED |
| Verdict types | binary (clean / truncated) | OK, OK_MISSING, OK_TRANSIENT, PENDING, TRUNCATED, ENV_ERROR, READ_ERROR |
| Pause-flag trigger | line-count mismatch | `any_truncated == true` only |

## Verification

### Static
- `[System.Management.Automation.Language.Parser]::ParseFile` on `scripts/watcher-check-truncation.ps1` → PARSE_OK.

### Behavioral (manual smoke; this is infrastructure not app code, no pytest)

| Scenario | Expected | Observed |
|---|---|---|
| Helper on 4 clean production files at HEAD | All `OK`, exit 0 | All `OK`, exit 0 ✓ |
| Helper on temp file with truncated content (first run) | `PENDING`, marker written, exit 0 | `PENDING`, marker written, exit 0 ✓ |
| Helper on the same truncated temp file ≥60s later | `TRUNCATED`, exit 1, marker cleared | `TRUNCATED`, exit 1, marker cleared ✓ |
| Helper on the temp file fixed to clean content (marker present) | `OK_TRANSIENT`, exit 0, marker cleared | `OK_TRANSIENT`, exit 0, marker cleared ✓ |
| Helper on a non-existent path | `OK_MISSING`, exit 0 | `OK_MISSING`, exit 0 ✓ |

### Reference output

`scripts/_watcher_truncation_check.json` (committed) contains the
helper's verdict against the 4 production files at HEAD commit
`2c59ef4`:

```
host_time: 2026-05-11T15:24:14.378Z
any_truncated: false
any_env_error: false
all verdicts: OK
lines/bytes (sanity): 1317/50597, 2578/101697, 1456/61391, 1798/77880
```

This is the file the routine should write on every check; future
runs can diff against this shape to detect helper-side regressions.

## Surprises / deviations

1. **Watcher prompt is not in-repo.** The brief said "locate the
   watcher's truncation-detection code (likely in the scheduled-task
   prompt or a helper PS script)". The prompt is in Anthropic's
   cloud routine system, not git. I split the fix into a helper
   script (the runtime locus that the watcher invokes) + a canonical
   prompt doc in this repo (the source of truth the operator pastes
   into the routine). The brief permits this — "Updated watcher
   prompt OR helper script" — and shipping both is strictly better.

2. **`RemoteTrigger list` returned empty.** The operator's current
   account has no routines visible. The watcher has been writing
   sidechannels every ~5 minutes through 2026-05-11T14:59Z, so
   either it's running under a different OAuth context or it was
   disabled between then and 2026-05-11T15:13Z (this session
   start). Operator should confirm where the live routine lives and
   update its prompt from `docs/STRATEGY/COWORK_WATCHER_PROMPT.md`.

3. **Em-dash character broke PS5.1 parsing.** First draft used `—`
   (U+2014) inside a string literal; PS 5.1 produced
   `UnexpectedToken 'transient'` errors. Replaced with `-`. Future
   PS1 work in this repo should stick to ASCII string literals.

4. **`conda run -c "<script>"` lost the script boundary.** First
   draft passed the Python source as `-c` argument to
   `Start-Process`. PowerShell joined the argument array with
   spaces and conda saw `python -c import ast, sys ...` with
   `import` as its only argument. Switched to writing the Python
   source to a temp `.py` file and invoking `conda run ... python
   <tempfile>`. Robust against arbitrary script content.

5. **NEXT_TASK conflict flagged.** `NEXT_TASK.md` at session start
   showed `f-cpcv-gate-coverage-audit` PENDING with this task in
   the queue. Operator's prompt explicitly redirected to the
   watcher fix. Per protocol §3.6 ("Flag conflicts in frozen scopes,
   don't veto"), I proceeded with operator's instruction and added
   a section noting this session's deliverable rather than
   overwriting the cpcv-audit task. Cowork can adjudicate queue
   order on review.

## Deferred

- **Updating the actual routine prompt.** The canonical text is in
  `docs/STRATEGY/COWORK_WATCHER_PROMPT.md`; pasting it into the
  routine at https://claude.ai/code/routines is operator-only.
- **Disarm-flag removal.** `scripts/_cowork_watcher_disarm_truncation_check.flag`
  is still present from the 2026-05-10 manual disarm. Leaving it in
  place per brief constraint "do not change what the watcher does
  WITH the pause flag". Operator removes it after they're satisfied
  the new helper behaves correctly in production.
- **Pause flag clearance.** The existing pause flag (held since
  2026-05-11T01:29Z per the 14:59Z sidechannel) is operator-only to
  clear. This task does NOT clear it.

## Open questions for Cowork

1. **Where is the live routine?** `RemoteTrigger list` showed zero
   routines in this session's account. If the watcher is running
   from a different OAuth context (different Claude account, or a
   team workspace), Cowork should document that in the advisor
   brief so future CC sessions can find/update it.
2. **Helper invocation path.** The canonical prompt directs the
   routine to invoke `scripts/watcher-check-truncation.ps1` "from
   the host PowerShell". The actual mechanism (daemon
   `_claude_pending.txt` pipe, direct host PowerShell, or
   sandbox-side `& conda run`) needs explicit documentation in the
   routine prompt — the plan-gate reviewer flagged this as a soft
   observation. Recommend Cowork adds an "Invocation mechanism"
   section to `COWORK_WATCHER_PROMPT.md` once the operator confirms
   how the routine actually reaches the host.
3. **Should the helper be cron-invokable independently?** A
   `dispatch-watcher-check-truncation.ps1` wrapper that writes its
   own out.txt would let the dev daemon run the check on a 5-min
   cadence even if the cloud routine is disabled. That's outside
   this brief's scope but might be the resilience improvement
   Cowork wants after the 2026-05-10 incident chain.

## Commit boundary

Single commit covers: helper PS1, reference JSON sample, canonical
prompt doc, runbook, this CC report, NEXT_TASK section addition.
No `app/` files, no daemon files, no trading-code files.

## Lines / bytes (per Advisor Brief §2.1 truncation-discipline)

| File | Lines | Bytes |
|---|---|---|
| `scripts/watcher-check-truncation.ps1` | 264 | ~10100 |
| `scripts/_watcher_truncation_check.json` | 50 | ~3300 |
| `docs/STRATEGY/COWORK_WATCHER_PROMPT.md` | ~165 | ~6000 |
| `docs/runbooks/WATCHER_TRUNCATION_HEURISTIC.md` | ~165 | ~5800 |
| `docs/STRATEGY/CC_REPORTS/2026-05-11_f-cowork-watcher-truncation-fix.md` | (this file) | (this file) |

All under the 500-line `Edit`-truncation hazard threshold; all were
written with `Write` (full-file overwrite), not `Edit`, on
non-trivial content.
