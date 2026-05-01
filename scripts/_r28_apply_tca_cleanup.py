"""R28: remove TCA corruption from synthetic-close paths.

Two code paths in broker_service.py write tca_reference_exit_price =
exit_price (same value as fill) and call apply_tca_on_trade_close,
which computes slippage = (ref - fill) / ref = 0 every time. That
populates tca_exit_slippage_bps with corrupt zeros that masquerade as
real measurements.

This script removes BOTH corrupt writes via two surgical string-replace
edits applied to a clean HEAD checkout, ast.parse-validated, written
back. Pure cleanup -- no caller behavior changes, no new logic.

After this fix:
  * sync_positions_to_db close path: tca_exit_slippage_bps stays NULL
    (was sometimes silently NameError'd, sometimes 0).
  * cleanup_manual_trades close path: tca_exit_slippage_bps stays NULL
    (was 0).
  * autotrader-decision exit path: unchanged (tca_reference_exit_price
    still captured at robinhood_exit_execution.py:910; the missing
    apply_tca_on_trade_close call in _finalize_filled_exit is deferred
    to a later round so R27's edit gets dwell time).
  * downstream readers: attribution_service.py already uses
    `t.tca_exit_slippage_bps or 0` -- no NULL-handling regression.
"""
import subprocess, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "app" / "services" / "broker_service.py"

head = subprocess.check_output(
    ["git", "show", "HEAD:app/services/broker_service.py"],
    cwd=str(ROOT),
).decode("utf-8")


# Edit 1: sync_positions_to_db close path (line ~1578-1584 in HEAD).
# Note: this block had a latent NameError because `exit_price` is not
# defined in the local scope -- the bare except swallowed it and the
# call silently no-op'd. Removing the dead code clarifies intent.
OLD1 = """        try:
            from .trading.tca_service import apply_tca_on_trade_close

            trade.tca_reference_exit_price = exit_price
            apply_tca_on_trade_close(trade)
        except Exception:
            pass
        try:
            from .trading.brain_work.execution_hooks import on_broker_reconciled_close

            on_broker_reconciled_close(db, trade, source=\"sync_positions_to_db\")
        except Exception:
            pass"""

NEW1 = """        # R28 (2026-04-30): TCA call removed from this synthetic-close path.
        # The original code did `tca_reference_exit_price = exit_price` then
        # called apply_tca_on_trade_close, but `exit_price` was undefined in
        # this scope (latent NameError silently swallowed by the bare
        # except). Even if the var were valid, setting ref = fill produces
        # slippage = 0 by construction -- a corrupt zero rather than a
        # genuine measurement. Leaving tca_exit_slippage_bps NULL is the
        # honest state for an externally-driven close where CHILI made no
        # decision and has no reference price.
        try:
            from .trading.brain_work.execution_hooks import on_broker_reconciled_close

            on_broker_reconciled_close(db, trade, source=\"sync_positions_to_db\")
        except Exception:
            pass"""


# Edit 2: cleanup_manual_trades close path (line ~1674-1680 in HEAD).
# Here `exit_price` IS defined locally (line ~1662) but is the same
# market quote that is written to trade.exit_price -- so ref == fill
# and slippage == 0 by construction. Same fix: remove the corrupt
# write.
OLD2 = """            try:
                from .trading.tca_service import apply_tca_on_trade_close

                trade.tca_reference_exit_price = exit_price
                apply_tca_on_trade_close(trade)
            except Exception:
                pass
            try:
                from .trading.brain_work.execution_hooks import on_broker_reconciled_close

                on_broker_reconciled_close(db, trade, source=\"cleanup_manual_trades\")"""

NEW2 = """            # R28 (2026-04-30): TCA call removed from this synthetic-close
            # path. The original code set tca_reference_exit_price = the
            # SAME market quote it had just written to trade.exit_price,
            # then called apply_tca_on_trade_close -- producing slippage =
            # (ref - fill) / ref = 0 every time. Corrupt zeros that
            # masquerade as real measurements. Leaving the column NULL
            # is the honest state when CHILI synthesized the close from a
            # market quote rather than a decision-time reference.
            try:
                from .trading.brain_work.execution_hooks import on_broker_reconciled_close

                on_broker_reconciled_close(db, trade, source=\"cleanup_manual_trades\")"""


def apply():
    content = head
    if OLD1 not in content:
        print("OLD1 not found"); sys.exit(1)
    content = content.replace(OLD1, NEW1)
    print("removed corrupt-zero TCA from sync_positions_to_db")

    if OLD2 not in content:
        print("OLD2 not found"); sys.exit(1)
    content = content.replace(OLD2, NEW2)
    print("removed corrupt-zero TCA from cleanup_manual_trades")

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
