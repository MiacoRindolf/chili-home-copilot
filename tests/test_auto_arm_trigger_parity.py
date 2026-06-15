"""Dual-path parity for the momentum auto-arm SELECTION probe (2026-06-15).

The auto-arm's ``_entry_trigger_fires`` selection probe used to call
``pullback_break_confirmation`` with LIBRARY DEFAULTS (require_retest=False ->
``_evaluate_raw_break``), which can NEVER reach the deep_reclaim / dip-buy entry
(those are reachable only through the require_retest=True ``_evaluate_break_retest``
path). So a deep-retrace reclaim name (MTEN / KAIO-USD / EDHL) that the LIVE runner
would ENTER was INVISIBLE to selection and never armed — the conversion gap — while
raw breaks the live runner then DECLINED were armed (wasted churn).

The fix makes the probe call the SAME settings-resolved trigger the live + paper
runners use (``momentum_pullback_trigger``, symbol-aware), so the selection probe
makes the IDENTICAL bar-level entry decision as the live runner. These tests assert
(a) PARITY — the probe agrees with the live runner's bar-level fire/reason on the
same frame, (b) the deep_reclaim path is now REACHABLE from the probe, and (c) the
kill-switch reverts to the legacy library-defaults behaviour.
"""
from __future__ import annotations

import pandas as pd

import app.services.trading.momentum_neural.auto_arm as aa
from app.config import settings
from app.services.trading import market_data
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger


def _frame(bars: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """bars = [(open, high, low, close, volume)] on a 1m index."""
    idx = pd.date_range("2026-06-11 12:00", periods=len(bars), freq="1min")
    return pd.DataFrame(
        {
            "Open": [b[0] for b in bars],
            "High": [b[1] for b in bars],
            "Low": [b[2] for b in bars],
            "Close": [b[3] for b in bars],
            "Volume": [b[4] for b in bars],
        },
        index=idx,
    )


def _deep_v_bars(
    *, dip_low: float = 13.5, recovery_closes: tuple = (14.0, 14.3, 14.5),
    last_bar: tuple = (14.6, 15.4, 14.55, 15.3, 500_000.0),
) -> list[tuple[float, float, float, float, float]]:
    """A deep-retrace-then-reclaim frame: 20-bar ramp 10->15, 3-bar deep fade to
    ``dip_low`` (too deep for the flag checks), reclaim bars holding the decayed
    EMA-9, then a break of the recovery swing high. Mirrors the proven builder in
    ``test_deep_reclaim_entry`` so the probe is exercised on a frame that returns
    ``deep_reclaim_ok`` through the shared trigger."""
    bars = []
    px = 10.0
    for _ in range(20):
        o = px
        c = px + 0.25
        bars.append((o, c + 0.15, o - 0.15, c, 100_000.0))
        px = c
    bars.append((15.0, 15.05, 14.2, 14.0, 120_000.0))
    bars.append((14.0, 14.1, dip_low + 0.1, dip_low + 0.2, 120_000.0))
    bars.append((dip_low + 0.2, dip_low + 0.4, dip_low, dip_low + 0.1, 120_000.0))
    rec_h = (14.2, 14.5, 14.8)
    for h, c in zip(rec_h, recovery_closes):
        bars.append((c - 0.2, h, c - 0.3, c, 150_000.0))
    bars.append(last_bar)
    return bars


def _shallow_flag_break_bars() -> list[tuple[float, float, float, float, float]]:
    """A clean shallow-flag pullback-break: ramp, a 2-bar shallow flag holding above
    the EMA-9, then the current bar breaks the flag high with a volume spike. Fires
    ``pullback_break_ok`` through both the raw-break (legacy) and retest paths' shared
    success label, so it is a control case that should agree regardless of the knob."""
    bars = []
    px = 10.0
    for _ in range(20):
        o = px
        c = px + 0.20
        bars.append((o, c + 0.05, o - 0.05, c, 100_000.0))
        px = c
    top = px  # ~14.0
    # shallow 2-bar flag (holds well above EMA-9), then a strong break-out bar
    bars.append((top, top + 0.02, top - 0.10, top - 0.05, 90_000.0))
    bars.append((top - 0.05, top + 0.03, top - 0.12, top - 0.04, 90_000.0))
    bars.append((top - 0.02, top + 0.40, top - 0.02, top + 0.35, 600_000.0))
    return bars


def _entry_test_settings(monkeypatch) -> None:
    """Pin the lane to the pullback_break branch on a 1m interval and hold off the
    ATR-scaled verticality skip (covered elsewhere) so the parity assertions isolate
    the trigger-routing change, not an unrelated gate."""
    monkeypatch.setattr(settings, "chili_momentum_entry_trigger_mode", "pullback_break", raising=False)
    monkeypatch.setattr(settings, "chili_momentum_pullback_entry_interval", "1m", raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0, raising=False)


def _patch_ohlcv(monkeypatch, df: pd.DataFrame) -> None:
    """``_entry_trigger_fires`` does ``from ..market_data import fetch_ohlcv_df``
    at call time, so patch the source module attribute."""
    monkeypatch.setattr(market_data, "fetch_ohlcv_df", lambda *a, **k: df)


# ── PARITY: the probe agrees with the live runner's bar-level trigger ──────────

def test_probe_matches_live_trigger_bar_level_deep_reclaim(monkeypatch):
    """On a deep-retrace reclaim frame the probe returns the SAME (fires, reason) the
    live runner's bar-level ``momentum_pullback_trigger`` returns — and that reason is
    a deep_reclaim outcome the LEGACY raw-break probe could never produce."""
    _entry_test_settings(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True, raising=False)
    df = _frame(_deep_v_bars())
    _patch_ohlcv(monkeypatch, df)

    live_ok, live_reason, _ = momentum_pullback_trigger(df, entry_interval="1m", symbol="MTEN")
    probe_ok, probe_reason = aa._entry_trigger_fires("MTEN")
    assert (probe_ok, probe_reason) == (live_ok, live_reason), (
        (probe_ok, probe_reason), (live_ok, live_reason),
    )
    # And this is the deep_reclaim class the conversion gap was missing.
    assert live_reason.startswith("deep_reclaim"), live_reason
    assert live_ok is True


def test_probe_matches_live_trigger_bar_level_shallow_flag(monkeypatch):
    """Control: a clean shallow-flag break fires the same in the probe and the live
    runner (the fix doesn't regress the breaks the lane already armed)."""
    _entry_test_settings(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True, raising=False)
    df = _frame(_shallow_flag_break_bars())
    _patch_ohlcv(monkeypatch, df)

    live_ok, live_reason, _ = momentum_pullback_trigger(df, entry_interval="1m", symbol="ABCD")
    probe_ok, probe_reason = aa._entry_trigger_fires("ABCD")
    assert (probe_ok, probe_reason) == (live_ok, live_reason)


# ── REACHABILITY: deep_reclaim now fires via the probe (it could not before) ──

def test_deep_reclaim_now_fires_via_probe(monkeypatch):
    """A frame that fires ``deep_reclaim_ok`` through the shared trigger now ALSO fires
    via ``_entry_trigger_fires`` — the keystone of the conversion fix."""
    _entry_test_settings(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True, raising=False)
    df = _frame(_deep_v_bars())
    _patch_ohlcv(monkeypatch, df)

    fires, reason = aa._entry_trigger_fires("MTEN")
    assert fires is True and reason.startswith("deep_reclaim"), (fires, reason)


def test_legacy_probe_cannot_reach_deep_reclaim(monkeypatch):
    """KILL-SWITCH OFF reverts to the legacy library-defaults probe (require_retest=
    False -> raw break), which on the SAME deep-retrace frame returns a dead-end
    rejection — never the deep_reclaim entry. This is the bug the fix removes."""
    _entry_test_settings(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", False, raising=False)
    df = _frame(_deep_v_bars())
    _patch_ohlcv(monkeypatch, df)

    fires, reason = aa._entry_trigger_fires("MTEN")
    assert fires is False
    assert "deep_reclaim" not in reason
    assert reason in ("pullback_too_deep", "pullback_below_ema9"), reason


def test_kill_switch_off_diverges_from_live_runner(monkeypatch):
    """The legacy probe DISAGREES with the live runner on the deep-retrace frame
    (the parity violation); the fix ON makes them agree (asserted above) — together
    these bound the kill-switch behaviour on both sides."""
    _entry_test_settings(monkeypatch)
    df = _frame(_deep_v_bars())
    _patch_ohlcv(monkeypatch, df)
    live_ok, live_reason, _ = momentum_pullback_trigger(df, entry_interval="1m", symbol="MTEN")

    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", False, raising=False)
    legacy_ok, legacy_reason = aa._entry_trigger_fires("MTEN")
    assert (legacy_ok, legacy_reason) != (live_ok, live_reason)


# ── Fallbacks preserved ────────────────────────────────────────────────────────

def test_volume_fallback_preserved_in_hybrid_mode(monkeypatch):
    """In hybrid mode, a non-firing pullback-break must still fall through to the 15m
    ``momentum_volume_confirmation`` fallback (the fix only changes the pullback-break
    branch, never the fallback ladder)."""
    monkeypatch.setattr(settings, "chili_momentum_entry_trigger_mode", "hybrid", raising=False)
    monkeypatch.setattr(settings, "chili_momentum_pullback_entry_interval", "1m", raising=False)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0, raising=False)
    # A non-deep, non-firing frame (flat) -> pullback-break does not fire.
    df_flat = _frame([(100.0, 100.1, 99.9, 100.0, 1000.0) for _ in range(40)])
    _patch_ohlcv(monkeypatch, df_flat)

    called = {"volume": 0}

    def _fake_volume(_df):
        called["volume"] += 1
        return True, "momentum_volume_ok"

    import app.services.trading.momentum_neural.entry_gates as eg
    monkeypatch.setattr(eg, "momentum_volume_confirmation", _fake_volume)
    fires, reason = aa._entry_trigger_fires("ABCD")
    assert called["volume"] == 1
    assert fires is True and reason == "momentum_volume_ok"


def test_pullback_break_mode_does_not_fall_through_to_volume(monkeypatch):
    """In pullback_break mode a non-firing trigger returns the trigger's own wait
    reason — it must NOT fall through to the volume gate (mode contract preserved)."""
    _entry_test_settings(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True, raising=False)
    df_flat = _frame([(100.0, 100.1, 99.9, 100.0, 1000.0) for _ in range(40)])
    _patch_ohlcv(monkeypatch, df_flat)

    import app.services.trading.momentum_neural.entry_gates as eg
    monkeypatch.setattr(
        eg, "momentum_volume_confirmation",
        lambda _df: (_ for _ in ()).throw(AssertionError("volume fallback must not run")),
    )
    fires, reason = aa._entry_trigger_fires("ABCD")
    assert fires is False
    live_ok, live_reason, _ = momentum_pullback_trigger(df_flat, entry_interval="1m", symbol="ABCD")
    assert (fires, reason) == (live_ok, live_reason)
