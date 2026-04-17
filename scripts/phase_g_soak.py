"""Phase G Docker soak - live brackets + reconciliation (shadow only).

Verifies inside the running ``chili`` container that:
  1. Migration 133 applied and the new tables exist.
  2. ``BRAIN_LIVE_BRACKETS_MODE=shadow`` visible in settings.
  3. A synthetic live ``Trade`` emits exactly one ``BracketIntent`` row
     with ``intent_state='shadow_logged'``.
  4. Running ``run_reconciliation_sweep`` with a mocked broker writes
     reconciliation rows and returns the frozen summary shape.
  5. Diagnostics endpoints (via their helper functions) return the
     frozen shape.
  6. Sweep idempotency: running twice with the same broker view does
     not create duplicate intents and keeps the ``agree`` count stable.
  7. Forcing authoritative mode on the sweep raises.

Safe on a real chili stack: inserts use ``PHG_SOAK_*`` tickers and are
cleaned up before and after. No mode flips beyond shadow.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.trading import (  # noqa: E402
    BracketIntent,
    BracketReconciliationLog,
    Trade,
)
from app.services.trading.bracket_intent import BracketIntentInput  # noqa: E402
from app.services.trading.bracket_intent_writer import (  # noqa: E402
    bracket_intent_summary,
    upsert_bracket_intent,
)
from app.services.trading.bracket_reconciler import BrokerView  # noqa: E402
from app.services.trading.bracket_reconciliation_service import (  # noqa: E402
    bracket_reconciliation_summary,
    run_reconciliation_sweep,
)


SOAK_TICKERS = ["PHG_SOAK_AAPL", "PHG_SOAK_MSFT"]


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[phase_g_soak] FAIL: {msg}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[phase_g_soak] OK  : {msg}")


def _cleanup(db) -> None:
    trade_ids = [
        t.id
        for t in db.query(Trade).filter(Trade.ticker.in_(SOAK_TICKERS)).all()
    ]
    if trade_ids:
        db.query(BracketReconciliationLog).filter(
            BracketReconciliationLog.trade_id.in_(trade_ids)
        ).delete(synchronize_session=False)
        db.query(BracketIntent).filter(
            BracketIntent.trade_id.in_(trade_ids)
        ).delete(synchronize_session=False)
    db.query(Trade).filter(
        Trade.ticker.in_(SOAK_TICKERS)
    ).delete(synchronize_session=False)
    db.commit()


def _seed_trade(db, ticker: str) -> Trade:
    t = Trade(
        user_id=None,
        ticker=ticker,
        direction="long",
        entry_price=100.0,
        quantity=10.0,
        status="open",
        broker_source="robinhood",
        stop_loss=96.0,
        take_profit=106.0,
        stop_model="atr_swing",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _check_migration(db) -> None:
    row = db.execute(text("""
        SELECT version_id FROM schema_version WHERE version_id = '133_live_brackets_reconciliation'
    """)).fetchone()
    _assert(row is not None, "migration 133_live_brackets_reconciliation applied")
    for tbl in ("trading_bracket_intents", "trading_bracket_reconciliation_log"):
        exists = db.execute(text("""
            SELECT to_regclass(:name) IS NOT NULL AS ok
        """), {"name": tbl}).scalar_one()
        _assert(bool(exists), f"table {tbl} exists")


def _check_settings() -> None:
    mode = getattr(settings, "brain_live_brackets_mode", "off") or "off"
    _assert(
        mode.lower() == "shadow",
        f"brain_live_brackets_mode=shadow (got={mode!r})",
    )
    _assert(
        bool(getattr(settings, "brain_live_brackets_ops_log_enabled", True)),
        "brain_live_brackets_ops_log_enabled=True",
    )


def _check_emitter(db) -> Trade:
    t = _seed_trade(db, SOAK_TICKERS[0])
    res = upsert_bracket_intent(
        db,
        trade_id=t.id,
        user_id=None,
        bracket_input=BracketIntentInput(
            ticker=t.ticker,
            direction=t.direction,
            entry_price=t.entry_price,
            quantity=t.quantity,
            atr=2.0,
            stop_model="atr_swing",
            regime="cautious",
        ),
        broker_source=t.broker_source,
    )
    _assert(res is not None, "upsert returned a result")
    _assert(res.created, "first upsert created a new row")
    _assert(
        res.state == "shadow_logged",
        f"intent_state=shadow_logged (got={res.state!r})",
    )

    # Idempotent: second call does not create a new row.
    res2 = upsert_bracket_intent(
        db,
        trade_id=t.id,
        user_id=None,
        bracket_input=BracketIntentInput(
            ticker=t.ticker,
            direction=t.direction,
            entry_price=t.entry_price,
            quantity=t.quantity,
            atr=2.0,
            stop_model="atr_swing",
            regime="cautious",
        ),
        broker_source=t.broker_source,
    )
    _assert(res2 is not None and not res2.created, "second upsert is idempotent")

    count = db.execute(text("""
        SELECT COUNT(*) FROM trading_bracket_intents WHERE trade_id = :tid
    """), {"tid": t.id}).scalar_one()
    _assert(int(count) == 1, f"exactly 1 bracket intent for trade_id={t.id}")
    return t


def _check_sweep_agree(db, t: Trade) -> None:
    # Read the actual intent prices so the broker view is a true "agree"
    row = db.execute(text("""
        SELECT stop_price, target_price FROM trading_bracket_intents WHERE trade_id = :tid
    """), {"tid": t.id}).fetchone()
    _assert(row is not None, "bracket intent row present for soak trade")
    intent_stop, intent_target = float(row[0]), float(row[1])

    def broker_fn(local_rows):
        return [
            BrokerView(
                available=True,
                ticker=t.ticker,
                broker_source=t.broker_source,
                position_quantity=t.quantity,
                stop_order_id="soak-stop-1",
                stop_order_state="open",
                stop_order_price=intent_stop,
                target_order_id="soak-tgt-1",
                target_order_state="open",
                target_order_price=intent_target,
            )
        ]

    s1 = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
    _assert(s1.mode == "shadow", f"sweep mode=shadow (got={s1.mode!r})")
    _assert(s1.trades_scanned >= 1, "at least 1 trade scanned")
    _assert(s1.brackets_checked >= 1, "at least 1 bracket checked")
    _assert(s1.agree >= 1, "at least 1 agree row")
    _assert(s1.rows_written >= 1, "at least 1 reconciliation row written")

    s2 = run_reconciliation_sweep(db, broker_view_fn=broker_fn)
    _assert(s2.agree == s1.agree, "idempotent agree count across two sweeps")

    intent_count = db.execute(text("""
        SELECT COUNT(*) FROM trading_bracket_intents WHERE trade_id = :tid
    """), {"tid": t.id}).scalar_one()
    _assert(int(intent_count) == 1, "no duplicate intents after two sweeps")


def _check_diagnostics_shape(db) -> None:
    bi = bracket_intent_summary(db, lookback_hours=1)
    _assert(
        set(bi.keys()) == {
            "mode", "lookback_hours", "intents_total", "by_state",
            "by_broker_source", "latest_intent",
        },
        "bracket_intent_summary has frozen shape",
    )
    _assert(bi["mode"] == "shadow", "bracket_intent_summary mode=shadow")

    br = bracket_reconciliation_summary(db, lookback_hours=1, recent_sweeps=5)
    _assert(
        set(br.keys()) == {
            "mode", "lookback_hours", "recent_sweeps_requested", "rows_total",
            "by_kind", "by_severity", "last_sweep_id", "last_observed_at",
            "sweeps_recent",
        },
        "bracket_reconciliation_summary has frozen shape",
    )
    _assert(br["mode"] == "shadow", "bracket_reconciliation_summary mode=shadow")


def _check_authoritative_refused(db) -> None:
    import os as _os

    saved = _os.environ.get("BRAIN_LIVE_BRACKETS_MODE")
    try:
        original = settings.brain_live_brackets_mode
        settings.brain_live_brackets_mode = "authoritative"
        try:
            raised = False
            try:
                run_reconciliation_sweep(db)
            except RuntimeError:
                raised = True
            _assert(raised, "authoritative mode raises inside reconciliation service")
        finally:
            settings.brain_live_brackets_mode = original
    finally:
        if saved is None:
            _os.environ.pop("BRAIN_LIVE_BRACKETS_MODE", None)
        else:
            _os.environ["BRAIN_LIVE_BRACKETS_MODE"] = saved


def main() -> int:
    db = SessionLocal()
    try:
        _cleanup(db)
        _check_migration(db)
        _check_settings()
        trade = _check_emitter(db)
        _check_sweep_agree(db, trade)
        _check_diagnostics_shape(db)
        _check_authoritative_refused(db)
        print("[phase_g_soak] SUCCESS: all checks passed")
        return 0
    finally:
        try:
            _cleanup(db)
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
