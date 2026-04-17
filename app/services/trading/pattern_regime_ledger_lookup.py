"""Phase M.2 — shared read-only lookup over the M.1 ledger.

The M.1 ledger (``trading_pattern_regime_performance_daily``) is
append-only: each day a full set of ``(pattern_id, regime_dimension,
regime_label)`` cells is written. All three M.2 slices (tilt,
promotion gate, kill-switch) need the *same* primitive: "for a given
pattern, what is the most recent confident cell per dimension?". This
module is that primitive, as a **pure function** that takes a plain
DB-session query helper (so tests can stub the DB without a full
``SessionLocal``). It never writes.

Key guarantees:

* **Confidence gate at the reader.** Cells with ``has_confidence=FALSE``
  (n_trades < ``brain_pattern_regime_perf_min_trades_per_cell``) are
  excluded unless the caller explicitly asks for all cells (diagnostics
  path). Consumers MUST NOT make policy decisions on unconfident cells.
* **Staleness clamp.** Cells older than ``max_staleness_days`` are
  ignored. Stale cells are treated as "unavailable" for that dimension.
* **Dimension universe is frozen** — exactly 8 regime dimensions that
  match M.1's ``DEFAULT_DIMENSIONS``. Callers cannot request arbitrary
  dimensions.
* **Deterministic hash.** ``resolved_context_hash(...)`` returns a 16-
  char sha256 slice over (pattern_id, sorted dimension+label+expectancy)
  so the three slices can share evaluation IDs when they're inspecting
  the same resolved context (useful for cross-slice audits in M.3).

No I/O happens in this module *directly*. The service layer is
expected to call :func:`load_resolved_context` with a live session;
the function runs one bounded SQL query.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

# Re-export for convenience so the M.2 slices don't need to import
# from the M.1 model directly.
from .pattern_regime_performance_model import (
    DEFAULT_DIMENSIONS,
    LABEL_UNAVAILABLE,
)

__all__ = [
    "LedgerCell",
    "ResolvedContext",
    "load_resolved_context",
    "resolved_context_hash",
    "summarise_context",
]


@dataclass(frozen=True)
class LedgerCell:
    """A single confident cell from the M.1 ledger."""

    pattern_id: int
    regime_dimension: str
    regime_label: str
    as_of_date: date
    window_days: int
    n_trades: int
    hit_rate: Optional[float]
    mean_pnl_pct: Optional[float]
    expectancy: Optional[float]
    profit_factor: Optional[float]
    has_confidence: bool


@dataclass
class ResolvedContext:
    """Everything the M.2 slices need for a single decision.

    ``cells_by_dimension`` contains at most one cell per dimension
    (the most recent confident one). Dimensions that had no confident
    cell within staleness are absent from the mapping.
    """

    pattern_id: int
    as_of_date: date
    max_staleness_days: int
    cells_by_dimension: Dict[str, LedgerCell] = field(default_factory=dict)
    unavailable_dimensions: Tuple[str, ...] = ()
    stale_dimensions: Tuple[str, ...] = ()
    # ``n_confident_dimensions`` = len(cells_by_dimension).
    # Exposed as a property for readability in callers.

    @property
    def n_confident_dimensions(self) -> int:
        return len(self.cells_by_dimension)

    @property
    def n_unavailable_dimensions(self) -> int:
        return len(self.unavailable_dimensions)

    def expectancies(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for dim, cell in self.cells_by_dimension.items():
            if cell.expectancy is not None and math.isfinite(float(cell.expectancy)):
                out[dim] = float(cell.expectancy)
        return out

    def mean_expectancy(self) -> Optional[float]:
        vals = list(self.expectancies().values())
        if not vals:
            return None
        return sum(vals) / len(vals)

    def negative_expectancy_dimensions(
        self, threshold: float = 0.0
    ) -> List[str]:
        """Dimensions whose expectancy is strictly below ``threshold``.

        Non-finite expectancies are treated as non-negative.
        """
        out: List[str] = []
        for dim, cell in self.cells_by_dimension.items():
            exp = cell.expectancy
            if exp is None:
                continue
            try:
                f = float(exp)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(f):
                continue
            if f < threshold:
                out.append(dim)
        return out


def load_resolved_context(
    db: Session,
    *,
    pattern_id: int,
    as_of_date: date,
    max_staleness_days: int,
    dimensions: Tuple[str, ...] = DEFAULT_DIMENSIONS,
) -> ResolvedContext:
    """Load the most recent confident cell per dimension for ``pattern_id``.

    Runs a single SQL query constrained by staleness window; the result
    is reduced in Python to at-most-one cell per dimension via ordering.
    Callers MUST own the session; this helper does not commit.
    """
    if not isinstance(pattern_id, int):
        raise TypeError(f"pattern_id must be int, got {type(pattern_id).__name__}")
    if not isinstance(as_of_date, date):
        raise TypeError("as_of_date must be a date")
    if int(max_staleness_days) < 0:
        raise ValueError("max_staleness_days must be >= 0")
    if not dimensions:
        raise ValueError("dimensions must be non-empty")

    cutoff = as_of_date - timedelta(days=int(max_staleness_days))

    rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (regime_dimension)
                regime_dimension,
                regime_label,
                as_of_date,
                window_days,
                n_trades,
                hit_rate,
                mean_pnl_pct,
                expectancy,
                profit_factor,
                has_confidence
            FROM trading_pattern_regime_performance_daily
            WHERE pattern_id = :pattern_id
              AND regime_dimension = ANY(:dims)
              AND as_of_date <= :as_of
              AND as_of_date >= :cutoff
              AND has_confidence = TRUE
            ORDER BY regime_dimension, as_of_date DESC, id DESC
            """
        ),
        {
            "pattern_id": int(pattern_id),
            "dims": list(dimensions),
            "as_of": as_of_date,
            "cutoff": cutoff,
        },
    ).fetchall()

    cells: Dict[str, LedgerCell] = {}
    seen_dimensions: set[str] = set()
    for row in rows:
        dim = str(row[0])
        if dim in cells:
            continue
        seen_dimensions.add(dim)
        cells[dim] = LedgerCell(
            pattern_id=int(pattern_id),
            regime_dimension=dim,
            regime_label=str(row[1]),
            as_of_date=row[2],
            window_days=int(row[3] or 0),
            n_trades=int(row[4] or 0),
            hit_rate=(float(row[5]) if row[5] is not None else None),
            mean_pnl_pct=(float(row[6]) if row[6] is not None else None),
            expectancy=(float(row[7]) if row[7] is not None else None),
            profit_factor=(float(row[8]) if row[8] is not None else None),
            has_confidence=bool(row[9]),
        )

    unavailable = tuple(sorted(set(dimensions) - set(cells.keys())))
    # Stale dimensions = unavailable dimensions that *did* have a cell
    # beyond the cutoff. We do a second tiny query only if stalemness
    # diagnosis is relevant (not perf-critical here: bounded at 8).
    stale_list: List[str] = []
    if unavailable:
        stale_rows = db.execute(
            text(
                """
                SELECT regime_dimension, MAX(as_of_date) AS last_at
                FROM trading_pattern_regime_performance_daily
                WHERE pattern_id = :pattern_id
                  AND regime_dimension = ANY(:dims)
                  AND has_confidence = TRUE
                  AND as_of_date <= :as_of
                  AND as_of_date < :cutoff
                GROUP BY regime_dimension
                """
            ),
            {
                "pattern_id": int(pattern_id),
                "dims": list(unavailable),
                "as_of": as_of_date,
                "cutoff": cutoff,
            },
        ).fetchall()
        stale_list = [str(r[0]) for r in stale_rows]

    return ResolvedContext(
        pattern_id=int(pattern_id),
        as_of_date=as_of_date,
        max_staleness_days=int(max_staleness_days),
        cells_by_dimension=cells,
        unavailable_dimensions=unavailable,
        stale_dimensions=tuple(sorted(stale_list)),
    )


def resolved_context_hash(ctx: ResolvedContext) -> str:
    """Deterministic 16-char fingerprint over the resolved context.

    Two calls that load the exact same set of (dimension, label, exp)
    tuples for the same pattern & date produce the same hash. Useful
    for audit trails and for linking the three slices' decision rows
    when they operated on identical evidence.
    """
    payload: Dict[str, object] = {
        "pattern_id": int(ctx.pattern_id),
        "as_of": ctx.as_of_date.isoformat(),
        "staleness": int(ctx.max_staleness_days),
        "cells": sorted(
            [
                {
                    "dim": dim,
                    "label": cell.regime_label,
                    "exp": (
                        round(float(cell.expectancy), 6)
                        if cell.expectancy is not None
                        and math.isfinite(float(cell.expectancy))
                        else None
                    ),
                    "n": int(cell.n_trades),
                }
                for dim, cell in ctx.cells_by_dimension.items()
            ],
            key=lambda x: x["dim"],
        ),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def summarise_context(ctx: ResolvedContext) -> Dict[str, object]:
    """Return a small JSON-safe dict for persistence in ``payload_json``."""
    return {
        "n_confident_dimensions": ctx.n_confident_dimensions,
        "unavailable_dimensions": list(ctx.unavailable_dimensions),
        "stale_dimensions": list(ctx.stale_dimensions),
        "expectancies": ctx.expectancies(),
        "mean_expectancy": ctx.mean_expectancy(),
    }
