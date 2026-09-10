"""EXIT VERDICT F -- the PURE module on synthetic tapes (2026-09-10, [21]/[44]/[47]).

DB-free. Every rule in `exit_verdict.py` judged on hand-built prints:
  * the two floors (feature 3, binding 4) and what `binding` names when each holds;
  * the three conditions of D, each failing ALONE names itself; all three => `all_three`;
  * the leg high is the FIRST occurrence on a tied max and the high print is EXCLUDED;
  * the tick deadman base picks the first of swing_low_prev / swing_low_now /
    buy_support_px strictly below the entry, else the resting stop; the ratchet never lowers;
  * the runner walk exits on the first print <= level, honours a mid-batch ratchet, and the
    deadman beats D2 inside one batch;
  * `partial_split` cannot_split => whole; the phase table accepts every allowed edge and
    raises on every other pair (full product).

Runnable: pytest tests/test_exit_verdict_f_pure.py -v
"""
from __future__ import annotations

import inspect
import itertools
from datetime import datetime, timedelta

import pytest

from app.services.trading.momentum_neural import exit_verdict as EV

T0 = datetime(2026, 9, 10, 14, 0, 0)
E0 = 1_800_000_000.0


def _row(i: int, px: float, size: float = 100.0, *, bid: float | None = None, ask: float | None = None,
         dt_s: float = 0.5):
    """(price, size, bid, ask, epoch, observed_at, id) -- the readers' shape."""
    b = bid if bid is not None else px - 0.01
    a = ask if ask is not None else px + 0.01
    return (px, size, b, a, E0 + i * dt_s, T0 + timedelta(seconds=i * dt_s), 1000 + i)


def _tape(prices, sizes=None, *, aggressor=None, dt_s: float = 0.5):
    """Build rows; `aggressor` per print: +1 lifts the ask (buy), -1 hits the bid (sell)."""
    out = []
    for i, px in enumerate(prices):
        sz = sizes[i] if sizes else 100.0
        ag = aggressor[i] if aggressor else 0
        if ag > 0:
            out.append(_row(i, px, sz, bid=px - 0.02, ask=px, dt_s=dt_s))      # px >= ask => buy
        elif ag < 0:
            out.append(_row(i, px, sz, bid=px, ask=px + 0.02, dt_s=dt_s))      # px <= bid => sell
        else:
            out.append(_row(i, px, sz, dt_s=dt_s))
    return out


# ── floors ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n", [0, 1, 2])
def test_below_the_feature_floor_is_no_verdict_and_names_the_floor(n):
    v = EV.since_high_verdict(_tape([1.0] * n), window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "feature_floor_n_lt_3"
    assert v["n_since_high"] == n
    assert v["floor_prints"] == 4 and v["feature_floor"] == 3


def test_three_prints_clear_the_feature_floor_but_not_the_count_halves_floor():
    v = EV.since_high_verdict(_tape([1.0, 0.99, 0.98]), window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "count_halves_floor_n_lt_4"
    assert v["n_ticks"] == 3
    assert v["swing_low_now"] is None and v["buy_share_delta"] is None


def test_feature_none_is_named():
    # zero sizes => the feature function returns None (zero total volume)
    v = EV.since_high_verdict(_tape([1.0, 1.0, 1.0, 1.0], sizes=[0, 0, 0, 0]),
                              window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False and v["binding"] == "feature_none"


# ── the three conditions ───────────────────────────────────────────────────────

def _fires_tape():
    """4 prints: front half buyer-heavy and high, back half seller-heavy and lower."""
    return _tape([1.00, 1.00, 0.98, 0.97], sizes=[300, 300, 100, 100], aggressor=[1, 1, -1, -1])


def test_the_minimal_four_print_fire():
    v = EV.since_high_verdict(_fires_tape(), window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is True and v["binding"] == "all_three"
    assert v["signed_tape_accel"] < 0 and v["buy_share_delta"] < 0
    assert v["swing_low_now"] < v["swing_low_prev"]
    assert v["n_since_high"] == 4


def test_accel_failing_alone_names_itself():
    # back half buys MORE volume (accel > 0) but its buy SHARE falls and it prints a lower low
    rows = _tape([1.00, 1.00, 0.98, 0.97], sizes=[100, 100, 900, 100], aggressor=[1, 1, 1, -1])
    v = EV.since_high_verdict(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "accel_not_negative", v
    assert v["failed"] == ["accel_not_negative"]


def test_buy_share_delta_failing_alone_names_itself():
    # back half: less raw buy volume (accel < 0) but a HIGHER buy share; lower low still true
    rows = _tape([1.00, 1.00, 0.98, 0.97], sizes=[500, 500, 100, 100], aggressor=[1, -1, 1, 1])
    v = EV.since_high_verdict(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "buy_share_delta_not_negative", v


def test_no_lower_low_failing_alone_names_itself():
    # sellers took the back half but the back half's low is NOT below the front half's low
    rows = _tape([1.00, 0.97, 0.99, 0.98], sizes=[300, 300, 100, 100], aggressor=[1, 1, -1, -1])
    v = EV.since_high_verdict(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "no_lower_low", v


# ── the since-high anchor ──────────────────────────────────────────────────────

def test_leg_high_is_the_first_occurrence_on_a_tied_max():
    rows = _tape([1.00, 1.05, 1.02, 1.05, 1.01])
    hi = EV.leg_high_print(rows)
    assert hi["price"] == 1.05 and hi["index"] == 1 and hi["id"] == 1001
    assert hi["tie_rule"] == "first_occurrence"
    # the feature dict's own `prints_since_high` uses the NEWEST occurrence -- not this rule
    since = rows[hi["index"] + 1:]
    assert len(since) == 3   # the high print itself is EXCLUDED; the later tie is COUNTED


def test_the_high_print_is_excluded_from_the_window():
    rows = _tape([1.00, 1.05, 1.00, 0.99, 0.98, 0.97], sizes=[100, 900, 300, 300, 100, 100],
                 aggressor=[1, 1, 1, 1, -1, -1])
    hi = EV.leg_high_print(rows)
    since = rows[hi["index"] + 1:]
    v = EV.since_high_verdict(since, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["n_since_high"] == 4
    assert v["fired"] is True


def test_leg_high_ignores_unreadable_prices():
    rows = [(None, 1, None, None, E0, T0, 1), ("x", 1, None, None, E0, T0, 2)] + _tape([0.5])
    assert EV.leg_high_print(rows)["price"] == 0.5
    assert EV.leg_high_print([]) is None


# ── the tick deadman ───────────────────────────────────────────────────────────

def test_tick_deadman_base_picks_the_first_of_the_three_strictly_below_entry():
    feats = {"swing_low_prev": 9.80, "swing_low_now": 9.70, "buy_support_px": 9.60}
    assert EV.tick_deadman_base(feats, entry_px=10.0, resting_stop=9.0) == (9.80, "swing_low_prev")
    feats = {"swing_low_prev": 10.10, "swing_low_now": 9.70, "buy_support_px": 9.60}
    assert EV.tick_deadman_base(feats, entry_px=10.0, resting_stop=9.0) == (9.70, "swing_low_now")
    feats = {"swing_low_prev": None, "swing_low_now": 10.5, "buy_support_px": 9.95}
    assert EV.tick_deadman_base(feats, entry_px=10.0, resting_stop=9.0) == (9.95, "buy_support_px")


def test_tick_deadman_base_falls_back_to_the_resting_stop():
    assert EV.tick_deadman_base(None, entry_px=10.0, resting_stop=9.0) == (9.0, "resting_stop")
    assert EV.tick_deadman_base({"swing_low_prev": 10.2}, entry_px=10.0, resting_stop=9.0) == (9.0, "resting_stop")
    assert EV.tick_deadman_base({}, entry_px=10.0, resting_stop=None) == (None, "none")


def test_ratchet_never_lowers():
    assert EV.tick_deadman_ratchet(9.5, 9.7) == (9.7, True)
    assert EV.tick_deadman_ratchet(9.7, 9.6) == (9.7, False)
    assert EV.tick_deadman_ratchet(9.7, None) == (9.7, False)
    assert EV.tick_deadman_ratchet(None, 9.1) == (9.1, True)
    assert EV.tick_deadman_ratchet(9.7, "junk") == (9.7, False)
    assert EV.tick_deadman_ratchet(9.7, 0.0) == (9.7, False)


def test_walk_exits_on_the_first_print_at_or_below_the_level():
    batch = _tape([10.2, 10.1, 9.9, 9.8, 10.5])
    out = EV.walk_runner_prints(batch, level=9.9, runner_high=10.0, ratchet_feats=lambda at: None)
    assert out["exit_print"]["price"] == 9.9 and out["exit_print"]["id"] == 1002
    assert out["prints_scanned"] == 3            # the walk STOPS at the crossing
    assert out["runner_high"] == 10.2 and out["saw_new_high"] is True


def test_walk_honours_a_mid_batch_ratchet_and_the_deadman_beats_d2_in_the_same_batch():
    # new high at 10.3 => swing_low_prev 10.05 (from the callback) => level 9.9 -> 10.05;
    # the LATER print 10.0 is <= the NEW level => exit inside the same batch.
    calls = []

    def feats(at):
        calls.append(at)
        return {"swing_low_prev": 10.05}

    batch = _tape([10.1, 10.3, 10.2, 10.0, 10.6])
    out = EV.walk_runner_prints(batch, level=9.9, runner_high=10.0, ratchet_feats=feats)
    # 10.1 and 10.3 are both new highs; the callback answers 10.05 both times, so the level
    # MOVES once (9.9 -> 10.05) and the second candidate is refused (never lowered, never
    # re-stamped) -- one ratchet receipt, not two (p90 2 / max 7 per runner, measured).
    assert len(out["ratchets"]) == 1
    assert out["ratchets"][0]["old_level"] == 9.9 and out["ratchets"][0]["new_level"] == 10.05
    assert out["ratchets"][0]["new_high_print"]["price"] == 10.1
    assert out["level"] == 10.05
    assert out["exit_print"]["price"] == 10.0        # deadman wins; D2 is judged AFTER the walk
    assert calls == [batch[0][5], batch[1][5]]       # the callback is asked AT the new-high print


def test_walk_with_no_level_never_exits_but_still_tracks_the_high():
    batch = _tape([10.0, 10.4, 10.1])
    out = EV.walk_runner_prints(batch, level=None, runner_high=None, ratchet_feats=lambda at: None)
    assert out["exit_print"] is None and out["runner_high"] == 10.4 and out["level"] is None


# ── the partial split ──────────────────────────────────────────────────────────

def test_partial_split_is_the_venue_valid_scale_out_split():
    f, r, ok = EV.partial_split(current_qty=31.0, original_qty=31.0, fraction=11 / 31,
                                base_increment=1.0, base_min_size=1.0)
    assert (f, r, ok) == (11.0, 20.0, True)


def test_partial_split_cannot_split_means_whole():
    f, r, ok = EV.partial_split(current_qty=1.0, original_qty=1.0, fraction=0.5,
                                base_increment=1.0, base_min_size=1.0)
    assert ok is False and r == 1.0 and f == 0.0
    f, r, ok = EV.partial_split(current_qty=2.0, original_qty=2.0, fraction=11 / 31,
                                base_increment=1.0, base_min_size=1.0)
    assert ok is False   # 2 * 0.35 floors to 0


# ── the phase table ────────────────────────────────────────────────────────────

ALLOWED = {
    (None, "armed"), ("armed", "armed"), ("armed", "partial_shrink_pending"), ("armed", "exited"),
    ("partial_shrink_pending", "partial_sell_pending"), ("partial_shrink_pending", "armed"),
    ("partial_shrink_pending", "exited"),
    ("partial_sell_pending", "runner"), ("partial_sell_pending", "armed"), ("partial_sell_pending", "exited"),
    ("runner", "runner"), ("runner", "runner_exit_pending"), ("runner", "exited"),
    ("runner_exit_pending", "exited"),
}


@pytest.mark.parametrize("frm,to", sorted(itertools.product([None, *EV.PHASES], EV.PHASES), key=str))
def test_the_phase_table_over_the_full_product(frm, to):
    if (frm, to) in ALLOWED:
        EV.assert_verdict_transition(frm, to)
    else:
        with pytest.raises(ValueError):
            EV.assert_verdict_transition(frm, to)


def test_unknown_phases_raise():
    with pytest.raises(ValueError):
        EV.assert_verdict_transition("armed", "flying")
    with pytest.raises(ValueError):
        EV.assert_verdict_transition("flying", "armed")


def test_no_terminal_phase_in_the_pending_set_and_the_bypass_sets_are_what_the_spec_says():
    assert not (EV.PENDING_PHASES & EV.TERMINAL_PHASES)
    assert EV.TRAIL_BYPASS_PHASES == {"armed", "partial_shrink_pending", "partial_sell_pending",
                                      "runner", "runner_exit_pending"}
    assert EV.FIRST_TARGET_BYPASS_PHASES == {"partial_shrink_pending", "partial_sell_pending",
                                             "runner", "runner_exit_pending"}
    assert "armed" not in EV.FIRST_TARGET_BYPASS_PHASES   # the target whole-exit stays reachable


# ── the receipt shape and the derivations ──────────────────────────────────────

def test_verdict_receipt_has_a_stable_key_set():
    keys = set(EV.verdict_receipt(None))
    assert keys == {"fired", "binding", "n_since_high", "signed_tape_accel", "buy_share_delta",
                    "swing_low_now", "swing_low_prev", "gap_restricted", "n_ticks", "window_s",
                    "floor_prints", "feature_floor"}
    assert EV.verdict_receipt(None)["fired"] is False


def test_the_module_is_pure_and_the_derivations_carry_the_measurement():
    src = inspect.getsource(EV)
    assert "datetime.now(" not in src and "datetime.utcnow(" not in src
    assert "getattr(settings" not in src and "config import" not in src   # no settings read
    assert "sqlalchemy" not in src and "iqfeed_trade_ticks" not in src
    # ONE harness of record (tick-by-tick, bid-priced); the superseded in-memory STEP=100
    # numbers (-232.62 / -152.79 / -129.60) are cited NOWHERE in a receipt any more
    for tok in ("2026-09-10", "-697.87", "-502.18", "-380.82", "-485.50", "-489.25", "-495.7",
                "F(0.75, shipped)", "16/34", "-304.93", "tick-by-tick"):
        assert tok in EV._EXIT_VERDICT_DERIVATION, tok
    for tok in ("-232.62", "-152.79", "-129.60"):
        assert tok not in EV._EXIT_VERDICT_DERIVATION, tok
    for tok in ("swing_low_prev", "buy_support_px", "255", "resting_stop", "35/35", "delivery-bounded"):
        assert tok in EV._TICK_DEADMAN_BASE_DERIVATION, tok
    for tok in ("8/32", "24/32", "0.75", "0.5", "2026-09-10", "[0.13, 0.42]", "20/31",
                "F(0.75) = -495.7", "WORST of the three"):
        assert tok in EV._SELL_FRACTION_DERIVATION, tok
    assert EV.FEATURE_FLOOR_PRINTS == 3 and EV.BINDING_FLOOR_PRINTS == 4
    assert EV.SELL_FRACTION_FALLBACK == 0.5
