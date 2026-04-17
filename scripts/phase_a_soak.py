"""Phase A soak script: open + close a synthetic paper trade and verify
ledger + parity rows are written in shadow mode.

Run inside the chili container:
    docker compose exec -T chili python scripts/phase_a_soak.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from app.db import SessionLocal
from app.models.trading import PaperTrade, EconomicLedgerEvent, LedgerParityLog
from app.services.trading import paper_trading as _paper
from app.config import settings


def main() -> int:
    print(f"[soak] BRAIN_ECONOMIC_LEDGER_MODE={settings.brain_economic_ledger_mode}")
    print(f"[soak] BRAIN_ECONOMIC_LEDGER_OPS_LOG_ENABLED={settings.brain_economic_ledger_ops_log_enabled}")
    ticker = f"SOAK{int(datetime.utcnow().timestamp())%100000}"
    db = SessionLocal()
    try:
        ledger_before = db.query(EconomicLedgerEvent).count()
        parity_before = db.query(LedgerParityLog).count()
        print(f"[soak] ledger rows before: {ledger_before}, parity rows before: {parity_before}")

        pt = _paper.open_paper_trade(
            db,
            user_id=None,
            ticker=ticker,
            entry_price=100.0,
            scan_pattern_id=None,
            stop_price=97.0,
            target_price=110.0,
            direction="long",
            quantity=10,
            signal_json={"soak_test": True},
        )
        if pt is None:
            print("[soak] FAILED: open_paper_trade returned None")
            return 2
        db.commit()
        print(f"[soak] opened paper_trade id={pt.id} ticker={pt.ticker} entry={pt.entry_price}")

        # Refetch and close
        pt = db.query(PaperTrade).filter(PaperTrade.id == pt.id).one()
        _paper._close_paper_trade(pt, exit_price=105.0, reason="soak_target")
        db.commit()
        print(f"[soak] closed paper_trade id={pt.id} status={pt.status} pnl={pt.pnl}")

        ledger_after = db.query(EconomicLedgerEvent).count()
        parity_after = db.query(LedgerParityLog).count()
        ledger_rows_for_trade = db.query(EconomicLedgerEvent).filter(
            EconomicLedgerEvent.paper_trade_id == pt.id,
        ).order_by(EconomicLedgerEvent.id).all()
        parity_rows_for_trade = db.query(LedgerParityLog).filter(
            LedgerParityLog.paper_trade_id == pt.id,
        ).all()
        print(f"[soak] ledger rows after: {ledger_after} (delta={ledger_after-ledger_before})")
        print(f"[soak] parity rows after: {parity_after} (delta={parity_after-parity_before})")
        print(f"[soak] ledger rows for trade {pt.id}: {len(ledger_rows_for_trade)}")
        for r in ledger_rows_for_trade:
            print(f"  - id={r.id} event={r.event_type} qty={r.quantity} px={r.price} cash_d={r.cash_delta} pnl_d={r.realized_pnl_delta}")
        print(f"[soak] parity rows for trade {pt.id}: {len(parity_rows_for_trade)}")
        for r in parity_rows_for_trade:
            print(f"  - id={r.id} legacy={r.legacy_pnl} ledger={r.ledger_pnl} delta={r.delta_pnl} agree={r.agree_bool}")

        rc = 0
        if len(ledger_rows_for_trade) < 2:
            print("[soak] FAIL: expected >= 2 ledger rows (entry + exit)")
            rc = 3
        if len(parity_rows_for_trade) < 1:
            print("[soak] FAIL: expected >= 1 parity row")
            rc = 3
        if rc == 0:
            print("[soak] OK")
        return rc
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
