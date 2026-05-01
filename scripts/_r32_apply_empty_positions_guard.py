"""R32: empty-positions guard in sync_positions_to_db.

CRITICAL bug pattern: when broker_service.get_positions() returns an
empty list (broker auth lapsed, transient API failure, or genuine
account empty), sync_positions_to_db's ``stale`` query at line 1482
expands to ``Trade.ticker.notin_([])`` which is treated as ``True``,
meaning EVERY open local trade is added to the stale list and
auto-closed via 'broker_reconcile_position_gone'.

This was the root cause of today's 15:56:02-15:56:03 cascade where
3 crypto positions (SOL/AAVE/GRT) were closed within 1 second by a
single broker_sync pass while the broker session was failing
refresh_token with 401 invalid_grant. The synthetic losses then
tripped the consecutive-loss breaker (R31 closed that loop after
the fact, but the underlying wipeout is a separate bug).

Fix: add an explicit guard. If broker positions endpoint returns
empty/None AND we have ANY local open trades, refuse to mass-close.
Log a loud warning and skip the close pass entirely. Will retry on
the next sync cycle. If the account really has 0 positions for an
extended period, the operator will see repeated warnings and can
manually reconcile.

The trade-off: a legitimately-zeroed broker (operator closed
everything manually) will keep local rows 'open' until the operator
manually closes them. That is much safer than the inverse: an
auth-flap silently wiping local trade tracking.
"""
import subprocess, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "app" / "services" / "broker_service.py"

head = subprocess.check_output(
    ["git", "show", "HEAD:app/services/broker_service.py"],
    cwd=str(ROOT),
).decode("utf-8")


OLD = """    stale = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_source == \"robinhood\",
            Trade.status == \"open\",
            Trade.ticker.notin_(rh_tickers) if rh_tickers else True,
        )
        .all()
    )"""


NEW = """    # R32 (2026-04-30): guard against the auth-lapse / api-failure case.
    # When ``rh_tickers`` is empty -- because get_positions() returned []
    # while broker auth was failing, the network was flaky, or any other
    # transient -- the original ``Trade.ticker.notin_(rh_tickers) if
    # rh_tickers else True`` short-circuited to True, so EVERY open
    # local trade joined the stale list and got auto-closed via
    # ``broker_reconcile_position_gone``. That manufactured phantom
    # losses (R31's consecutive-loss breaker fix closes that loop, but
    # the underlying wipeout is THIS bug). Real incident: 2026-04-30
    # 15:56:02-15:56:03 UTC, 3 crypto positions closed within 1 second.
    #
    # We CANNOT distinguish ''broker auth is flapping'' from ''account
    # legitimately has 0 positions'' looking only at this snapshot.
    # Default to safety: refuse to mass-close. If the operator really
    # zeroed their account, repeated warnings will tell them to manually
    # reconcile the stale local rows.
    if not rh_tickers:
        open_local_count = (
            db.query(Trade)
            .filter(
                Trade.user_id == user_id,
                Trade.broker_source == \"robinhood\",
                Trade.status == \"open\",
            )
            .count()
        )
        if open_local_count > 0:
            logger.warning(
                \"[broker_sync] R32 GUARD: get_positions() returned 0 positions \"
                \"but %d local trade(s) are open. Likely broker auth issue or \"
                \"transient API failure. REFUSING to mass-close (would \"
                \"manufacture phantom broker_reconcile_position_gone losses). \"
                \"Will retry next cycle.\",
                open_local_count,
            )
            return {
                \"created\": created,
                \"updated\": updated,
                \"closed\": 0,
                \"skipped_reason\": \"empty_broker_positions_with_open_local_trades\",
            }

    stale = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_source == \"robinhood\",
            Trade.status == \"open\",
            Trade.ticker.notin_(rh_tickers) if rh_tickers else True,
        )
        .all()
    )"""


def apply():
    if OLD not in head:
        print("OLD block not found in HEAD")
        sys.exit(1)
    content = head.replace(OLD, NEW)
    try:
        ast.parse(content)
        print("ast OK")
    except SyntaxError as e:
        print(f"SYNTAX line {e.lineno}: {e.msg}")
        for i, line in enumerate(
            content.split("\n")[max(0, e.lineno - 5):e.lineno + 3],
            start=max(1, e.lineno - 4),
        ):
            print(f"{i}: {line[:120]}")
        sys.exit(1)
    target.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {len(content.splitlines())} lines to {target}")


if __name__ == "__main__":
    apply()
