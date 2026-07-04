"""G4 P1 — GRIND/TREND exit-mode classifier (pure helper, no I/O).

Losers-eat-the-winner fix (CLRO 07-02): a day-leader runner that has printed a
confirmed higher-low above entry and is holding its 5m EMA-9 switches to
structure-trailing instead of the climax-lock ratchet. Every activation input
is fail-CLOSED: missing/uncertain ⇒ inactive ⇒ scalp behavior unchanged."""

from __future__ import annotations

from app.services.trading.momentum_neural.paper_execution import (
    grind_effective_max_adds,
    grind_mode_decision,
)


_ACTIVATE_OK = dict(
    enabled=True,
    prior_active=False,
    is_day_leader=True,
    cadence_cls="FAST",
    entry_price=10.0,
    bid=10.9,          # +0.9 vs entry
    atr_pct=0.05,
    stop_atr_mult=0.60,  # risk_dist = 10*0.30 = 3.0 -> 1R = 10.3 (0.9 < 1R? see below)
    high_water_mark=11.0,  # peak_r = (11-10)/3.0 = 0.33 -> tune per-test
    ema_5m=10.5,
    last_higher_low=10.7,
)


def _activate_kwargs(**overrides):
    kw = dict(_ACTIVATE_OK)
    kw.update(overrides)
    return kw


def test_flag_off_is_inactive() -> None:
    out = grind_mode_decision(**_activate_kwargs(enabled=False))
    assert out["active"] is False
    assert out["reason"] == "flag_off"


def test_activates_when_all_signals_align() -> None:
    # risk_dist = 10 * max(0.003, 0.05*0.60) = 10*0.03 = 0.30 -> peak_r = (12.0-10)/0.30 = 6.67
    out = grind_mode_decision(**_activate_kwargs(high_water_mark=12.0, bid=11.5))
    assert out["active"] is True
    assert out["reason"] == "activated"
    assert out["structure_floor"] is not None
    assert out["peak_r"] >= 1.0


def test_not_day_leader_blocks_activation() -> None:
    out = grind_mode_decision(**_activate_kwargs(is_day_leader=False, high_water_mark=12.0))
    assert out["active"] is False
    assert out["reason"] == "not_day_leader"


def test_uncertain_day_leader_none_blocks_activation() -> None:
    # fail-closed: None (unreadable board) must NOT activate, same as False.
    out = grind_mode_decision(**_activate_kwargs(is_day_leader=None, high_water_mark=12.0))
    assert out["active"] is False
    assert out["reason"] == "not_day_leader"


def test_slow_chopper_cadence_blocks_activation() -> None:
    out = grind_mode_decision(**_activate_kwargs(cadence_cls="SLOW_CHOPPER", high_water_mark=12.0))
    assert out["active"] is False
    assert out["reason"] == "cadence_not_fast"


def test_missing_cadence_blocks_activation() -> None:
    out = grind_mode_decision(**_activate_kwargs(cadence_cls=None, high_water_mark=12.0))
    assert out["active"] is False
    assert out["reason"] == "cadence_not_fast"


def test_uncertain_cadence_activates_when_all_other_signals_align() -> None:
    """Replay finding (CLRO 2.74R / SVRE 1.32R at full tick density): the classifier's
    own default is UNCERTAIN -> treated as FAST/normal with no modulation, and
    MAINTENANCE already accepts UNCERTAIN. ACTIVATION must accept it too — gating on
    exact "FAST" made grind-hold near-totally inert despite every other signal firing."""
    out = grind_mode_decision(**_activate_kwargs(
        cadence_cls="UNCERTAIN", high_water_mark=12.0, bid=11.5,
    ))
    assert out["active"] is True
    assert out["reason"] == "activated"
    assert out["structure_floor"] is not None


def test_below_1r_blocks_activation() -> None:
    out = grind_mode_decision(**_activate_kwargs(high_water_mark=10.2, bid=10.1))
    assert out["active"] is False
    assert out["reason"] == "below_1r"


def test_ema_not_held_blocks_activation() -> None:
    out = grind_mode_decision(**_activate_kwargs(high_water_mark=12.0, bid=9.0, ema_5m=10.5))
    assert out["active"] is False
    assert out["reason"] == "ema_not_held"


def test_missing_ema_blocks_activation() -> None:
    out = grind_mode_decision(**_activate_kwargs(high_water_mark=12.0, ema_5m=None))
    assert out["active"] is False
    assert out["reason"] == "ema_not_held"


def test_no_higher_low_above_entry_blocks_activation() -> None:
    out = grind_mode_decision(**_activate_kwargs(high_water_mark=12.0, last_higher_low=None))
    assert out["active"] is False
    assert out["reason"] == "no_higher_low_above_entry"


def test_higher_low_below_entry_blocks_activation() -> None:
    # a "higher low" that isn't actually above entry is not a real grind signature.
    out = grind_mode_decision(**_activate_kwargs(high_water_mark=12.0, last_higher_low=9.5))
    assert out["active"] is False
    assert out["reason"] == "no_higher_low_above_entry"


def test_bad_inputs_fail_closed() -> None:
    out = grind_mode_decision(**_activate_kwargs(entry_price=float("nan"), high_water_mark=12.0))
    assert out["active"] is False
    assert out["reason"] == "bad_inputs"


def test_maintenance_holds_through_hysteresis() -> None:
    # once active, cadence dropping to UNCERTAIN (not SLOW_CHOPPER) with structure
    # intact keeps it active — board flicker alone cannot drop a working grind.
    out = grind_mode_decision(**_activate_kwargs(
        prior_active=True, cadence_cls="UNCERTAIN", bid=10.8, high_water_mark=12.0,
    ))
    assert out["active"] is True
    assert out["reason"] == "maintained"


def test_maintenance_drops_on_slow_chopper() -> None:
    out = grind_mode_decision(**_activate_kwargs(
        prior_active=True, cadence_cls="SLOW_CHOPPER", high_water_mark=12.0,
    ))
    assert out["active"] is False
    assert out["reason"] == "cadence_dropped"


def test_maintenance_drops_on_structure_break() -> None:
    out = grind_mode_decision(**_activate_kwargs(
        prior_active=True, bid=9.0, high_water_mark=12.0,
    ))
    assert out["active"] is False
    assert out["reason"] == "structure_broken"


def test_maintenance_drops_when_anchors_missing() -> None:
    out = grind_mode_decision(**_activate_kwargs(
        prior_active=True, ema_5m=None, last_higher_low=None, high_water_mark=12.0,
    ))
    assert out["active"] is False
    assert out["reason"] == "structure_anchors_missing"


def test_structure_floor_never_below_placed_stop_is_caller_responsibility() -> None:
    # the helper returns a raw structure_floor; INVARIANT-A (never loosen the placed
    # stop) is enforced by callers composing max(current_stop, structure_floor). Sanity
    # check the floor is a real number below the anchors (wick buffer subtracted).
    out = grind_mode_decision(**_activate_kwargs(high_water_mark=12.0, bid=11.5))
    assert out["structure_floor"] < max(_ACTIVATE_OK["ema_5m"], _ACTIVATE_OK["last_higher_low"])


# ── grind_effective_max_adds ──────────────────────────────────────────────


def test_max_adds_outside_grind_is_unchanged() -> None:
    assert grind_effective_max_adds(
        base_max_adds=2, grind_active=False, cushion_r=5.0, min_cushion_r=1.0,
    ) == 2


def test_max_adds_missing_cushion_basis_falls_back_to_base() -> None:
    assert grind_effective_max_adds(
        base_max_adds=2, grind_active=True, cushion_r=None, min_cushion_r=1.0,
    ) == 2


def test_max_adds_scales_with_banked_cushion() -> None:
    # 3.4R banked / 1R per add -> 3 adds, floored at base (2) so it only ever raises.
    assert grind_effective_max_adds(
        base_max_adds=2, grind_active=True, cushion_r=3.4, min_cushion_r=1.0,
    ) == 3


def test_max_adds_never_drops_below_base() -> None:
    # sub-threshold cushion (0.5R) never REDUCES the base cap.
    assert grind_effective_max_adds(
        base_max_adds=2, grind_active=True, cushion_r=0.5, min_cushion_r=1.0,
    ) == 2


def test_max_adds_bad_basis_fails_to_base() -> None:
    assert grind_effective_max_adds(
        base_max_adds=2, grind_active=True, cushion_r=float("nan"), min_cushion_r=1.0,
    ) == 2


def test_max_adds_bad_base_type_returns_zero() -> None:
    assert grind_effective_max_adds(
        base_max_adds="oops", grind_active=True, cushion_r=3.0, min_cushion_r=1.0,
    ) == 0


# ── review M2: no active=True with the floor already broken; explicit grind death ──


def test_activation_blocked_when_floor_already_broken() -> None:
    """The M2 defect: hl > ema puts the floor ABOVE a bid that still holds the EMA —
    activation must refuse rather than report an already-broken structure floor."""
    out = grind_mode_decision(**_activate_kwargs(
        high_water_mark=12.0, bid=10.6, ema_5m=10.5, last_higher_low=11.5,
    ))
    # floor = 11.5 - 10*max(0.001, 0.05*0.25) = 11.375 > bid 10.6
    assert out["active"] is False
    assert out["reason"] == "structure_floor_not_held"


def test_active_true_implies_floor_held() -> None:
    """M2 property: whenever the decision reports active, the reported floor is at/below
    the evaluated bid (never an already-broken level)."""
    for kw in (
        _activate_kwargs(high_water_mark=12.0, bid=11.5),
        _activate_kwargs(prior_active=True, high_water_mark=12.0, bid=10.8),
    ):
        out = grind_mode_decision(**kw)
        if out["active"]:
            assert float(kw["bid"]) >= float(out["structure_floor"])


def test_activation_blocked_on_vwap_not_held() -> None:
    out = grind_mode_decision(**_activate_kwargs(
        high_water_mark=12.0, bid=11.5, vwap=11.8,
    ))
    assert out["active"] is False
    assert out["reason"] == "vwap_not_held"


def test_activation_skips_unreadable_vwap() -> None:
    out = grind_mode_decision(**_activate_kwargs(high_water_mark=12.0, bid=11.5, vwap=None))
    assert out["active"] is True
    out2 = grind_mode_decision(**_activate_kwargs(
        high_water_mark=12.0, bid=11.5, vwap=float("nan"),
    ))
    assert out2["active"] is True


def test_maintenance_drops_on_lower_low_below_entry() -> None:
    """Explicit grind death: a READABLE swing-low anchor degrading to/below entry
    (the lower-low signature) deactivates even while the EMA floor still holds."""
    out = grind_mode_decision(**_activate_kwargs(
        prior_active=True, high_water_mark=12.0, bid=10.8, last_higher_low=9.8,
    ))
    assert out["active"] is False
    assert out["reason"] == "lower_low_below_entry"


def test_maintenance_drops_on_vwap_loss() -> None:
    out = grind_mode_decision(**_activate_kwargs(
        prior_active=True, high_water_mark=12.0, bid=10.8, vwap=11.0,
    ))
    assert out["active"] is False
    assert out["reason"] == "vwap_lost"


def test_maintenance_unreadable_hl_keeps_ema_hysteresis() -> None:
    """Hysteresis intact: an UNREADABLE (None) swing-low this tick is flicker, not a
    lower-low — the EMA-anchored floor keeps a working grind alive."""
    out = grind_mode_decision(**_activate_kwargs(
        prior_active=True, high_water_mark=12.0, bid=10.8, last_higher_low=None,
    ))
    assert out["active"] is True
    assert out["reason"] == "maintained"
