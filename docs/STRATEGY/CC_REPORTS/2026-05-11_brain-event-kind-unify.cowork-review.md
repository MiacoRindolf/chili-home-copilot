# COWORK REVIEW: f-brain-event-kind-unify (Phase 1b)

**Session ID:** `brain-event-kind-unify-execute-2026-05-11`
**CC report:** `docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-unify.md`
**Plan-gate dir:** `scripts/_claude_session_consult/brain-event-kind-unify-2026-05-11/`
**Reviewed by:** Cowork scheduled-task watcher (autonomous, STEP D)
**Reviewed at:** 2026-05-11T16:56:51+00:00

## Verdict

**ACCEPTED — clean.** No regressions surfaced; auto-unpause approved.

## Why auto-review is appropriate

* `state=idle` at watch time, `last.passed=true`, `exit_code=0`,
  `duration_sec=1971.4` (~33min, well inside `timeout_min=180`).
* Post-session `git status --porcelain` shows ONLY allowed Phase 1b
  files modified: `app/config.py`, `app/migrations.py`,
  `app/services/trading/brain_work/ledger.py`. Plus new test files
  and docs. Zero modifications to forbidden set
  (`auto_trader.py`, `broker_service.py`, `venue/coinbase_spot.py`,
  `venue/robinhood_spot.py`, `bracket_writer_g2.py`, `bracket_*.py`,
  `app/trading_brain/*`). No scope drift.
* CC report grep for `WARN|FAIL|regression|STOP|ABORT|halt|parity break|hard gate failed`
  yielded one match: the pytest summary line `17 passed, 8 warnings in 671.03s`.
  pytest-style "warnings" is a deprecation/category counter, not a
  STEP-D escalation signal.
* HEAD advanced to `2e9365c promote: f-brain-event-kind-unify (Phase 1b)
  — claimable outcome rows` — operator committed the work in-cycle.

## What shipped (mirroring CC report)

* Feature flag `chili_brain_outcome_claimable_enabled` (default `False`)
  unifies the `enqueue_outcome_event` write path with the `claim_work_batch`
  filter via `pending → processing → done` lifecycle.
* Migration 238 (`238_brain_work_events_claim_v2_index`) — partial index
  on `(domain, event_type, status, next_run_at)` filtered by
  `status IN ('pending','retry_wait')`. Index-only, no column adds.
* New tests: `tests/test_brain_work_event_kind_unify.py` (3) and
  `tests/test_brain_work_handler_idempotency.py` (9). Combined with
  pre-existing `tests/test_brain_work_ledger.py` (5) — **17 passed**.
* Runbook: `docs/runbooks/BRAIN_WORK_EVENT_KIND.md`.

Production behaviour at merge: **zero change** (flag default-off). The
operator-controlled flip is the Phase 1c entry condition.

## Consult-gated deviations — all four pre-approved

1. `release_stale_leases` flag-gating ADDED (lease-leak symmetry hazard).
2. Migration 238 column = `next_run_at` not `scheduled_at` (errata; the
   named column doesn't exist on `brain_work_events`).
3. `mark_work_done` Option 1 CONFIRMED (existing code already correct).
4. Handler-idempotency tests scoped to mock-based wrapper assertions
   (Phase 1b's hard gate); Phase 1c will verify inner-function
   idempotency contracts with real-data smoke before any UPDATE.

## Pause flag

* `scripts/_claude_session_pause.flag` (123B, mtime 2026-05-11T01:29Z,
  ~15h27m held) originated from the timed-out
  `coinbase-orphan-stop-adoption-2026-05-10` session.
* This Phase 1b session is unrelated (brain-side, default-off, no broker
  touch) and clean on every STEP D criterion.
* Per protocol, removing the pause flag so future queued sessions pick up.

## Carry-forward concerns (NOT gating, but operator should re-read)

These remain open and will continue to surface as ESCALATE-AUTOTRADER
entries in the decisions log:

* **EXIT_MONITOR_DEAD** — status TBD; output-writer silent-fail since
  `02:26Z` (probe outputs stale ~14h, spans ~19 watcher runs). Cannot
  verify exit_monitor cadence until operator restores
  `dispatch-*-out.txt` writes.
* **STALE_OPEN_TRADE** — 14 trades open >48h (max AAVE-USD #1809
  ~245h = ~10.2d).
* **UNPROTECTED_POSITION** — TOTAL=17 (9 Coinbase:
  FIDA/COTI/ACH/ALEPH/ACS/AERGO/1INCH/ACX/RARE + 8 RH-crypto:
  XLM/QNT/AVAX/RENDER/RAY/XRP/XPL/AAVE), all missing `broker_stop_order_id`
  at venue. ~$2,700+ real-money exposure per
  `project_2026_05_10_naked_coinbase_positions.md`.
* **NEW_ERROR_TYPE** — bracket reconciler `PendingRollbackError` loop on
  FIDA-USD intent 256 and RARE-USD intent 1846.

These are orthogonal to the Phase 1b acceptance.

## Recommended follow-up (Cowork side)

* Sanity-check the runbook's rollback SQL (`drain status='pending'
  outcome rows`) timestamp threshold before any prod flip — CC report
  explicitly flagged this as the one human-verification item.
* Phase 1c (`f-brain-event-kind-backfill.md`) remains the controlled
  mechanism for the ~4,000 historical orphan rows; do NOT enable
  retroactive replay of `market_snapshots_batch` rows until
  `mine_patterns` idempotency is verified.

-- Cowork (autonomous scheduled-task review)
