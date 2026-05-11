# Dispatcher silence audit (Phase 1a of f-adaptive-promotion-architecture)

Date: 2026-05-11
Author: Claude Code (executor)
Brief: `docs/STRATEGY/QUEUED/f-cpcv-gate-dispatcher-silence-audit.md`
Parent memo (Phase 0): `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`
Audit script: `scripts/audit-dispatcher-silence.ps1`
Raw output: `scripts/audit-dispatcher-silence-out.txt`

## TL;DR

The dispatcher is **NOT** silent. Phase 0 grepped for `brain_work:dispatch`
(colon), but `dispatcher.py:25` defines `LOG_PREFIX = "[brain_work_dispatch]"`
(underscore). Re-counting with the correct token shows **5 dispatch rounds
in the brain-worker container's current 4.5-hour uptime**, plus 5 paired
`[brain] work ledger dispatch round processed=...` lines from
`scripts/brain_worker.py:1092`. The dispatcher is running on the expected
~25–90 min cadence (gated by mining cycle length, not silence).

The rogue done-writer is **`app/services/trading/brain_work/ledger.py`,
line 103** — `enqueue_outcome_event` INSERTs `BrainWorkEvent` rows with
hard-coded `status="done"` and `event_kind="outcome"`. All
`emit_*_outcome` helpers in `app/services/trading/brain_work/emitters.py`
route to that INSERT. The 1055 historical `backtest_completed` rows
(202/24h current rate) are all `event_kind='outcome'`, `status='done'`,
`lease_holder=NULL`, `attempts=0` — born done, never claimed.

The real defect is architectural: every event type the dispatcher's
`_dispatch_limits` chain lists (`backtest_completed`,
`pattern_eligible_promotion`, `market_snapshots_batch`,
`live_trade_closed`, `paper_trade_closed`, `broker_fill_closed`,
`breakout_alert_resolved`) is emitted via an `emit_*_outcome` helper
that writes `event_kind='outcome'`. The dispatcher's `claim_work_batch`
(`ledger.py:184`) filters `event_kind = 'work'`. **Result: no
handler-targeted outcome event is ever claimed; the cpcv_gate, mine,
promote, demote, regime_ledger, pattern_stats, breakout_outcomes,
live_drift, and execution_robustness handlers have never fired against
production traffic from those event types.** The only work the
dispatcher actually drains is `backtest_requested` and
`execution_feedback_digest` (both correctly enqueued as
`event_kind='work'`).

This invalidates the Phase 1b assumption that synthetic
`backtest_completed` events will be drained. They will not be —
because Phase 1b would have to emit them via `enqueue_outcome_event`
(matching production), which writes them as already-`done` outcomes.
**Phase 1b must either (a) bypass the emitter and enqueue
`event_kind='work'` rows directly, or (b) wait for an upstream fix
that changes the producer kind.**

## Hypothesis verdicts

### H1 — `run_brain_work_dispatch_round` not running at all
**Status: RULED OUT.** Brain-worker container has been up since
2026-05-11T11:08:53Z. Five complete dispatch rounds visible (11:34Z,
12:00Z, 12:39Z, 13:06Z, 14:24Z), each emitting both
`[brain_work_dispatch] dispatch_market_snapshots emitted ...` (from
`dispatcher.py:611`) and `[brain] work ledger dispatch round
processed=N claimed=N per_type=...` (from `brain_worker.py:1092`).
`per_type` always shows `'backtest_completed': 0` because no work-kind
event of that type exists to claim (see H5).
Bootstrap is intact: `_maybe_run_brain_work_batch` is invoked at three
points in `brain_worker.py` (lines 1308, 1591, 1681). The dispatcher
cadence is gated by mining cycle length, not a missing call site.

### H2 — Logger filtered / log-level mismatch
**Status: CONFIRMED as the cause of Phase 0's "zero log lines" reading,
but RULED OUT as the cause of handler silence.** `dispatcher.py:25`
declares `LOG_PREFIX = "[brain_work_dispatch]"` (underscore). Every
handler module (`cpcv_gate.py:35`, `mine.py:24`, `promote.py:35`,
`demote.py:31`, `regime_ledger.py:26`, `pattern_stats.py:46`,
`breakout_outcomes.py:32`, `live_drift.py:29`,
`execution_robustness.py:25`) declares `[brain_work:<name>]` with a
colon. Phase 0 grepped `brain_work:dispatch` and counted zero, but
`brain_work_dispatch` returns 5 occurrences. Note: the handler grep
`brain_work:cpcv_gate` still returns zero because the cpcv_gate
handler genuinely never fires (per H5/H6 below) — this is a real
silence, not a grep artifact, for handlers.

Recommendation: future audit scripts should grep both variants. The
dispatcher could be renormalized to `[brain_work:dispatch]` for
consistency, but that's out of scope for Phase 1a (no `app/` edits).

### H3 — `brain_work_ledger_enabled()` returns False
**Status: RULED OUT.** In-container probe:
```
brain_work_ledger_enabled_setting=True
brain_work_ledger_enabled_call=True
brain_work_dispatch_batch_size=8
brain_work_cpcv_gate_batch_size=8
```
The feature flag is enabled; batch sizes are sane.
`chili_brain_dispatch_market_snapshots_enabled=True` and the 900s
interval gate is in effect — consistent with the 25–90 min observed
cadence on a busy mining cycle.

### H4 — `learning.py` is the rogue done-writer
**Status: RULED OUT.** `learning.py` contains zero references to
`BrainWorkEvent` or `brain_work_events`. Repo-wide audit confirms only
six files touch the table:
- `app/services/trading/brain_work/ledger.py` (58 hits — the producer + reader)
- `app/services/trading/brain_work/dispatcher.py` (1 hit — imports from ledger)
- `app/services/trading/brain_work/__init__.py` (4 hits — re-exports)
- `app/services/trading/cron_jobs/__init__.py` (1 hit — docstring only)
- `app/services/trading/brain_work/handlers/__init__.py` (1 hit — docstring only)
- `app/models/trading.py` (3 hits — ORM model)
- `app/migrations.py` (19 hits — schema)
The `run_learning_cycle` legacy gate flag is irrelevant; that code
path doesn't write to `brain_work_events`.

### H5 — `backtest_queue_worker.py` marks its own events done
**Status: CONFIRMED, with precision: the actual rogue done-writer is one
level deeper.**

- The hard-coded `status="done"` INSERT lives at
  **`app/services/trading/brain_work/ledger.py:103`**, inside
  `enqueue_outcome_event` (lines 72–113). It writes
  `event_kind="outcome"`, `status="done"`, `processed_at=now` in the
  same statement — the row is born terminal.
- That function is called from `app/services/trading/brain_work/
  emitters.py` lines 57, 80, 105, 131, 166, 195, 246, 278 (every
  `emit_*_outcome` helper in the module).
- The specific chain for `backtest_completed`:
  `backtest_queue_worker.py:202` →
  `emit_backtest_completed_outcome` (emitters.py:209–251) →
  `enqueue_outcome_event` (ledger.py:72–113) →
  INSERT with `status="done"` at ledger.py:103.
- `dispatcher.py:80` ALSO calls `enqueue_outcome_event` directly to
  emit `backtest_completed` after `_handle_backtest_requested` runs a
  backtest, so the dispatcher itself produces outcome rows for the 32
  historical `backtest_requested` work events it has processed.
- `cpcv_gate.py:149` calls `enqueue_outcome_event` to emit
  `pattern_eligible_promotion` — so even if cpcv_gate ran, its
  downstream event would also be born `outcome/done` and the
  `promote` handler would not claim it. The architectural defect is
  end-to-end.

The 1055 historical `backtest_completed` rows all show
`lease_holder=NULL`, `attempts=0`, `processed_at = created_at` (same
instant), confirming they never transited the work-queue lifecycle.

### H6 — A different handler is consuming under a different prefix
**Status: RULED OUT.** Across all six workers (brain-worker,
scheduler-worker, autotrader-worker, broker-sync-worker,
fast-data-worker, chili) and all nine handler prefixes
(`brain_work:cpcv_gate`, `brain_work:mine`, `brain_work:promote`,
`brain_work:demote`, `brain_work:regime_ledger`,
`brain_work:pattern_stats`, `brain_work:breakout_outcomes`,
`brain_work:live_drift`, `brain_work:execution_robustness`) the 24h
grep returns zero. The dispatcher's handler-import wiring is intact
(`dispatcher.py:316–437`) and `[handler_verify] OK 6/6` fires at
startup — handlers can be imported and called. They simply never get
a chance because `claim_work_batch` finds no work-kind events of
their type. There is no parallel consumer under a different log
prefix.

## Why the funnel is dead

`claim_work_batch` (`ledger.py:160–209`) builds its SQL with the hard
filter:

```sql
WHERE domain = 'trading'
  AND event_kind = 'work'
  AND event_type = :etype
  AND status IN ('pending', 'retry_wait')
```

`_dispatch_limits` (`dispatcher.py:264–274`) iterates through nine
event types. Cross-checked against `enqueue_*` call sites:

| event_type | enqueued via | event_kind | dispatched? |
|---|---|---|---|
| `execution_feedback_digest` | `enqueue_or_refresh_debounced_work` → `enqueue_work_event` | `work` | YES (28 done lifetime) |
| `market_snapshots_batch` | `emit_market_snapshots_batch_outcome` → `enqueue_outcome_event` | `outcome` | NO (179 done lifetime, all born done) |
| `backtest_requested` | `emit_backtest_requested_for_pattern` → `enqueue_work_event` | `work` | YES (32 done lifetime) |
| `backtest_completed` | `emit_backtest_completed_outcome` → `enqueue_outcome_event` | `outcome` | NO (1055 done lifetime) |
| `pattern_eligible_promotion` | `cpcv_gate.py:149` → `enqueue_outcome_event` | `outcome` | NO (0 lifetime; gate doesn't fire) |
| `live_trade_closed` | `emit_live_trade_closed_outcome` → `enqueue_outcome_event` | `outcome` | NO (4 done lifetime) |
| `paper_trade_closed` | `emit_paper_trade_closed_outcome` → `enqueue_outcome_event` | `outcome` | NO (1 done lifetime) |
| `broker_fill_closed` | `emit_broker_fill_closed_outcome` → `enqueue_outcome_event` | `outcome` | NO (131 done lifetime) |
| `breakout_alert_resolved` | `emit_breakout_alert_resolved_outcome` → `enqueue_outcome_event` | `outcome` | NO (2659 done lifetime) |

Two event types route through `enqueue_work_event` (which writes
`event_kind='work'`, `status='pending'`) and ARE drained. The other
seven route through `enqueue_outcome_event` (`event_kind='outcome'`,
`status='done'`) and are never drained — they exist purely as audit
trail.

The cpcv_gate, mine, promote, demote, regime_ledger, pattern_stats,
breakout_outcomes, live_drift, and execution_robustness handlers
have never run against production traffic of their target event
types. The startup `[handler_verify] OK 6/6` proves they can be
imported and called; nothing has ever called them.

## Phase 1b safety recommendation

A literal "enqueue 275 synthetic `backtest_completed` events" backfill
using `emit_backtest_completed_outcome` would write 275 more
`event_kind='outcome'`, `status='done'` rows. The cpcv_gate handler
would still not fire — the events would already be terminal at INSERT
time. The backfill would look successful (rows visible in
`brain_work_events`), but `cpcv_n_paths` would remain NULL across the
275, just as it has for the 1055 historical `backtest_completed`
outcome rows.

Two viable Phase 1b shapes, in order of operator preference:

**Option A — Direct work-kind enqueue (recommended for Phase 1b).**
Phase 1b enqueues `backtest_completed` rows directly via
`enqueue_work_event` (not `enqueue_outcome_event`), with `event_kind='work'`
and `status='pending'`. The dispatcher will claim them on the next
round, route them to `cpcv_gate.handle_backtest_completed`, and we
will see the `[brain_work:cpcv_gate]` log lines that Phase 0 found
missing. This requires a new, single-purpose helper in
`scripts/` (NOT in `app/`) that uses `enqueue_work_event` with a
distinct `source="cpcv_backfill_2026_05_11"` payload tag for audit
trail. The Phase 1b brief should specify it explicitly to avoid the
maintainer reaching for the existing emitter.

**Option B — Synthesize work events AND fix the producer (correct
long-term answer, but blocks on a code change to `app/`).**
The architectural fix is to convert the seven handler-targeted
emitters in `emitters.py` from `enqueue_outcome_event` to
`enqueue_work_event` (with an `outcome` audit row separately emitted
on successful handler completion via `mark_work_done` + a new outcome
helper). That's a non-trivial change with replay/idempotency
implications across all the affected handlers and is OUT OF SCOPE
for Phase 1a per the brief. It belongs in Phase 2 (adaptive-promotion
redesign) or a dedicated `f-brain-emitter-kind-fix` brief.

**Concrete handoff for Phase 1b:**
1. Enumerate the 275 candidate patterns (the parent brief's universe;
   re-run the Phase 0 query at `audit-cpcv-gate-coverage.ps1` if a
   refresh is needed).
2. For each, enqueue a single `event_kind='work'`, `event_type='backtest_completed'`
   row via `enqueue_work_event` with `dedupe_key=f"cpcv_backfill_2026_05_11:{pid}"`
   and `payload={"scan_pattern_id": pid, "source": "cpcv_backfill_2026_05_11", "synthetic": True}`.
   Rate-limit at `brain_work_cpcv_gate_batch_size = 8` per dispatch
   round (so ~8 patterns / 75-90s = ~6-8/min steady state).
3. Expected drainer: brain-worker's `_maybe_run_brain_work_batch` →
   `run_brain_work_dispatch_round` → `cpcv_gate.handle_backtest_completed`.
4. Expected throughput: the 275 candidates drain in 35–60 min of
   dispatcher time (gated by mining cycle interleaving).
5. Expected outcome split (from Phase 0 force-eval): a subset reaches
   CPCV and produces `cpcv_n_paths` numeric values (lifecycle →
   `backtested` or `challenged`); a subset short-circuits at the
   ensemble pre-gate (lifecycle → `challenged`, `cpcv_n_paths` stays
   NULL — Phase 0 finding, not a Phase 1b regression).

**Observability hooks for the Phase 1b smoke:**
- Watch `docker logs --since 5m chili-home-copilot-brain-worker-1 |
  grep "brain_work:cpcv_gate"` — should produce dozens of lines once
  the backfill enqueues work rows.
- Watch the dispatch round summary: `per_type={'backtest_completed':
  N}` should be > 0 for the rounds following enqueue.
- Watch `brain_work_events` for the synthetic rows transitioning
  `pending → processing → done` (visible because they carry the
  distinct `source` tag in their payload).

## Open questions for Cowork

1. **Is the outcome/work emit split intentional?** The Phase 2 docstring
   (`brain_work/handlers/__init__.py`) says "each handler subscribes to
   a specific event_type" — but the producer side writes those event
   types as `outcome` (terminal). Was the design intent that outcome
   events should ALSO be claimable by handlers (a kind-agnostic queue),
   or that the producers should write `work` rows that the dispatcher
   processes and then a separate `outcome` row gets written on
   completion? The current code does neither cleanly.
2. **Should the dispatcher's LOG_PREFIX be normalized?** Renaming
   `dispatcher.py:25` from `[brain_work_dispatch]` (underscore) to
   `[brain_work:dispatch]` (colon) would prevent the next grep
   mismatch. Out of scope for Phase 1a; flag for Phase 2 or a
   one-line follow-up.
3. **Is the `breakout_alert_resolved` 2659 lifetime count a missed
   opportunity?** That handler's prefix has produced 0 log lines —
   meaning 2659 alert outcomes are recorded but never aggregated
   into the secondary-evidence path the handler's docstring
   describes. Likely Phase 1c after the kind/work fix lands.

## Files produced by this audit

| Deliverable | Path                                                                    |
|-------------|-------------------------------------------------------------------------|
| D1 (script) | `scripts/audit-dispatcher-silence.ps1`                                  |
| D2 (run)    | `scripts/audit-dispatcher-silence-out.txt`                              |
| D3 (memo)   | `docs/AUDITS/2026-05-11_dispatcher_silence.md` (this file)              |
| D4 (report) | `docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-dispatcher-silence-audit.md` |
