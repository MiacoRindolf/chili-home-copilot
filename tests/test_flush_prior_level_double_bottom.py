"""Ross 08-21 (JUNS) — flush detector: prior-level support + double-bottom acceptance.

Ang punch niya: biglang flush sa 8.75 PRIOR RESISTANCE (hindi VWAP) → double
bottom → punch. Ang detector dati ay VWAP/9EMA-only ang support at single-bar
curl lang ang tinatanggap. Runnable: pytest tests/test_flush_prior_level_double_bottom.py -v
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import entry_gates


def test_signature_accepts_known_levels():
    sig = inspect.signature(entry_gates.flush_dip_buy_confirmation)
    assert "known_levels" in sig.parameters
    assert sig.parameters["known_levels"].default is None


def test_prior_level_touch_bypasses_vwap_requirement_source_contract():
    src = inspect.getsource(entry_gates.flush_dip_buy_confirmation)
    prior_at = src.index("_prior_level_touch")
    no_support_at = src.index('"flush_dip_no_support_touch"')
    assert prior_at < no_support_at, \
        "prior-level check must run before the no-support reject"
    # Ang reject ay elif na lang — hindi tatama kapag may prior-level touch.
    guarded = src[src.index("if _prior_level_touch is not None"):no_support_at]
    assert "elif support is not None" in guarded


def test_double_bottom_acceptance_source_contract():
    src = inspect.getsource(entry_gates.flush_dip_buy_confirmation)
    curl_at = src.index("is_bounce_curl_candle(c_o, c_h, c_l, c_c)")
    dbl_at = src.index("_second_touch", curl_at)
    weak_at = src.index('"flush_dip_weak_curl"', curl_at)
    assert curl_at < dbl_at < weak_at, \
        "double-bottom acceptance must be evaluated before the weak-curl reject"
    accepted = src[dbl_at:weak_at]
    assert "double_bottom_touch" in accepted
    # Ang undercut guard (unang depensa ng dip low) ay HINDI ginalaw.
    assert '"flush_dip_undercut"' in src


def test_runner_passes_known_levels():
    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner.tick_live_session)
    call_at = src.index("flush_dip_buy_confirmation(")
    seg = src[call_at:call_at + 1200]
    assert "known_levels=" in seg
    assert "watch_break_level" in seg and "breakout_level_price" in seg
