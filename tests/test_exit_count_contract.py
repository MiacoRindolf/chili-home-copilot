"""Count geometry controls on frozen membership and actual held evaluations.

These are feature/chronology tests, not executable PnL or completed-pivot claims.
"""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import exit_verdict as ev
from app.services.trading.momentum_neural import live_runner as lr
from tests.test_exit_verdict_f_state_machine import (
    Env, T_ENTRY, E0, _le, _spike_tape, _spike_sells, _quiet_tape, _tick,
)


def contract(**overrides):
    args = dict(window_prints=255, print_age_bound_s=14.69, gap_mult=7.82, tick_rate_floor_pctile=0.)
    args.update(overrides)
    return ev.count_exit_contract(**args)


@pytest.mark.parametrize("field,expected", [("window_prints", 255), ("print_age_bound_s", 14.69), ("gap_mult", 7.82)])
@pytest.mark.parametrize("bad", [None, "broken", float("nan"), float("inf"), float("-inf"), -1, 0, True])
def test_malformed_existing_config_has_named_finite_calibrated_fallback(field, expected, bad):
    c = contract(**{field: bad})
    assert c[field] == expected
    assert c["sources"][field] == "invalid_or_missing_existing_setting_measured_fallback"
    assert c["contract_id"] == contract()["contract_id"]


@pytest.mark.parametrize("kwargs", [{"window_prints": 3}, {"window_prints": 4.1}, {"print_age_bound_s": .1},
                                   {"print_age_bound_s": 601}, {"gap_mult": .99}, {"gap_mult": 101}])
def test_existing_settings_validation_ranges_are_respected(kwargs):
    c = contract(**kwargs)
    assert c["contract_id"] == contract()["contract_id"]


def frozen_rows():
    out = []
    for n, (seconds, px, size, buy) in enumerate(zip(
        [0, .1, .2, .3, .4, .5, .6, 5.],
        [10., 10., 10., 10., 9.9, 9.9, 9.9, 9.8],
        [100., 100., 100., 100., 200., 200., 200., 300.],
        [True, True, True, True, True, True, True, False],
    )):
        bid, ask = (px-.02, px) if buy else (px, px+.02)
        out.append((px, size, bid, ask, E0+seconds, T_ENTRY+timedelta(seconds=seconds), 9100+n))
    return out


def test_d_frozen_membership_names_actual_count_vs_legacy_disagreement():
    rows = frozen_rows()
    original = deepcopy(rows)
    count = ev.since_high_verdict(rows, feature_contract="count_v1", as_of_ts=E0+5.2)
    legacy = ev.since_high_verdict(rows, feature_contract="legacy_time_split", window_s=15.)
    assert rows == original and [r[6] for r in rows] == list(range(9100, 9108))
    assert count["signed_tape_accel"] == 200.  # back600 - front400 positive BUY shares
    assert legacy["signed_tape_accel"] == -1000.  # time midpoint has seven front prints
    assert count["fired"] is False and legacy["fired"] is True
    assert count["binding"] == "accel_not_negative" and legacy["binding"] == "all_three"
    assert count["half_print_counts"] == {"front": 4, "back": 4}
    assert count["completed_pivot_claim"] is False
    assert count["window_prints"] is None and count["window_kind"] == "since_high_prints"
    assert "window_prints" not in count["calibration"]


def test_d_count_has_no_hidden_seconds_window_midpoint_trim_or_rate_fallback():
    rows = frozen_rows()
    a = ev.since_high_verdict(rows, window_s=.0001, as_of_ts=E0+5.2)
    b = ev.since_high_verdict(rows, window_s=100000., as_of_ts=E0+5.2)
    assert a == b and a["window_s"] is None and a["split"] == "count"
    assert a["gap_trim_basis"] == "window_gap_p90 x measured_p99_over_p90"
    assert a["gap_trim_mult"] == 7.82 and a["tick_rate_basis"] == "back_half_span"
    assert a["gap_trim_s"] == pytest.approx(a["gap_trim_window_p90_s"] * 7.82)


def test_sparse_window_never_raises_its_own_freshness_ceiling():
    rows = [(10.-i*.001, 100., 9., 10.-i*.001, E0+i*20., T_ENTRY+timedelta(seconds=i*20), i)
            for i in range(40)]
    count = ev.since_high_verdict(rows, as_of_ts=rows[-1][4]+16.)
    assert count["n_ticks"] == 40 and count["gap_restricted"] is False
    assert count["gap_trim_s"] == pytest.approx(20.*7.82)
    assert count["print_age_s"] == 16. and count["print_age_bound_s"] == 14.69
    assert count["print_stale"] is True
    assert count["half_print_counts"] == {"front": 20, "back": 20}


def test_odd_population_halves_and_missing_cadence_are_explicit():
    rows = frozen_rows()[:-1]
    odd = ev.since_high_verdict(rows, as_of_ts=E0+.7)
    assert odd["half_print_counts"] == {"front": 3, "back": 4}
    tied = [(*r[:4], E0, T_ENTRY, r[6]) for r in rows]
    none = ev.since_high_verdict(tied, as_of_ts=E0+.7)
    assert none["binding"] == "feature_none"  # no invented 7.5s denominator
    assert none["print_age_bound_s"] == 14.69


def seeded_prior(tape, prior_contract):
    le = _le()
    high = tape.rows[-1]
    le["exit_verdict"] = {
        "phase": "armed", "entry_at": T_ENTRY.isoformat(), "entry_px": 10.,
        "frontier_at": high[5].isoformat(), "frontier_id": high[6],
        "last_print": high[0], "last_print_at": high[5].isoformat(),
        "leg_high": {"price": high[0], "observed_at": high[5].isoformat(), "id": high[6]},
        "deadman": {"level": 9., "level_source": "resting_stop", "ratchets": 0},
        "prints_since_entry": 3, "prints_since_high": 0, "accel_prev": 1200.,
        "accel_prev_as_of": (T_ENTRY+timedelta(seconds=4)).isoformat(),
        "accel_prev_contract": prior_contract,
    }
    return le


@pytest.mark.parametrize("prior", [None, "legacy_time_split", "different-count-parameters"])
def test_old_or_mismatched_previous_feature_is_not_compared_as_count(prior, monkeypatch):
    tape = _spike_tape()
    le = seeded_prior(tape, prior)
    _spike_sells(tape)
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    out = _tick(env, le, seconds=12.5)
    assert out["action"] is None and out["rollover"]["binding"] == "no_previous_evaluation"
    assert le["exit_verdict"]["accel_prev"] <= 0
    assert le["exit_verdict"]["accel_prev_contract"] == lr._exit_verdict_settings()["contract_id"]
    assert le["exit_verdict"]["feature_contract_reset"]["retired_value"] == 1200.
    assert le["exit_verdict"]["deadman"]["level"] >= 9.
    assert le["exit_verdict"]["deadman"]["retained_prior_base"]["level_preserved"] is True
    assert le["deadman_stop"]["order_id"] == "dm-oid-1"


def test_same_contract_previous_feature_can_fire_whole_rollover(monkeypatch):
    tape = _spike_tape()
    le = seeded_prior(tape, lr._exit_verdict_settings()["contract_id"])
    _spike_sells(tape)
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    out = _tick(env, le, seconds=12.5)
    assert out["action"] == "accel_rollover"
    assert le["exit_verdict"]["exit"]["exit_fraction"] == 1.
    assert "feature_contract_reset" not in le["exit_verdict"]


def test_stale_contract_reset_retires_receipt_pointer_without_seed_value(monkeypatch):
    tape = _spike_tape()
    le = seeded_prior(tape, "legacy_time_split")
    _spike_sells(tape)
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    out = _tick(env, le, seconds=40)
    assert out["stale"] is True and out["action"] is None
    assert le["exit_verdict"]["accel_prev"] is None
    assert le["exit_verdict"]["evaluation_audit"]["previous_feature"]["status"] == "previous_feature_contract_retired"


def test_count_config_snapshot_is_shared_by_actual_base_and_g_calls(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    original = tape.signed_tape_accel_features
    calls = []
    before = lr._exit_verdict_settings()

    def read(symbol, **kwargs):
        cfg = kwargs["settings_obj"]
        calls.append((kwargs["as_of"], kwargs["feature_contract"], kwargs["window_prints"],
                      cfg.chili_momentum_g4_reentry_max_print_age_seconds,
                      cfg.chili_momentum_tape_gap_discontinuity_p90_mult))
        if len(calls) == 1:
            monkeypatch.setattr(settings, "chili_momentum_g4_reentry_max_print_age_seconds", .5)
            monkeypatch.setattr(settings, "chili_momentum_tape_gap_discontinuity_p90_mult", 1.)
        return original(symbol, **kwargs)

    monkeypatch.setattr(eg, "signed_tape_accel_features", read)
    le = _le()
    out = _tick(env, le, seconds=36)
    assert out["action"] is None and len(calls) == 2
    assert all(c[1:] == ("count_v1", before["window_prints"], before["print_age_bound_s"], before["gap_mult"]) for c in calls)
    assert le["exit_verdict"]["feature_contract"]["contract_id"] == before["contract_id"]


def test_malformed_live_settings_do_not_hide_stale_tape_or_deadman_crossing(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_g4_reentry_max_print_age_seconds", float("nan"))
    monkeypatch.setattr(settings, "chili_momentum_tape_gap_discontinuity_p90_mult", float("inf"))
    monkeypatch.setattr(settings, "chili_momentum_g4_reentry_tape_window_prints", float("inf"))
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le()
    _tick(env, le, seconds=4)
    _spike_sells(tape)
    held = _tick(env, le, seconds=40)
    assert held["stale"] is True and held["action"] is None
    assert le["exit_verdict"]["feature_contract"]["stale_bound_s"] == 14.69
    tape.add(41., 8.5, aggressor=-1)
    crossing = _tick(env, le, seconds=100)
    assert crossing["action"] == "tick_deadman" and crossing["stale"] is True
    assert le["exit_verdict"]["exit"]["exit_fraction"] == 1.


def test_actual_d_receipt_preserves_original_malformed_setting_provenance(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_g4_reentry_max_print_age_seconds", float("nan"))
    monkeypatch.setattr(settings, "chili_momentum_tape_gap_discontinuity_p90_mult", True)
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le()
    out = _tick(env, le, seconds=36)
    sources = le["exit_verdict"]["feature_contract"]["sources"]
    d = out["verdict"]
    assert d["feature_contract"] == "count_v1"
    for key in ("print_age_bound_s", "gap_mult", "tick_rate_floor_pctile"):
        assert d["count_parameter_sources"][key] == sources[key]
    assert d["count_parameter_sources"]["print_age_bound_s"].endswith("measured_fallback")
    assert d["count_parameter_sources"]["gap_mult"].endswith("measured_fallback")


def test_seconds_setting_cannot_change_held_count_contract(monkeypatch):
    before = lr._exit_verdict_settings()
    monkeypatch.setattr(settings, "chili_momentum_l2_confirm_window_s", float("nan"))
    assert lr._exit_verdict_settings() == before
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    out = _tick(env, _le(), seconds=36)
    assert out["G_feature_geometry"]["split"] == out["verdict"]["split"] == "count"
    assert out["G_feature_geometry"]["window_s"] is out["verdict"]["window_s"] is None


def test_recent_walk_print_cannot_make_an_older_g_feature_fresh(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le()
    _tick(env, le, seconds=4)
    previous = le["exit_verdict"]["accel_prev"]
    _spike_sells(tape)
    # The unchanged deadman walk accepts a price; the canonical feature parser
    # excludes zero-size rows. G must use its own newest valid feature print age.
    tape.add(40., 10.26, size=0, aggressor=1)
    out = _tick(env, le, seconds=40.1)
    assert out["stale"] is False and out["action"] is None
    assert out["rollover"]["condition_fired_before_freshness"] is True
    assert out["rollover"]["withheld"] == "G_feature_age_unknown_or_stale"
    assert le["exit_verdict"]["accel_prev"] == previous
