from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import sqlalchemy as sa

from app.config import settings
from app.services.trading.momentum_neural.counterfactual_replay import (
    CounterfactualReplayResult,
    ReplayTapeTick,
    SymbolReplayResult,
    _bar_release_times,
    _iter_bar_candidates,
    _tape_to_microbars,
    _trade_dollar_volume_asof,
    _trade_tape_to_microbars,
    load_nbbo_tape,
    load_trade_tape,
    opportunity_label_summary,
)
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger
from app.services.trading.momentum_neural.replay_provenance import (
    IQFEED_NBBO_TIMESTAMP_BASIS,
    IQFEED_TRADE_TIMESTAMP_BASIS,
    certify_iqfeed_tape_row,
)
from app.services.trading.momentum_neural.replay_v3 import RecordedOhlcvProvider


UTC = timezone.utc
REFERENCE = datetime(2026, 7, 14, 15, 0, 0, tzinfo=UTC)
TEST_BRIDGE_BUILD = "iqfeed-l1-exact-print-provenance-v3+sha256:0123456789abcdef"


def _certified_quote_row(**overrides):
    row = {
        "id": 1,
        "observed_at": REFERENCE.replace(tzinfo=None),
        "bid": 4.99,
        "ask": 5.01,
        "mid": 5.0,
        "spread_bps": 40.0,
        "source": "iqfeed_l1",
        "provider_event_at": None,
        "received_at": REFERENCE + timedelta(milliseconds=250),
        "available_at": REFERENCE + timedelta(milliseconds=500),
        "timestamp_basis": IQFEED_NBBO_TIMESTAMP_BASIS,
        "bridge_version": TEST_BRIDGE_BUILD,
        "provider_trade_reference_at": REFERENCE,
        "message_type": "Q",
        "bridge_run_id": "11111111-1111-1111-1111-111111111111",
        "connection_generation": 3,
        "availability_quarantined": False,
        "availability_quarantine_checked": True,
    }
    row.update(overrides)
    return row


def _certified_trade_row(**overrides):
    row = _certified_quote_row(
        price=5.0,
        size=100.0,
        timestamp_basis=IQFEED_TRADE_TIMESTAMP_BASIS,
    )
    row.update(overrides)
    return row


def test_complete_q_tuple_certifies_receive_clock_not_reference_clock():
    row = _certified_quote_row()

    out = certify_iqfeed_tape_row(
        row,
        expected_timestamp_basis=IQFEED_NBBO_TIMESTAMP_BASIS,
        expected_bridge_build=TEST_BRIDGE_BUILD,
    )

    assert out.certified is True
    assert out.reasons == ()
    assert out.market_reference_ts == REFERENCE
    assert out.received_ts == REFERENCE + timedelta(milliseconds=250)
    assert out.availability_ts == REFERENCE + timedelta(milliseconds=500)
    assert out.receive_reference_delta_seconds == pytest.approx(0.25)


def test_legitimate_exact_equality_is_not_quarantined_by_clock_shape_alone():
    exact = _certified_quote_row(
        received_at=REFERENCE,
        available_at=REFERENCE,
    )

    trusted = certify_iqfeed_tape_row(
        exact,
        expected_timestamp_basis=IQFEED_NBBO_TIMESTAMP_BASIS,
        expected_bridge_build=TEST_BRIDGE_BUILD,
    )
    quarantined = certify_iqfeed_tape_row(
        {**exact, "availability_quarantined": True},
        expected_timestamp_basis=IQFEED_NBBO_TIMESTAMP_BASIS,
        expected_bridge_build=TEST_BRIDGE_BUILD,
    )

    assert trusted.certified is True
    assert "availability_quarantined" not in trusted.reasons
    assert quarantined.certified is False
    assert "availability_quarantined" in quarantined.reasons


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"message_type": "P"}, "message_type_not_q"),
        ({"bridge_run_id": None}, "bridge_run_id_missing"),
        ({"bridge_run_id": "not-a-uuid"}, "bridge_run_id_invalid"),
        ({"connection_generation": 0}, "connection_generation_invalid"),
        ({"connection_generation": True}, "connection_generation_invalid"),
        ({"available_at": None}, "available_at_missing_or_naive"),
        (
            {"availability_quarantine_checked": False},
            "availability_quarantine_not_checked",
        ),
        (
            {"bridge_version": "iqfeed-l1-exact-print-provenance-v3+sha256:ffffffffffffffff"},
            "bridge_version_not_pinned",
        ),
        (
            {"received_at": REFERENCE.replace(tzinfo=None)},
            "received_at_missing_or_naive",
        ),
        (
            {"provider_trade_reference_at": REFERENCE.replace(tzinfo=None)},
            "provider_trade_reference_missing_or_naive",
        ),
        (
            {"received_at": REFERENCE + timedelta(seconds=2.01)},
            "receive_reference_delta_out_of_bounds",
        ),
        (
            {"provider_event_at": REFERENCE},
            "provider_event_at_mislabeled",
        ),
        (
            {"observed_at": REFERENCE + timedelta(milliseconds=2)},
            "observed_at_reference_mismatch",
        ),
    ],
)
def test_incomplete_or_mislabeled_tuple_fails_closed(overrides, reason):
    out = certify_iqfeed_tape_row(
        _certified_quote_row(**overrides),
        expected_timestamp_basis=IQFEED_NBBO_TIMESTAMP_BASIS,
        expected_bridge_build=TEST_BRIDGE_BUILD,
    )

    assert out.certified is False
    assert reason in out.reasons


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, _sql, _params):
        self.calls.append((str(_sql), dict(_params)))
        return _Mappings(self.rows)


def test_strict_nbbo_loader_drops_legacy_rows_and_uses_availability_clock(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay.settings.chili_iqfeed_l1_authoritative_bridge_build",
        TEST_BRIDGE_BUILD,
        raising=False,
    )
    certified = _certified_quote_row()
    legacy = _certified_quote_row(
        id=2,
        observed_at=(REFERENCE + timedelta(seconds=1)).replace(tzinfo=None),
        received_at=None,
        available_at=None,
        provider_trade_reference_at=None,
        timestamp_basis=None,
        bridge_version=None,
        message_type=None,
        bridge_run_id=None,
        connection_generation=None,
    )
    db = _FakeDb([certified, legacy])
    since = REFERENCE - timedelta(seconds=1)
    until = REFERENCE + timedelta(seconds=3)

    strict = load_nbbo_tape(
        db,
        "TEST",
        since=since,
        until=until,
        require_causal_provenance=True,
    )
    diagnostic = load_nbbo_tape(
        db,
        "TEST",
        since=since,
        until=until,
        require_causal_provenance=False,
    )

    assert len(strict) == 1
    assert strict[0].provenance_certified is True
    assert strict[0].ts == certified["available_at"]
    assert len(diagnostic) == 2
    assert [tick.provenance_certified for tick in diagnostic] == [True, False]
    strict_sql = db.calls[0][0]
    assert "available_at >= :since" in strict_sql
    assert "bridge_version = :expected_build" in strict_sql
    assert "source = 'iqfeed_l1'" in strict_sql
    assert "iqfeed_availability_quarantines" in strict_sql
    assert "iqfeed_availability_quarantine_manifests" in strict_sql
    assert "affected_update_xid" in strict_sql


def test_strict_loader_rejects_quarantined_row_even_if_db_fake_ignores_sql(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay.settings.chili_iqfeed_l1_authoritative_bridge_build",
        TEST_BRIDGE_BUILD,
        raising=False,
    )
    row = _certified_quote_row(
        availability_quarantined=True,
        received_at=REFERENCE,
        available_at=REFERENCE,
    )

    strict = load_nbbo_tape(
        _FakeDb([row]),
        "TEST",
        since=REFERENCE - timedelta(seconds=1),
        until=REFERENCE + timedelta(seconds=1),
        require_causal_provenance=True,
    )

    assert strict == []


def test_strict_trade_loader_filters_the_incident_quarantine(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay.settings.chili_iqfeed_l1_authoritative_bridge_build",
        TEST_BRIDGE_BUILD,
        raising=False,
    )
    trusted = _certified_trade_row()
    quarantined = _certified_trade_row(
        id=2,
        availability_quarantined=True,
    )
    db = _FakeDb([trusted, quarantined])

    strict = load_trade_tape(
        db,
        "TEST",
        since=REFERENCE - timedelta(seconds=1),
        until=REFERENCE + timedelta(seconds=1),
        require_causal_provenance=True,
    )

    assert [tick.sequence for tick in strict] == [1]
    sql = db.calls[0][0]
    assert "quarantine.stream_row_id = iqfeed_trade_ticks.id" in sql
    assert "manifest.bridge_run_id = iqfeed_trade_ticks.bridge_run_id" in sql
    assert "iqfeed_trade_ticks.xmin::text" in sql
    assert "available_at = iqfeed_trade_ticks.observed_at AT TIME ZONE 'UTC'" in sql


def test_strict_trade_loader_fails_closed_before_query_when_349_is_parked(
    monkeypatch,
):
    from app.services.trading.momentum_neural import counterfactual_replay

    class NoQueryDb:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("strict loader queried after missing authority")

    monkeypatch.setattr(
        counterfactual_replay,
        "_database_table_has_column",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        counterfactual_replay,
        "_database_migration_applied",
        lambda *_args, **_kwargs: False,
    )

    assert load_trade_tape(
        NoQueryDb(),
        "M349SQL",
        since=REFERENCE - timedelta(seconds=1),
        until=REFERENCE + timedelta(seconds=2),
        require_causal_provenance=True,
    ) == []
    assert load_nbbo_tape(
        NoQueryDb(),
        "M349SQL",
        since=REFERENCE - timedelta(seconds=1),
        until=REFERENCE + timedelta(seconds=2),
        require_causal_provenance=True,
    ) == []


def test_recorded_ohlcv_provider_excludes_future_and_incomplete_bar():
    index = pd.date_range("2026-07-14T15:00:00Z", periods=3, freq="1min")
    frame = pd.DataFrame(
        {
            "Open": [1.0, 2.0, 3.0],
            "High": [1.1, 2.1, 3.1],
            "Low": [0.9, 1.9, 2.9],
            "Close": [1.05, 2.05, 3.05],
            "Volume": [100.0, 200.0, 300.0],
        },
        index=index,
    )
    provider = RecordedOhlcvProvider(
        {"1m": frame},
        clock=lambda: datetime(2026, 7, 14, 15, 1, 30, tzinfo=UTC),
    )

    out = provider("TEST", interval="1m")

    assert list(out.index) == [index[0]]
    assert float(out.iloc[-1]["Close"]) == pytest.approx(1.05)


def test_recorded_ohlcv_certification_rejects_clockless_frame():
    frame = pd.DataFrame(
        {
            "Open": [1.0],
            "High": [1.1],
            "Low": [0.9],
            "Close": [1.05],
            "Volume": [100.0],
        }
    )
    provider = RecordedOhlcvProvider(
        {"1m": frame},
        clock=lambda: REFERENCE,
        certification_mode=True,
    )

    out = provider("TEST", interval="1m")

    assert out.empty
    assert provider.rejection_log == [("1m", "clock_axis_missing")]


def test_recorded_ohlcv_certification_rejects_interval_fallback():
    frame = pd.DataFrame(
        {
            "Open": [1.0],
            "High": [1.1],
            "Low": [0.9],
            "Close": [1.05],
            "Volume": [100.0],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-07-14T15:00:00Z")]),
    )
    provider = RecordedOhlcvProvider(
        {"15m": frame},
        clock=lambda: datetime(2026, 7, 14, 15, 5, tzinfo=UTC),
        certification_mode=True,
    )

    out = provider("TEST", interval="1m")

    assert out.empty
    assert provider.rejection_log == [("1m", "interval_missing")]


def test_diagnostic_provider_rejects_interval_fallback():
    frame = pd.DataFrame(
        {
            "Open": [1.0],
            "High": [1.1],
            "Low": [0.9],
            "Close": [1.05],
            "Volume": [100.0],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-07-14T15:00:00Z")]),
    )
    provider = RecordedOhlcvProvider(
        {"15m": frame},
        clock=lambda: datetime(2026, 7, 14, 15, 5, tzinfo=UTC),
    )

    out = provider("TEST", interval="1m")

    assert out.empty
    assert provider.rejection_log == [("1m", "interval_missing")]


@pytest.mark.parametrize("builder", [_tape_to_microbars, _trade_tape_to_microbars])
def test_counterfactual_microbars_are_available_only_at_bucket_close(builder):
    start = datetime(2026, 7, 14, 15, 0, 0, tzinfo=UTC)
    ticks = [
        ReplayTapeTick(
            ts=start + timedelta(seconds=1),
            bid=4.99,
            ask=5.01,
            mid=5.00,
            size=100.0,
        ),
        # This eventual high is not knowable at the bucket's 15:00:00 start.
        ReplayTapeTick(
            ts=start + timedelta(seconds=14),
            bid=8.99,
            ask=9.01,
            mid=9.00,
            size=200.0,
        ),
        # An exact-boundary print belongs to the next left-closed bucket.
        ReplayTapeTick(
            ts=start + timedelta(seconds=15),
            bid=5.99,
            ask=6.01,
            mid=6.00,
            size=300.0,
        ),
    ]

    bars = builder(ticks, bar_seconds=15)

    assert bars is not None
    assert list(bars.index) == [
        pd.Timestamp(start + timedelta(seconds=15)),
        pd.Timestamp(start + timedelta(seconds=30)),
    ]
    assert float(bars.iloc[0]["High"]) == pytest.approx(9.0)
    assert float(bars.iloc[0]["Close"]) == pytest.approx(9.0)
    assert float(bars.iloc[1]["Open"]) == pytest.approx(6.0)
    assert bars.attrs["replay_timestamp_semantics"] == (
        "market_close_geometry_with_strategy_release_clock"
    )
    assert _bar_release_times(bars) == tuple(
        ts.to_pydatetime() for ts in bars.index
    )


def test_microbars_bucket_on_market_reference_but_release_on_availability():
    start = datetime(2026, 7, 14, 15, 0, 0, tzinfo=UTC)
    ticks = [
        ReplayTapeTick(
            ts=start + timedelta(seconds=16),
            availability_ts=start + timedelta(seconds=16),
            market_reference_ts=start + timedelta(seconds=1),
            bid=4.99,
            ask=5.01,
            mid=5.0,
            sequence=1,
        ),
        # This later market event was published before the first row.  It must
        # still be the close, not the open, of the market-time bucket.
        ReplayTapeTick(
            ts=start + timedelta(seconds=15, milliseconds=500),
            availability_ts=start + timedelta(seconds=15, milliseconds=500),
            market_reference_ts=start + timedelta(seconds=14),
            bid=8.99,
            ask=9.01,
            mid=9.0,
            sequence=2,
        ),
        ReplayTapeTick(
            ts=start + timedelta(seconds=15, milliseconds=600),
            availability_ts=start + timedelta(seconds=15, milliseconds=600),
            market_reference_ts=start + timedelta(seconds=15),
            bid=5.99,
            ask=6.01,
            mid=6.0,
            sequence=3,
        ),
    ]

    bars = _tape_to_microbars(ticks, bar_seconds=15)

    assert bars is not None
    assert list(bars.index) == [
        pd.Timestamp(start + timedelta(seconds=15)),
        pd.Timestamp(start + timedelta(seconds=30)),
    ]
    assert float(bars.iloc[0]["Open"]) == pytest.approx(5.0)
    assert float(bars.iloc[0]["Close"]) == pytest.approx(9.0)
    assert float(bars.iloc[1]["Open"]) == pytest.approx(6.0)
    assert _bar_release_times(bars) == (
        start + timedelta(seconds=16),
        start + timedelta(seconds=30),
    )


def test_late_trade_volume_member_delays_quote_bar_release():
    start = datetime(2026, 7, 14, 15, 0, 0, tzinfo=UTC)
    quotes = [
        ReplayTapeTick(
            ts=start + timedelta(seconds=15, milliseconds=100),
            availability_ts=start + timedelta(seconds=15, milliseconds=100),
            market_reference_ts=start + timedelta(seconds=1),
            bid=4.99,
            ask=5.01,
            mid=5.0,
        ),
        ReplayTapeTick(
            ts=start + timedelta(seconds=15, milliseconds=200),
            availability_ts=start + timedelta(seconds=15, milliseconds=200),
            market_reference_ts=start + timedelta(seconds=14),
            bid=5.99,
            ask=6.01,
            mid=6.0,
        ),
        ReplayTapeTick(
            ts=start + timedelta(seconds=16, milliseconds=200),
            availability_ts=start + timedelta(seconds=16, milliseconds=200),
            market_reference_ts=start + timedelta(seconds=16),
            bid=6.99,
            ask=7.01,
            mid=7.0,
        ),
    ]
    trades = [
        ReplayTapeTick(
            ts=start + timedelta(seconds=18),
            availability_ts=start + timedelta(seconds=18),
            market_reference_ts=start + timedelta(seconds=2),
            bid=99.0,
            ask=101.0,
            mid=100.0,
            size=40.0,
        )
    ]

    bars = _tape_to_microbars(quotes, bar_seconds=15, trade_ticks=trades)

    assert bars is not None
    assert float(bars.iloc[0]["Volume"]) == pytest.approx(40.0)
    assert _bar_release_times(bars)[0] == start + timedelta(seconds=18)


def test_equal_cumulative_release_evaluates_only_atomic_final_prefix(monkeypatch):
    start = datetime(2026, 7, 14, 15, 0, 0, tzinfo=UTC)
    delayed_release = start + timedelta(seconds=180)
    ticks = []
    for idx in range(10):
        market_ts = start + timedelta(seconds=idx * 15 + 1)
        available = delayed_release if idx == 0 else market_ts + timedelta(seconds=1)
        ticks.append(
            ReplayTapeTick(
                ts=available,
                availability_ts=available,
                market_reference_ts=market_ts,
                bid=4.99 + idx * 0.01,
                ask=5.01 + idx * 0.01,
                mid=5.0 + idx * 0.01,
                sequence=idx + 1,
            )
        )
    bars = _tape_to_microbars(ticks, bar_seconds=15)
    assert bars is not None
    assert len(set(_bar_release_times(bars))) == 1

    calls: list[tuple[int, datetime, float]] = []

    def _decline(_family, *, frame, now, live_price, **_kwargs):
        calls.append((len(frame), now, live_price))
        return False, "test_decline", {}

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay._call_bar_gate",
        _decline,
    )

    _iter_bar_candidates(
        symbol="TEST",
        ticks=sorted(ticks, key=lambda tick: tick.ts),
        bars=bars,
        bar_seconds=15,
        require_release_clock=True,
    )

    assert calls
    assert {length for length, _now, _price in calls} == {10}
    assert {now for _length, now, _price in calls} == {delayed_release}


def test_synthetic_gap_bars_do_not_create_decision_events(monkeypatch):
    start = datetime(2026, 7, 14, 15, 0, 0, tzinfo=UTC)
    ticks = [
        ReplayTapeTick(
            ts=start + timedelta(seconds=2),
            market_reference_ts=start + timedelta(seconds=1),
            availability_ts=start + timedelta(seconds=2),
            bid=4.99,
            ask=5.01,
            mid=5.0,
        ),
        ReplayTapeTick(
            ts=start + timedelta(seconds=152),
            market_reference_ts=start + timedelta(seconds=151),
            availability_ts=start + timedelta(seconds=152),
            bid=5.99,
            ask=6.01,
            mid=6.0,
        ),
    ]
    bars = _tape_to_microbars(ticks, bar_seconds=15)
    assert bars is not None and len(bars) == 11
    calls: list[int] = []

    def _decline(_family, *, frame, **_kwargs):
        calls.append(len(frame))
        return False, "test_decline", {}

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay._call_bar_gate",
        _decline,
    )

    _iter_bar_candidates(
        symbol="TEST",
        ticks=ticks,
        bars=bars,
        bar_seconds=15,
        require_release_clock=True,
    )

    assert calls
    assert set(calls) == {11}


def test_strict_iterator_rejects_hand_built_bars_without_release_metadata():
    index = pd.date_range("2026-07-14T15:00:15Z", periods=10, freq="15s")
    bars = pd.DataFrame(
        {
            "Open": [5.0] * 10,
            "High": [5.0] * 10,
            "Low": [5.0] * 10,
            "Close": [5.0] * 10,
            "Volume": [100.0] * 10,
        },
        index=index,
    )

    candidates, reasons = _iter_bar_candidates(
        symbol="TEST",
        ticks=[],
        bars=bars,
        bar_seconds=15,
        require_release_clock=True,
    )

    assert candidates == []
    assert reasons == {"bar_release_clock_missing": 1}


def test_counterfactual_gate_frame_preserves_full_causal_session(monkeypatch):
    index = pd.date_range("2026-07-13T11:00:15Z", periods=170, freq="15s")
    closes = [20.0] + [5.0] * 169
    bars = pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [100.0] * 170,
        },
        index=index,
    )
    ticks = [
        ReplayTapeTick(
            ts=index[0].to_pydatetime(),
            bid=4.99,
            ask=5.01,
            mid=5.0,
        )
    ]
    observed: list[tuple[int, float]] = []

    def _decline(_family, *, frame, **_kwargs):
        observed.append((len(frame), float(frame["High"].max())))
        return False, "test_decline", {}

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay._call_bar_gate",
        _decline,
    )

    _iter_bar_candidates(
        symbol="PLSM",
        ticks=ticks,
        bars=bars,
        bar_seconds=15,
    )

    assert observed
    assert observed[-1] == (170, 20.0)


def test_sixty_second_replay_matches_one_minute_first_pullback(monkeypatch):
    """A 60-second replay bar is the production 1m interval, not a new 60s lane."""

    monkeypatch.setattr(settings, "chili_momentum_entry_first_pullback_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pullback_entry_interval", "1m")
    monkeypatch.setattr(settings, "chili_momentum_first_pullback_interval", "15s")
    monkeypatch.setattr(settings, "chili_momentum_pullback_require_retest", True)

    n = 30
    base = [10.0 + index * (2.10 / (n - 5)) for index in range(n - 4)]
    opens = [price - 0.05 for price in base]
    highs = [price + 0.10 for price in base]
    lows = [price - 0.10 for price in base]
    closes = [price + 0.05 for price in base]
    for high, low, close in (
        (12.20, 11.95, 12.05),
        (12.10, 11.90, 12.00),
        (12.05, 11.92, 12.02),
    ):
        opens.append(close - 0.03)
        highs.append(high)
        lows.append(low)
        closes.append(close)
    opens.append(12.05)
    highs.append(12.40)
    lows.append(12.00)
    closes.append(12.30)
    volume = [
        200_000.0 + index * (400_000.0 / (n - 5))
        for index in range(n - 4)
    ] + [300_000.0, 280_000.0, 320_000.0, 900_000.0]
    index = pd.date_range("2026-07-14T14:00:00Z", periods=n, freq="1min")
    bars = pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volume,
        },
        index=index,
    )
    now = index[-1].to_pydatetime()

    live_ok, live_reason, live_debug = momentum_pullback_trigger(
        bars,
        entry_interval="1m",
        live_price=12.30,
        symbol="TEST",
        now=now,
        db=None,
        l2_as_of=now,
    )
    assert live_ok is True
    assert live_reason == "first_pullback_ok"
    assert live_debug.get("pattern") == "first_pullback"

    # Only the final completed bar has an executable quote, so this exercises
    # one full replay evaluation without fabricating earlier tick decisions.
    ticks = [
        ReplayTapeTick(
            ts=now,
            bid=12.29,
            ask=12.31,
            mid=12.30,
        )
    ]
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay._BAR_GATE_FAMILIES",
        ("momentum_pullback",),
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.entry_gates.evaluate_sticky_backside_bench",
        lambda *_args, **_kwargs: (False, "front_side", None, {}),
    )

    candidates, _reasons = _iter_bar_candidates(
        symbol="TEST",
        ticks=ticks,
        bars=bars,
        bar_seconds=60,
    )

    assert len(candidates) == 1
    assert candidates[0].reason == live_reason
    assert candidates[0].trigger_debug.get("pattern") == live_debug.get("pattern")


def test_counterfactual_applies_production_sticky_backside_bench(monkeypatch):
    index = pd.date_range("2026-07-13T12:00:15Z", periods=20, freq="1min")
    closes = [10.0, 9.0, 8.0, 7.0, 6.0] + [5.0] * 15
    bars = pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [100.0] * len(closes),
        },
        index=index,
    )
    ticks = [
        ReplayTapeTick(
            ts=index[0].to_pydatetime(),
            bid=4.99,
            ask=5.01,
            mid=5.0,
        )
    ]

    def _late_fire(family, *, now, **_kwargs):
        if family == "momentum_pullback" and now == index[-1].to_pydatetime():
            return True, "synthetic_late_curl", {"pullback_low": 4.90}
        return False, "test_decline", {}

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.counterfactual_replay._call_bar_gate",
        _late_fire,
    )

    candidates, reasons = _iter_bar_candidates(
        symbol="PLSM",
        ticks=ticks,
        bars=bars,
        bar_seconds=60,
    )

    assert candidates == []
    assert reasons.get("backside_bench_veto:benched_backside_sticky") == 1


def test_liquidity_volume_excludes_prints_after_candidate():
    start = datetime(2026, 7, 14, 15, 0, 0, tzinfo=UTC)
    ticks = [
        ReplayTapeTick(
            ts=start,
            bid=9.99,
            ask=10.01,
            mid=10.0,
            size=100.0,
        ),
        ReplayTapeTick(
            ts=start + timedelta(seconds=1),
            bid=19.99,
            ask=20.01,
            mid=20.0,
            size=10_000.0,
        ),
    ]

    asof_volume = _trade_dollar_volume_asof(ticks, start)

    assert asof_volume == pytest.approx(1_000.0)


def test_replay_bar_geometry_uses_nbbo_mid_and_trade_print_volume():
    start = datetime(2026, 7, 14, 15, 0, 0, tzinfo=UTC)
    quotes = [
        ReplayTapeTick(
            ts=start + timedelta(seconds=1),
            bid=4.99,
            ask=5.01,
            mid=5.0,
        ),
        ReplayTapeTick(
            ts=start + timedelta(seconds=14),
            bid=5.99,
            ask=6.01,
            mid=6.0,
        ),
        ReplayTapeTick(
            ts=start + timedelta(seconds=15),
            bid=6.99,
            ask=7.01,
            mid=7.0,
        ),
    ]
    trades = [
        ReplayTapeTick(
            ts=start + timedelta(seconds=2),
            bid=99.0,
            ask=101.0,
            mid=100.0,
            size=40.0,
        ),
        ReplayTapeTick(
            ts=start + timedelta(seconds=3),
            bid=199.0,
            ask=201.0,
            mid=200.0,
            size=60.0,
        ),
    ]

    bars = _tape_to_microbars(
        quotes,
        bar_seconds=15,
        trade_ticks=trades,
    )

    assert bars is not None
    assert float(bars.iloc[0]["High"]) == pytest.approx(6.0)
    assert float(bars.iloc[0]["Volume"]) == pytest.approx(100.0)


def test_opportunity_labels_refuse_legacy_diagnostic_tape():
    row = SymbolReplayResult(
        symbol="TEST",
        ok=True,
        confidence="tick_quote_complete_limited",
        confidence_reasons=["causal_provenance_not_enforced"],
        tape_rows=10,
        trade_rows=10,
        causal_tape_rows=0,
        causal_trade_rows=0,
        causal_provenance_enforced=False,
        micro_bars=10,
        source_events=[
            {
                "ts": REFERENCE.isoformat(),
                "source": "reviewed_ross_frame",
                "certifiable": True,
                "text": "front-side setup",
            }
        ],
        trades=[],
        candidate_count=1,
        skipped_reasons={},
        gate_reason_counts={},
        first_candidate={"ts": REFERENCE.isoformat()},
    )
    result = CounterfactualReplayResult(
        since=REFERENCE - timedelta(minutes=1),
        until=REFERENCE + timedelta(minutes=1),
        symbols=["TEST"],
        results=[row],
        causal_provenance_enforced=False,
    )

    summary = opportunity_label_summary(result)

    assert summary["label_ready_symbol_count"] == 0
    assert summary["pnl_minmax_label_ready"] is False
    assert summary["rows"][0]["status"] == "causal_provenance_unavailable"
