"""Phase 6: bounded one-line ops log for prediction mirror write/read (grep token + enums)."""

from __future__ import annotations

from .prediction_line_mapper import prediction_universe_fingerprint

CHILI_PREDICTION_OPS_PREFIX = "[chili_prediction_ops]"

# Dual-write outcome (inferred in learning.py; not logged from mirror_session).
DUAL_WRITE_NA = "na"
DUAL_WRITE_OK = "ok"
DUAL_WRITE_SKIP_EMPTY = "skip_empty"
DUAL_WRITE_FAIL = "fail"

# Read outcome (from phase5_apply_prediction_read metadata).
READ_NA = "na"
READ_COMPARE_OK = "compare_ok"
READ_COMPARE_MISS = "compare_miss"
READ_COMPARE_MISMATCH = "compare_mismatch"
READ_AUTH_MIRROR = "auth_mirror"
READ_FALLBACK_MISS = "fallback_miss"
READ_FALLBACK_EMPTY = "fallback_empty"
READ_FALLBACK_STALE = "fallback_stale"
READ_FALLBACK_PARITY = "fallback_parity"
READ_FALLBACK_INELIGIBLE = "fallback_ineligible"
READ_ERROR = "error"


def universe_fingerprint_fp16(ticker_batch: list[str]) -> str:
    """First 16 hex chars of universe fingerprint, or ``none`` (no raw tickers in log)."""
    fp = prediction_universe_fingerprint(ticker_batch)
    if not fp:
        return "none"
    return fp[:16]


def format_chili_prediction_ops_line(
    *,
    dual_write: str,
    read: str,
    explicit_api_tickers: bool,
    fp16: str,
    snapshot_id: int | None,
    line_count: int | None,
) -> str:
    """Single bounded INFO line; no ticker lists or blobs."""
    sid = "none" if snapshot_id is None else str(int(snapshot_id))
    lc = "none" if line_count is None else str(int(line_count))
    fp = fp16 if fp16 else "none"
    ea = str(bool(explicit_api_tickers)).lower()
    return (
        f"{CHILI_PREDICTION_OPS_PREFIX} dual_write={dual_write} read={read} "
        f"explicit_api_tickers={ea} fp16={fp} snapshot_id={sid} line_count={lc}"
    )
