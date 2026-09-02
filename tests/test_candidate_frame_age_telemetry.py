"""Edad ng frame sa mismong candidate fire (#1286) — SUKAT, hindi hinuha.

NASUKAT 2026-09-02 sa AUUD 09-01: LAHAT ng 24 `live_entry_candidate_detected`
ay may trigger na `momentum_ok_rel_vol` na WALANG `_rate` suffix. Ayon sa
entry_gates `_forming_bar_elapsed_fraction` ⇒ None ⇒ KUMPLETO na (>= 15 min
ang tanda) ang huling 15m bar nang pumutok ang trigger. Ang 15m/1m frame ay
dumadaan sa `fetch_ohlcv_df` (600s DataFrame cache) sa ibabaw ng Massive bar
cache na max(180s, 3x bar), at WALANG nag-i-invalidate sa exec lane; ang 10s
micro frame ay segundo-sariwa. Ang `read_tick_bar_age_meter` ay thread-local
at hindi pinepersist; ang `cache_age_seconds` ay 0.0 sa bawat store.

Ang payload ng candidate fire ngayon ay nagdadala ng::

    frame_15m_last_bar_age_s   edad ng huling bar ng 15m _entry_df (None kung di mabasa)
    frame_trig_interval        label ng _iv_trig ('15s' micro, '1m', '5m')
    frame_trig_last_bar_age_s  edad ng huling bar ng _df_trig
    frame_trig_rows            len(_df_trig)
    micro_frame_used           bool: naipalit ba ang micro-bar frame
    tick_ohlcv_bar_age_max_s   cross-check: max ng thread-local meter sa pass na ito

⚠️ TELEMETRY LAMANG. Zero decision change. Ang orasan ay ang orasan ng TICK
(`_utcnow_aware()` ⇒ sim clock sa replay), hindi ang wall clock.

Runnable: pytest tests/test_candidate_frame_age_telemetry.py -v
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)

NOW = datetime(2026, 9, 1, 13, 45, 0, tzinfo=timezone.utc)

KEYS = (
    "frame_15m_last_bar_age_s",
    "frame_trig_interval",
    "frame_trig_last_bar_age_s",
    "frame_trig_rows",
    "micro_frame_used",
)


def _df(last_ts, n=3, step=timedelta(minutes=15), tz="UTC"):
    idx = pd.DatetimeIndex([last_ts - step * (n - 1 - i) for i in range(n)])
    if tz is not None and idx.tz is None:
        idx = idx.tz_localize(tz)
    return pd.DataFrame({"close": [1.0] * n}, index=idx)


# ── ang purong helper: _frame_last_bar_age_seconds(df, now) ──────────────────


def test_tz_aware_index_measures_against_the_supplied_clock():
    """Ang AUUD na kaso: kumpletong 15m bar = 900s+ ang tanda sa oras ng fire."""
    age = LR._frame_last_bar_age_seconds(_df(NOW - timedelta(seconds=930)), NOW)
    assert age == 930.0


def test_naive_index_is_read_as_utc():
    naive_last = (NOW - timedelta(seconds=600)).replace(tzinfo=None)
    age = LR._frame_last_bar_age_seconds(_df(naive_last, tz=None), NOW)
    assert age == 600.0


def test_naive_now_is_read_as_utc_too():
    age = LR._frame_last_bar_age_seconds(
        _df(NOW - timedelta(seconds=45)), NOW.replace(tzinfo=None)
    )
    assert age == 45.0


def test_the_clock_is_the_callers_not_the_wall_clock():
    """⚠️ Sa replay ang sim clock ang orasan. Ang isang `now` na taong 2026-09-01
    laban sa frame ng 2026-09-01 ay dapat magbasa ng segundo, HINDI ng mga araw
    mula sa wall clock ng makinang nagre-replay."""
    frame = _df(NOW - timedelta(seconds=120))
    assert LR._frame_last_bar_age_seconds(frame, NOW) == 120.0
    # parehong frame, ibang orasan ⇒ ibang edad: patunay na ang `now` ang ginagamit
    assert LR._frame_last_bar_age_seconds(frame, NOW + timedelta(hours=1)) == 3720.0


def test_a_new_york_stamped_index_converts_correctly():
    last = (NOW - timedelta(seconds=300)).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    age = LR._frame_last_bar_age_seconds(_df(last, tz=None), NOW)
    assert age == 300.0


def test_future_stamp_clamps_to_zero_not_negative():
    assert LR._frame_last_bar_age_seconds(_df(NOW + timedelta(minutes=5)), NOW) == 0.0


def test_empty_frame_yields_None():
    assert LR._frame_last_bar_age_seconds(pd.DataFrame(), NOW) is None


def test_non_datetime_index_yields_None():
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=[10, 20])
    assert LR._frame_last_bar_age_seconds(df, NOW) is None
    assert LR._frame_last_bar_age_seconds(SimpleNamespace(index=["x"]), NOW) is None


def test_missing_frame_or_clock_yields_None_never_raises():
    assert LR._frame_last_bar_age_seconds(None, NOW) is None
    assert LR._frame_last_bar_age_seconds(object(), NOW) is None
    assert LR._frame_last_bar_age_seconds(_df(NOW), None) is None
    assert LR._frame_last_bar_age_seconds(_df(NOW), "not-a-clock") is None


def test_wall_clock_wrapper_still_serves_the_tick_meter():
    """Ang lumang `_frame_bar_age_seconds` (meter #1116) ay dumadaan na ngayon sa
    purong core; dapat pareho pa rin ang basa nito."""
    now = datetime.now(timezone.utc)
    age = LR._frame_bar_age_seconds(_df(now - timedelta(minutes=10)))
    assert age is not None and 595 < age < 605


# ── ang payload builder: _candidate_frame_age_telemetry ──────────────────────


def test_builder_reports_the_auud_shape_stale_15m_fresh_trigger_frame():
    """AUUD 09-01: 15m bar kumpleto na (>=900s), 1m trigger frame mas bago."""
    LR.reset_tick_ohlcv_meter()
    out = LR._candidate_frame_age_telemetry(
        entry_df=_df(NOW - timedelta(seconds=960)),
        df_trig=_df(NOW - timedelta(seconds=75), n=40, step=timedelta(minutes=1)),
        iv_trig="1m",
        micro_frame_used=False,
        now=NOW,
    )
    for k in KEYS:
        assert k in out
    assert out["frame_15m_last_bar_age_s"] == 960.0
    assert out["frame_trig_interval"] == "1m"
    assert out["frame_trig_last_bar_age_s"] == 75.0
    assert out["frame_trig_rows"] == 40
    assert out["micro_frame_used"] is False
    # walang OHLCV read sa thread na ito ⇒ walang cross-check na numero
    assert out["tick_ohlcv_bar_age_max_s"] is None


def test_builder_marks_the_micro_frame_swap():
    out = LR._candidate_frame_age_telemetry(
        entry_df=_df(NOW - timedelta(seconds=1200)),
        df_trig=_df(NOW - timedelta(seconds=12), n=12, step=timedelta(seconds=15)),
        iv_trig="15s",
        micro_frame_used=True,
        now=NOW,
    )
    assert out["micro_frame_used"] is True
    assert out["frame_trig_interval"] == "15s"
    assert out["frame_trig_last_bar_age_s"] == 12.0
    assert out["frame_trig_rows"] == 12
    assert out["frame_15m_last_bar_age_s"] == 1200.0


def test_builder_reports_None_for_unknown_frames_not_a_guessed_zero():
    """⚠️ Ang unbound na frame sa ilang sangay ay dapat lumabas na None. Ang 0
    ay magmumukhang SARIWA at magsisinungaling sa mismong tanong."""
    out = LR._candidate_frame_age_telemetry(
        entry_df=None, df_trig=None, iv_trig=None, micro_frame_used=False, now=NOW
    )
    assert out["frame_15m_last_bar_age_s"] is None
    assert out["frame_trig_interval"] is None
    assert out["frame_trig_last_bar_age_s"] is None
    assert out["frame_trig_rows"] is None
    assert out["micro_frame_used"] is False


def test_builder_carries_the_tick_meter_max_as_a_cross_check():
    LR.reset_tick_ohlcv_meter()
    try:
        wall = datetime.now(timezone.utc)
        LR._meter_tick_ohlcv(0.01, _df(wall - timedelta(minutes=9)))
        out = LR._candidate_frame_age_telemetry(
            entry_df=None, df_trig=None, iv_trig=None, micro_frame_used=False, now=NOW
        )
        assert out["tick_ohlcv_bar_age_max_s"] is not None
        assert 535 < out["tick_ohlcv_bar_age_max_s"] < 545
    finally:
        LR.reset_tick_ohlcv_meter()


def test_builder_never_raises_on_garbage():
    out = LR._candidate_frame_age_telemetry(
        entry_df=object(), df_trig=SimpleNamespace(index=None),
        iv_trig=object(), micro_frame_used=None, now="bad",
    )
    assert set(KEYS) <= set(out)
    assert out["micro_frame_used"] is False


# ── bantay sa wiring ─────────────────────────────────────────────────────────


def _emit_window() -> str:
    src = _SRC.read_text(encoding="utf-8")
    idx = src.find('db, sess, "live_entry_candidate_detected",')
    assert idx > 0, "dapat umiiral ang candidate emit"
    return src[max(0, idx - 2600): idx + 900]


def test_the_candidate_emit_payload_carries_the_five_keys():
    """BANTAY SA WIRING. Ang sukat ay walang silbi kung hindi naiuulat."""
    window = _emit_window()
    assert "_candidate_frame_age_telemetry(" in window
    assert "**_cf_payload" in window
    assert "now=_utcnow_aware()" in window, "orasan ng TICK, hindi wall clock"
    helper_src = _SRC.read_text(encoding="utf-8")
    start = helper_src.find("def _candidate_frame_age_telemetry(")
    assert start > 0
    body = helper_src[start: helper_src.find("\ndef ", start + 10)]
    for k in KEYS:
        assert '"%s"' % k in body, k


def test_unbound_frames_fall_open_to_a_missing_number():
    """⚠️ Ang _df_trig/_iv_trig/_vol_15m_df ay unbound sa ilang sangay (halt-
    resume dip, zero-bar cold start). Bawat pagbasa ay nakabalot; walang
    hulang default."""
    window = _emit_window()
    assert "_cf_df_trig, _cf_iv_trig = _df_trig, _iv_trig" in window
    assert "_cf_15m = _vol_15m_df" in window
    assert "_cf_micro = bool(_micro_frame_used)" in window
    assert window.count("except Exception:") >= 5


def test_the_micro_marker_sits_on_the_swap_itself():
    """Ang `micro_frame_used` ay totoo LAMANG sa linya kung saan naipalit ang
    micro frame; ang default ay kasama ng `_df_trig, _iv_trig = _df_pb`."""
    src = _SRC.read_text(encoding="utf-8")
    swap = src.find('_df_trig, _iv_trig = _df_micro, "15s"')
    assert swap > 0
    after = src[swap: swap + 200]
    assert "_micro_frame_used = True" in after
    base = src.find("_df_trig, _iv_trig = _df_pb, _interval")
    assert base > 0 and base < swap
    assert "_micro_frame_used = False" in src[base: base + 400]


def test_no_decision_reads_the_new_keys():
    """⚠️⚠️ ANG BANTAY NA PINAKAMAHALAGA. Telemetry ito. Bawat susi ay isinusulat
    nang isang beses (sa builder) at hindi binabasa ng kahit anong gate."""
    src = _SRC.read_text(encoding="utf-8")
    start = src.find("def _candidate_frame_age_telemetry(")
    end = src.find("\ndef ", start + 10)
    assert 0 < start < end
    builder, rest = src[start:end], src[:start] + src[end:]
    for k in KEYS + ("tick_ohlcv_bar_age_max_s",):
        # sa builder: init sa dict + isang assignment lamang (walang pagbasa)
        assert 1 <= builder.count('"%s"' % k) <= 2, k
        assert 'out["%s"] =' % k in builder or ('"%s": ' % k) in builder, k
        # sa labas ng builder: ZERO quoted key — walang gate, walang le.get,
        # walang payload_json (ang `_micro_frame_used` local at ang kwarg ng
        # builder ay wiring, hindi pagbasa ng susi)
        assert rest.count('"%s"' % k) == 0, "%s ay binabasa sa labas ng builder" % k
        assert rest.count("'%s'" % k) == 0, "%s ay binabasa sa labas ng builder" % k
        for forbidden in ('if out["%s"]' % k, "%s >" % k, "%s <" % k):
            assert forbidden not in builder, "may nagpapasya na batay dito: %s" % forbidden
    # ang marker ay isinusulat sa 2 linya (False/True) at binabasa sa 1 (payload) lamang
    assert src.count("_micro_frame_used = ") == 2
    assert src.count("bool(_micro_frame_used)") == 1
