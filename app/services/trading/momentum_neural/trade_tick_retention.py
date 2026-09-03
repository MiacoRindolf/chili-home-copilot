"""IQFeed trade-tick retention — a bounded, PRIMARY-KEY-RANGE prune of
``iqfeed_trade_ticks``. Sibling of ``nbbo_tape.prune_nbbo_tape`` (same job shape,
knobs and logging), but it cannot share that function's ``WHERE observed_at <``
delete: the trade tape's ``observed_at`` BRIN is damaged, so any time-predicate
scan over it runs for HOURS.

Why this exists (measured 2026-09-03): ``iqfeed_trade_ticks`` had NO retention —
94 GB / 248M rows, oldest row 2026-07-18 (47 days ⇒ ~5.3M rows/day). Autovacuum
at the default 0.2 scale factor waited for ~50M dead rows before a pass, so the
table reached dead ≈ live. Ids are BIGSERIAL and monotonic in time, so this prune

  1. finds the cutoff id by a bounded pk BISECTION — ≤ 64 probes of
     ``SELECT observed_at FROM iqfeed_trade_ticks WHERE id >= :probe ORDER BY id
     LIMIT 1`` (each is a single pkey index touch; log2(248M) ≈ 28 probes in
     practice), never a time scan;
  2. deletes ``id < cutoff_id`` in half-open PRIMARY-KEY RANGES of ``batch_ids``
     ids, committing EACH batch so dead tuples reach autovacuum incrementally and
     no long transaction ever holds the table;
  3. is BOUNDED per call (``max_batches``) so the 6-hourly scheduler job can never
     run for hours — a backlog drains across runs and the return dict reports
     whether ids remain (``exhausted``).

Retention default = 14 days: the IQFeed lookup feed (port 9100) is bit-identical
to our recording with 180-day provider retention, so anything older than the
local window can be re-pulled (memory: BUY the history, never lower fidelity).

``observed_at`` is a naive ``TIMESTAMP`` written in UTC by the host bridge;
all comparisons here are done on naive-UTC datetimes.

THE MONOTONIC PREMISE HAS KNOWN EXCEPTIONS (code-verified 2026-09-03, review of
this module): the bridge's ``_parse_selected_l1`` writes the trade row's
``observed_at`` from the frame's Most-Recent-Trade DATE+TIME, fenced only
against the FUTURE — the <=2 s age fence gates the QUOTE row only — and its
trade dedup is in-process, so the FIRST Q frame per symbol after every bridge
start / reconnect / halt resumption inserts a row carrying the PRIOR session's
(or, for a dormant name, a weeks-old) last print under a fresh id. Two guards
make that harmless here, in depth:

  * the DELETE itself carries ``AND observed_at < :cutoff`` — a retained row can
    never be removed no matter what the bisection returned. The access path is
    pinned to the pkey range by ``SET LOCAL enable_bitmapscan = off`` for the
    batch transaction: BRIN indexes are usable ONLY via bitmap scans, so this is
    exactly the switch that keeps the planner off the damaged observed_at BRIN
    (which it WOULD prefer once the time predicate's estimated selectivity drops
    below ~0.1 %, e.g. a weekend-shadow window) — the predicate is then evaluated
    on the heap tuples the pkey range already fetched, at unchanged cost;
  * the bisection fails CLOSED at the top of the tape: one stale newest row is
    corroborated by a second probe ``batch_ids`` ids lower before "the whole
    tape is old" is accepted; otherwise it bisects below the stale block.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings

logger = logging.getLogger(__name__)

TABLE = "iqfeed_trade_ticks"

# Fallbacks for the ``getattr(settings, ..., d) or d`` shape prune_nbbo_tape uses;
# the authoritative defaults + bounds + rationale live on the Settings fields.
_DEFAULT_RETENTION_DAYS = 14
_DEFAULT_BATCH_IDS = 200_000
_DEFAULT_MAX_BATCHES = 50
# Not a tunable: an int64 id space halves to width 1 in <= 63 steps, so 64 is the
# mathematical ceiling of a correct bisection. The cap only exists so a
# misbehaving probe (e.g. a non-monotonic tape) cannot loop forever.
_MAX_PROBES = 64


def _as_naive_utc(ts: datetime) -> datetime:
    """Normalize to naive UTC (the tape column is ``TIMESTAMP`` written in UTC)."""
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def retention_knobs(cfg: Any = None) -> dict[str, int]:
    """Resolve the prune knobs from ``settings`` (or any object exposing the same
    attributes), falling back to the code defaults when a knob is missing / None /
    non-numeric, and clamping to >= 1 so a bad override can never disable a bound."""
    cfg = settings if cfg is None else cfg

    def _int(name: str, default: int) -> int:
        try:
            v = int(getattr(cfg, name, default) or default)
        except (TypeError, ValueError):
            v = default
        return max(1, v)

    return {
        "retention_days": _int("chili_momentum_trade_tick_retention_days", _DEFAULT_RETENTION_DAYS),
        "batch_ids": _int("chili_momentum_trade_tick_prune_batch_ids", _DEFAULT_BATCH_IDS),
        "max_batches": _int("chili_momentum_trade_tick_prune_max_batches", _DEFAULT_MAX_BATCHES),
    }


def bisect_cutoff_id(
    probe: Callable[[int], Optional[datetime]],
    *,
    lo: int,
    hi: int,
    cutoff: datetime,
    corroborate_span: int = 0,
) -> tuple[int, Optional[datetime], int]:
    """Smallest id whose observed_at >= ``cutoff``, by primary-key bisection.

    ``probe(i)`` must return the ``observed_at`` of the FIRST row with ``id >= i``
    (``None`` when no such row) — i.e. ``SELECT observed_at ... WHERE id >= :i
    ORDER BY id LIMIT 1``. ``lo``/``hi`` are the table's min/max id. Ids are
    expected to be monotonic in observed_at (measured true for the BIGSERIAL trade
    tape up to the bridge's stale first-frame rows, see the module docstring).

    Returns ``(cutoff_id, boundary_observed_at, probes)`` where every row with
    ``id < cutoff_id`` is older than ``cutoff`` and ``boundary_observed_at`` is the
    observed_at of the first RETAINED row (``None`` when nothing is retained).
    ``cutoff_id == lo`` ⇒ nothing to delete; ``cutoff_id == hi + 1`` ⇒ every row
    is older than the cutoff. Gaps in the id space are fine: a probe that lands in
    a gap answers for the next existing row, which is what the invariant needs.

    ``corroborate_span`` > 0 makes the top-of-tape verdict fail CLOSED: when the
    newest row reads older than the cutoff, a second probe ``corroborate_span``
    ids below it must agree before "the whole tape is old" is returned; if that
    probe is retained, the newest row is a stale outlier (bridge first-frame
    print) and the bisection runs below it instead, deleting LESS.
    """
    cutoff = _as_naive_utc(cutoff)
    probes = 0

    first = probe(lo)
    probes += 1
    if first is None:
        return lo, None, probes  # empty range
    if _as_naive_utc(first) >= cutoff:
        return lo, first, probes  # nothing older than the cutoff

    last = probe(hi)
    probes += 1
    hi_ts: Optional[datetime] = last
    if last is None or _as_naive_utc(last) < cutoff:
        span = max(0, int(corroborate_span))
        below = hi - span
        if span <= 0 or below <= lo:
            return hi + 1, None, probes  # the whole range is older than the cutoff
        check = probe(below)
        probes += 1
        if check is None or _as_naive_utc(check) < cutoff:
            return hi + 1, None, probes  # corroborated: the whole range is old
        logger.warning(
            "[trade_tick_retention] newest row (id >= %d, observed_at=%s) reads older "
            "than the cutoff %s while id %d is retained — stale top-of-tape print; "
            "bisecting below it (fail closed)",
            hi, _as_naive_utc(last).isoformat(timespec="seconds") if last else None,
            cutoff.isoformat(timespec="seconds"), below,
        )
        hi, hi_ts = below, check

    # Invariant from here: probe(lo) < cutoff <= probe(hi).
    while hi - lo > 1 and probes < _MAX_PROBES:
        mid = (lo + hi) // 2
        ts = probe(mid)
        probes += 1
        # A None mid-range cannot happen on a real pkey (hi exists), but treat it
        # as "retained" — shrinking hi deletes LESS, never more.
        if ts is None or _as_naive_utc(ts) >= cutoff:
            hi, hi_ts = mid, ts
        else:
            lo = mid
    if hi - lo > 1:
        # Probe cap hit (unreachable on an int64 pkey; defensive): rows in (lo, hi)
        # are of unknown age, so fail closed — only ``id <= lo`` is known old.
        return lo + 1, None, probes
    return hi, hi_ts, probes


def plan_batches(
    min_id: int,
    cutoff_id: int,
    *,
    batch_ids: int,
    max_batches: int,
) -> list[tuple[int, int]]:
    """Half-open pk ranges ``[lo, hi)`` covering ``[min_id, cutoff_id)`` in spans of
    ``batch_ids``, at most ``max_batches`` of them (the per-call bound)."""
    batch_ids = max(1, int(batch_ids))
    max_batches = max(1, int(max_batches))
    out: list[tuple[int, int]] = []
    lo = int(min_id)
    cutoff_id = int(cutoff_id)
    while lo < cutoff_id and len(out) < max_batches:
        hi = min(lo + batch_ids, cutoff_id)
        out.append((lo, hi))
        lo = hi
    return out


def prune_trade_ticks(
    db: Session,
    *,
    retention_days: Optional[int] = None,
    batch_ids: Optional[int] = None,
    max_batches: Optional[int] = None,
    now_utc: Optional[datetime] = None,
) -> dict[str, Any]:
    """Trim ``iqfeed_trade_ticks`` older than the retention window by PRIMARY-KEY
    RANGE, one committed batch at a time, bounded by ``max_batches`` per call.
    Best-effort (mirrors ``prune_nbbo_tape``): never raises, logs a warning and
    returns ``ok=False`` with the progress made so far on failure."""
    knobs = retention_knobs()
    days = max(1, int(retention_days if retention_days is not None else knobs["retention_days"]))
    batch = max(1, int(batch_ids if batch_ids is not None else knobs["batch_ids"]))
    cap = max(1, int(max_batches if max_batches is not None else knobs["max_batches"]))
    now = _as_naive_utc(now_utc or datetime.now(timezone.utc))
    cutoff_ts = now - timedelta(days=days)
    t0 = time.monotonic()
    out: dict[str, Any] = {
        "ok": True,
        "deleted": 0,
        "batches": 0,
        "cutoff_id": None,
        "cutoff_observed_at": None,
        "seconds": 0.0,
        "retention_days": days,
        "retention_cutoff": cutoff_ts.isoformat(timespec="seconds"),
        "batch_ids": batch,
        "max_batches": cap,
        "probes": 0,
        "span_ids": 0,
        "remaining_ids": 0,
        "exhausted": True,
    }
    try:
        # The host IQFeed bridge owns this table (mig334); a fresh app DB may lack it.
        if db.execute(text("SELECT to_regclass(:t)"), {"t": TABLE}).scalar() is None:
            out["skipped"] = "no_table"
            return out
        min_id, max_id = db.execute(text(f"SELECT min(id), max(id) FROM {TABLE}")).one()
        if min_id is None or max_id is None:
            out["skipped"] = "empty"
            return out
        min_id, max_id = int(min_id), int(max_id)
        out["min_id"], out["max_id"] = min_id, max_id

        def _probe(i: int) -> Optional[datetime]:
            return db.execute(
                text(f"SELECT observed_at FROM {TABLE} WHERE id >= :probe ORDER BY id LIMIT 1"),
                {"probe": int(i)},
            ).scalar()

        cutoff_id, boundary_ts, probes = bisect_cutoff_id(
            _probe, lo=min_id, hi=max_id, cutoff=cutoff_ts, corroborate_span=batch
        )
        out["cutoff_id"] = cutoff_id
        out["cutoff_observed_at"] = (
            _as_naive_utc(boundary_ts).isoformat(timespec="seconds") if boundary_ts else None
        )
        out["probes"] = probes

        ranges = plan_batches(min_id, cutoff_id, batch_ids=batch, max_batches=cap)
        for lo, hi in ranges:
            # Pin the access path to the pkey range: BRIN indexes are usable ONLY
            # through bitmap scans, so this keeps the planner off the damaged
            # observed_at BRIN for the time predicate below. SET LOCAL is
            # transaction-scoped — it dies with this batch's commit.
            db.execute(text("SET LOCAL enable_bitmapscan = off"))
            # The observed_at guard is evaluated on the heap tuples the pkey range
            # already fetched: a retained row can never be deleted, whatever the
            # bisection returned (stale first-frame prints, see module docstring).
            res = db.execute(
                text(
                    f"DELETE FROM {TABLE} "
                    "WHERE id >= :lo AND id < :hi AND observed_at < :cutoff"
                ),
                {"lo": lo, "hi": hi, "cutoff": cutoff_ts},
            )
            db.commit()
            out["deleted"] += int(getattr(res, "rowcount", 0) or 0)
            out["batches"] += 1
            out["span_ids"] += hi - lo

        last_hi = ranges[-1][1] if ranges else min_id
        out["remaining_ids"] = max(0, cutoff_id - last_hi)
        out["exhausted"] = last_hi >= cutoff_id
        out["seconds"] = round(time.monotonic() - t0, 3)
        if out["batches"]:
            logger.info(
                "[trade_tick_retention] pruned %d rows over %d ids in %d batch(es) of %d ids "
                "(id < %d, observed_at < %s, %d probes) in %.1fs%s",
                out["deleted"], out["span_ids"], out["batches"], batch, cutoff_id,
                out["retention_cutoff"], probes, out["seconds"],
                "" if out["exhausted"] else
                f" — BOUNDED by max_batches={cap}; ~{out['remaining_ids']} ids remain for the next run",
            )
        return out
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        out["ok"] = False
        out["error"] = str(exc)[:120]
        out["seconds"] = round(time.monotonic() - t0, 3)
        logger.warning(
            "[trade_tick_retention] prune failed after %d batch(es)/%d rows: %s",
            out["batches"], out["deleted"], exc,
        )
        return out
