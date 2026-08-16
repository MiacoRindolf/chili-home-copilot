"""Live NBBO source preference (2026-08-16, canon-v4 poison autopsy).

Ang `source='massive_snapshot'` rows (1-min delayed HTTP poll) ay sinukat na
**17.27% MAX divergence** vs iqfeed_l1 sa parehong minuto sa live 08-14 RTH
tape (0.83% mean, p95 3.34%) — at pinakamalala mismo sa mabibilis na mover.
Sa replay, ang parehong lason ang gumawa ng VTAK −425.53 fill artifact
(inayos ng #1029). Sa LIVE, dalawang decision reader ang lantad:

1. ``_tape_running_up_records`` (nbbo_tape) — isang lasong first/last mid ay
   PHANTOM burst na nagpapagana ng ignition exemption;
2. ``_micro_bar_df_from_session`` (live_runner) — isang lasong row sa micro-bar
   frame ay pekeng wick sa entry/trail/veto na desisyon.

Ang ayos: event-grade rows muna; ang mga symbol/frame na WALANG event coverage
ay bumabalik sa lahat ng rows (ang SKYQ-class coverage na dahilan ng sampler ay
buo — walang nawawalang symbol, nawawala lang ang phantom evidence).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.nbbo_tape import tape_running_up_symbols
import app.services.trading.momentum_neural.live_runner as LR


def _seed(db: Session, sym: str, mids: list[float], *, source: str,
          minutes_ago_start: float = 4.0) -> None:
    n = len(mids)
    for i, m in enumerate(mids):
        ago = minutes_ago_start * (1 - i / max(n - 1, 1))
        db.execute(text(
            "INSERT INTO momentum_nbbo_spread_tape "
            "(symbol, observed_at, bid, ask, mid, spread_bps, source) "
            "VALUES (:s, now() at time zone 'utc' - make_interval(secs => :ago), "
            ":b, :a, :m, 100, :src)"
        ), {"s": sym, "ago": ago * 60.0, "m": m,
            "b": m * 0.995, "a": m * 1.005, "src": source})
    db.commit()


def test_running_up_ignores_snapshot_phantom_when_event_rows_exist(db: Session) -> None:
    """Flat event tape + isang lasong snapshot last-mid: WALANG phantom burst."""
    _seed(db, "PHNT", [1.00, 1.00, 1.01, 1.01], source="iqfeed_l1")
    # lasong snapshot row bilang pinakabagong mid (+30%)
    _seed(db, "PHNT", [1.30], source="massive_snapshot", minutes_ago_start=0.1)
    assert "PHNT" not in tape_running_up_symbols(db)


def test_running_up_keeps_snapshot_only_coverage(db: Session) -> None:
    """SKYQ-class: snapshot lang ang tape → nadedetect pa rin ang tunay na burst."""
    _seed(db, "SNAPO", [1.80, 1.85, 1.90, 1.95], source="massive_snapshot")
    assert "SNAPO" in tape_running_up_symbols(db)


def test_running_up_event_burst_still_detected(db: Session) -> None:
    _seed(db, "EVNT", [1.00, 1.05, 1.10, 1.20], source="iqfeed_l1")
    assert "EVNT" in tape_running_up_symbols(db)


def test_micro_bars_exclude_snapshot_rows_when_event_coverage_exists(db: Session) -> None:
    """Ang lasong +17% snapshot row ay HINDI dapat pumasok sa micro-bar frame.

    Kailangan ng >=10 REAL na 15s bucket (F6 density floor) para bumuo ang
    micro frame — 24 event rows sa loob ng 6 minuto = 24 bucket."""
    mids = [2.00 + i * 0.001 for i in range(24)]
    _seed(db, "MBAR", mids, source="iqfeed_l1", minutes_ago_start=6.0)
    _seed(db, "MBAR", [2.40], source="massive_snapshot", minutes_ago_start=0.05)
    df = LR._micro_bar_df_from_session(db, "MBAR", bar_seconds=15, lookback_minutes=8.0)
    assert df is not None
    assert float(df["High"].max()) < 2.30, "pumasok ang lasong 2.40 snapshot row"


def test_micro_bars_snapshot_only_tape_stays_below_density_floor(db: Session) -> None:
    """Snapshot-lang na tape: ang F6 density floor (>=10 real bucket) ang
    nagpapanatili ng None -> 1m fallback ng caller — ang coverage fallback ng
    source preference ay HINDI binabago ang disenyong ito (walang 1/min tape
    na kayang buuin ang 15s micro frame)."""
    _seed(db, "MSNP", [3.00, 3.01, 3.02, 3.03, 3.04, 3.05], source="massive_snapshot")
    df = LR._micro_bar_df_from_session(db, "MSNP", bar_seconds=15, lookback_minutes=5.0)
    assert df is None
