"""Purged / segmented validation for mined pattern candidates (lightweight CPCV-style)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable


def _row_time_key(row: dict[str, Any]) -> float:
    b = row.get("bar_start_utc")
    if isinstance(b, datetime):
        return b.timestamp()
    if hasattr(b, "timestamp"):
        try:
            return float(b.timestamp())  # type: ignore[no-any-return]
        except Exception:
            pass
    return 0.0


def mined_candidate_passes_purged_segments(
    filtered: list[dict[str, Any]],
    *,
    n_segments: int = 3,
    min_samples_per_segment: int = 2,
    min_positive_segments: int = 2,
    min_segment_mean_5d_pct: float = 0.0,
) -> tuple[bool, dict[str, Any]]:
    """Time-ordered segments: require positive mean 5d return in enough segments.

    Approximates combinatorial purged CV for discovery: unstable edges fail when
    performance concentrates in one era.
    """
    if not filtered:
        return False, {"reason": "empty"}

    ordered = sorted(filtered, key=_row_time_key)
    n = len(ordered)
    segs = max(2, int(n_segments))
    seg_len = max(1, n // segs)
    detail: dict[str, Any] = {"segments": []}
    positive = 0
    for s in range(segs):
        lo = s * seg_len
        hi = (s + 1) * seg_len if s < segs - 1 else n
        chunk = ordered[lo:hi]
        if len(chunk) < min_samples_per_segment:
            detail["segments"].append({"index": s, "n": len(chunk), "skipped": True})
            continue
        avg_5d = sum(float(r.get("ret_5d") or 0) for r in chunk) / len(chunk)
        seg_ok = avg_5d > min_segment_mean_5d_pct
        if seg_ok:
            positive += 1
        detail["segments"].append({
            "index": s,
            "n": len(chunk),
            "avg_5d": round(avg_5d, 4),
            "positive": seg_ok,
        })

    ok = positive >= min_positive_segments
    detail["positive_segments"] = positive
    detail["passes"] = ok
    return ok, detail


def filter_with_purged_gate(
    rows: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    **kw: Any,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """Apply predicate then purged-segment gate to the filtered subset."""
    filt = [r for r in rows if predicate(r)]
    ok, meta = mined_candidate_passes_purged_segments(filt, **kw)
    return filt, ok, meta


def decay_signals_from_walk_forward_windows(
    windows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fold-level spread and simple late-vs-early decay hints from bench windows."""
    rets: list[float] = []
    wrs: list[float] = []
    for w in windows:
        if not w.get("ok"):
            continue
        rp = w.get("return_pct")
        if rp is not None:
            rets.append(float(rp))
        wr = w.get("win_rate")
        if wr is not None:
            wrs.append(float(wr))
    if len(rets) < 2:
        return {}
    spread = max(rets) - min(rets)
    slope_neg = bool(rets[-1] < rets[0])
    out: dict[str, Any] = {
        "fold_return_spread": round(spread, 4),
        "first_last_return_slope_neg": slope_neg,
    }
    if len(wrs) >= 2:
        out["fold_wr_spread"] = round(max(wrs) - min(wrs), 4)
    return out
