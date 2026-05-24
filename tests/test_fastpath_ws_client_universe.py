"""Tests for fast-path websocket universe resolution.

These are helper-level tests: no Coinbase socket, no database connection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.services.trading.fast_path.gates import ALERT_RECENCY_MAX_AGE_S
from app.services.trading.fast_path.settings import FastPathSettings
from app.services.trading.fast_path.universe_status import (
    UNIVERSE_STATUS_ACTIVE,
    UNIVERSE_STATUS_SHADOW,
)
from app.services.trading.fast_path.ws_client import CoinbaseWSClient


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeStatus:
    def __init__(self) -> None:
        self.paused: list[tuple[str, str]] = []
        self.registered: list[str] = []
        self.reconnected: list[str] = []
        self.bars: list[tuple[str, datetime]] = []
        self.errors: list[tuple[str, str]] = []

    def register(self, ticker: str) -> None:
        self.registered.append(ticker)

    def mark_paused(self, ticker: str, reason: str) -> None:
        self.paused.append((ticker, reason))

    def record_bar(self, ticker: str, close_at: datetime, seq=None) -> None:
        self.bars.append((ticker, close_at))

    def record_error(self, ticker: str, reason: str) -> None:
        self.errors.append((ticker, reason))

    def record_reconnect(self, ticker: str) -> None:
        self.reconnected.append(ticker)


class _FakeWriter:
    def __init__(self) -> None:
        self.bars = []
        self.books = []
        self.alerts = []

    def enqueue_bar(self, item) -> bool:
        self.bars.append(item)
        return True

    def enqueue_book(self, item) -> bool:
        self.books.append(item)
        return True

    def enqueue_alert(self, item) -> bool:
        self.alerts.append(item)
        return True


class _FakeUniverseStatusEngine:
    def __init__(self, status: str | None) -> None:
        self.status = status
        self.queries = 0

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        self.queries += 1
        return self

    def mappings(self):
        return self

    def one_or_none(self):
        if self.status is None:
            return None
        return {"status": self.status}


def _client(
    settings: FastPathSettings,
    *,
    writer: _FakeWriter | None = None,
    status: _FakeStatus | None = None,
    engine=None,
) -> CoinbaseWSClient:
    return CoinbaseWSClient(
        settings,
        db_writer=writer or _FakeWriter(),  # type: ignore[arg-type]
        status=status or _FakeStatus(),  # type: ignore[arg-type]
        engine=engine,
    )


def _candle(*, close_ts: float, volume: float, close: float = 100.0) -> dict:
    return {
        "product_id": "TEST-USD",
        "start": str(close_ts - 60.0),
        "open": "100.0",
        "high": str(max(100.0, close)),
        "low": str(min(100.0, close)),
        "close": str(close),
        "volume": str(volume),
    }


def test_rotation_empty_returns_empty_without_legacy_fallback():
    settings = FastPathSettings(
        universe_rotation_enabled=True,
        universe_empty_fallback_enabled=False,
        pairs=["BTC-USD", "ETH-USD"],
    )
    fake_db = _FakeSession()
    client = _client(settings)

    with patch("app.db.SessionLocal", return_value=fake_db), patch(
        "app.services.trading.fast_path.universe_rotator.get_subscribed_pairs",
        return_value=[],
    ):
        tickers = client._resolve_active_pairs()

    assert tickers == []
    assert fake_db.closed is True


def test_rotation_empty_can_use_explicit_legacy_fallback():
    settings = FastPathSettings(
        universe_rotation_enabled=True,
        universe_empty_fallback_enabled=True,
        pairs=["BTC-USD", "ETH-USD"],
    )
    client = _client(settings)

    with patch("app.db.SessionLocal", return_value=_FakeSession()), patch(
        "app.services.trading.fast_path.universe_rotator.get_subscribed_pairs",
        return_value=[],
    ):
        tickers = client._resolve_active_pairs()

    assert tickers == ["BTC-USD", "ETH-USD"]


def test_rotation_read_failure_returns_empty_without_legacy_fallback():
    settings = FastPathSettings(
        universe_rotation_enabled=True,
        universe_empty_fallback_enabled=False,
        pairs=["BTC-USD"],
    )
    client = _client(settings)

    with patch("app.db.SessionLocal", side_effect=RuntimeError("db down")):
        tickers = client._resolve_active_pairs()

    assert tickers == []


def test_rotation_disabled_uses_configured_pairs():
    settings = FastPathSettings(
        universe_rotation_enabled=False,
        pairs=["BTC-USD", "ETH-USD"],
    )
    client = _client(settings)

    assert client._resolve_active_pairs() == ["BTC-USD", "ETH-USD"]


def test_universe_refresh_reconnects_when_rotator_pairs_change():
    settings = FastPathSettings(
        universe_rotation_enabled=True,
        universe_empty_fallback_enabled=False,
        pairs=[],
    )
    fake_db = _FakeSession()
    status = _FakeStatus()
    client = _client(settings, status=status)
    client._active_pairs = ["OLD-USD", "KEEP-USD"]

    with patch("app.db.SessionLocal", return_value=fake_db), patch(
        "app.services.trading.fast_path.universe_rotator.get_subscribed_pairs",
        return_value=["KEEP-USD", "NEW-USD"],
    ):
        changed = client._refresh_active_pairs_if_changed()

    assert changed is True
    assert client._active_pairs == ["KEEP-USD", "NEW-USD"]
    assert ("OLD-USD", "universe_rotated") in status.paused
    assert status.registered == ["KEEP-USD", "NEW-USD"]
    assert status.reconnected == ["KEEP-USD", "NEW-USD"]
    assert fake_db.closed is True
    assert client.stats()["universe_refreshes_total"] == 1
    assert client.stats()["universe_reconnects_total"] == 1


def test_universe_refresh_ignores_unchanged_pairs():
    settings = FastPathSettings(
        universe_rotation_enabled=True,
        universe_empty_fallback_enabled=False,
        pairs=[],
    )
    client = _client(settings)
    client._active_pairs = ["KEEP-USD", "NEW-USD"]

    with patch("app.db.SessionLocal", return_value=_FakeSession()), patch(
        "app.services.trading.fast_path.universe_rotator.get_subscribed_pairs",
        return_value=["KEEP-USD", "NEW-USD"],
    ):
        changed = client._refresh_active_pairs_if_changed()

    assert changed is False
    assert client._active_pairs == ["KEEP-USD", "NEW-USD"]
    assert client.stats()["universe_refreshes_total"] == 1
    assert client.stats()["universe_reconnects_total"] == 0


def test_ws_client_passes_scanner_threshold_settings():
    settings = FastPathSettings(
        scanner_vol_breakout_lookback=7,
        scanner_vol_breakout_mult=2.5,
        scanner_imbalance_long_threshold=0.72,
        scanner_imbalance_short_threshold=0.28,
        scanner_imbalance_cooldown_s=17.0,
        scanner_spread_squeeze_bps=2.25,
        scanner_spread_squeeze_vol_mult=1.4,
        scanner_spread_squeeze_cooldown_s=45.0,
        scanner_book_pressure_enabled=False,
        scanner_book_pressure_window=6,
        scanner_book_pressure_min_avg_imbalance=0.7,
        scanner_book_pressure_min_microprice_bps=0.4,
        scanner_book_pressure_max_spread_bps=2.75,
        scanner_book_pressure_min_mid_move_bps=0.35,
        scanner_book_pressure_cooldown_s=29.0,
        scanner_book_pressure_min_touch_notional_usd=18.5,
    )
    client = _client(settings)

    cfg = client.stats()["scanner"]["config"]
    assert cfg["vol_breakout_lookback"] == 7
    assert cfg["vol_breakout_mult"] == 2.5
    assert cfg["imbalance_long_threshold"] == 0.72
    assert cfg["imbalance_short_threshold"] == 0.28
    assert cfg["imbalance_cooldown_s"] == 17.0
    assert cfg["spread_squeeze_bps"] == 2.25
    assert cfg["spread_squeeze_vol_mult"] == 1.4
    assert cfg["spread_squeeze_cooldown_s"] == 45.0
    assert cfg["book_pressure_enabled"] is False
    assert cfg["book_pressure_window"] == 6
    assert cfg["book_pressure_min_avg_imbalance"] == 0.7
    assert cfg["book_pressure_min_microprice_bps"] == 0.4
    assert cfg["book_pressure_max_spread_bps"] == 2.75
    assert cfg["book_pressure_min_mid_move_bps"] == 0.35
    assert cfg["book_pressure_cooldown_s"] == 29.0
    assert cfg["book_pressure_min_touch_notional_usd"] == 18.5


def test_stale_replay_bars_warm_scanner_without_emitting_alerts():
    settings = FastPathSettings(universe_rotation_enabled=False, pairs=["TEST-USD"])
    writer = _FakeWriter()
    status = _FakeStatus()
    client = _client(settings, writer=writer, status=status)

    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
    now_ts = now.timestamp()
    first_close = now - timedelta(
        seconds=ALERT_RECENCY_MAX_AGE_S,
        minutes=25,
    )
    for i in range(20):
        close_ts = (first_close + timedelta(minutes=i)).timestamp()
        client._maybe_emit_bar(_candle(close_ts=close_ts, volume=1.0), now_ts)

    spike_close = first_close + timedelta(minutes=20)
    client._maybe_emit_bar(
        _candle(close_ts=spike_close.timestamp(), volume=3.0, close=101.0),
        now_ts,
    )

    assert len(writer.bars) == 21
    assert writer.alerts == []
    assert len(status.bars) == 21
    assert client.stats()["candles_scanned_warmup_only"] == 21
    scanner_stats = client.stats()["scanner"]
    assert scanner_stats["suppressed_bar_close_alerts_disabled"] == 1
    assert scanner_stats["pullback_deferred_scheduled"] == 0


def test_dispatch_alert_suppresses_min_score_before_db_write():
    settings = FastPathSettings(
        cost_aware_admission_enabled=True,
        execution_mode="maker_only",
    )
    writer = _FakeWriter()
    client = _client(settings, writer=writer, engine=object())

    with patch.dict(
        "os.environ",
        {"CHILI_FAST_PATH_EXEC_MIN_SCORE": "0.55"},
    ), patch(
        "app.services.trading.fast_path.calibration.is_negative_edge_excluded",
        side_effect=AssertionError("min-score alert should stop first"),
    ), patch(
        "app.services.trading.fast_path.calibration.is_cost_barrier_excluded",
        side_effect=AssertionError("min-score alert should stop first"),
    ), patch(
        "app.services.trading.fast_path.calibration."
        "maker_attempt_adverse_selection_excluded",
        side_effect=AssertionError("min-score alert should stop first"),
    ):
        client._dispatch_alert({
            "ticker": "TEST-USD",
            "alert_type": "volume_breakout_long",
            "fired_at": datetime(2026, 5, 23, 18, 0, 0),
            "signal_score": 0.54,
            "features": {},
        })

    assert writer.alerts == []
    assert client.stats()["alerts_suppressed_min_score"] == 1
    assert client.stats()["alerts_suppressed_negative_edge"] == 0
    assert client.stats()["alerts_suppressed_cost_barrier"] == 0
    assert client.stats()["alerts_suppressed_maker_attempt_adverse"] == 0


def test_dispatch_alert_allows_score_at_executor_min_score():
    settings = FastPathSettings(execution_mode="maker_only")
    writer = _FakeWriter()
    client = _client(settings, writer=writer, engine=object())

    with patch.dict(
        "os.environ",
        {"CHILI_FAST_PATH_EXEC_MIN_SCORE": "0.55"},
    ), patch(
        "app.services.trading.fast_path.calibration.is_negative_edge_excluded",
        return_value=(False, {"verdict": "not_excluded"}),
    ), patch(
        "app.services.trading.fast_path.calibration."
        "maker_attempt_adverse_selection_excluded",
        return_value=(False, {"verdict": "not_excluded"}),
    ):
        client._dispatch_alert({
            "ticker": "TEST-USD",
            "alert_type": "volume_breakout_long",
            "fired_at": datetime(2026, 5, 23, 18, 0, 0),
            "signal_score": 0.55,
            "features": {},
        })

    assert len(writer.alerts) == 1
    assert client.stats()["alerts_suppressed_min_score"] == 0


def test_dispatch_alert_suppresses_learned_negative_edge_before_db_write():
    settings = FastPathSettings(
        execution_mode="maker_only",
        negative_edge_filter_ttl_s=30,
    )
    writer = _FakeWriter()
    client = _client(settings, writer=writer, engine=object())

    with patch(
        "app.services.trading.fast_path.calibration.is_negative_edge_excluded",
        return_value=(True, {"verdict": "negative_edge"}),
    ) as lookup:
        client._dispatch_alert({
            "ticker": "TEST-USD",
            "alert_type": "imbalance_long",
            "fired_at": datetime(2026, 5, 23, 18, 0, 0),
            "signal_score": 0.5,
            "features": {},
        })

    assert writer.alerts == []
    assert client.stats()["alerts_suppressed_negative_edge"] == 1
    assert lookup.call_args.kwargs["table"] == "fast_signal_decay_maker_filled"


def test_dispatch_alert_allows_shadow_universe_alerts_to_learn():
    settings = FastPathSettings(
        universe_rotation_enabled=True,
        cost_aware_admission_enabled=True,
        execution_mode="maker_only",
        maker_attempt_adverse_filter_enabled=True,
    )
    writer = _FakeWriter()
    engine = _FakeUniverseStatusEngine(UNIVERSE_STATUS_SHADOW)
    client = _client(settings, writer=writer, engine=engine)

    with patch(
        "app.services.trading.fast_path.calibration.is_negative_edge_excluded",
        side_effect=AssertionError("shadow alert should feed learning"),
    ), patch(
        "app.services.trading.fast_path.calibration.is_cost_barrier_excluded",
        side_effect=AssertionError("shadow alert should feed learning"),
    ), patch(
        "app.services.trading.fast_path.calibration."
        "maker_attempt_adverse_selection_excluded",
        side_effect=AssertionError("shadow alert should feed learning"),
    ):
        client._dispatch_alert({
            "ticker": "TEST-USD",
            "alert_type": "imbalance_long",
            "fired_at": datetime(2026, 5, 23, 18, 0, 0),
            "signal_score": 0.5,
            "features": {},
        })

    assert len(writer.alerts) == 1
    assert engine.queries == 1
    assert client.stats()["alerts_suppressed_negative_edge"] == 0
    assert client.stats()["alerts_suppressed_cost_barrier"] == 0
    assert client.stats()["alerts_suppressed_maker_attempt_adverse"] == 0


def test_dispatch_alert_still_suppresses_active_universe_negative_edge():
    settings = FastPathSettings(
        universe_rotation_enabled=True,
        execution_mode="maker_only",
        negative_edge_filter_ttl_s=30,
    )
    writer = _FakeWriter()
    engine = _FakeUniverseStatusEngine(UNIVERSE_STATUS_ACTIVE)
    client = _client(settings, writer=writer, engine=engine)

    with patch(
        "app.services.trading.fast_path.calibration.is_negative_edge_excluded",
        return_value=(True, {"verdict": "negative_edge"}),
    ):
        client._dispatch_alert({
            "ticker": "TEST-USD",
            "alert_type": "imbalance_long",
            "fired_at": datetime(2026, 5, 23, 18, 0, 0),
            "signal_score": 0.5,
            "features": {},
        })

    assert writer.alerts == []
    assert engine.queries == 1
    assert client.stats()["alerts_suppressed_negative_edge"] == 1


def test_dispatch_alert_suppresses_maker_attempt_adverse_before_db_write():
    settings = FastPathSettings(
        execution_mode="maker_only",
        negative_edge_filter_ttl_s=30,
        maker_attempt_adverse_filter_enabled=True,
        maker_attempt_adverse_filter_window_h=12,
    )
    writer = _FakeWriter()
    client = _client(settings, writer=writer, engine=object())

    with patch(
        "app.services.trading.fast_path.calibration.is_negative_edge_excluded",
        return_value=(False, {"verdict": "not_excluded"}),
    ), patch(
        "app.services.trading.fast_path.calibration.maker_attempt_adverse_selection_excluded",
        return_value=(True, {"verdict": "adverse_selection"}),
    ) as lookup:
        client._dispatch_alert({
            "ticker": "TEST-USD",
            "alert_type": "imbalance_long",
            "fired_at": datetime(2026, 5, 23, 18, 0, 0),
            "signal_score": 0.5,
            "features": {},
        })

    assert writer.alerts == []
    assert client.stats()["alerts_suppressed_maker_attempt_adverse"] == 1
    assert client.stats()["maker_attempt_adverse_cache_size"] == 1
    assert lookup.call_args.kwargs["window_hours"] == 12


def test_dispatch_alert_suppresses_cost_exhausted_lane_before_db_write():
    settings = FastPathSettings(
        cost_aware_admission_enabled=True,
        execution_mode="maker_only",
        cost_aware_maker_fee_bps=2.0,
        negative_edge_filter_ttl_s=30,
    )
    writer = _FakeWriter()
    client = _client(settings, writer=writer, engine=object())

    with patch(
        "app.services.trading.fast_path.calibration.is_negative_edge_excluded",
        return_value=(False, {"verdict": "uncertain"}),
    ), patch(
        "app.services.trading.fast_path.calibration.is_cost_barrier_excluded",
        return_value=(True, {"verdict": "below_cost"}),
    ) as lookup, patch(
        "app.services.trading.fast_path.calibration.maker_attempt_adverse_selection_excluded",
        side_effect=AssertionError("cost-exhausted alert should stop first"),
    ):
        client._dispatch_alert({
            "ticker": "TEST-USD",
            "alert_type": "book_pressure_reclaim_long",
            "fired_at": datetime(2026, 5, 23, 18, 0, 0),
            "signal_score": 0.5,
            "features": {"spread_bps": 3.0},
        })

    assert writer.alerts == []
    assert client.stats()["alerts_suppressed_cost_barrier"] == 1
    assert client.stats()["cost_barrier_cache_size"] == 1
    assert lookup.call_args.kwargs["cost_bps"] == 10.0
    assert lookup.call_args.kwargs["allow_pooled"] is True


def test_maker_attempt_adverse_prefilter_skips_taker_mode():
    settings = FastPathSettings(execution_mode="taker")
    writer = _FakeWriter()
    client = _client(settings, writer=writer, engine=object())

    with patch(
        "app.services.trading.fast_path.calibration.is_negative_edge_excluded",
        return_value=(False, {"verdict": "not_excluded"}),
    ), patch(
        "app.services.trading.fast_path.calibration.maker_attempt_adverse_selection_excluded",
        side_effect=AssertionError("maker attempt evidence should not be queried"),
    ):
        client._dispatch_alert({
            "ticker": "TEST-USD",
            "alert_type": "imbalance_long",
            "fired_at": datetime(2026, 5, 23, 18, 0, 0),
            "signal_score": 0.5,
            "features": {},
        })

    assert len(writer.alerts) == 1
    assert client.stats()["alerts_suppressed_maker_attempt_adverse"] == 0
