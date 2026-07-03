"""WAVE-4 ITEM-1 (R6) — PRINT-RECENCY halt inference.

The quote-freshness halt path (``_register_stale_quote_tick`` → ``stale_bbo`` streak)
STARVED since 2026-06-26: a secondary BBO refetch stamps FRESH meta on cached quotes so
``stale_bbo`` never returns → ``suspected_halt`` went 602/day → 0 and ``halt_resume_dip``
has NEVER fired. But a real LULD halt STOPS THE TRADE PRINTS even when the quote meta looks
fresh. ``_register_print_recency_halt_check`` is the independent second path: for a
WATCHED/HELD name that was RECENTLY ACTIVE, a silent trade tape (no prints for an adaptive
window) while the market is open ⇒ a suspected halt (same downstream lifecycle the quote
path sets).

These tests pin (PURE-LOGIC, mocked ``print_recency_state`` / ``is_data_session_now`` /
``_emit`` — no live DB):
  * an ACTIVE name + a 300s print gap + fresh quotes ⇒ suspected-halt SET (adaptive
    window from the median gap; the resume-dip lifecycle is now armable);
  * a QUIET never-active name ⇒ NO inference (fail-closed — no false halt);
  * no tape data ⇒ NO inference (fail-closed);
  * RESUME (prints return within the window) ⇒ the streak clears / halt clears;
  * the flag OFF ⇒ byte-identical (no inference regardless of tape).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.trading.momentum_neural import live_runner as LR


class _Tick:
    def __init__(self, bid=None, mid=None, open=None):  # noqa: A002 - mirror source attr name
        self.bid = bid
        self.mid = mid
        self.open = open


def _sess(state="watching_live", symbol="ABCD"):
    return SimpleNamespace(
        id=4242,
        symbol=symbol,
        risk_snapshot_json={},
        state=state,
        updated_at=None,
        user_id=1,
        variant_id=7,
        execution_family="momentum_neural",
        mode="live",
    )


def _flags(**overrides):
    base = {
        "chili_momentum_halt_print_recency_enabled": True,
        "chili_momentum_halt_print_gap_multiple": 8.0,
        "chili_momentum_halt_print_gap_floor_seconds": 30.0,
        "chili_momentum_halt_print_recent_active_seconds": 300.0,
        "chili_momentum_halt_print_recent_active_min_prints": 5,
        # halt_level-capture sub-flags OFF (byte-identical marker path).
        "chili_momentum_halt_resumption_direction_enabled": False,
        "chili_momentum_false_halt_avoid_enabled": False,
        "chili_momentum_add_into_halt_enabled": False,
    }
    base.update(overrides)
    return base


def _run(recency_state, *, flag_overrides=None, data_session=True, sess=None, le=None):
    """Drive ``_register_print_recency_halt_check`` with a mocked tape + open-market read.
    Returns the mutated ``le`` (the persisted lane store)."""
    sess = sess or _sess()
    le = {} if le is None else le
    with patch.object(LR, "settings", SimpleNamespace(**_flags(**(flag_overrides or {})))), \
        patch.object(LR, "_emit"), \
        patch("app.services.trading.momentum_neural.nbbo_tape.print_recency_state",
              return_value=recency_state), \
        patch("app.services.trading.momentum_neural.market_profile.is_data_session_now",
              return_value=data_session):
        LR._register_print_recency_halt_check(None, sess, le, _Tick(bid=10.0))
    return le


# --------------------------------------------------------------------------- #
# 1. ACTIVE name + 300s print gap + fresh quotes ⇒ suspected-halt SET          #
# --------------------------------------------------------------------------- #
def test_active_name_long_print_gap_sets_suspected_halt():
    # median gap ~2s ⇒ adaptive window = max(30, 2*8) = 30s; last print 300s ago ⇒ halted.
    st = {"last_print_age_s": 300.0, "recent_print_count": 40, "median_gap_s": 2.0}
    le = _run(st)
    assert le.get("suspected_halt_since_utc"), "an active name silent 300s must be suspected-halt"


def test_adaptive_window_scales_with_median_gap():
    # A slow name printing every 20s: window = max(30, 20*8) = 160s. A 120s gap is NOT a
    # halt for THIS name (it prints slowly), even though it would be for a 2s-gap name.
    st_slow = {"last_print_age_s": 120.0, "recent_print_count": 20, "median_gap_s": 20.0}
    le = _run(st_slow)
    assert "suspected_halt_since_utc" not in le, "120s < 160s adaptive window ⇒ not halted"
    # 200s > 160s ⇒ halted.
    st_slow_halted = {"last_print_age_s": 200.0, "recent_print_count": 20, "median_gap_s": 20.0}
    le2 = _run(st_slow_halted)
    assert le2.get("suspected_halt_since_utc")


# --------------------------------------------------------------------------- #
# 2. QUIET never-active name ⇒ NO inference (fail-closed)                       #
# --------------------------------------------------------------------------- #
def test_quiet_never_active_name_no_inference():
    # Long silence but only 1 print in the recent-active window (< min 5) ⇒ never active
    # ⇒ we must NOT infer a halt (it was never trading).
    st = {"last_print_age_s": 3600.0, "recent_print_count": 1, "median_gap_s": None}
    le = _run(st)
    assert "suspected_halt_since_utc" not in le, "a never-active quiet name must not false-halt"


def test_no_tape_data_no_inference():
    # print_recency_state returns None (no prints at all) ⇒ fail-closed.
    le = _run(None)
    assert "suspected_halt_since_utc" not in le


def test_market_closed_no_inference():
    st = {"last_print_age_s": 300.0, "recent_print_count": 40, "median_gap_s": 2.0}
    le = _run(st, data_session=False)
    assert "suspected_halt_since_utc" not in le, "a silent tape when closed is normal, not a halt"


# --------------------------------------------------------------------------- #
# 3. RESUME (prints return within the window) ⇒ no halt / cleared              #
# --------------------------------------------------------------------------- #
def test_prints_returned_within_window_no_halt():
    # Last print 5s ago, window 30s ⇒ tape is printing ⇒ NOT halted.
    st = {"last_print_age_s": 5.0, "recent_print_count": 40, "median_gap_s": 2.0}
    le = _run(st)
    assert "suspected_halt_since_utc" not in le


def test_resume_clears_via_fresh_quote_tick():
    # A halt was inferred; then a fresh quote tick arrives (prints back) ⇒ the fresh-tick
    # path clears the streak AND pops suspected_halt_since_utc (the resume lifecycle).
    st_halt = {"last_print_age_s": 300.0, "recent_print_count": 40, "median_gap_s": 2.0}
    le = _run(st_halt)
    assert le.get("suspected_halt_since_utc")
    sess = _sess()
    with patch.object(LR, "settings", SimpleNamespace(**_flags())), patch.object(LR, "_emit"), \
        patch.object(LR, "_commit_le"):
        LR._register_fresh_quote_tick(None, sess, le, _Tick(bid=10.1))
    assert "suspected_halt_since_utc" not in le, "prints back ⇒ resume clears the halt marker"
    assert le.get("halt_resumed_at_utc"), "resume stamps the cooldown marker (resume-dip window)"


# --------------------------------------------------------------------------- #
# 4. Flag OFF ⇒ byte-identical (no inference)                                  #
# --------------------------------------------------------------------------- #
def test_flag_off_no_inference():
    st = {"last_print_age_s": 300.0, "recent_print_count": 40, "median_gap_s": 2.0}
    le = _run(st, flag_overrides={"chili_momentum_halt_print_recency_enabled": False})
    assert "suspected_halt_since_utc" not in le, "flag OFF ⇒ the print-recency path is a no-op"


# --------------------------------------------------------------------------- #
# 5. Non-watched state ⇒ no inference (no live tape stake)                      #
# --------------------------------------------------------------------------- #
def test_non_watched_state_no_inference():
    st = {"last_print_age_s": 300.0, "recent_print_count": 40, "median_gap_s": 2.0}
    le = _run(st, sess=_sess(state="live_cooldown"))
    assert "suspected_halt_since_utc" not in le


def test_crypto_symbol_no_inference():
    st = {"last_print_age_s": 300.0, "recent_print_count": 40, "median_gap_s": 2.0}
    le = _run(st, sess=_sess(symbol="BTC-USD"))
    assert "suspected_halt_since_utc" not in le


def test_already_halted_is_noop():
    st = {"last_print_age_s": 300.0, "recent_print_count": 40, "median_gap_s": 2.0}
    le = {"suspected_halt_since_utc": "2026-07-02T12:00:00"}
    out = _run(st, le=le)
    # unchanged (idempotent) — the pre-existing marker is preserved, not overwritten.
    assert out["suspected_halt_since_utc"] == "2026-07-02T12:00:00"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
