"""R29: add apply_tca_on_trade_close call inside _finalize_filled_exit.

Edit tool truncated; recover from HEAD with single string-replace.
"""
import subprocess, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "app" / "services" / "trading" / "robinhood_exit_execution.py"

head = subprocess.check_output(
    ["git", "show", "HEAD:app/services/trading/robinhood_exit_execution.py"],
    cwd=str(ROOT),
).decode("utf-8")


OLD = """    trade.last_broker_sync = filled_at.replace(tzinfo=None)
    trade.broker_status = (raw_order.get(\"state\") or raw_order.get(\"status\") or trade.broker_status or \"filled\")
    db.add(trade)
    db.commit()"""

NEW = """    trade.last_broker_sync = filled_at.replace(tzinfo=None)
    trade.broker_status = (raw_order.get(\"state\") or raw_order.get(\"status\") or trade.broker_status or \"filled\")
    # R29 (2026-04-30): compute exit slippage. tca_reference_exit_price was
    # captured at decision time (~line 910 of this file when the exit was
    # submitted); trade.exit_price was just set above to the actual fill.
    # apply_tca_on_trade_close returns silently if either is None, so this
    # is a no-op for trades where the reference wasn't captured (e.g.
    # synthetic-close paths in broker_sync that R28 cleaned up). Without
    # this call the legitimate exit path was leaving tca_exit_slippage_bps
    # NULL even though both inputs were available -- that gap kept Phase F
    # producer (rebuild_all) from having any real slippage data to compute
    # rolling cost estimates from.
    try:
        from .tca_service import apply_tca_on_trade_close
        apply_tca_on_trade_close(trade)
    except Exception:
        logger.debug(
            \"[rh_exit] R29 apply_tca_on_trade_close failed for trade=%s\",
            getattr(trade, \"id\", None), exc_info=True,
        )
    db.add(trade)
    db.commit()"""


def apply():
    if OLD not in head:
        print("OLD not found"); sys.exit(1)
    content = head.replace(OLD, NEW)

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
