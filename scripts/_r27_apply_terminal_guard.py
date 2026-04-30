"""R27: apply 'respect terminal trade state' guard to
apply_execution_event_to_trade. Edit tool truncated; recover via
HEAD checkout + string replace + ast.parse.
"""
import subprocess, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "app" / "services" / "trading" / "execution_audit.py"

head = subprocess.check_output(
    ["git", "show", "HEAD:app/services/trading/execution_audit.py"],
    cwd=str(ROOT),
).decode("utf-8")


OLD1 = """def apply_execution_event_to_trade(trade: Any, event: Any) -> None:
    requested = _safe_float(getattr(event, "requested_quantity", None))
    cumulative = _safe_float(getattr(event, "cumulative_filled_quantity", None))
    avg_fill = _safe_float(getattr(event, "average_fill_price", None))
    status = (getattr(event, "status", None) or "").strip().lower()

    if getattr(event, "submitted_at", None) and getattr(trade, "submitted_at", None) is None:"""

NEW1 = """def apply_execution_event_to_trade(trade: Any, event: Any) -> None:
    requested = _safe_float(getattr(event, "requested_quantity", None))
    cumulative = _safe_float(getattr(event, "cumulative_filled_quantity", None))
    avg_fill = _safe_float(getattr(event, "average_fill_price", None))
    status = (getattr(event, "status", None) or "").strip().lower()

    # R27 (2026-04-30): respect terminal trade states. This function does
    # NOT distinguish ENTRY fills from EXIT fills -- both look like
    # ``status='filled'`` from the broker. Without the guard below, a SELL
    # fill (exit) calls _finalize_filled_exit which sets trade.status =
    # 'closed', then this same fill flows through record_execution_event
    # which calls apply_execution_event_to_trade and the line "trade.status
    # = 'open' if status == 'filled'" flips the closed trade BACK to open.
    # Symptom: trade has exit_price + exit_date + pnl set, but
    # status='open' -- and downstream loops (bracket reconciler, exit
    # monitor) keep firing on the still-open-locally trade with no broker
    # position. Real bug found 2026-04-30 on ADT/WDCX/ABEV.
    #
    # Once a trade is closed or cancelled with an exit_date populated, it
    # is terminal -- record everything else from the event but do not
    # reanimate trade.status.
    _is_terminal = (
        (getattr(trade, "status", "") or "").strip().lower() in ("closed", "cancelled")
        and getattr(trade, "exit_date", None) is not None
    )

    if getattr(event, "submitted_at", None) and getattr(trade, "submitted_at", None) is None:"""


OLD2 = """    if requested is not None:
        if trade.quantity in (None, 0):
            trade.quantity = requested
        if cumulative is not None:
            trade.remaining_quantity = max(0.0, requested - cumulative)
    if cumulative is not None:
        trade.filled_quantity = cumulative
        if getattr(trade, "remaining_quantity", None) is None and requested is not None:
            trade.remaining_quantity = max(0.0, requested - cumulative)
        if cumulative > 0 and status in ("filled", "partially_filled", "open"):
            trade.status = "open" if status == "filled" else "working"
    if avg_fill is not None and avg_fill > 0:
        trade.avg_fill_price = avg_fill
        if status == "filled":
            trade.entry_price = avg_fill
    # Safety: if the broker reports cancelled/rejected/failed but the
    # order actually executed (cumulative_filled_quantity > 0), prefer
    # the fill over the state field. RH's state can briefly report
    # terminal-not-filled for filled orders (observed on autotrader
    # WGS/GH/INFQ on 2026-04-21, broker_order_ids 69e7a26d / 69e7a261 /
    # 69e7a52f all reported filled via rh.orders.get_stock_order_info
    # but had state 'cancelled' at the moment of sync).
    has_real_fill = cumulative is not None and cumulative > 0
    if status in ("cancelled", "canceled"):
        trade.status = "open" if has_real_fill else "cancelled"
    elif status in ("rejected", "failed", "expired"):
        if has_real_fill:
            trade.status = "open"
        else:
            trade.status = "rejected" if status in ("rejected", "failed") else "cancelled"
    elif status in ("queued", "pending", "confirmed", "unconfirmed", "open", "partially_filled"):
        trade.status = "working"
    elif status == "filled":
        trade.status = "open" """

NEW2 = """    if requested is not None:
        if trade.quantity in (None, 0):
            trade.quantity = requested
        if cumulative is not None:
            trade.remaining_quantity = max(0.0, requested - cumulative)
    if cumulative is not None:
        trade.filled_quantity = cumulative
        if getattr(trade, "remaining_quantity", None) is None and requested is not None:
            trade.remaining_quantity = max(0.0, requested - cumulative)
        if not _is_terminal and cumulative > 0 and status in ("filled", "partially_filled", "open"):
            trade.status = "open" if status == "filled" else "working"
    if avg_fill is not None and avg_fill > 0:
        trade.avg_fill_price = avg_fill
        # Only seed entry_price from this event if the trade has not
        # already closed -- on EXIT fills this same event would otherwise
        # smash entry_price with the SELL avg fill price.
        if not _is_terminal and status == "filled" and not getattr(trade, "exit_date", None):
            trade.entry_price = avg_fill
    # Safety: if the broker reports cancelled/rejected/failed but the
    # order actually executed (cumulative_filled_quantity > 0), prefer
    # the fill over the state field. RH's state can briefly report
    # terminal-not-filled for filled orders (observed on autotrader
    # WGS/GH/INFQ on 2026-04-21, broker_order_ids 69e7a26d / 69e7a261 /
    # 69e7a52f all reported filled via rh.orders.get_stock_order_info
    # but had state 'cancelled' at the moment of sync).
    has_real_fill = cumulative is not None and cumulative > 0
    if not _is_terminal:
        if status in ("cancelled", "canceled"):
            trade.status = "open" if has_real_fill else "cancelled"
        elif status in ("rejected", "failed", "expired"):
            if has_real_fill:
                trade.status = "open"
            else:
                trade.status = "rejected" if status in ("rejected", "failed") else "cancelled"
        elif status in ("queued", "pending", "confirmed", "unconfirmed", "open", "partially_filled"):
            trade.status = "working"
        elif status == "filled":
            trade.status = "open" """


def apply():
    content = head
    if OLD1.rstrip() not in content:
        print("OLD1 not found"); sys.exit(1)
    content = content.replace(OLD1.rstrip(), NEW1.rstrip())
    print("inserted terminal-guard preamble")

    if OLD2.rstrip() not in content:
        print("OLD2 not found"); sys.exit(1)
    content = content.replace(OLD2.rstrip(), NEW2.rstrip())
    print("gated status-mutation branches behind not _is_terminal")

    try:
        ast.parse(content)
        print("ast OK")
    except SyntaxError as e:
        print(f"SYNTAX line {e.lineno}: {e.msg}")
        for i, line in enumerate(content.split("\n")[max(0, e.lineno - 5):e.lineno + 3], start=max(1, e.lineno - 4)):
            print(f"{i}: {line[:120]}")
        sys.exit(1)

    target.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {len(content.splitlines())} lines to {target}")


if __name__ == "__main__":
    apply()
