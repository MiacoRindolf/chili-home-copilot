# Watcher truncation-detection heuristic

How `cowork-watcher-chili` decides whether a working-tree Python file
is truncated, and why the heuristic looks the way it does.

## TL;DR

- Oracle: `ast.parse()` exit code. Not line counts.
- Read path: `[System.IO.File]::ReadAllText` against the absolute
  Windows path. Not the bash sandbox mount.
- Debounce: 60-second re-check. A single parse failure is `PENDING`,
  not `TRUNCATED`. Only persistent failures pause the loop.
- Helper: `scripts/watcher-check-truncation.ps1`.
- Reference output: `scripts/_watcher_truncation_check.json`.

## Why line-count comparison was wrong

The prior heuristic compared `wc -l <file>` (read from the bash
sandbox mount inside the watcher's session) against either:
- `wc -l` of `git show HEAD:<file>`, or
- the line count CC stated in its `CC_REPORT` for that file.

This produced false positives on 2026-05-10 (four times in one day)
because:

1. **The bash sandbox mount is stale.** When the host filesystem has
   been edited recently, the mount can serve a snapshot from minutes
   earlier. A file that's clean on host can look "short" on the
   mount.
2. **CC_REPORT line counts can be misreads.** The "lines: NNNN" a CC
   session records can be the BEFORE state of an Edit (or a wrong
   shell measurement), not the AFTER. Comparing against that
   produces phantom deltas even when the file is fine.
3. **Line counts are a proxy, not the property.** What we actually
   care about is "can the interpreter still load this file." That's
   exactly what `ast.parse()` answers. A file with a line-count
   delta but clean parse is not truncated. A file with the same
   line count but a malformed mid-line cut IS truncated. Line counts
   conflate the two.

## Why AST parse is the right oracle

Truncation by the Edit tool manifests as one of:
- Unclosed paren / brace / bracket at the cut point.
- An identifier or keyword chopped mid-token.
- A statement that ends mid-expression.

All three are SyntaxErrors. `ast.parse()` catches them deterministi-
cally. The 2026-05-10 production incidents all showed:

| File | wc -l (working) | HEAD | AST status |
|---|---|---|---|
| `stop_engine.py` | 1302 | 1316 | FAIL line 1299 `(` never closed |
| `bracket_reconciliation_service.py` | 2276 | 2577 | FAIL line 2270 `{` never closed |
| `venue/coinbase_spot.py` | 1163 | 1450 | FAIL line 1164 `except Except` |

In all three, AST parse caught the real corruption. The false
positives on 2026-05-10 also showed nonzero line-count deltas — but
the files parsed cleanly. AST parse would have correctly said "OK."

## Why 60-second debounce

Transient I/O can fail an `ast.parse()` for non-corruption reasons:
- A file is being mid-rewritten by `Write` (the tool truncates to 0
  bytes then writes the new content; a parse in that window reads
  partial content).
- Antivirus is holding a write lock briefly.
- The filesystem cache hasn't flushed.

These resolve within a few seconds. A 60-second debounce filters
them out cheaply: if the file is genuinely truncated, the failure
persists. If it's transient, the second check sees the clean file.

The trade-off: a real truncation takes up to 60s longer to escalate.
In practice the operator is the one who has to act on truncation
(restore from HEAD), and that takes minutes anyway. 60s extra is
acceptable. False positives that halt the autonomous loop are not.

## Helper script behavior

`scripts/watcher-check-truncation.ps1` exit codes:

| Exit | Meaning | Watcher action |
|---|---|---|
| 0 | No `TRUNCATED` verdicts (may include PENDING, OK, OK_TRANSIENT, OK_MISSING) | Log + continue |
| 1 | At least one `TRUNCATED` verdict (persistent failure, debounce elapsed) | Set pause flag + sidechannel |
| 2 | `ENV_ERROR` (conda/python unreachable from watcher shell) | Log inconclusive, do NOT pause |

Verdict glossary (in JSON output `results[].verdict`):

- `OK` — file exists, parses cleanly, no prior marker.
- `OK_MISSING` — file does not exist. Watcher's not the auditor for
  intentional deletions.
- `OK_TRANSIENT` — file parses cleanly on this run AND a prior
  marker existed (transient failure resolved). Marker cleared.
- `PENDING` — file failed to parse. Marker armed. Re-check in 60s.
  NOT an escalation.
- `TRUNCATED` — file failed to parse AND prior marker is ≥60s old.
  THIS is the only verdict that triggers a pause flag.
- `ENV_ERROR` — conda/python invocation failed for non-parse
  reasons. Inconclusive.
- `READ_ERROR` — couldn't read the file via .NET. Inconclusive.

Markers are stored at
`scripts/_watcher_truncation_pending/<base>_<hash>.json` and
auto-clear after 24h to prevent stale state.

## Manual invocation (for operators)

Sanity-check the four production files:

```powershell
.\scripts\watcher-check-truncation.ps1 `
    -Paths @(
        "app/services/trading/stop_engine.py",
        "app/services/trading/bracket_reconciliation_service.py",
        "app/services/trading/venue/coinbase_spot.py",
        "app/services/trading/bracket_writer_g2.py"
    ) `
    -OutFile "scripts/_watcher_truncation_check.json"
```

Expected on a clean HEAD: `EXIT_CODE=0`, all verdicts `OK`,
`any_truncated: false`.

To test the debounce path against a temp file, see the validation
scenarios in
`docs/STRATEGY/CC_REPORTS/2026-05-11_f-cowork-watcher-truncation-fix.md`.

## Rollback

If the new heuristic itself misbehaves:

1. Set the disarm flag:
   `scripts/_cowork_watcher_disarm_truncation_check.flag` — the
   watcher prompt's STEP G skips truncation checks while this flag
   is present.
2. Clear any stale markers:
   `Remove-Item scripts\_watcher_truncation_pending\*.json -Force`.
3. Disable the routine at https://claude.ai/code/routines until the
   helper is fixed.

The forward state of the watcher prompt is canonicalized in
`docs/STRATEGY/COWORK_WATCHER_PROMPT.md`. Operator updates the
routine there.

## Future maintenance

If the helper's JSON schema changes (new fields, renamed verdicts),
regenerate the reference sample:

```powershell
.\scripts\watcher-check-truncation.ps1 `
    -Paths @(<the four production files>) `
    -OutFile "scripts/_watcher_truncation_check.json"
```

and commit the regenerated `_watcher_truncation_check.json` so the
new shape is visible in the diff.
