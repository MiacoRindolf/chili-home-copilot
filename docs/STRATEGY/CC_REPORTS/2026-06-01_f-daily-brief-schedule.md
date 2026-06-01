# CC_REPORT: f-daily-brief-schedule

**Type:** operator-directed, out-of-band ("go for everything", 2026-06-01;
commit→push→PR→merge per change). First of three "full send" deliverables
(schedule / teacher hook / UI). `NEXT_TASK.md` (phase-5i soak) untouched.

## What shipped

- **`app/services/trading/daily_trading_brief.py`** (new, built by a subagent,
  reviewed) — isolated orchestration over the existing on-demand brief stack:
  `generate_user_brief_html`, `persist_user_brief` (writes one HTML/user,
  swallows errors → None), `_active_user_ids` (thin patchable query), and
  `run_daily_brief_for_all_users` (per-user fault isolation → `{generated,
  failed, paths}`). Touches no broker/live state.
- **`app/services/trading_scheduler.py`** (my surgical edit) — new
  `_run_daily_trading_brief_job()` mirroring `_run_daily_prescreen_job` (opens its
  own `SessionLocal`, rollback-before-close, `run_scheduler_job_guarded`), plus a
  registration block (`CronTrigger` 17:00 America/Los_Angeles) gated on
  `include_web_light` AND `chili_daily_trading_brief_enabled`.
- **`app/config.py`** — `chili_daily_trading_brief_enabled` (default **False** —
  dormant), `_hour_pt` (17), `_window_hours` (24), `_dir` (`data/briefs`).

## Verification

- `tests/test_daily_trading_brief.py` (7 cases, DB-free): HTML generation,
  persistence to tmp dir, error-swallowing (returns None, no raise), user-id
  mapping, batch success/failure counting, empty-user list. All pass.
- Compile + import smoke: scheduler + module + config import cleanly; the job
  function imports; the flag defaults False. The registration block is additive
  and gated off by default — inert unless the operator enables it.

## Surprises / deviations

- The `User` model has no `active`/`is_active` column (only `id` + nullable
  `email`), so `_active_user_ids` keeps `db.query(User.id).all()`. If a real
  "active users" filter is wanted later, refine that one helper.

## Deferred

- No notification delivery — the job writes HTML files; a Telegram/`dispatch_alert`
  ping (net P/L summary) is a clean follow-up but adds external-send surface, so
  it's intentionally out of this first cut.
- Retention/cleanup of old brief files (currently overwrites `brief_user_<id>.html`
  per run — effectively keeps only the latest per user; fine for now).

## Open questions for Cowork

1. Enable in a soak (`CHILI_DAILY_TRADING_BRIEF_ENABLED=1`) once reviewed, or hold?
2. Want a daily Telegram summary ping in addition to the persisted HTML?
