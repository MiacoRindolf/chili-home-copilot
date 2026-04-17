"""Phase A: bounded one-line ops log for the economic-truth ledger shadow rollout.

Mirrors the shape of ``net_edge_ops_log.py`` and ``exit_engine_ops_log.py`` so
the same grep/soak discipline applies. A single INFO line per ledger write or
parity reconciliation, fixed field order, fixed enums, no raw provenance.

Release blocker (mirrors prediction-mirror + NetEdgeRanker + ExitEngine contract):
    Any line with ``mode=authoritative`` while ``brain_economic_ledger_mode``
    is not ``authoritative`` is a deploy blocker.

The canonical ledger is shadow-only until a later cutover phase. An
``authoritative`` line in logs from the current phase implies a leak.
"""

from __future__ import annotations

CHILI_LEDGER_OPS_PREFIX = "[ledger_ops]"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_COMPARE = "compare"
MODE_AUTHORITATIVE = "authoritative"

SOURCE_PAPER = "paper"
SOURCE_LIVE = "live"
SOURCE_BROKER_SYNC = "broker_sync"

EVENT_ENTRY_FILL = "entry_fill"
EVENT_EXIT_FILL = "exit_fill"
EVENT_PARTIAL_FILL = "partial_fill"
EVENT_FEE = "fee"
EVENT_ADJUSTMENT = "adjustment"
EVENT_RECONCILE = "reconcile"


def _fmt_float(v: float | None, digits: int = 4) -> str:
    if v is None:
        return "none"
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return "none"


def format_ledger_ops_line(
    *,
    mode: str,
    source: str,
    event_type: str,
    trade_ref: str,
    ticker: str,
    quantity: float | None,
    price: float | None,
    cash_delta: float | None,
    realized_pnl_delta: float | None,
    agree: bool | None = None,
) -> str:
    """Return a single bounded INFO line; no raw provenance or ticker lists."""
    tr = (trade_ref or "none")[:32]
    tk = (ticker or "none")[:24]
    et = (event_type or "none")[:20]
    ag = "none" if agree is None else ("true" if bool(agree) else "false")
    return (
        f"{CHILI_LEDGER_OPS_PREFIX} mode={mode} source={source} "
        f"event_type={et} trade_ref={tr} ticker={tk} "
        f"qty={_fmt_float(quantity, 6)} price={_fmt_float(price, 6)} "
        f"cash_delta={_fmt_float(cash_delta, 4)} "
        f"realized_pnl_delta={_fmt_float(realized_pnl_delta, 4)} "
        f"agree={ag}"
    )
