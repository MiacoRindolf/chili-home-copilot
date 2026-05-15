"""Single funnel for reading authoritative pattern stats.

After f-canonical-outcome-layer Phase A (2026-05-14) the truth column
for trade_count / win_rate / avg_return_pct is ``corrected_*``. The
legacy ``{trade_count, win_rate, avg_return_pct}`` columns are still
populated (dual-written by
:func:`learning.update_pattern_stats_from_closed_trades`) so existing
indirect consumers don't break -- they remain a safe fallback when
``corrected_*`` is NULL during the merge window before the backfill
runs.

Readers should NOT inline ``getattr(pat, 'win_rate')`` -- route every
access through :func:`get_corrected_pattern_stats` so the
read-corrected-first / fallback-to-legacy contract has one home.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CorrectedPatternStats:
    trade_count: int | None
    win_rate: float | None
    avg_return_pct: float | None
    source_trade_count: str  # "corrected" | "legacy" | "missing"
    source_win_rate: str
    source_avg_return_pct: str


def _pick(pat: Any, corrected_attr: str, legacy_attr: str) -> tuple[Any, str]:
    v = getattr(pat, corrected_attr, None)
    if v is not None:
        return v, "corrected"
    v = getattr(pat, legacy_attr, None)
    if v is not None:
        return v, "legacy"
    return None, "missing"


def get_corrected_pattern_stats(pat: Any) -> CorrectedPatternStats:
    """Read corrected_* first; fall back to legacy when NULL.

    During the merge window between code-shipping and the one-shot
    backfill (``scripts/canonical-outcome-backfill.ps1``), most
    patterns will have corrected_* = NULL. Reading corrected-only
    would temporarily blacklist every pattern from promotion. The
    fallback is a strict improvement: post-backfill it is a no-op.
    """
    n, n_src = _pick(pat, "corrected_trade_count", "trade_count")
    wr, wr_src = _pick(pat, "corrected_win_rate", "win_rate")
    ret, ret_src = _pick(pat, "corrected_avg_return_pct", "avg_return_pct")
    try:
        n_int = int(n) if n is not None else None
    except (TypeError, ValueError):
        n_int = None
    try:
        wr_f = float(wr) if wr is not None else None
    except (TypeError, ValueError):
        wr_f = None
    try:
        ret_f = float(ret) if ret is not None else None
    except (TypeError, ValueError):
        ret_f = None
    return CorrectedPatternStats(
        trade_count=n_int,
        win_rate=wr_f,
        avg_return_pct=ret_f,
        source_trade_count=n_src,
        source_win_rate=wr_src,
        source_avg_return_pct=ret_src,
    )
