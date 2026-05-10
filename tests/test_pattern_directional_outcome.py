"""f-promotion-pipeline-rebalance Phase 2 (2026-05-09).

Verify the directional-correctness evaluator and the rolling
quality view it feeds.

The evaluator answers the question Phases 3-4 actually need:
"given an imminent-alert prediction, did price actually move in the
predicted direction within the hold window?" — gate-chain-free,
measured on every imminent alert.

Pure / unit (no DB):
  - direction resolution (rules_json + name heuristic + default)
  - window-outcome math (favorable / adverse / correct verdict)

Integration (DB + mock OHLC fetcher):
  - end-to-end: AlertHistory → outcome row inserted, fields correct
  - skip when window not yet closed
  - dedupe on rerun (UNIQUE alert_id)
  - skip when OHLC unavailable
  - down direction: favorable = price-down move
  - rolling view aggregates per-pattern WR (rolling-30 cap)

OHLC fetcher is mocked via the ``fetch_ohlcv`` injection seam so
tests do not hit real market-data providers.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import text

from app.models.trading import AlertHistory, ScanPattern
from app.services.trading.pattern_directional_outcome import (
    _compute_window_outcome,
    _entry_price_from_df,
    _resolve_predicted_direction,
    evaluate_directional_outcomes,
    get_rolling_directional_quality,
)


# ── Pure unit tests ──────────────────────────────────────────────────


def test_direction_default_up_when_no_hint():
    pat = SimpleNamespace(name="ema_breakout_v3", rules_json={})
    assert _resolve_predicted_direction(pat) == "up"


def test_direction_rules_json_short_returns_down():
    pat = SimpleNamespace(name="generic", rules_json={"direction": "short"})
    assert _resolve_predicted_direction(pat) == "down"


def test_direction_rules_json_bias_bearish_returns_down():
    pat = SimpleNamespace(name="generic", rules_json={"bias": "bearish"})
    assert _resolve_predicted_direction(pat) == "down"


def test_direction_name_heuristic_short_token_returns_down():
    pat = SimpleNamespace(name="vwap_short_fade_2026", rules_json=None)
    assert _resolve_predicted_direction(pat) == "down"


def test_compute_outcome_up_with_strong_favorable_move():
    # Entry 100, window high 105 (5%), low 99 (-1%).
    df = pd.DataFrame(
        {
            "Open": [100.0, 102.0, 104.0],
            "High": [101.0, 103.0, 105.0],
            "Low": [99.5, 101.0, 103.0],
            "Close": [100.0, 102.5, 104.5],
        },
        index=pd.to_datetime(
            ["2026-05-09 12:00", "2026-05-09 13:00", "2026-05-09 14:00"]
        ),
    )
    out = _compute_window_outcome(
        df,
        alert_at=datetime(2026, 5, 9, 12, 0),
        window_close_at=datetime(2026, 5, 9, 14, 0),
        entry_price=100.0,
        direction="up",
        threshold_pct=1.5,
    )
    assert out is not None
    assert out["max_favorable_pct"] == pytest.approx(5.0, rel=1e-6)
    assert out["max_adverse_pct"] == pytest.approx(-0.5, rel=1e-6)
    assert out["directional_correct"] is True


def test_compute_outcome_up_weak_favorable_below_threshold():
    df = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [100.5],
            "Low": [99.5],
            "Close": [100.0],
        },
        index=pd.to_datetime(["2026-05-09 12:00"]),
    )
    out = _compute_window_outcome(
        df,
        alert_at=datetime(2026, 5, 9, 12, 0),
        window_close_at=datetime(2026, 5, 9, 12, 0),
        entry_price=100.0,
        direction="up",
        threshold_pct=1.5,
    )
    assert out is not None
    assert out["max_favorable_pct"] == pytest.approx(0.5, rel=1e-6)
    assert out["directional_correct"] is False


def test_compute_outcome_up_adverse_only():
    # Price went down — favorable max_high <= entry.
    df = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [100.0],
            "Low": [97.0],
            "Close": [97.5],
        },
        index=pd.to_datetime(["2026-05-09 12:00"]),
    )
    out = _compute_window_outcome(
        df,
        alert_at=datetime(2026, 5, 9, 12, 0),
        window_close_at=datetime(2026, 5, 9, 12, 0),
        entry_price=100.0,
        direction="up",
        threshold_pct=1.5,
    )
    assert out is not None
    assert out["max_favorable_pct"] == pytest.approx(0.0, rel=1e-6)
    assert out["max_adverse_pct"] == pytest.approx(-3.0, rel=1e-6)
    assert out["directional_correct"] is False


def test_compute_outcome_down_inverts_favorable():
    # Short prediction; price dropped 3% from entry 100 → low 97.
    df = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [100.5],
            "Low": [97.0],
            "Close": [97.5],
        },
        index=pd.to_datetime(["2026-05-09 12:00"]),
    )
    out = _compute_window_outcome(
        df,
        alert_at=datetime(2026, 5, 9, 12, 0),
        window_close_at=datetime(2026, 5, 9, 12, 0),
        entry_price=100.0,
        direction="down",
        threshold_pct=1.5,
    )
    assert out is not None
    # Down direction: favorable = (entry - low)/entry = (100-97)/100 = 3%.
    assert out["max_favorable_pct"] == pytest.approx(3.0, rel=1e-6)
    # Adverse = (entry - high)/entry = (100-100.5)/100 = -0.5%.
    assert out["max_adverse_pct"] == pytest.approx(-0.5, rel=1e-6)
    assert out["directional_correct"] is True


def test_compute_outcome_returns_none_when_window_empty():
    df = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [101.0],
            "Low": [99.0],
            "Close": [100.5],
        },
        index=pd.to_datetime(["2026-05-09 12:00"]),
    )
    # Window is BEFORE the only bar in df — slice is empty.
    out = _compute_window_outcome(
        df,
        alert_at=datetime(2026, 5, 9, 8, 0),
        window_close_at=datetime(2026, 5, 9, 9, 0),
        entry_price=100.0,
        direction="up",
        threshold_pct=1.5,
    )
    assert out is None


def test_entry_price_picks_last_close_at_or_before_alert():
    df = pd.DataFrame(
        {
            "Open": [100.0, 102.0, 104.0],
            "High": [101.0, 103.0, 105.0],
            "Low": [99.5, 101.0, 103.0],
            "Close": [100.5, 102.5, 104.5],
        },
        index=pd.to_datetime(
            ["2026-05-09 11:00", "2026-05-09 12:00", "2026-05-09 13:00"]
        ),
    )
    # alert_at=12:30 → last close at-or-before = 102.5 (12:00 bar).
    px = _entry_price_from_df(df, datetime(2026, 5, 9, 12, 30))
    assert px == pytest.approx(102.5, rel=1e-6)


# ── Integration tests (DB + mock OHLC fetcher) ───────────────────────


def _make_pattern(db, *, name="test_pattern_up", rules=None):
    pat = ScanPattern(
        name=name,
        rules_json=(rules or {}),
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        confidence=0.7,
        evidence_count=10,
        active=True,
        promotion_status="promoted",
        lifecycle_stage="promoted",
    )
    db.add(pat)
    db.flush()
    return pat


def _make_alert(db, *, pattern_id, ticker, created_at):
    a = AlertHistory(
        alert_type="pattern_breakout_imminent",
        ticker=ticker,
        message=f"imminent breakout for {ticker}",
        scan_pattern_id=pattern_id,
        sent_via="log_only",
        success=True,
    )
    a.created_at = created_at  # type: ignore[assignment]
    db.add(a)
    db.flush()
    return a


def _build_ohlc_window(
    *,
    start: datetime,
    n_bars: int = 26,
    entry_price: float = 100.0,
    high_pct: float = 5.0,
    low_pct: float = -1.0,
) -> pd.DataFrame:
    """Build a 1h OHLC frame: bar 0 establishes entry_price, the rest
    of the window hits ``high_pct`` peak and ``low_pct`` trough."""
    rows = []
    idx = []
    high_target = entry_price * (1 + high_pct / 100.0)
    low_target = entry_price * (1 + low_pct / 100.0)
    for i in range(n_bars):
        t = start + timedelta(hours=i)
        idx.append(t)
        if i == 0:
            rows.append((entry_price, entry_price + 0.1, entry_price - 0.1, entry_price))
        elif i == 1:
            # Mark the trough.
            rows.append((entry_price, entry_price + 0.2, low_target, entry_price - 0.2))
        elif i == 2:
            # Mark the peak.
            rows.append((entry_price, high_target, entry_price - 0.1, entry_price + 0.5))
        else:
            rows.append((entry_price + 0.4, entry_price + 0.5, entry_price + 0.3, entry_price + 0.4))
    df = pd.DataFrame(
        rows, columns=["Open", "High", "Low", "Close"], index=pd.to_datetime(idx)
    )
    return df


def _settings_stub(**overrides):
    base = dict(
        chili_pattern_directional_outcome_enabled=True,
        chili_pattern_directional_threshold_pct=1.5,
        chili_pattern_directional_default_hold_hours=24,
        chili_pattern_directional_max_lookback_hours=168,
        chili_pattern_directional_max_alerts_per_run=200,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_evaluator_inserts_correct_outcome_for_up_pattern(db):
    pat = _make_pattern(db, name="up_breakout", rules={"direction": "up"})
    now = datetime.utcnow().replace(microsecond=0)
    alert_at = now - timedelta(hours=30)  # window of 24h has closed
    a = _make_alert(db, pattern_id=pat.id, ticker="AAPL", created_at=alert_at)
    db.commit()

    captured = {}

    def fake_fetch(ticker, *, start, end):
        captured["ticker"] = ticker
        captured["start"] = start
        captured["end"] = end
        return _build_ohlc_window(
            start=alert_at - timedelta(hours=1),
            n_bars=30,
            entry_price=100.0,
            high_pct=5.0,
            low_pct=-1.0,
        )

    summary = evaluate_directional_outcomes(
        db, now=now, fetch_ohlcv=fake_fetch, settings_=_settings_stub()
    )
    assert summary["candidates"] == 1
    assert summary["evaluated"] == 1
    assert summary["errors"] == 0
    assert captured["ticker"] == "AAPL"

    rows = db.execute(
        text(
            "SELECT alert_id, scan_pattern_id, ticker, predicted_direction, "
            "directional_correct, window_max_favorable_pct, window_max_adverse_pct, "
            "hold_window_hours FROM pattern_alert_directional_outcome"
        )
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == a.id
    assert row[1] == pat.id
    assert row[2] == "AAPL"
    assert row[3] == "up"
    assert row[4] is True
    assert float(row[5]) >= 1.5  # >= threshold
    assert row[7] == 24


def test_evaluator_skips_when_window_still_open(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    alert_at = now - timedelta(hours=2)  # still open at 24h hold
    _make_alert(db, pattern_id=pat.id, ticker="MSFT", created_at=alert_at)
    db.commit()

    def fake_fetch(ticker, *, start, end):
        return _build_ohlc_window(start=alert_at - timedelta(hours=1))

    summary = evaluate_directional_outcomes(
        db, now=now, fetch_ohlcv=fake_fetch, settings_=_settings_stub()
    )
    assert summary["candidates"] == 0  # SQL prefilter excludes window-open alerts
    rows = db.execute(
        text("SELECT COUNT(*) FROM pattern_alert_directional_outcome")
    ).fetchone()
    assert rows[0] == 0


def test_evaluator_dedupes_on_rerun(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    alert_at = now - timedelta(hours=30)
    _make_alert(db, pattern_id=pat.id, ticker="NVDA", created_at=alert_at)
    db.commit()

    def fake_fetch(ticker, *, start, end):
        return _build_ohlc_window(start=alert_at - timedelta(hours=1))

    s1 = evaluate_directional_outcomes(
        db, now=now, fetch_ohlcv=fake_fetch, settings_=_settings_stub()
    )
    s2 = evaluate_directional_outcomes(
        db, now=now, fetch_ohlcv=fake_fetch, settings_=_settings_stub()
    )
    assert s1["evaluated"] == 1
    # Second run: candidates filter excludes already-evaluated alerts.
    assert s2["candidates"] == 0
    assert s2["evaluated"] == 0
    rows = db.execute(
        text("SELECT COUNT(*) FROM pattern_alert_directional_outcome")
    ).fetchone()
    assert rows[0] == 1


def test_evaluator_skips_when_ohlc_unavailable(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    alert_at = now - timedelta(hours=30)
    _make_alert(db, pattern_id=pat.id, ticker="GHOSTX", created_at=alert_at)
    db.commit()

    def fake_fetch(ticker, *, start, end):
        return pd.DataFrame()  # provider returned nothing

    summary = evaluate_directional_outcomes(
        db, now=now, fetch_ohlcv=fake_fetch, settings_=_settings_stub()
    )
    assert summary["candidates"] == 1
    assert summary["evaluated"] == 0
    assert summary["skipped_no_ohlc"] == 1
    rows = db.execute(
        text("SELECT COUNT(*) FROM pattern_alert_directional_outcome")
    ).fetchone()
    assert rows[0] == 0


def test_evaluator_down_direction_correct_on_drop(db):
    pat = _make_pattern(db, name="bearish_setup", rules={"direction": "short"})
    now = datetime.utcnow().replace(microsecond=0)
    alert_at = now - timedelta(hours=30)
    _make_alert(db, pattern_id=pat.id, ticker="QQQ", created_at=alert_at)
    db.commit()

    def fake_fetch(ticker, *, start, end):
        return _build_ohlc_window(
            start=alert_at - timedelta(hours=1),
            n_bars=30,
            entry_price=100.0,
            high_pct=0.5,   # tiny upside
            low_pct=-3.0,   # 3% drop in window
        )

    summary = evaluate_directional_outcomes(
        db, now=now, fetch_ohlcv=fake_fetch, settings_=_settings_stub()
    )
    assert summary["evaluated"] == 1
    row = db.execute(
        text(
            "SELECT predicted_direction, directional_correct, "
            "window_max_favorable_pct FROM pattern_alert_directional_outcome"
        )
    ).fetchone()
    assert row[0] == "down"
    assert row[1] is True
    assert float(row[2]) >= 1.5


def test_evaluator_flag_disabled_short_circuits(db):
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    alert_at = now - timedelta(hours=30)
    _make_alert(db, pattern_id=pat.id, ticker="SPY", created_at=alert_at)
    db.commit()

    def fake_fetch(ticker, *, start, end):
        raise AssertionError("should not be called when flag is off")

    summary = evaluate_directional_outcomes(
        db,
        now=now,
        fetch_ohlcv=fake_fetch,
        settings_=_settings_stub(chili_pattern_directional_outcome_enabled=False),
    )
    assert summary.get("skipped") == "flag_disabled"


def test_rolling_view_aggregates_per_pattern_directional_wr(db):
    pat = _make_pattern(db, name="aggregate_target")
    now = datetime.utcnow().replace(microsecond=0)
    # Insert 5 outcomes directly: 3 correct, 2 incorrect.
    for i, correct in enumerate([True, True, True, False, False]):
        # Need a real alert to satisfy FK.
        a = _make_alert(
            db,
            pattern_id=pat.id,
            ticker="X",
            created_at=now - timedelta(hours=30 + i),
        )
        db.execute(
            text(
                """
                INSERT INTO pattern_alert_directional_outcome (
                    alert_id, scan_pattern_id, ticker, alert_at,
                    predicted_direction, hold_window_hours, window_close_at,
                    directional_threshold_pct, directional_correct
                ) VALUES (
                    :aid, :pid, 'X', :alert_at,
                    'up', 24, :wc,
                    1.5, :corr
                )
                """
            ),
            {
                "aid": a.id,
                "pid": pat.id,
                "alert_at": now - timedelta(hours=30 + i),
                "wc": now - timedelta(hours=6 + i),
                "corr": correct,
            },
        )
    db.commit()

    q = get_rolling_directional_quality(db, pat.id)
    assert q is not None
    assert q["rolling_sample_n"] == 5
    assert q["rolling_directional_wr"] == pytest.approx(0.6, rel=1e-6)


def test_rolling_view_caps_sample_at_30(db):
    pat = _make_pattern(db, name="rolling_cap_target")
    now = datetime.utcnow().replace(microsecond=0)
    # Insert 35 outcomes; rolling view should report n=30.
    for i in range(35):
        a = _make_alert(
            db,
            pattern_id=pat.id,
            ticker="Z",
            created_at=now - timedelta(hours=30 + i),
        )
        db.execute(
            text(
                """
                INSERT INTO pattern_alert_directional_outcome (
                    alert_id, scan_pattern_id, ticker, alert_at,
                    predicted_direction, hold_window_hours, window_close_at,
                    directional_threshold_pct, directional_correct
                ) VALUES (
                    :aid, :pid, 'Z', :alert_at,
                    'up', 24, :wc,
                    1.5, TRUE
                )
                """
            ),
            {
                "aid": a.id,
                "pid": pat.id,
                "alert_at": now - timedelta(hours=30 + i),
                "wc": now - timedelta(hours=6 + i),
            },
        )
    db.commit()

    q = get_rolling_directional_quality(db, pat.id)
    assert q is not None
    assert q["rolling_sample_n"] == 30
    assert q["rolling_directional_wr"] == pytest.approx(1.0, rel=1e-6)


def test_evaluator_skips_alert_with_missing_pattern(db):
    """If the pattern row is gone (deleted), the FK on AlertHistory is
    SET NULL — those alerts have ``scan_pattern_id IS NULL`` and the
    evaluator's SQL prefilter excludes them. Belt-and-suspenders: this
    test inserts an alert with a bad scan_pattern_id (FK SET NULL
    triggers if pattern is deleted; we simulate the no-pattern case)."""
    pat = _make_pattern(db)
    now = datetime.utcnow().replace(microsecond=0)
    alert_at = now - timedelta(hours=30)
    _make_alert(db, pattern_id=pat.id, ticker="DEL", created_at=alert_at)
    db.commit()
    # Delete the pattern after the alert exists. FK SET NULL on alert.
    db.delete(pat)
    db.commit()

    def fake_fetch(ticker, *, start, end):
        return _build_ohlc_window(start=alert_at - timedelta(hours=1))

    summary = evaluate_directional_outcomes(
        db, now=now, fetch_ohlcv=fake_fetch, settings_=_settings_stub()
    )
    # SQL prefilter excludes scan_pattern_id IS NULL.
    assert summary["candidates"] == 0
    assert summary["evaluated"] == 0
