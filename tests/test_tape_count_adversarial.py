"""Short windows, discontinuities, old prints and legacy caller compatibility."""
from datetime import datetime, timezone
from random import Random
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import entry_gates as eg


def rows_from_gaps(gaps):
    t = 1_800_000_000.0
    rows = [(10, 100, 9.99, 10, t)]
    for i, gap in enumerate(gaps):
        t += gap
        px = 10 + (i + 1) * .001
        rows.append((px, 100, px - .01, px, t))
    return rows


@pytest.mark.parametrize("count", [30, 50, 100, 255])
def test_one_outage_cannot_set_its_own_threshold(count):
    gaps = [.05] * (count - 1)
    split = count // 2
    gaps[split - 1] = 300
    out = eg._signed_tape_features(rows_from_gaps(gaps), split="count", gap_trim_s=14.69,
                                   tick_rate_floor_pctile=0)
    assert out["gap_restricted"]
    assert out["n_ticks"] == count - split
    assert out["gap_trim_s"] < 1


def test_three_outages_cannot_inflate_own_p99_to_hide_all_three():
    gaps = [.05] * 254
    for index in [50, 100, 150]:
        gaps[index] = 300
    out = eg._signed_tape_features(rows_from_gaps(gaps), split="count", gap_trim_s=14.69,
                                   tick_rate_floor_pctile=0)
    assert out["gap_restricted"]
    assert out["n_ticks"] == 104


def test_seeded_healthy_slow_jitter_is_not_shredded_by_own_p99():
    random = Random(29)
    rows = rows_from_gaps([random.expovariate(1 / 23) for _ in range(254)])
    out = eg._signed_tape_features(rows, split="count", gap_trim_s=14.69,
                                   tick_rate_floor_pctile=0)
    assert out["n_ticks"] == 255
    assert out["gap_restricted"] is False


def test_sparse_window_cannot_raise_independent_print_age_ceiling():
    rows = rows_from_gaps([60] * 254)
    out = eg._signed_tape_features(rows, split="count", gap_trim_s=14.69,
                                   tick_rate_floor_pctile=0, as_of_ts=rows[-1][4] + 30)
    assert out["gap_p99_s"] == 60
    assert out["print_age_bound_s"] == 14.69
    assert out["print_stale"] is True


def test_legacy_contract_keeps_the_shipped_feature_geometry(monkeypatch):
    from app.services.trading.momentum_neural import optional_db_read
    rows = rows_from_gaps([.05] * 130 + [12] + [.05] * 123)
    monkeypatch.setattr(optional_db_read, "optional_fetchall", lambda *a, **k: rows)
    baseline = eg._signed_tape_features(rows, window_s=15, tick_rate_floor_pctile=0)
    out = eg.signed_tape_accel_features(
        "ABC", db=object(), window_prints=255, as_of=datetime.now(timezone.utc),
        feature_contract="legacy_time_split", settings_obj=SimpleNamespace())
    for key in ["signed_tape_accel", "tick_rate", "tick_rate_floor", "n_ticks",
                "gap_restricted", "buy_share_delta", "window_high_px"]:
        assert out[key] == baseline[key]
    assert out["feature_contract"] == "legacy_time_split"
    assert out["split"] == "time"
    assert out["gap_trim_s"] == 7.5


def test_explicit_g4_override_survives_settings_construction():
    from app.config import Settings
    config = Settings(chili_momentum_tape_window_prints=181,
                      chili_momentum_g4_reentry_tape_window_prints=64)
    assert config.chili_momentum_tape_window_prints == 181
    assert config.chili_momentum_g4_reentry_tape_window_prints == 64


def test_actual_arm_wrapper_publishes_nonbinding_measurement(monkeypatch, caplog):
    from app.services.trading.momentum_neural import auto_arm
    monkeypatch.setattr(auto_arm, "_tape_cold_probe", lambda s: (
        False, {"binding": "observational_arm_population_not_calibrated", "cold_observed": True,
                "window_prints": 255, "print_age_bound_s": 14.69}))
    with caplog.at_level("INFO"):
        assert auto_arm._tape_cold("ABC") is False
    assert "cold_observed" in caplog.text
    assert "observational_arm_population_not_calibrated" in caplog.text
