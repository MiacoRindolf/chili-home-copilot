"""Bar-age meter (2026-08-23) — ang tanging tapat na sukat ng frame freshness.

Ang `cache_age_seconds` ay itinatakda sa 0.0 sa BAWAT store, kasama ang store
na ang laman ay galing sa 1-oras-na-TTL na aggregate layer — kaya ang frame na
may isang-oras nang bars ay nag-uulat ng edad na zero. Ang distansya papunta sa
timestamp ng HULING BAR ang tanging hindi mapepeke.

Observability lang: walang nagdedesisyon dito.

Runnable: pytest tests/test_bar_age_meter.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from app.services.trading.momentum_neural import live_runner as lr


def _df(last_ts, n=3):
    idx = pd.DatetimeIndex(
        [last_ts - timedelta(minutes=(n - 1 - i)) for i in range(n)]
    )
    return pd.DataFrame({"close": [1.0] * n}, index=idx)


def test_fresh_frame_reads_near_zero():
    now = datetime.now(timezone.utc)
    age = lr._frame_bar_age_seconds(_df(now))
    assert age is not None and age < 5.0


def test_stale_frame_reports_its_true_age():
    now = datetime.now(timezone.utc)
    age = lr._frame_bar_age_seconds(_df(now - timedelta(minutes=10)))
    assert age is not None and 590 < age < 610


def test_hour_old_frame_is_visible():
    """Ang eksaktong kaso na hindi kayang makita ng cache_age_seconds."""
    now = datetime.now(timezone.utc)
    age = lr._frame_bar_age_seconds(_df(now - timedelta(hours=1)))
    assert age is not None and age > 3500


def test_naive_index_is_treated_as_utc():
    naive = datetime.utcnow() - timedelta(minutes=5)
    idx = pd.DatetimeIndex([naive])
    df = pd.DataFrame({"close": [1.0]}, index=idx)
    age = lr._frame_bar_age_seconds(df)
    assert age is not None and 290 < age < 310


def test_future_stamp_is_clamped_not_negative():
    now = datetime.now(timezone.utc)
    age = lr._frame_bar_age_seconds(_df(now + timedelta(minutes=5)))
    assert age == 0.0


def test_unreadable_frames_never_raise():
    assert lr._frame_bar_age_seconds(None) is None
    assert lr._frame_bar_age_seconds(SimpleNamespace()) is None
    assert lr._frame_bar_age_seconds(pd.DataFrame()) is None
    assert lr._frame_bar_age_seconds(SimpleNamespace(index=["not-a-time"])) is None


def test_meter_accumulates_max_and_mean():
    lr.reset_tick_ohlcv_meter()
    now = datetime.now(timezone.utc)
    lr._meter_tick_ohlcv(0.01, _df(now - timedelta(minutes=1)))
    lr._meter_tick_ohlcv(0.01, _df(now - timedelta(minutes=9)))
    bmax, bsum, bn = lr.read_tick_bar_age_meter()
    assert bn == 2
    assert 530 < bmax < 550          # ang pinakamatanda
    assert 590 < bsum < 610          # ~60 + ~540
    lr.reset_tick_ohlcv_meter()


def test_reset_clears_bar_age_state():
    lr.reset_tick_ohlcv_meter()
    lr._meter_tick_ohlcv(0.01, _df(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert lr.read_tick_bar_age_meter()[2] == 1
    lr.reset_tick_ohlcv_meter()
    assert lr.read_tick_bar_age_meter() == (0.0, 0.0, 0)


def test_unreadable_frame_does_not_pollute_the_meter():
    lr.reset_tick_ohlcv_meter()
    lr._meter_tick_ohlcv(0.01, None)
    lr._meter_tick_ohlcv(0.01, pd.DataFrame())
    assert lr.read_tick_bar_age_meter() == (0.0, 0.0, 0)
    lr.reset_tick_ohlcv_meter()


def test_meter_never_raises_on_hostile_input():
    lr.reset_tick_ohlcv_meter()

    class _Hostile:
        @property
        def index(self):
            raise RuntimeError("boom")

        @property
        def attrs(self):
            raise RuntimeError("boom")

    lr._meter_tick_ohlcv(0.01, _Hostile())  # dapat hindi sumabog
    lr.reset_tick_ohlcv_meter()


def test_scheduler_logs_bar_age():
    import inspect

    from app.services import trading_scheduler as ts

    src = inspect.getsource(ts._run_momentum_live_runner_batch_job)
    assert "read_tick_bar_age_meter" in src
    assert "bar_age_max" in src and "bar_age_mean" in src
