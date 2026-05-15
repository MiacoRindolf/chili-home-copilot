# f-triple-barrier-activation (Phase C of evidence-fidelity-architecture)

> **Type:** Scheduler job + invocation of existing labeler
> **Parent:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`

## Goal

`label_snapshots()` in `triple_barrier_labeler.py:273` is fully
implemented but has no scheduler caller. The
`trading_triple_barrier_labels` table is at 0 rows. This is the
canonical labeling primitive (López de Prado *Advances in Financial
Machine Learning*) — it answers "did this setup hit profit-target
before stop-loss?" which is strictly more informative than the
current N-bar-forward-return labeling. Once populated it enables a
meta-classifier ("take this signal vs skip") that filters false
positives from existing alpha without inventing new alpha.

## Design

### Scheduler job

Add to `scripts/scheduler_worker.py` (or wherever apscheduler jobs
are registered):

```python
from app.services.trading.triple_barrier_labeler import label_snapshots
from app.db import SessionLocal

def _label_recent_snapshots():
    sess = SessionLocal()
    try:
        report = label_snapshots(
            sess,
            limit=500,
            side="long",
            min_lookback_days=10,
        )
        logger.info(
            "[triple_barrier_labeler] cycle: requested=%d written=%d missing=%d",
            report.requested, report.written, report.missing_data,
        )
    finally:
        sess.close()

scheduler.add_job(
    _label_recent_snapshots,
    'interval',
    hours=4,
    id='triple_barrier_label_cycle',
    max_instances=1,
    coalesce=True,
)
```

### Flag flip

`brain_triple_barrier_mode` in config.py:317 currently defaults to
`shadow`. Phase C does NOT flip it to authoritative — that's a
separate operator decision. Shadow mode writes labels to the table
but doesn't yet feed the meta-classifier into the gate stack.

### Initial backfill

One-shot script `scripts/triple-barrier-backfill.ps1` that calls
`label_snapshots(limit=N, min_lookback_days=14)` repeatedly until the
oldest unlabeled snapshot is reached or `limit` exhausted. Operator-
controlled, `-DryRun` default.

## Deliverables

1. **`scripts/scheduler_worker.py`** (or equivalent): register the
   4h triple-barrier-label scheduler job
2. **`scripts/triple-barrier-backfill.ps1`** — one-shot historical backfill
3. **`tests/test_triple_barrier_scheduler.py`** — scheduler invocation
   test (run once, verify rows written, verify report shape)
4. **`docs/runbooks/TRIPLE_BARRIER_LABELING.md`** — operator runbook:
   what triple-barrier is, how to read labels, when to flip mode to
   authoritative, kill switch
5. **CC_REPORT**: `docs/STRATEGY/CC_REPORTS/2026-05-14_triple-barrier-activation.md`

## Hard constraints

- `brain_triple_barrier_mode` defaults to "shadow" at merge. Phase C
  does NOT flip it. Operator-controlled flip post-soak.
- Scheduler job uses `max_instances=1` + `coalesce=True` to prevent
  overlap if a cycle runs long.
- The labeler itself (`triple_barrier_labeler.py:label_snapshots`)
  is NOT modified. Read-only consumer.
- Backfill script: `-DryRun` default, kill switch via
  `scripts/triple-barrier-backfill-stop.flag`.

## Consult gate

Scheduler cadence — 4h vs 6h? Brief default 4h. Operator confirm.

## What this unlocks

Once `trading_triple_barrier_labels` has 1000+ rows, a per-family
meta-classifier becomes trainable. The classifier inputs: regime,
volatility, breadth, time-of-day, setup vitals. Output: P(take | hit
TP before SL given features). Phase F (separate brief, not in this
arc) would wire this classifier as an additional autotrader gate.
