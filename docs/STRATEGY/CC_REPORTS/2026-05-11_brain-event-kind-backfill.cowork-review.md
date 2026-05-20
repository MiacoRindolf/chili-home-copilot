# COWORK REVIEW: f-brain-event-kind-backfill (Phase 1c)

**Session ID:** `brain-event-kind-backfill-execute-2026-05-11`
**CC report:** `docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-backfill.md`
**Brief:** `docs/STRATEGY/QUEUED/f-brain-event-kind-backfill.md`
**Parent initiative:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
**Reviewed by:** Cowork scheduled-task watcher (autonomous, STEP D)
**Reviewed at:** 2026-05-11T20:42:13+00:00

## Verdict

**ACCEPTED — clean.** No regressions surfaced; auto-unpause approved.

## Why auto-review is appropriate

* `state=idle` at watch time, `last.passed=true`, `exit_code=0`,
  `verify_exit=0`, `stderr_bytes=0`, `duration_sec=538.6` (~9min, well
  inside `timeout_min=120`). `started_at=2026-05-11T20:31:15Z`,
  `ended_at=2026-05-11T20:40:14Z`.
* Execution-only delivery as briefed — zero `app/` code, zero
  migrations, zero schema. Phase 1c was deliberately operator-driven
  for the actual UPDATE runs; CC's job here was to ship the script +
  pre-flight memos + runbook + report, and that is exactly what landed.
* Session commit `3cdc8992de37e3af51e6182bc858d2460196c922`
  ("feat(brain): Phase 1c brain-event-backfill script + memos +
  runbook") touches exactly 6 files: 4 docs + 1 PS1 script +
  `NEXT_TASK.md` (STATUS flip to DONE). All within scope. Zero
  modifications to the forbidden set (`auto_trader.py`,
  `broker_service.py`, `venue/coinbase_spot.py`,
  `venue/robinhood_spot.py`, `bracket_writer_g2.py`, `bracket_*.py`,
  `app/trading_brain/*`). No scope drift.
* Uncommitted working-copy modifications surfaced by `git status`
  (`app/config.py`, `app/migrations.py`,
  `app/services/trading/brain_work/ledger.py`,
  `app/services/trading/promotion_gate.py`) predate this session —
  inherited from Phase 1b/Phase 2 work that already shipped via
  `2e9365c`/`fd2e687`. Not this session's responsibility.
* CC report grep for
  `WARN|FAIL|regression|STOP|ABORT|halt|parity break|hard gate failed`
  yielded 8 matches, all describing **intentional features** of the
  script: `HALTED` log status for the kill switch, `GATED`
  warn+pause for `market_snapshots_batch`, "fail-fast gates" for the
  `-EventType` whitelist, "abort path"/"aborting mid-batch" for the
  `NOT EXISTS` collision guard. None are findings of regression or
  breakage.

## What shipped (mirroring CC report)

| Deliverable | Path |
|---|---|
| D1 — backfill script  | `scripts/brain-event-backfill.ps1` (235 lines)            |
| D2a — pre-flight memo | `docs/AUDITS/2026-05-11_backfill_safety_backtest_completed.md` (145 lines) |
| D2b — pre-flight memo | `docs/AUDITS/2026-05-11_backfill_safety_breakout_alert_resolved.md` (183 lines) |
| D3 — operator runbook | `docs/RUNBOOKS/BRAIN_EVENT_BACKFILL.md` (255 lines)        |
| D4 — CC report        | `docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-backfill.md` (166 lines) |

Key D1 contract anchors (all defensive, all whitelisted):

* `-EventType` is `[Parameter(Mandatory=$true)]` and whitelist-validated
  against the 7 known Phase 1a orphan types. Unknown types exit
  non-zero before any SQL.
* `-DryRun` defaults to `$true`; live mode requires explicit
  `-DryRun:$false`.
* Inter-batch sleep hardcoded (`$INTER_BATCH_SLEEP_SECONDS = 30`). No
  CLI override.
* Backfill marker `phase_1c_backfill_2026_05_11` keyed via `jsonb_set`
  into `payload->>'backfill_source'`; candidate SELECT excludes marked
  rows so re-runs are idempotent.
* `NOT EXISTS` guard against `uq_brain_work_events_open_dedupe` partial
  unique index prevents naive `done → pending` flip from colliding
  with an organic non-terminal row sharing `dedupe_key`. Surfaced by
  CC while reading `app/migrations.py:5842-5849` — outside the brief
  but correctly anticipated.
* Kill switch: `Test-Path scripts/brain-event-backfill-stop.flag` at
  the top of each batch iteration → exit 0 with `HALTED` progress-log
  entry.
* `market_snapshots_batch` is **GATED** by a script-level warn +
  5s pause before any live run, plus a runbook section, plus a
  footer in both D2 memos. CC's judgment call (operator-approved
  during plan) — chose not to spawn a third D2c memo. Reasonable.

Production behaviour at merge: **zero change**. The actual UPDATE
runs are operator-controlled via the script. No row was flipped in
this session.

## Surprises / deviations — all reasonable

1. **Live-mode psql transport** swapped from the plan's literal
   `psql -h ... -U ... -d ... PGPASSWORD=...` to
   `docker compose exec -T postgres psql -U chili -d chili` (codebase
   convention used by every `scripts/dispatch-*.ps1`). Avoids a host
   `psql` install dependency on the operator's workstation and keeps
   credentials inside the container. Same SQL, same parameter
   hygiene. CC flagged this in the report — accepted.
2. **`uq_brain_work_events_open_dedupe` collision guard** not in
   brief; CC surfaced it from the migration code and built the
   `NOT EXISTS` defense proactively. Documented in script header.
   Accepted.
3. **Backfill marker name** `phase_1c_backfill_2026_05_11` — the
   approved plan selected this from the brief's two suggestions.
   Shipped as-is.

## Open questions for Cowork (carry forward, NOT gating)

CC report's three open questions are genuinely Cowork-side
decisions; surfacing here so the next plan cycle can address them:

1. **`mine_patterns` inner contract** — Phase 1b runbook noted
   `mine_patterns` has no event-level dedupe. The 179
   `market_snapshots_batch` rows stay GATED until that contract memo
   ships. Decide whether a D2c equivalent earns its own brief or
   folds into the regime-ledger/mining refactor.
2. **`pattern_eligible_promotion` consumer** — row count was 0 at
   brief time; Phase 1c will generate the first organic rows as
   `cpcv_gate` verdicts fire on the 1055 `backtest_completed`
   replays. `f-adaptive-cpcv-gate` (Phase 2) is presumably the
   consumer; confirm.
3. **`breakout_alert_resolved` wave sizing** — D2b argues EWMA blend
   converges after ~10 events per asset_type/tier bucket, so the tail
   of the 2659-row set is effectively no-op work. Recommend an
   explicit `-MaxRows 200` cap vs. running the full set for audit
   completeness on `scan_patterns.win_rate` etc.

## Pause flag

* `scripts/_claude_session_pause.flag` was originally placed
  `2026-05-11T01:29Z` (held ~19h) after the timed-out
  `coinbase-orphan-stop-adoption-2026-05-10` session.
* Operator already cleared the pause flag in commit `d80824b`
  ("promote: f-brain-event-kind-backfill (Phase 1c) + clear pause
  flag", `2026-05-11T20:13:19Z`) — 18min before this session started
  — which is why the supervisor was able to launch this Phase 1c
  execution at all.
* Confirmed via Windows-side `Glob` check: `_claude_session_pause.flag`
  is NOT present on disk. (The bash mount view still shows a stale
  inode at 123B mtime 01:29Z — a known mount-cache asymmetry,
  consistent with the recurring "bash mount-append silent-fail"
  pattern documented in `docs/STRATEGY/SIDECHANNEL/`.)
* No removal action needed — pause flag is already gone. Future
  queued sessions will pick up unimpeded.

## Carry-forward ESCALATE-AUTOTRADER concerns (NOT gating)

These remain open and will continue to surface in the decisions log
on every pulse where fresh probe data is available:

* **EXIT_MONITOR_DEAD** — status still TBD; output-writer silent-fail
  since `02:26Z` (probe outputs stale ~18h17m at this review time).
  Cannot verify exit_monitor cadence until operator restores
  `dispatch-*-out.txt` writes. Operator is separately diagnosing via
  dev daemon (pid 62932 took over from prior pid 50072,
  `dispatch-codex-plan-gate-probe.ps1` latest).
* **STALE_OPEN_TRADE** — 14 trades open >48h (max AAVE-USD #1809
  ~242h ~10.1d as of 12:48Z TIME-CORRECTION).
* **UNPROTECTED_POSITION** — TOTAL=17 (9 Coinbase +
  8 RH-crypto), all missing `broker_stop_order_id` at venue. ~$2,700+
  real-money exposure per
  `project_2026_05_10_naked_coinbase_positions.md`.
* **NEW_ERROR_TYPE** — bracket reconciler `PendingRollbackError` loop
  on FIDA-USD intent 256 and RARE-USD intent 1846.

All orthogonal to the Phase 1c acceptance.

## Recommended follow-up (Cowork side)

* Decide the three open questions above in the next plan cycle (most
  pressing: `mine_patterns` contract gate path).
* Phase 1c is the controlled mechanism for the ~4,000 historical
  orphan rows — operator now drives via
  `scripts/brain-event-backfill.ps1 -EventType <kind>` with `-DryRun`
  default true. Recommended order per runbook: `paper_trade_filled`
  → `live_trade_filled` → `broker_fill_recorded` →
  `market_snapshots_batch` (GATED, requires `mine_patterns` memo) →
  `backtest_completed` → `breakout_alert_resolved` last.
* Drought relief payload (1055 `backtest_completed` replays driving
  `cpcv_gate` verdicts → first organic `pattern_eligible_promotion`
  rows) is the actual measurable outcome. Watch the Phase 2
  adaptive-cpcv-gate consumer pickup once the operator runs the
  backfill.

-- Cowork (autonomous scheduled-task review)
