"""Hot-mover subscribe hint job (2026-08-17, MNDR/WETO L1 subscription lag).

Ang gap class: sariwang ``source='massive_snapshot'`` tape rows (pumasa sa
payak na Ross screen ng snapshot poller) na WALANG ``iqfeed_l1`` rows = hindi
pa naka-subscribe ang bridge. Ang job ay nagsusulat ng HINT sa
``momentum_bridge_subscribe_requests`` na binabasa ng bridge kada 3s.
"""
from __future__ import annotations

from sqlalchemy import text

from app.services import trading_scheduler as ts


def _seed_tape(db, sym: str, *, source: str) -> None:
    db.execute(text(
        "INSERT INTO momentum_nbbo_spread_tape "
        "(symbol, observed_at, bid, ask, mid, spread_bps, source) "
        "VALUES (:s, now() at time zone 'utc' - interval '1 minute', 1.0, 1.01, 1.005, 100, :src)"
    ), {"s": sym, "src": source})
    db.commit()


def test_gap_symbol_gets_hint_covered_symbol_does_not(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_hot_mover_subscribe_hints_enabled", True
    )
    monkeypatch.setattr(
        settings, "chili_momentum_bridge_subscribe_on_alert_enabled", True
    )
    _seed_tape(db, "GAPX", source="massive_snapshot")
    _seed_tape(db, "COVR", source="massive_snapshot")
    _seed_tape(db, "COVR", source="iqfeed_l1")
    # ang job ay gumagamit ng SARILING SessionLocal — tumutukoy sa parehong
    # chili_test DB (pinilit ng conftest env)
    ts._run_hot_mover_subscribe_hint_job()
    rows = db.execute(text(
        "SELECT symbol, reason FROM momentum_bridge_subscribe_requests "
        "WHERE symbol IN ('GAPX','COVR')"
    )).fetchall()
    syms = {r[0] for r in rows}
    assert "GAPX" in syms, "ang gap symbol ay dapat na-hint"
    assert "COVR" not in syms, "ang covered symbol ay hindi dapat na-hint"
    assert all(r[1] == "hot_mover_snapshot_gap" for r in rows if r[0] == "GAPX")


def test_kill_switch_off_is_noop(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_hot_mover_subscribe_hints_enabled", False
    )
    _seed_tape(db, "GAPY", source="massive_snapshot")
    ts._run_hot_mover_subscribe_hint_job()
    n = db.execute(text(
        "SELECT count(*) FROM momentum_bridge_subscribe_requests WHERE symbol='GAPY'"
    )).scalar()
    assert int(n) == 0
