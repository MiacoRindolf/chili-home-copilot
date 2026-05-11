# CC_REPORT: f-cpcv-gate-dispatcher-silence-audit

Date: 2026-05-11
Brief: `docs/STRATEGY/QUEUED/f-cpcv-gate-dispatcher-silence-audit.md`
Phase 0 memo (predecessor): `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`

## What shipped

- Commit: (added after `git commit` — pushed to `main`)
- Files added (5):
  - `scripts/audit-dispatcher-silence.ps1` — idempotent, read-only
    PowerShell audit that probes all six hypotheses H1–H6 and
    enumerates the rogue done-writer call chain.
  - `scripts/audit-dispatcher-silence-out.txt` — captured run output
    (~13.9 KB).
  - `docs/AUDITS/2026-05-11_dispatcher_silence.md` — one-page memo
    with H1–H6 verdicts + rogue-writer file:line + Phase 1b safety
    recommendation.
  - `docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-dispatcher-silence-audit.md` (this file).
- No `app/` code edits. No DB writes. No migrations / restarts /
  env edits.

## Verification

- Audit script ran end-to-end in ~2.5 min against live containers and
  the production Postgres (`docker exec ... psql -t -A -c SELECT ...`,
  no writes; in-container Python probes are SELECT-only and roll back
  in `finally`).
- All six hypotheses received an explicit status verdict.
- The rogue done-writer is identified by file:line:
  **`app/services/trading/brain_work/ledger.py:103`** — the
  `status="done"` literal inside `enqueue_outcome_event`. Routed
  through 9 `emit_*_outcome` helpers in `emitters.py`, called from
  `backtest_queue_worker.py:202`, `dispatcher.py:80`, `cpcv_gate.py:149`,
  and the trade-close paths.

## Hypothesis verdicts (one line each)

- **H1 — dispatcher not running:** RULED OUT. 5 rounds in 4.5h uptime;
  bootstrap intact at `brain_worker.py:1308,1591,1681`.
- **H2 — logger filtered / prefix mismatch:** PARTIALLY CONFIRMED.
  Dispatcher LOG_PREFIX = `[brain_work_dispatch]` (underscore) vs.
  Phase 0 grep `brain_work:dispatch` (colon). Real lines exist; Phase 0
  read silence because of separator. Handler silence (`brain_work:cpcv_gate
  = 0`) is real and unrelated to grep — those handlers genuinely never fire.
- **H3 — `brain_work_ledger_enabled()` False:** RULED OUT. Returns True;
  batch sizes sane (8 / 8).
- **H4 — `learning.py` rogue writer:** RULED OUT. Zero `BrainWorkEvent`
  references in `learning.py`; the table is only touched by
  `ledger.py`, `dispatcher.py`, models, and migrations.
- **H5 — `backtest_queue_worker.py` marking events done:** CONFIRMED
  (one level deeper). The actual INSERT is at `ledger.py:103`; the
  chain is `backtest_queue_worker.py:202` →
  `emit_backtest_completed_outcome` (emitters.py:209) →
  `enqueue_outcome_event` → `ledger.py:103`.
- **H6 — alternate handler under different prefix:** RULED OUT. Zero
  log lines across all six workers × nine handler prefixes. No parallel
  consumer.

## Root cause (single sentence)

`emit_*_outcome` helpers route through `enqueue_outcome_event`, which
writes `event_kind='outcome'` + `status='done'` in a single INSERT;
`claim_work_batch` filters `event_kind='work'`; therefore the
dispatcher never claims any of the seven handler-targeted event types
(`backtest_completed`, `pattern_eligible_promotion`,
`market_snapshots_batch`, `live_trade_closed`, `paper_trade_closed`,
`broker_fill_closed`, `breakout_alert_resolved`) — they exist as
audit-only outcome rows that no handler ever processes.

## Surprises / deviations

- **Broader than the brief expected.** The brief framed this as
  "why is cpcv_gate silent?" — the real answer is "ALL nine handlers
  are silent for outcome-kind event types, by design of the current
  emitter chain." This includes `mine`, `promote`, `demote`,
  `regime_ledger`, `pattern_stats`, `breakout_outcomes`, `live_drift`,
  and `execution_robustness`. The brain-worker has been running the
  legacy paths for these (via APScheduler / `run_learning_cycle` /
  per-cycle sweep hooks) the whole time; the event-driven handler
  architecture from Phase 2 of FIX 31 was never wired end-to-end. The
  startup `[handler_verify] OK 6/6` only proves the modules import.
- **Phase 0's "zero dispatcher logs" was a grep artifact.** The
  dispatcher IS logging; Phase 0 just used the wrong token. This
  inverts one of Phase 0's open questions ("dispatcher silence: bug
  or expected?") — the dispatcher is healthy; the handlers are the
  ones that have never seen traffic.
- **`breakout_alert_resolved` has 2659 lifetime outcome rows.** That's
  a substantial untapped secondary-evidence signal stuck behind the
  same kind/work defect. Worth surfacing separately for Cowork.

## Deferred

- **The architectural fix.** The brief is read-only; no `app/` edits.
  The fix (changing emitters from `enqueue_outcome_event` to
  `enqueue_work_event` plus a paired outcome-after-success emit) is
  a Phase 2 or `f-brain-emitter-kind-fix` work item.
- **Normalizing the dispatcher LOG_PREFIX** from `[brain_work_dispatch]`
  (underscore) to `[brain_work:dispatch]` (colon) for grep
  consistency. One-line change; left for a follow-up brief.
- **Phase 1b execution itself.** This audit ends with a recommended
  Phase 1b shape (enqueue work-kind rows directly via
  `enqueue_work_event` from `scripts/`); Cowork will write the
  Phase 1b brief calibrated to that recommendation.

## Open questions for Cowork

1. **Phase 1b kind decision.** Do we go with Option A (script-level
   `enqueue_work_event` bypass — fast; provides immediate cpcv_gate
   coverage data) or Option B (block on a producer-side emitter fix —
   correct long-term, but a multi-handler change)? My recommendation
   in the memo is Option A; calling it out explicitly here so the
   choice surfaces.
2. **Is the outcome/work split intentional?** Per memo §Open Questions
   item 1: was the design intent that outcome events would also be
   claimable, or that producers should write `work` rows? The current
   code does neither — outcomes are written terminal, work claims
   filter to `work`-kind only. Needs a Cowork architectural call before
   the emitter-fix scope can be drafted.
3. **`breakout_alert_resolved` backlog.** 2659 lifetime alert outcomes
   that the `breakout_outcomes` handler was meant to aggregate into
   pattern secondary evidence — currently unused. Should this become a
   Phase 1c or be folded into the emitter-fix scope?
