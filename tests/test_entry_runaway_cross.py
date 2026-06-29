"""FIX B — aggressive repeg/cross on fast pushes (2026-06-29): unit tests for the
ask-advance fast-push detector ``_ask_advanced_past_limit``.

The detector is the EARLY signal the existing bid-based ``entry_limit_left_behind``
misses: on a fast vertical the ASK climbs through our resting limit first (we stop
being marketable) while the bid lags below the chase ceiling. FIX B then escalates
to a marketable cross via the SAME repeg machinery. These tests pin the adaptive
band (one documented base, vol-widened by chase_move_ratio, hard-capped at the live
spread cap) and the fail-closed contract, plus that the cross price itself is still
bounded by ``_entry_repeg_price`` (the R:R-protecting cumulative ceiling).
"""
from __future__ import annotations

import importlib

from app.services.trading.momentum_neural import live_runner as lr
from app.config import settings


def _band_bps(emb):
    base = float(settings.chili_momentum_runaway_cross_ask_band_bps)
    ratio = float(settings.chili_momentum_entry_chase_move_ratio)
    band = max(base, (emb or 0.0) * ratio)
    return min(band, lr._adaptive_live_max_spread_bps(emb))


def test_ask_just_above_limit_within_band_does_not_trigger():
    # A sub-band pip at the offer must NOT churn a cancel+replace (debounce).
    limit = 10.0
    band = _band_bps(None)
    ask = limit * (1.0 + (band * 0.5) / 10_000.0)  # half the band
    assert lr._ask_advanced_past_limit(ask=ask, limit_px=limit, expected_move_bps=None) is False


def test_ask_past_band_triggers():
    limit = 10.0
    band = _band_bps(None)
    ask = limit * (1.0 + (band + 5.0) / 10_000.0)  # comfortably past the band
    assert lr._ask_advanced_past_limit(ask=ask, limit_px=limit, expected_move_bps=None) is True


def test_band_widens_with_expected_move():
    # An explosive name (large expected move) gets a WIDER band, so the same ask
    # advance that would trigger on a quiet name is tolerated here.
    limit = 10.0
    quiet_band = _band_bps(None)
    explosive_emb = 5000.0
    explosive_band = _band_bps(explosive_emb)
    assert explosive_band > quiet_band
    # An ask just past the quiet band but inside the explosive band:
    ask = limit * (1.0 + (quiet_band + 1.0) / 10_000.0)
    assert lr._ask_advanced_past_limit(ask=ask, limit_px=limit, expected_move_bps=None) is True
    assert lr._ask_advanced_past_limit(ask=ask, limit_px=limit, expected_move_bps=explosive_emb) is False


def test_band_hard_capped_at_live_spread_cap():
    # A huge expected move cannot widen the band past the adaptive live spread cap.
    limit = 10.0
    huge_emb = 9_999_999.0
    cap_bps = lr._adaptive_live_max_spread_bps(huge_emb)
    band = _band_bps(huge_emb)
    assert band <= cap_bps + 1e-9
    # Just inside the cap-derived band -> no trigger; just past -> trigger.
    ask_in = limit * (1.0 + (band - 1.0) / 10_000.0)
    ask_out = limit * (1.0 + (band + 1.0) / 10_000.0)
    assert lr._ask_advanced_past_limit(ask=ask_in, limit_px=limit, expected_move_bps=huge_emb) is False
    assert lr._ask_advanced_past_limit(ask=ask_out, limit_px=limit, expected_move_bps=huge_emb) is True


def test_invalid_inputs_fail_closed():
    assert lr._ask_advanced_past_limit(ask=None, limit_px=10.0, expected_move_bps=None) is False
    assert lr._ask_advanced_past_limit(ask=10.0, limit_px=0.0, expected_move_bps=None) is False
    assert lr._ask_advanced_past_limit(ask=-1.0, limit_px=10.0, expected_move_bps=None) is False
    assert lr._ask_advanced_past_limit(ask=0.0, limit_px=10.0, expected_move_bps=None) is False


def test_ask_below_limit_never_triggers():
    # Ask at or below our limit means we are still marketable / at the front — no chase.
    assert lr._ask_advanced_past_limit(ask=9.99, limit_px=10.0, expected_move_bps=None) is False
    assert lr._ask_advanced_past_limit(ask=10.0, limit_px=10.0, expected_move_bps=None) is False


def test_triggered_cross_is_still_bounded_by_cumulative_ceiling():
    # FIX B promotes; the actual cross PRICE is then bounded by _entry_repeg_price,
    # so even an aggressive ask-advance can't erode R:R past one spread budget.
    orig = 10.0
    emb = 500.0
    ceiling = orig * (1.0 + lr._adaptive_live_max_spread_bps(emb) / 10_000.0)
    # Ask advanced well past the band but still inside the cumulative ceiling.
    ask = ceiling - 0.001
    assert lr._ask_advanced_past_limit(ask=ask, limit_px=orig, expected_move_bps=emb) is True
    px = lr._entry_repeg_price(original_limit_px=orig, live_ask=ask, expected_move_bps=emb)
    assert px is not None and px <= ceiling + 1e-9


def test_config_flag_defaults_on_with_documented_base():
    # Operator style: default-ON with one documented base band. Parity off = flag False.
    assert settings.chili_momentum_runaway_cross_enabled is True
    assert float(settings.chili_momentum_runaway_cross_ask_band_bps) == 8.0
