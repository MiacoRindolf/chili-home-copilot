from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.config import settings
from app.models import Device, User
from app.models.core import BrainWorkerControl
from app.models.trading import BrainBatchJob, ScanPattern, Trade
from app.services.backtest_service import save_backtest
from app.services.credential_vault import save_broker_credentials
from app.services.trading.backtest_provenance import repair_backtest_provenance
from app.services.trading.batch_job_constants import JOB_PATTERN_IMMINENT_SCANNER
from app.services.trading.broker_account_repair import (
    repair_broker_account_truth,
    resolve_canonical_broker_user,
)
from app.services.trading.learning import run_live_pattern_depromotion
from app.services.trading.management_scope import (
    MANAGEMENT_SCOPE_BROKER_SYNC,
    MANAGEMENT_SCOPE_BROKER_SYNC_LEGACY,
)
from app.services.trading.runtime_status import get_runtime_overview
from app.services.trading.runtime_surface_state import upsert_runtime_surface_state


def test_resolve_canonical_broker_user_prefers_explicit_owner(db) -> None:
    canonical = User(name="Rindolf", email="rindolf.miaco@gmail.com")
    guest = User(name="Trader-abc123", email=None)
    db.add_all([canonical, guest])
    db.commit()

    save_broker_credentials(db, canonical.id, "robinhood", {"username": "same-user", "password": "pw"})
    save_broker_credentials(db, guest.id, "robinhood", {"username": "same-user", "password": "pw"})

    resolved = resolve_canonical_broker_user(
        db,
        broker="robinhood",
        creds={"username": "same-user", "password": "pw"},
        preferred_user_id=canonical.id,
    )

    assert resolved["canonical_user_id"] == canonical.id
    assert guest.id in resolved["duplicate_user_ids"]


def test_repair_broker_account_truth_collapses_duplicate_open_book(db) -> None:
    canonical = User(name="Rindolf", email="rindolf.miaco@gmail.com")
    guest_a = User(name="Trader-a1b2c3", email=None)
    guest_b = User(name="Trader-d4e5f6", email=None)
    db.add_all([canonical, guest_a, guest_b])
    db.flush()
    db.add_all(
        [
            Device(token="dup-a", user_id=guest_a.id, label="guest-a", client_ip_last="127.0.0.1"),
            Device(token="dup-b", user_id=guest_b.id, label="guest-b", client_ip_last="127.0.0.1"),
        ]
    )
    db.commit()

    save_broker_credentials(db, canonical.id, "robinhood", {"username": "same-user", "password": "pw"})
    save_broker_credentials(db, guest_a.id, "robinhood", {"username": "same-user", "password": "pw"})
    save_broker_credentials(db, guest_b.id, "robinhood", {"username": "same-user", "password": "pw"})

    db.add_all(
        [
            Trade(
                user_id=guest_a.id,
                ticker="AAPL",
                direction="long",
                entry_price=10.0,
                quantity=1.0,
                status="open",
                broker_source="robinhood",
                tags="robinhood-sync",
            ),
            Trade(
                user_id=guest_b.id,
                ticker="AAPL",
                direction="long",
                entry_price=10.0,
                quantity=1.0,
                status="open",
                broker_source="robinhood",
                tags="robinhood-sync",
                stop_loss=9.2,
            ),
            Trade(
                user_id=guest_a.id,
                ticker="MSFT",
                direction="long",
                entry_price=25.0,
                quantity=2.0,
                status="open",
                broker_source="robinhood",
                tags="robinhood-sync",
            ),
            Trade(
                user_id=None,
                ticker="TSLA",
                direction="long",
                entry_price=11.0,
                exit_price=13.0,
                quantity=1.0,
                status="closed",
                pnl=2.0,
                broker_source="robinhood",
                exit_date=datetime.utcnow(),
            ),
        ]
    )
    db.commit()

    out = repair_broker_account_truth(
        db,
        broker="robinhood",
        canonical_user_id=canonical.id,
        preview_only=False,
    )

    aapl_rows = db.query(Trade).filter(Trade.ticker == "AAPL").order_by(Trade.id.asc()).all()
    open_rows = [row for row in aapl_rows if row.status == "open"]
    cancelled_rows = [row for row in aapl_rows if row.status == "cancelled"]
    assert len(open_rows) == 1
    assert open_rows[0].user_id == canonical.id
    assert open_rows[0].stop_loss == 9.2
    assert open_rows[0].management_scope == MANAGEMENT_SCOPE_BROKER_SYNC
    assert len(cancelled_rows) == 1
    assert cancelled_rows[0].user_id == canonical.id
    assert cancelled_rows[0].management_scope == MANAGEMENT_SCOPE_BROKER_SYNC_LEGACY

    msft = db.query(Trade).filter(Trade.ticker == "MSFT", Trade.status == "open").first()
    assert msft is not None
    assert msft.user_id == canonical.id
    assert msft.management_scope == MANAGEMENT_SCOPE_BROKER_SYNC

    tsla = db.query(Trade).filter(Trade.ticker == "TSLA").first()
    assert tsla is not None
    assert tsla.user_id == canonical.id
    assert tsla.management_scope == MANAGEMENT_SCOPE_BROKER_SYNC_LEGACY

    dup_devices = db.query(Device).filter(Device.token.in_(["dup-a", "dup-b"])).all()
    assert {d.user_id for d in dup_devices} == {canonical.id}
    assert out["closed_null_rows_backfilled"] == 1
    assert out["open_trades_deduped"] == 1


def test_runtime_status_uses_db_backed_truth_and_no_data(db) -> None:
    now = datetime.utcnow()
    db.add(
        BrainBatchJob(
            id="scanner-ok-1",
            job_type=JOB_PATTERN_IMMINENT_SCANNER,
            status="ok",
            started_at=now - timedelta(minutes=1),
            ended_at=now,
            payload_json={"picked": 7},
        )
    )
    db.add(
        BrainWorkerControl(
            id=1,
            last_heartbeat_at=now,
            learning_live_json=json.dumps({"phase": "idle"}),
        )
    )
    upsert_runtime_surface_state(
        db,
        surface="predictions",
        state="ok",
        source="unit_test",
        as_of=now,
        details={"cached_result_count": 5},
        updated_by="pytest",
    )
    db.commit()

    overview = get_runtime_overview(db)

    assert overview["surfaces"]["scanner"]["state"] == "ok"
    assert overview["surfaces"]["scanner"]["source"] == "brain_batch_jobs"
    assert overview["surfaces"]["predictions"]["state"] == "ok"
    assert overview["surfaces"]["market_data"]["state"] == "no_data"
    assert overview["surfaces"]["regime"]["state"] == "no_data"
    assert overview["surfaces"]["learning"]["state"] == "ok"


def test_save_backtest_normalizes_strategy_and_provenance(db) -> None:
    user = User(name="BacktestUser", email="bt@example.com")
    pattern = ScanPattern(
        name="Aligned Pattern Name",
        rules_json={"conditions": []},
        origin="brain_discovered",
        active=True,
    )
    db.add_all([user, pattern])
    db.commit()
    db.refresh(user)
    db.refresh(pattern)

    rec = save_backtest(
        db,
        user.id,
        {
            "ok": True,
            "ticker": "AAPL",
            "strategy": "Mismatch",
            "return_pct": 2.0,
            "win_rate": 60.0,
            "trade_count": 4,
            "equity_curve": [],
            "data_provenance": {
                "period": "6mo",
                "interval": "1d",
                "ohlc_bars": 120,
                "chart_time_from": 1700000000,
                "chart_time_to": 1705000000,
            },
        },
        scan_pattern_id=pattern.id,
    )

    params = rec.params if isinstance(rec.params, dict) else json.loads(rec.params)
    assert rec.strategy_name == "Aligned Pattern Name"
    assert params["period"] == "6mo"
    assert params["interval"] == "1d"
    assert params["provenance_status"] == "complete"
    assert params["data_provenance"]["scan_pattern_id"] == pattern.id

    repair = repair_backtest_provenance(db, apply=False, limit=10)
    assert repair["rows_scanned"] >= 1


def test_run_live_pattern_depromotion_demotes_pattern_failing_repaired_gate(db, monkeypatch) -> None:
    user = User(name="BrainUser", email="brain@example.com")
    db.add(user)
    db.commit()
    db.refresh(user)

    pattern = ScanPattern(
        name="Weak Live Pattern",
        rules_json={"conditions": []},
        origin="brain_discovered",
        active=True,
        promotion_status="promoted",
        lifecycle_stage="promoted",
        oos_trade_count=5,
        oos_avg_return_pct=-1.5,
        last_backtest_at=datetime.utcnow() - timedelta(days=10),
    )
    db.add(pattern)
    db.commit()
    db.refresh(pattern)

    monkeypatch.setattr(settings, "brain_live_depromotion_enabled", True, raising=False)
    monkeypatch.setattr(settings, "brain_default_user_id", user.id, raising=False)
    monkeypatch.setattr(settings, "brain_min_trades_for_promotion", 30, raising=False)

    out = run_live_pattern_depromotion(db)
    db.refresh(pattern)

    assert out["demoted"] >= 1
    assert pattern.lifecycle_stage == "decayed"
    assert pattern.promotion_status == "degraded_live"
