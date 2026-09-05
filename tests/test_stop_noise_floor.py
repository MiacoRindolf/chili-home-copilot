"""Stop-vs-noise floor: ang stop ay hindi nakaupo sa loob ng sariling ingay (#1278).

NASUKAT 2026-09-01 (forensics ng 4 talo): ang AUUD ay pumasok na may stop na
1.19 sentimo (1.07%) sa pangalang ang MEDIAN 30s high-low range ay 4.0 sentimo
(3.6%) — ang stop ay 0.30x ng sariling ingay; 11/11 bucket ang lampas. Ang
risk-first sizing ang nagpalala: $6.54 / $0.0119 = 551 shares; ang -6.54 na
binalak ay naging -44.01 (-6.73R). Sa 4c na floor: ~163 shares, ~-11.

MAY vol floor NA (effective_stop_atr_pct) pero mula sa 15m expected-move frame
— BULAG sa pangalang 5 minuto pa lang ang kilalang tape (AUUD: pre-11:05 ay
wala sa tape). Ang floor na ito ay mula sa SARILING 30s tick buckets.

HINDI ITO REJECT-GATE: pinalalapad ang stop, liliit ang qty sa risk-first math
na umiiral na. Walang entry na hinaharangan.

Runnable: pytest tests/test_stop_noise_floor.py -v
"""
from __future__ import annotations

from app.config import Settings
from app.services.trading.momentum_neural.risk_policy import (
    compute_risk_first_quantity,
    stop_noise_floor_decision,
)

# Ang AUUD sa mga numero: entry 1.11, stop fraction 1.07% (atr*mult), median
# 30s range 4.0c = 3.6036% ng entry.
AUUD_ENTRY = 1.11
AUUD_ATR = 0.0179          # x 0.60 mult = 1.074% stop fraction
MULT = 0.60
AUUD_NOISE = 0.040 / 1.11  # 3.6036%


def test_the_auud_case_floors_and_shrinks():
    """ANG PANGUNAHIN: lumalapad ang stop, lumiliit ang qty, pareho ang $risk."""
    a_out, meta = stop_noise_floor_decision(
        atr_pct=AUUD_ATR, stop_atr_mult=MULT,
        noise_range_pct=AUUD_NOISE, buckets_used=10, min_buckets=6,
    )
    assert meta["applied"] is True
    assert abs(meta["stop_pct_after"] - AUUD_NOISE) < 1e-6, "floor MISMO ang bagong stop"

    q_before, m_before = compute_risk_first_quantity(
        entry_price=AUUD_ENTRY, atr_pct=AUUD_ATR, max_loss_usd=6.54,
        max_notional_ceiling_usd=10_000.0, stop_atr_mult=MULT,
    )
    q_after, m_after = compute_risk_first_quantity(
        entry_price=AUUD_ENTRY, atr_pct=a_out, max_loss_usd=6.54,
        max_notional_ceiling_usd=10_000.0, stop_atr_mult=MULT,
    )
    assert 500 <= q_before <= 600, f"ang orihinal ay ~551, nakuha {q_before}"
    assert 140 <= q_after <= 180, f"sa floor ay ~163, nakuha {q_after}"
    # pareho ang dolyar na risk sa magkabilang panig
    assert abs(q_before * m_before["stop_distance"] - 6.54) < 0.20
    assert abs(q_after * m_after["stop_distance"] - 6.54) < 0.20


def test_a_stop_already_outside_noise_is_untouched():
    """Kung malapad na ang stop, WALANG pagbabago — hindi ito nagpapasikip."""
    a_out, meta = stop_noise_floor_decision(
        atr_pct=0.10, stop_atr_mult=MULT,       # 6% stop
        noise_range_pct=0.02, buckets_used=10, min_buckets=6,
    )
    assert a_out == 0.10
    assert meta["applied"] is False
    assert meta["reason"] == "stop_already_outside_noise"


def test_insufficient_own_tape_changes_nothing():
    """<6 nonempty bucket ⇒ dating gawi. Ang bagong pangalan ay hindi pinaparusahan."""
    a_out, meta = stop_noise_floor_decision(
        atr_pct=AUUD_ATR, stop_atr_mult=MULT,
        noise_range_pct=AUUD_NOISE, buckets_used=5, min_buckets=6,
    )
    assert a_out == AUUD_ATR
    assert meta["applied"] is False
    assert meta["reason"] == "insufficient_own_tape"


def test_no_measurement_changes_nothing():
    a_out, meta = stop_noise_floor_decision(
        atr_pct=AUUD_ATR, stop_atr_mult=MULT,
        noise_range_pct=None, buckets_used=0, min_buckets=6,
    )
    assert a_out == AUUD_ATR
    assert meta["reason"] == "no_noise_measurement"


def test_fails_open_on_garbage():
    """Ang bug sa pagsukat ay hindi kailanman dapat gumalaw ng stop."""
    for bad in (float("nan"), float("inf"), -0.01, 0.0, "kalokohan"):
        a_out, meta = stop_noise_floor_decision(
            atr_pct=AUUD_ATR, stop_atr_mult=MULT,
            noise_range_pct=bad, buckets_used=10, min_buckets=6,
        )
        assert a_out == AUUD_ATR, bad
        assert meta["applied"] is False


def test_it_can_only_widen_never_tighten():
    """INVARIANT: ang output stop fraction >= input stop fraction, palagi."""
    import itertools
    for atr, nr in itertools.product(
        (0.005, 0.0179, 0.05, 0.10), (0.001, 0.01, 0.036, 0.08),
    ):
        a_out, _ = stop_noise_floor_decision(
            atr_pct=atr, stop_atr_mult=MULT,
            noise_range_pct=nr, buckets_used=10, min_buckets=6,
        )
        before = max(0.003, atr * MULT)
        after = max(0.003, a_out * MULT)
        assert after >= before - 1e-12, (atr, nr)


def test_this_is_not_a_reject_gate():
    """Walang code path na nagbabalik ng 'huwag pumasok' — qty lang ang liliit.

    Kahit ang pinaka-ekstremong noise ay nagbibigay pa rin ng positibong qty
    hangga't kaya ng min size; ang qty=0 ay manggagaling LAMANG sa umiiral
    nang risk-first semantics (hindi kasya sa min size), hindi sa floor mismo.
    """
    a_out, meta = stop_noise_floor_decision(
        atr_pct=0.005, stop_atr_mult=MULT,
        noise_range_pct=0.15, buckets_used=10, min_buckets=6,   # 15%! ekstremo
    )
    assert meta["applied"] is True
    q, m = compute_risk_first_quantity(
        entry_price=4.00, atr_pct=a_out, max_loss_usd=50.0,
        max_notional_ceiling_usd=10_000.0, stop_atr_mult=MULT,
    )
    assert q > 0, "lumiit pero hindi tinanggihan"


def test_the_floor_knobs_are_wired():
    s = Settings()
    assert s.chili_momentum_stop_noise_floor_min_buckets == 6
    assert s.chili_momentum_stop_noise_floor_lookback_seconds == 900.0


def test_the_noise_floor_is_off_by_measured_negative_control():
    """2026-09-05: shipped ON at 16:30Z on a 6-pair A/B (winners +465, losers +3), then the
    FULL 15-pair A/B failed the negative control (Ross's losers -81.84: EZRA t3 alpaca -75 ->
    -143 after the smaller leg-3 loss dodged the -1.5R symbol-day lockout; INLF t1 RH -20 ->
    -40). Program rule: winners up AND losers not up. OFF is a measured decision, not a dark
    flag; the structural bound stays in the code and the env override turns it back on for a
    paper soak."""
    assert Settings().chili_momentum_stop_noise_floor_enabled is False
