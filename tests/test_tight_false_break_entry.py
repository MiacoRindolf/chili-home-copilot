"""GAP-B — TIGHT false-breakout-reversal / VWAP-reclaim entry test suite (pure, no DB).

Covers: TIGHT (compression < theta_c percentile of the name's own history), flow_ok
(fail-closed on stale tape), vol_ok required; false-breakout-reversal geometry detection;
VWAP-reclaim geometry detection; all 4 chase-guards fire (guard parity); mutually-exclusive
setup per tick; and the flag-off byte-identical (disabled) contract.
"""

from app.services.trading.momentum_neural.tight_false_break_entry import (
    ChaseGuards,
    TightEntryDecision,
    compression_percentile,
    detect_false_break_reversal,
    detect_vwap_reclaim,
    evaluate_chase_guards,
    evaluate_tight_false_break_entry,
    is_tight,
)


# ── TIGHT: compression below the theta_c percentile of the name's own history ─

def test_compression_percentile_self_relative():
    hist = [1.0, 2.0, 3.0, 4.0, 5.0]
    # A current compression of 1.0 sits at the very bottom (tightest).
    assert compression_percentile(0.5, hist) < 0.2
    # A current compression of 5.0 sits near the top (loosest).
    assert compression_percentile(6.0, hist) > 0.8


def test_compression_percentile_none_on_thin_history():
    assert compression_percentile(1.0, [1.0]) is None
    assert compression_percentile(1.0, []) is None


def test_is_tight_below_theta_c():
    hist = [1.0, 2.0, 3.0, 4.0, 5.0]
    # Compression below the 0.30 percentile of its own base => TIGHT.
    assert is_tight(0.5, hist, theta_c=0.30) is True
    # A loose (high) compression is NOT tight.
    assert is_tight(4.5, hist, theta_c=0.30) is False


def test_is_tight_fail_closed_on_thin_history():
    """Insufficient history => NOT tight (won't call a base tight without evidence)."""
    assert is_tight(0.1, [1.0], theta_c=0.9) is False


# ── false-breakout-reversal geometry ──────────────────────────────────────────

def test_false_break_reversal_detected():
    # Pierced below the trap level (low 98) then closed back above it (close 101).
    assert detect_false_break_reversal(trap_level=100.0, bar_low=98.0, bar_close=101.0) is True


def test_false_break_requires_a_real_pierce():
    # Never pierced below the trap => not a trap reversal, just a hold above.
    assert detect_false_break_reversal(trap_level=100.0, bar_low=100.5, bar_close=101.0) is False


def test_false_break_requires_close_back_above():
    # Pierced and stayed below (closed under the trap) => failed reclaim, not a reversal.
    assert detect_false_break_reversal(trap_level=100.0, bar_low=98.0, bar_close=99.0) is False


# ── VWAP-reclaim geometry ─────────────────────────────────────────────────────

def test_vwap_reclaim_detected():
    # Prior close lost VWAP (99 < 100), current bar reclaimed it (close 100.5).
    assert detect_vwap_reclaim(vwap=100.0, prev_close=99.0, bar_close=100.5) is True


def test_vwap_reclaim_requires_prior_loss():
    # Never lost VWAP => not a reclaim.
    assert detect_vwap_reclaim(vwap=100.0, prev_close=100.5, bar_close=101.0) is False


def test_vwap_reclaim_none_when_no_vwap():
    assert detect_vwap_reclaim(vwap=None, prev_close=99.0, bar_close=101.0) is False


# ── Chase-guard parity: all four guards always evaluated ──────────────────────

def _guard_kwargs(**over):
    kw = dict(
        tape_present=True,
        tape_supportive=True,
        price=101.0,
        reclaim_level=100.0,
        extension_atr_mult=2.0,
        atr=1.0,
        vwap=100.0,
        structural_stop=98.0,
    )
    kw.update(over)
    return kw


def test_all_four_guards_present_and_pass():
    g = evaluate_chase_guards(**_guard_kwargs())
    assert isinstance(g, ChaseGuards)
    # Parity: every guard is reported.
    assert set(g.reasons.keys()) == {"tape", "extension", "backside", "structural_stop"}
    assert g.all_pass() is True
    assert g.first_failure() is None


def test_tape_guard_fail_closed_when_missing():
    g = evaluate_chase_guards(**_guard_kwargs(tape_present=False))
    assert g.tape_ok is False
    assert g.first_failure() == "tape"


def test_tape_guard_fails_when_unsupportive():
    g = evaluate_chase_guards(**_guard_kwargs(tape_present=True, tape_supportive=False))
    assert g.tape_ok is False


def test_extension_guard_blocks_overextended_chase():
    # price 105 is > reclaim 100 + 2*ATR(1) = 102 => over-extended.
    g = evaluate_chase_guards(**_guard_kwargs(price=105.0))
    assert g.extension_ok is False
    assert g.first_failure() == "extension"


def test_extension_guard_fail_open_without_atr():
    g = evaluate_chase_guards(**_guard_kwargs(price=200.0, atr=None))
    assert g.extension_ok is True
    assert g.reasons["extension"] == "no_atr_fail_open"


def test_backside_guard_blocks_below_vwap():
    g = evaluate_chase_guards(**_guard_kwargs(price=99.0, vwap=100.0, structural_stop=97.0))
    assert g.backside_ok is False


def test_backside_guard_fail_open_without_vwap():
    g = evaluate_chase_guards(**_guard_kwargs(vwap=None))
    assert g.backside_ok is True


def test_structural_stop_guard_requires_finite_stop_below_price():
    assert evaluate_chase_guards(**_guard_kwargs(structural_stop=None)).structural_stop_ok is False
    assert evaluate_chase_guards(**_guard_kwargs(structural_stop=0.0)).structural_stop_ok is False
    # Stop above price is not a real protective stop.
    assert evaluate_chase_guards(**_guard_kwargs(price=100.0, structural_stop=101.0)).structural_stop_ok is False


# ── Full gate: eligibility + geometry + parity ────────────────────────────────

def _entry_kwargs(**over):
    kw = dict(
        enabled=True,
        compression_now=0.5,
        compression_history=[1.0, 2.0, 3.0, 4.0, 5.0],
        theta_c=0.30,
        tape_present=True,
        tape_supportive=True,
        vol_ratio=2.0,
        vol_spike_floor=1.5,
        price=101.0,
        vwap=100.0,
        prev_close=99.0,
        bar_low=98.0,
        bar_close=101.0,
        trap_level=100.0,
        atr=2.0,
        extension_atr_mult=3.0,
        structural_stop=98.0,
        reclaim_tol=0.0,
    )
    kw.update(over)
    return kw


def test_full_gate_fires_false_break_reversal():
    d = evaluate_tight_false_break_entry(**_entry_kwargs())
    assert d.ok is True
    assert d.setup == "false_break_reversal"
    assert d.guards is not None and d.guards.all_pass()


def test_full_gate_fires_vwap_reclaim_when_not_false_break():
    # No pierce of the trap (bar_low above it) => false-break excluded; vwap-reclaim fires.
    d = evaluate_tight_false_break_entry(
        **_entry_kwargs(bar_low=100.5, trap_level=100.0, prev_close=99.0, bar_close=100.6, vwap=100.0)
    )
    assert d.ok is True
    assert d.setup == "vwap_reclaim"


def test_setups_mutually_exclusive_false_break_takes_precedence():
    """When BOTH geometries hold on the same tick, exactly one setup is chosen and
    false-break takes precedence (a trap reversal IS the higher-conviction read)."""
    d = evaluate_tight_false_break_entry(
        **_entry_kwargs(bar_low=98.0, bar_close=101.0, prev_close=99.0, vwap=100.0, trap_level=100.0)
    )
    # Both detectors would be True here; the gate reports only one setup.
    assert detect_false_break_reversal(trap_level=100.0, bar_low=98.0, bar_close=101.0) is True
    assert detect_vwap_reclaim(vwap=100.0, prev_close=99.0, bar_close=101.0) is True
    assert d.setup == "false_break_reversal"


def test_gate_blocks_when_not_tight():
    d = evaluate_tight_false_break_entry(**_entry_kwargs(compression_now=4.9))
    assert d.ok is False
    assert d.reason == "not_tight"


def test_gate_flow_fail_closed_on_stale_tape():
    d = evaluate_tight_false_break_entry(**_entry_kwargs(tape_present=False))
    assert d.ok is False
    assert d.reason == "flow_stale_fail_closed"


def test_gate_blocks_unsupportive_flow():
    d = evaluate_tight_false_break_entry(**_entry_kwargs(tape_present=True, tape_supportive=False))
    assert d.ok is False
    assert d.reason == "flow_not_ok"


def test_gate_blocks_below_vol_floor():
    d = evaluate_tight_false_break_entry(**_entry_kwargs(vol_ratio=0.5, vol_spike_floor=1.5))
    assert d.ok is False
    assert d.reason == "vol_below_floor"


def test_gate_blocks_when_no_geometry():
    # Tight + flow + vol OK, but neither geometry holds (no pierce, never lost vwap).
    d = evaluate_tight_false_break_entry(
        **_entry_kwargs(bar_low=100.5, trap_level=100.0, prev_close=100.5, bar_close=101.0, vwap=100.0)
    )
    assert d.ok is False
    assert d.reason == "no_reversal_geometry"


def test_gate_blocks_on_guard_failure_extension():
    # Valid setup but over-extended (price far above reclaim level, tight ATR ceiling).
    d = evaluate_tight_false_break_entry(**_entry_kwargs(price=120.0, atr=1.0, extension_atr_mult=2.0))
    assert d.ok is False
    assert d.reason == "guard_block_extension"
    assert d.guards is not None and d.guards.extension_ok is False


def test_gate_blocks_on_guard_failure_structural_stop():
    d = evaluate_tight_false_break_entry(**_entry_kwargs(structural_stop=None))
    assert d.ok is False
    assert d.reason == "guard_block_structural_stop"


def test_guard_parity_always_reports_all_four_even_on_failure():
    d = evaluate_tight_false_break_entry(**_entry_kwargs(price=120.0, atr=1.0, extension_atr_mult=2.0))
    assert d.guards is not None
    assert set(d.guards.reasons.keys()) == {"tape", "extension", "backside", "structural_stop"}


# ── Flag-off byte-identical (disabled) contract ───────────────────────────────

def test_flag_off_returns_disabled():
    d = evaluate_tight_false_break_entry(**_entry_kwargs(enabled=False))
    assert isinstance(d, TightEntryDecision)
    assert d.ok is False
    assert d.reason == "disabled"
    assert d.setup is None
    assert d.guards is None
