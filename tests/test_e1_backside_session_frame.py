"""Unit tests for the E1 session-frame fix (chili/momentum-defensive-veto-bundle).

The live runner fetches a 5-DAY intraday OHLCV frame and flows it through
``pullback_break_confirmation`` -> the E1 block calls ``front_side_state(df)``.
``front_side_state`` is SESSION-anchored (closes[0] = session open, max/min = HOD/LOD,
cumulative session VWAP), so on a 5-day frame every anchor spans 5 days -> garbage
backside read. The fix adds ``_today_session_frame(df)`` and calls
``front_side_state(_today_session_frame(df))`` at the E1 site ONLY.

Cases:
  (1) PARITY      — flag OFF -> E1 path never taken; front_side_state never called;
                    result byte-identical across repeated runs.
  (2) CORRECTNESS — TODAY session is clearly backside (last < today VWAP / faded >66%
                    off today HOD) -> E1 vetoes "backside_lifecycle_veto".
  (3) NO-FALSE-VETO — 5-day view looks faded/extended but TODAY is a fresh front-side
                    thrust near VWAP -> E1 does NOT veto (proves the slice fixed it).
  (4) FAIL-OPEN   — single-session frame + non-DatetimeIndex (RangeIndex) frame ->
                    no crash; _today_session_frame returns the frame unchanged;
                    front_side_state fails open (is_backside=False).
  (5) DIP-BUY EXEMPT — the _deep_reclaim path is not vetoed by E1.

Also unit-tests the slice (_today_session_frame) and front_side_state directly.

The point-in-time MACD/EMA gate (_detect_back_side) sits just BEFORE E1 in the function;
the integration tests neutralise it (return front-side) so the E1 block is what is under
test. front_side_state itself is exercised on REAL frames (never stubbed) so the
session-anchored math is genuinely covered.
"""

import numpy as np
import pandas as pd

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural.entry_gates import (
    _today_session_frame,
    pullback_break_confirmation,
)
from app.services.trading.momentum_neural.ross_momentum import front_side_state


# --------------------------------------------------------------------------- #
# Frame builders — tz-aware UTC DatetimeIndex, realistic OHLCV.
# --------------------------------------------------------------------------- #
def _session_index(date: str, n: int, freq: str = "1min", start: str = "13:30") -> pd.DatetimeIndex:
    """A tz-aware UTC intraday index of ``n`` bars for one date (start ~RTH open UTC)."""
    return pd.date_range(f"{date} {start}", periods=n, freq=freq, tz="UTC")


def _ohlc_from_closes(closes, vols, idx) -> pd.DataFrame:
    """Build a plausible OHLC frame from a close path (small wicks, real volume)."""
    closes = np.asarray(closes, dtype=float)
    vols = np.asarray(vols, dtype=float)
    n = len(closes)
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) * 1.001
    lows = np.minimum(opens, closes) * 0.999
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


def _multi_day_frame(day_paths, freq="1min", start="13:30"):
    """Concatenate per-day (closes, vols) tuples into one multi-day tz-aware frame."""
    frames = []
    base = pd.Timestamp("2026-06-15")
    for i, (closes, vols) in enumerate(day_paths):
        date = (base + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
        idx = _session_index(date, len(closes), freq=freq, start=start)
        frames.append(_ohlc_from_closes(closes, vols, idx))
    return pd.concat(frames)


def _fresh_thrust_day():
    """A FRONT-SIDE day: a morning pop to the HOD, a long base/consolidation that pulls
    the cumulative VWAP UP near price, then a gentle break. The last close sits NEAR VWAP
    (LOW vwap_dist_sigma) and below the wick HOD -> NOT chasing_top, above VWAP, ~no fade
    -> front_side. This is exactly the Ross 'fresh thrust near VWAP' setup."""
    ramp = np.linspace(10.0, 11.0, 8)                       # morning pop to ~11 HOD
    base = 11.0 + 0.05 * np.sin(np.linspace(0, 6 * np.pi, 31))  # long consolidation @ ~11
    brk = np.array([11.04])                                 # gentle break, well below wick HOD
    closes = np.concatenate([ramp, base, brk])
    vols = np.full(len(closes), 1000.0)
    vols[-1] = 4000.0                                       # break-bar volume spike
    return closes, vols


def _faded_backside_day(n=30, lo=10.0, hi=12.0):
    """A BACKSIDE day: ran to a high then FADED hard -> last close below the session VWAP
    (and >66% retraced off HOD). front_side_state reads is_backside=True (below_vwap)."""
    up = np.linspace(lo, hi, n // 2)
    down = np.linspace(hi, lo - 0.5, n - (n // 2))   # crash back below the open + VWAP
    closes = np.concatenate([up, down])
    vols = np.full(n, 1000.0)
    return closes, vols


def _prior_runup_day(n=30):
    """A prior session that ran much HIGHER (10 -> 20) — used only to pollute the 5-day
    anchors so the full-frame read mis-fires while today (~11) is genuinely front-side."""
    return np.linspace(10.0, 20.0, n), np.full(n, 1000.0)


def _blowoff_chasing_top_day():
    """A GENUINE extended-AND-ROLLING blow-off (the QXL chase the recalibration KEEPS catching):
    a long flat base drags the cumulative VWAP low, a parabolic spike makes the HOD, then the
    name comes OFF the high making confirmed LOWER highs. Last close is still top-of-range AND
    far above VWAP (high vwap_dist_sigma) AND a lower high has formed after the HOD ->
    front_side_state reads is_backside=True reason='chasing_top'. Distinct from a CLEAN
    new-high thrust (HOD on the last bar -> NOT rolled over -> NOT chasing_top)."""
    base = np.full(18, 10.0)
    up = np.array([11.0, 13.0, 16.0, 19.0, 20.0])    # parabolic peak (HOD) at 20
    roll = np.array([19.2, 19.4, 19.3])              # confirmed lower highs AFTER the HOD
    closes = np.concatenate([base, up, roll])
    vols = np.full(len(closes), 1000.0)
    return closes, vols


def _clean_new_high_thrust_day():
    """A CLEAN front-side thrust that breaks to a NEW HIGH on the most recent bar: long base,
    then a steady climb to a fresh HOD as the LAST bar. Top-of-range AND (low-noise) far above
    VWAP -> the OLD chasing_top would have mis-vetoed it (the over-veto bug). With the
    OFF-THE-HIGH recalibration the HOD is the last bar -> no lower high -> NOT chasing_top ->
    NOT vetoed. This is the canonical clean breakout that MUST pass with the flag ON."""
    base = np.full(18, 10.0)
    climb = np.array([11.0, 13.0, 16.0, 19.0, 22.0])  # fresh HOD on the LAST bar
    closes = np.concatenate([base, climb])
    vols = np.full(len(closes), 1000.0)
    vols[-1] = 4000.0
    return closes, vols


# --------------------------------------------------------------------------- #
# Direct unit tests of the slice helper.
# --------------------------------------------------------------------------- #
def test_slice_returns_today_only_on_multiday_frame():
    df = _multi_day_frame([_prior_runup_day() for _ in range(4)] + [_fresh_thrust_day()])
    assert isinstance(df.index, pd.DatetimeIndex)
    assert len(set(df.index.date)) == 5            # 5 distinct dates in the full frame
    sliced = _today_session_frame(df)
    assert set(sliced.index.date) == {df.index.date[-1]}   # exactly the last session
    assert len(sliced) == len(_fresh_thrust_day()[0])
    assert sliced["Close"].iloc[-1] == df["Close"].iloc[-1]


def test_slice_passes_through_single_session_frame():
    closes, vols = _fresh_thrust_day()
    df = _ohlc_from_closes(closes, vols, _session_index("2026-06-15", len(closes)))
    assert _today_session_frame(df) is df          # single date -> unchanged object


def test_slice_passes_through_non_datetime_index():
    closes, vols = _fresh_thrust_day()
    df = pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": vols}
    )                                              # default RangeIndex
    assert not isinstance(df.index, pd.DatetimeIndex)
    assert _today_session_frame(df) is df          # non-datetime -> unchanged object


def test_slice_passes_through_len1_frame():
    idx = pd.date_range("2026-06-15 13:30", periods=1, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {"Open": [10.0], "High": [10.1], "Low": [9.9], "Close": [10.0], "Volume": [1.0]},
        index=idx,
    )
    assert _today_session_frame(df) is df


# --------------------------------------------------------------------------- #
# Direct unit tests of front_side_state on the SLICED vs FULL frame (the bug).
# --------------------------------------------------------------------------- #
def test_front_side_state_full_5d_misreads_fresh_thrust_as_backside():
    """The BUG: on the 5-day frame the fresh-thrust today gets a garbage backside read
    because the session open/HOD/VWAP anchors span 5 days. The slice fixes it."""
    df = _multi_day_frame([_prior_runup_day() for _ in range(4)] + [_fresh_thrust_day()])

    full = front_side_state(df)
    sliced = front_side_state(_today_session_frame(df))

    # The FULL 5-day frame mis-reads today as backside (the anchors moved).
    assert full.is_backside is True
    # The today-slice — the correct answer — reads front-side.
    assert sliced.is_backside is False, sliced.reason
    assert sliced.reason == "front_side"


def test_front_side_state_reads_backside_on_faded_today():
    closes, vols = _faded_backside_day()
    df = _ohlc_from_closes(closes, vols, _session_index("2026-06-15", len(closes)))
    fs = front_side_state(df)
    assert fs.is_backside is True
    assert fs.reason in ("below_vwap", "already_faded")


def test_front_side_state_fails_open_on_thin_frame():
    idx = _session_index("2026-06-15", 3)
    df = _ohlc_from_closes([10.0, 10.1, 10.2], [1.0, 1.0, 1.0], idx)
    fs = front_side_state(df)
    assert fs.is_backside is False                 # < 5 bars -> fail open
    assert fs.reason == "insufficient_bars"


# --------------------------------------------------------------------------- #
# Helpers to drive pullback_break_confirmation to the E1 block deterministically.
# --------------------------------------------------------------------------- #
def _patch_trigger_pass(monkeypatch, *, deep_reclaim=False, neutralise_macd_gate=True):
    """Stub the break-trigger so the function reaches the E1 veto block with a clean OK.

    * Patches ``_evaluate_raw_break`` (the non-retest trigger) to return an OK break.
    * Disables ``first_pullback_break`` so it cannot replace the stubbed OK.
    * Optionally neutralises the point-in-time ``_detect_back_side`` MACD/EMA gate (which
      sits just BEFORE E1) so the E1 block is what is exercised. (front_side_state itself
      is NEVER stubbed — it runs on the real frame.)
    The stub seeds pullback_high/low + the ``pattern`` key (deep_reclaim drives the E1
    exemption). Returns the debug dict the stub injects.
    """
    debug_seed = {
        "pullback_high": 100.0,
        "pullback_low": 90.0,
        "pattern": "deep_reclaim" if deep_reclaim else "shallow_flag",
    }

    def _fake_raw_break(high, low, ema9, cur, **kw):
        return True, "ok_first_break", 100.0, 90.0, dict(debug_seed)

    monkeypatch.setattr(eg, "_evaluate_raw_break", _fake_raw_break, raising=True)
    monkeypatch.setattr(
        settings, "chili_momentum_entry_first_pullback_enabled", False, raising=False
    )
    if neutralise_macd_gate:
        monkeypatch.setattr(eg, "_detect_back_side", lambda *a, **k: (False, ""), raising=True)
    return debug_seed


def _entry_frame(today_closes, today_vols, *, multiday=True):
    """A frame whose TODAY session = (today_closes, today_vols); tz-aware UTC.
    When multiday, prepends 4 prior run-up days so the 5d-anchor bug would bite."""
    days = []
    if multiday:
        days += [_prior_runup_day() for _ in range(4)]
    days.append((today_closes, today_vols))
    return _multi_day_frame(days)


# --------------------------------------------------------------------------- #
# (1) PARITY — flag OFF: E1 path never taken, front_side_state never called.
# --------------------------------------------------------------------------- #
def test_parity_flag_off_never_calls_front_side_state(monkeypatch):
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    # Tripwire: front_side_state must NOT be reached when the flag is OFF.
    def _boom(_df):
        raise AssertionError("front_side_state must NOT be called when flag is OFF")

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.ross_momentum.front_side_state", _boom
    )

    closes, vols = _faded_backside_day()           # would VETO if E1 ran
    df = _entry_frame(closes, vols)
    ok, reason, debug = pullback_break_confirmation(df, entry_interval="1m")

    assert reason != "backside_lifecycle_veto"
    assert "front_side_state" not in debug


def test_parity_flag_off_byte_identical_across_runs(monkeypatch):
    """Flag OFF -> the E1 block is skipped -> repeated runs are byte-identical and never
    carry the E1 veto reason."""
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False, raising=False)

    closes, vols = _faded_backside_day()
    df = _entry_frame(closes, vols)
    r1 = pullback_break_confirmation(df, entry_interval="1m")
    r2 = pullback_break_confirmation(df, entry_interval="1m")
    assert r1 == r2
    assert r1[1] != "backside_lifecycle_veto"


# --------------------------------------------------------------------------- #
# (2) CORRECTNESS — today is backside -> E1 vetoes.
# --------------------------------------------------------------------------- #
def test_correctness_backside_today_vetoes(monkeypatch):
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    closes, vols = _faded_backside_day()           # last close faded + below VWAP today
    df = _entry_frame(closes, vols)
    ok, reason, debug = pullback_break_confirmation(df, entry_interval="1m")

    assert ok is False
    assert reason == "backside_lifecycle_veto"
    assert debug.get("front_side_state") in ("below_vwap", "already_faded")


# --------------------------------------------------------------------------- #
# (3) NO-FALSE-VETO — 5d looks faded but TODAY is a fresh front-side thrust.
# --------------------------------------------------------------------------- #
def test_no_false_veto_fresh_thrust_today(monkeypatch):
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    closes, vols = _fresh_thrust_day()             # today: fresh, near own VWAP
    df = _entry_frame(closes, vols)                # prior 4 days ran high (anchors polluted)

    # Sanity: the FULL frame WOULD have mis-vetoed (the bug); the slice reads front-side.
    assert front_side_state(df).is_backside is True
    assert front_side_state(_today_session_frame(df)).is_backside is False

    ok, reason, debug = pullback_break_confirmation(df, entry_interval="1m")
    # The slice means E1 does NOT veto -> the slice fix is proven end-to-end.
    assert reason != "backside_lifecycle_veto"
    assert debug.get("front_side_state") != "below_vwap"


# --------------------------------------------------------------------------- #
# (4) FAIL-OPEN — single-session + non-datetime frames -> no crash, no E1 veto.
# --------------------------------------------------------------------------- #
def test_fail_open_single_session_frame(monkeypatch):
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    closes, vols = _fresh_thrust_day()
    df = _entry_frame(closes, vols, multiday=False)    # ONE session, datetime index
    ok, reason, debug = pullback_break_confirmation(df, entry_interval="1m")
    assert reason != "backside_lifecycle_veto"          # fresh -> front side, no veto, no crash


def test_fail_open_non_datetime_index(monkeypatch):
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    # RangeIndex frame: _today_session_frame returns it unchanged; front_side_state reads
    # the whole thing as one session. Must not crash and must not spuriously veto.
    closes, vols = _fresh_thrust_day()
    df = _ohlc_from_closes(closes, vols, _session_index("2026-06-15", len(closes)))
    df = df.reset_index(drop=True)                       # RangeIndex
    assert not isinstance(df.index, pd.DatetimeIndex)
    ok, reason, debug = pullback_break_confirmation(df, entry_interval="1m")
    assert reason != "backside_lifecycle_veto"


def test_fail_open_non_datetime_index_with_backside_shape_does_not_crash(monkeypatch):
    """A RangeIndex frame whose shape WOULD read backside still must not raise — the slice
    passes it through and front_side_state evaluates it (fail-open contract: no exception)."""
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)
    closes, vols = _faded_backside_day()
    df = _ohlc_from_closes(closes, vols, _session_index("2026-06-15", len(closes)))
    df = df.reset_index(drop=True)
    # Should not raise regardless of the veto outcome.
    pullback_break_confirmation(df, entry_interval="1m")


# --------------------------------------------------------------------------- #
# (5) DIP-BUY EXEMPT — the _deep_reclaim path is not vetoed by E1.
# --------------------------------------------------------------------------- #
def test_dip_buy_deep_reclaim_exempt_from_e1(monkeypatch):
    # Trigger returns pattern == "deep_reclaim" -> _deep_reclaim True -> E1 guard skips.
    _patch_trigger_pass(monkeypatch, deep_reclaim=True)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    # Tripwire: front_side_state must NOT be consulted on the deep-reclaim path.
    def _boom(_df):
        raise AssertionError("E1 must be EXEMPT on the deep_reclaim/dip-buy path")

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.ross_momentum.front_side_state", _boom
    )

    closes, vols = _faded_backside_day()           # would veto if E1 ran
    df = _entry_frame(closes, vols)
    ok, reason, debug = pullback_break_confirmation(df, entry_interval="1m")
    assert reason != "backside_lifecycle_veto"


# --------------------------------------------------------------------------- #
# (6) CHASING-TOP RECALIBRATION — the OFF-THE-HIGH structure discriminator (the flip-ON
# fix). A GENUINE extended-AND-ROLLING blow-off (top-of-range, far above VWAP, made a LOWER
# high after the HOD) is STILL vetoed; a CLEAN front-side thrust to a NEW HIGH (HOD on the
# last bar, equally top-of-range + far above VWAP) is NOT — even though pure extension cannot
# tell them apart. This is what lets the E1 flag ship ON without killing clean breakouts.
# --------------------------------------------------------------------------- #
def test_chasing_top_blowoff_still_vetoed(monkeypatch):
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    closes, vols = _blowoff_chasing_top_day()      # extended AND rolled over -> chasing_top
    df = _entry_frame(closes, vols)                 # 5-day frame; the slice reads TODAY only

    # front_side_state (on the today-slice) reads the genuine blow-off as chasing_top.
    assert front_side_state(_today_session_frame(df)).reason == "chasing_top"

    ok, reason, debug = pullback_break_confirmation(df, entry_interval="1m")
    assert ok is False
    assert reason == "backside_lifecycle_veto"
    assert debug.get("front_side_state") == "chasing_top"


def test_clean_new_high_thrust_not_vetoed_with_flag_on(monkeypatch):
    # The canonical CLEAN breakout: a fresh new high on the most recent bar, top-of-range AND
    # far above VWAP (the exact shape the OLD chasing_top over-vetoed). With the recalibration
    # it is NOT rolled over -> NOT chasing_top -> NOT vetoed, even with the flag ON.
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    closes, vols = _clean_new_high_thrust_day()
    df = _entry_frame(closes, vols)

    fs = front_side_state(_today_session_frame(df))
    assert fs.day_range_pos >= 0.85                          # top of range
    assert fs.vwap_dist_sigma is not None and fs.vwap_dist_sigma >= 2.0   # far above VWAP
    assert fs.reason == "front_side"                         # but a FRESH high -> not chasing_top

    ok, reason, debug = pullback_break_confirmation(df, entry_interval="1m")
    assert reason != "backside_lifecycle_veto"               # the over-veto bug is gone


# --------------------------------------------------------------------------- #
# (7) LIVE-TICK NEW-HIGH carve-out. front_side_state reads COMPLETED bars; a tick-break
# entry can fire with a live_price ABOVE the completed-bar HOD (the live tick IS the fresh
# new high). The rolled-over read is then stale -> the chasing_top veto must be SKIPPED.
# But the carve-out is chasing_top-ONLY and gated on live_price > frame-HOD: a blow-off
# whose live tick does NOT exceed the bar HOD is STILL vetoed (no hole opened), and a
# below-VWAP / faded backside is NEVER reprieved by a live tick.
# --------------------------------------------------------------------------- #
def test_chasing_top_live_tick_new_high_not_vetoed(monkeypatch):
    # The first-pullback tick-break case: the completed bars rolled over off the HOD
    # (chasing_top) but the live_price breaks to a NEW high above the bar HOD -> front-side
    # RIGHT NOW -> the veto is skipped (the test_first_pullback ARM-then-tick regression).
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    closes, vols = _blowoff_chasing_top_day()
    df = _entry_frame(closes, vols)
    assert front_side_state(_today_session_frame(df)).reason == "chasing_top"  # completed-bar read

    frame_hod = float(_today_session_frame(df)["High"].astype(float).max())
    ok, reason, debug = pullback_break_confirmation(
        df, entry_interval="1m", live_price=frame_hod * 1.05,   # decisive NEW high
    )
    assert reason != "backside_lifecycle_veto"                  # carve-out skips the veto
    assert debug.get("front_side_state_live_new_high") == "chasing_top"


def test_chasing_top_live_tick_below_hod_still_vetoed(monkeypatch):
    # Same blow-off, but the live tick does NOT exceed the completed-bar HOD -> still an
    # extended-and-rolling top -> STILL vetoed (the carve-out opens no hole).
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    closes, vols = _blowoff_chasing_top_day()
    df = _entry_frame(closes, vols)
    frame_hod = float(_today_session_frame(df)["High"].astype(float).max())
    ok, reason, debug = pullback_break_confirmation(
        df, entry_interval="1m", live_price=frame_hod * 0.98,   # below the bar HOD
    )
    assert ok is False
    assert reason == "backside_lifecycle_veto"
    assert debug.get("front_side_state") == "chasing_top"


def test_below_vwap_not_reprieved_by_live_new_high(monkeypatch):
    # The carve-out is chasing_top-ONLY: a below-VWAP / faded backside is a HARD veto that a
    # live tick over the bar HOD does NOT undo (being below VWAP is bearish regardless).
    _patch_trigger_pass(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)

    closes, vols = _faded_backside_day()
    df = _entry_frame(closes, vols)
    assert front_side_state(_today_session_frame(df)).reason in ("below_vwap", "already_faded")

    frame_hod = float(_today_session_frame(df)["High"].astype(float).max())
    ok, reason, debug = pullback_break_confirmation(
        df, entry_interval="1m", live_price=frame_hod * 1.10,   # a live new high
    )
    assert ok is False
    assert reason == "backside_lifecycle_veto"                  # still vetoed (not chasing_top)
