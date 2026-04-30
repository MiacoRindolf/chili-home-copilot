"""Apply R26 broker-reject defer-on-rejection edit to
robinhood_exit_execution.py from a clean HEAD checkout. Edit tool
truncated the file mid-edit (~1248 lines vs HEAD's ~1098). This script
runs on Windows host where git works.

Pulls HEAD content, applies two surgical string-replace edits
(_is_retryable_broker_rejection helper + defer-on-reject in the
broker-rejection block), validates via ast.parse, writes back.
"""
import subprocess, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "app" / "services" / "trading" / "robinhood_exit_execution.py"

head = subprocess.check_output(
    ["git", "show", "HEAD:app/services/trading/robinhood_exit_execution.py"],
    cwd=str(ROOT),
).decode("utf-8")


OLD1 = """def _mark_deferred_exit(
    db: Session,
    trade: Trade,
    *,
    exit_reason: str,
    execution_reason: str,
    now_utc: datetime,
) -> None:"""

NEW1 = """def _is_retryable_broker_rejection(broker_error):
    \"\"\"Whether a broker-reject reason is the kind that may succeed later.

    Used by R26 to engage the existing 5-min auto_trader_monitor cooldown
    via ``_mark_deferred_exit``. Only patterns that are KNOWN to be
    transient (PDT window clears, spread widens then narrows, market
    closed will reopen, rate limit will reset). Truly fatal errors
    (bad symbol, account-closed, no_position) fall through unmarked so
    the next cycle treats them as fresh failures and the operator sees
    the real signal.
    \"\"\"
    if not broker_error:
        return False
    e = str(broker_error).lower()
    return any(
        token in e
        for token in (
            "pdt designation",
            "wide_spread",
            "wide spread",
            "market closed",
            "extended hours",
            "rate_limited",
            "rate limit",
            "no_quote",
            "not enough shares",
        )
    )


def _mark_deferred_exit(
    db: Session,
    trade: Trade,
    *,
    exit_reason: str,
    execution_reason: str,
    now_utc: datetime,
) -> None:"""


OLD2 = """        # Always emit an audit row so monitor rejections show up in
        # ``trading_autotrader_runs`` instead of only in broker logs —
        # otherwise repeated silent rejections look like a stopped job.
        try:
            _record_autotrader_run(
                db,
                trade,
                decision=f"{audit_decision_prefix}_rejected",
                reason=broker_error[:200],
                snapshot=_audit_snapshot(
                    trade,
                    exit_reason=exit_reason,
                    monitor_exit_meta=monitor_exit_meta,
                    extra={
                        "broker_error": broker_error,
                        "order_type": order_type,
                        "limit_price": limit_price,
                        "submit_base_size": submit_base_size,
                    },
                ),
            )
        except Exception:
            logger.exception("[rh_exit] failed to record broker-reject audit trade=%s", trade.id)
        return {"ok": False, "error": broker_error}"""

NEW2 = """        # Always emit an audit row so monitor rejections show up in
        # ``trading_autotrader_runs`` instead of only in broker logs --
        # otherwise repeated silent rejections look like a stopped job.
        try:
            _record_autotrader_run(
                db,
                trade,
                decision=f"{audit_decision_prefix}_rejected",
                reason=broker_error[:200],
                snapshot=_audit_snapshot(
                    trade,
                    exit_reason=exit_reason,
                    monitor_exit_meta=monitor_exit_meta,
                    extra={
                        "broker_error": broker_error,
                        "order_type": order_type,
                        "limit_price": limit_price,
                        "submit_base_size": submit_base_size,
                    },
                ),
            )
        except Exception:
            logger.exception("[rh_exit] failed to record broker-reject audit trade=%s", trade.id)

        # R26 (2026-04-30 audit HIGH 6.3): defer this exit so the next
        # cycle does not immediately retry. Without this, the autotrader
        # monitor re-fires every ~30s and the broker keeps rejecting --
        # produced 1053 PDT + 227 wide_spread + 41 "Not enough shares"
        # rejections in 24h, all on the same handful of trades on a tight
        # loop. Marking the trade pending_exit_status='deferred' engages
        # the existing 5-min cooldown in auto_trader_monitor (LL.9).
        # Applied for KNOWN-retryable broker errors only -- truly fatal
        # broker errors fall through so the operator sees the real signal.
        try:
            if _is_retryable_broker_rejection(broker_error):
                _mark_deferred_exit(
                    db,
                    trade,
                    exit_reason=exit_reason,
                    execution_reason=f"broker_reject:{broker_error[:80]}",
                    now_utc=now,
                )
                logger.info(
                    "[rh_exit] R26: deferred trade=%s after broker rejection "
                    "(reason=%s) -- 5min cooldown engaged",
                    trade.id, broker_error[:80],
                )
        except Exception:
            logger.exception(
                "[rh_exit] R26 deferred-mark failed for trade=%s", trade.id
            )

        return {"ok": False, "error": broker_error}"""


def apply():
    content = head
    if OLD1 not in content:
        print("OLD1 not found in HEAD"); sys.exit(1)
    content = content.replace(OLD1, NEW1)
    print("inserted helper")

    if OLD2 not in content:
        print("OLD2 not found in HEAD"); sys.exit(1)
    content = content.replace(OLD2, NEW2)
    print("inserted defer-on-reject")

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
