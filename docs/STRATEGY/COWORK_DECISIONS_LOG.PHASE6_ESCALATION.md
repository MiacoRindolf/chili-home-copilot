# Phase 6 escalation — sidecar (decisions log appends are being clobbered by concurrent writer)

Written 2026-05-10T17:14:30Z by cowork-watcher-chili scheduled task. Multiple
attempts to `>> COWORK_DECISIONS_LOG.md` succeeded at the byte level (file
size grew) but the appended bytes were not visible on subsequent reads —
indicating a concurrent Windows-side writer is rewriting the file.
Documenting the escalation here so the operator can see it.

## Status

- `scripts/_claude_session_status.json` says `last.id =
  promotion-rebalance-phase6-2026-05-10`, `passed=true`, `state=idle`,
  ended `2026-05-10T10:06:46.1747374-07:00`, duration 1442.3s.
- `docs/STRATEGY/NEXT_TASK.md` shows `STATUS: DONE` for the entire
  `f-promotion-pipeline-rebalance` initiative.
- `docs/STRATEGY/CC_REPORTS/2026-05-10_f-promotion-pipeline-rebalance-phase6-final-summary.md`
  exists (14329B, mtime 17:03Z) and reads clean — doc-only, all hard
  rules ✅, three read-only SELECTs, no `.py`/migration/test/.env mods.
- `docs/STRATEGY/COWORK_REVIEWS/` does **not** yet contain a Phase 6
  review.

## SCOPE-DRIFT — pause flag content

`scripts/_claude_session_pause.flag` (190B, mtime 11:15:06Z) reads:

```
PAUSED 2026-05-10T17:02Z by cowork-watcher-chili scheduled task.
REASON: SCOPE-DRIFT during Phase 6 (promotion-rebalance-phase6-2026-05-10).
Phase 6 plan was approved as doc-only (no .py/migrations/tests/.env) but git index now shows:
  - 10 staged test deletions (2,568 lines): tests/test_vision.py,
    test_voice.py, test_volatility_dispersion_model.py,
    test_walk_forward.py, test_web_pattern_researcher_walk_forward.py,
    test_web_search.py, test_wellness.py, test_workflow_state.py,
    test_yf_breaker.py, test_yf_session_limiter.py
  - 4+ corrupted/control-char paths in UU/AD merge-conflict state:
    "./", "\004", "\b", "\t Y", "\324\277"
Phase 6 is still 'running' per status.json (started 16:42:43Z, elapsed
~20m, timeout 120m). The currently-running session will continue; this
pause prevents the next session from picking up.
Operator must investigate the staged test deletions and the UU/AD
merge-conflict markers before unpause + re-queue.
```

The flag mtime/content discrepancy (mtime 11:15Z, content stamped 17:02Z)
suggests rewrite-with-touch-r or a WSL bridge anomaly. Either way the
content is what's authoritative — a prior watcher run **did** detect
SCOPE-DRIFT mid-Phase-6.

## Git index corruption

`git status --porcelain` returns:

```
fatal: unable to read 6d50c33f00000000000000000000000000000000
```

A prior heartbeat reported `fatal: unknown index entry format
0x4c460000`. Both indicate `.git/index` is corrupt. STEP B2 scope-drift
detection is BLOCKED until the index is repaired.

## CC_REPORT vs reality

The Phase 6 CC_REPORT's "Hard rules check" claims:

> ✅ Phase 6 itself: doc-only; zero `.py` modifications; three read-only
> `SELECT`s for verification; no `.env` changes; no flag flips.

This is **contradicted** by the git-index state observed at the time the
prior watcher tripped the SCOPE-DRIFT gate. The 10 staged test deletions
+ corrupted UU/AD paths were present in the index *while Phase 6 was
running*. Either:

- (a) Those staged deletions are pre-existing cruft from earlier sessions
  (e.g., the Phase 4 truncation incident or earlier brain-side work) and
  Phase 6 itself didn't add them — in which case the CC_REPORT's claim
  is technically true but the index needs cleaning regardless;
- (b) Phase 6 actually did add those staged deletions — in which case the
  CC_REPORT is wrong and Phase 6 violated its own doc-only scope.

Without a working `git status` we cannot disambiguate.

## Decision

- **NOT** writing `COWORK_REVIEWS/2026-05-10_f-promotion-pipeline-rebalance-phase6.md`.
- **NOT** removing the pause flag.
- **NOT** auto-queueing.

## Operator action required

1. Repair `.git/index` — try `git fsck`, `git reflog`, restore from
   `.git/ORIG_HEAD` if present, or worst case `rm .git/index && git
   reset HEAD -- .` to rebuild from HEAD tree.
2. Once `git status --porcelain` works again, decide whether the 10
   staged test deletions are Phase 6 scope-creep or accumulated cruft.
3. Clear the corrupted UU/AD merge-conflict paths.
4. Investigate the COWORK_DECISIONS_LOG concurrent-writer race (lock
   file `COWORK_DECISIONS_LOG.md.lock` exists, 0B, mtime 14:29Z, and is
   permission-locked from this scheduled-task context).
5. Only then unpause + queue next initiative.

## Other STEP outputs (unchanged from prior runs)

- STEP E pulse-out STILL STALE: mtime 10:18:20Z, ~6h54m stale, 221556B
  unchanged across 22+ runs; pending file abandoned 12:14Z 37B
  unconsumed; per critical rule #5 NOT re-pending.
- STEP F autotrader-health-out STILL STALE: mtime 12:01:29Z, ~5h13m
  stale, 22510B unchanged single banner; daemon dispatch hung both
  paths; per rule #5 NOT re-pending.
- Last KNOWN-REAL state from 05:01 health probe: §4 crypto exit_monitor
  ALIVE 30/15m ~30s cadence (ACS-USD trade#1842 broker-qty defer
  recurring); §1/§2/§3/§8/§9 ALL psql column errors —
  UNPROTECTED_POSITION + STALE_OPEN_TRADE checks UNVERIFIABLE.
- Active escalations unchanged: (a) PROBE_SCHEMA_BROKEN —
  `t.broker → t.broker_source`, `autotrader_runs.occurred_at →
  created_at`; (b) NEW_ERROR_TYPE Groq auth_failed (key rotation needed
  via force-recreate); (c) NEW_ERROR_TYPE stop_engine FALLBACK_FIRED
  CRITICAL ATR=None on $0.0001917 ACS-USD trade#1842 ~every 2min.

## NEW escalation

**SCOPE-DRIFT-CONFIRMED + GIT-INDEX-CORRUPTED on Phase 6.**
Operator-only path forward.
