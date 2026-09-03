"""DEFECT 2 — move_pct reports fiction, because nothing validates the basis.

move_pct / gap_pct / todays_change_perc are an unguarded division by a vendor
field the code never checked:

    move_pct = (price - prev_close) / prev_close * 100

THE PROOF THAT IT IS THE BASIS AND NOT THE PRICE (lane log, 2026-09-02):

    06:39:23  [momentum_ws_ignition] symbol=HOS move_pct=3462.37 scored_ok=True
    06:39:37  [momentum_ws_ignition] symbol=HOS move_pct=3447.36 scored_ok=True

14s apart, 15.01 percentage points of "change" — which is a 0.43% price move.
The deltas are honest; only the level is invented. 10.687 / 35.6237 = 0.3000,
and the persisted row names it: momentum_symbol_viability HOS, updated_at
2026-09-02 22:56:34, ross_signals = {"price": 10.33, "gap_pct": 3345.63,
"prev_close": 0.3, "signal_type": "premarket_gap", ...}. Two independent tapes
say $0.30 never happened (iqfeed_trade_ticks 14:00-14:30Z: 1,687 prints,
10.37-10.6099; momentum_nbbo_spread_tape 14:00-14:20Z: 36 rows, mid
10.52-10.595), and HOS is absent from the independent full-market mover set.

The harm ran both ways. HOS took the #1 ross_score of the whole 32-name board
(0.5081 vs 0.3464 for the best real name) plus a top_market_gainers slot and the
+0.03 viability tilt, for a stock that moved 4.75% all day. In the other
direction ross_universe_change_below_profile (a +5.0% floor) fired 1,909 times
across 17 symbol-days whose true day change was +11.8% to +946.4% — SGLY x324
(+182.9%), HUIZ x323 (+186.3%), JZ x318 (+221.2%) on 2026-08-20 alone.

WHAT THIS FILE DOES ON origin/main — stated precisely, because the first draft
of this docstring overclaimed and a reviewer caught it by executing it.

On origin/main this file cannot be COLLECTED at all: the import at the top
raises ``ModuleNotFoundError: No module named 'app.services.trading.day_basis_
guard'`` and pytest reports ``1 error``, 0 tests executed. To learn what the
individual tests do there you have to make the file runnable first — copy it and
delete only the ``day_basis_guard`` import block. Doing exactly that against
origin/main's ``app/`` gives ``15 failed, 2 passed``:

  * the tests under "the guard itself" and the two parametrized/continuity unit
    tests exercise the new module and fail on the missing import (11 of them);
  * FIVE fail behaviourally, and these are the refutations that matter —
    "the fictional basis was cached", the once-per-session alarm count, "the
    lane silently re-based mid-session", "a 3,345% fiction took rank #1", and
    the universe-admission test below;
  * ``test_a_healthy_name_still_gets_its_baseline_and_change`` passes on main,
    correctly — it is the no-regression control, and a guard that broke it would
    be worse than the defect.

``test_basis_memory_resets_on_the_et_date_rollover`` used to be the second
passer, vacuously: with no basis memory at all main re-read the new value
anyway, so it would have passed even if the rollover branch were never
consulted. It now asserts both halves — frozen WITHOUT the rollover, moved WITH
it — so only the rollover branch can satisfy it.

Pure unit tests, no DB, no network.
"""

from __future__ import annotations

import logging

import pytest

from app.services.trading.day_basis_guard import (
    DAY_BASIS_IMPLAUSIBLE_VS_SESSION,
    DAY_BASIS_MISSING,
    DAY_BASIS_OK,
    DAY_BASIS_OUTSIDE_PREV_RANGE,
    DAY_BASIS_REJECTED,
    basis_continuity_broken,
    classify_day_basis,
)
from app.services.trading.momentum_neural import ignition_loop as IL


# ── the guard itself ─────────────────────────────────────────────────────────


def test_hos_shape_is_rejected():
    """price 10.33, prevDay.c 0.30 → 34.4x. The exact 2026-09-02 row."""
    basis, verdict = classify_day_basis(0.30, open_price=10.30, price=10.33)
    assert basis is None
    assert verdict == DAY_BASIS_IMPLAUSIBLE_VS_SESSION


def test_hos_shape_is_rejected_premarket_too():
    """Premarket has no day.o, so the live price is the reference."""
    basis, verdict = classify_day_basis(0.30, open_price=None, price=10.33)
    assert basis is None
    assert verdict == DAY_BASIS_IMPLAUSIBLE_VS_SESSION


def test_sgld_a_real_525_percent_gapper_is_NOT_rejected():
    """SGLD 2026-09-02: prev close 5.08, low 16.76, high 31.78 (+525.6% day).

    A guard that rejects this is worse than the defect. 31.78/5.08 = 6.3x.
    """
    basis, verdict = classify_day_basis(5.08, open_price=31.78, price=28.0)
    assert verdict == DAY_BASIS_OK
    assert basis == 5.08


def test_the_largest_real_mover_in_the_window_is_NOT_rejected():
    """WVVIP 2026-08-25, +946.4% on the day — 10.5x, inside the 20x bound."""
    basis, verdict = classify_day_basis(1.00, open_price=10.46, price=10.46)
    assert verdict == DAY_BASIS_OK
    assert basis == 1.00


def test_close_outside_its_own_prior_bar_is_rejected():
    """A close outside the range it supposedly closed in is a wrong row."""
    basis, verdict = classify_day_basis(
        3.70, open_price=2.20, price=2.60, prev_high=1.80, prev_low=1.55
    )
    assert basis is None
    assert verdict == DAY_BASIS_OUTSIDE_PREV_RANGE


def test_close_inside_its_own_prior_bar_is_accepted():
    basis, verdict = classify_day_basis(
        1.70, open_price=2.20, price=2.60, prev_high=1.80, prev_low=1.55
    )
    assert verdict == DAY_BASIS_OK
    assert basis == 1.70


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan")])
def test_unparseable_basis_is_missing_not_zero(bad):
    basis, verdict = classify_day_basis(bad)
    assert basis is None
    assert verdict == DAY_BASIS_MISSING
    assert verdict not in DAY_BASIS_REJECTED  # caller's own fallback still applies


def test_continuity_detects_a_mid_session_rebase():
    assert basis_continuity_broken(1.70, 3.70) is True
    assert basis_continuity_broken(1.70, 1.72) is False
    assert basis_continuity_broken(None, 3.70) is False


# ── the tracker: a corrupt basis must not become a stamped change ────────────


def _row(ticker, *, prev_c, day_o, price, prev_h=None, prev_l=None):
    return {
        "ticker": ticker,
        "lastTrade": {"p": price},
        "day": {"v": 5_000_000, "h": price, "l": price * 0.95, "o": day_o, "c": price},
        "prevDay": {
            "c": prev_c,
            "v": 4_000_000,
            "h": prev_h if prev_h is not None else prev_c * 1.05,
            "l": prev_l if prev_l is not None else prev_c * 0.95,
        },
    }


@pytest.fixture
def tracker(monkeypatch):
    state: dict = {"snapshot": [], "universe": []}

    import app.services.massive_client as MC

    monkeypatch.setattr(
        MC, "get_full_market_snapshot", lambda *a, **k: state["snapshot"], raising=False
    )
    monkeypatch.setattr(
        IL, "build_equity_universe", lambda _p, *, snapshot=None: list(state["universe"])
    )
    return IL._UniverseTracker(), state


def test_corrupt_basis_yields_no_baseline_so_no_change_is_stamped(tracker, caplog):
    """The HOS row, end to end through the tracker."""
    trk, state = tracker
    state["snapshot"] = [_row("HOS", prev_c=0.30, day_o=10.30, price=10.33)]
    state["universe"] = ["HOS"]

    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        assert trk.refresh() == {"HOS"}

    assert trk.baseline_for("HOS") is None, "the fictional basis was cached"
    assert any("basis REJECTED" in r.message for r in caplog.records)

    quote = type("Q", (), {"last": 10.33, "mid": None, "price": None, "bid": None})()
    loop = object.__new__(IL.IgnitionScoringLoop)
    loop._tracker = trk
    assert loop._move_pct("HOS", quote) is None, "3462% was still computed"


def test_a_healthy_name_still_gets_its_baseline_and_change(tracker):
    trk, state = tracker
    state["snapshot"] = [_row("SGLD", prev_c=5.08, day_o=31.78, price=28.0)]
    state["universe"] = ["SGLD"]
    trk.refresh()

    assert trk.baseline_for("SGLD") == 5.08
    quote = type("Q", (), {"last": 28.0, "mid": None, "price": None, "bid": None})()
    loop = object.__new__(IL.IgnitionScoringLoop)
    loop._tracker = trk
    assert loop._move_pct("SGLD", quote) == pytest.approx(451.18, abs=0.01)


def test_the_alarm_fires_once_not_once_per_refresh(tracker, caplog):
    """HOS produced 798 observations and 9,290 viability writes in one session."""
    trk, state = tracker
    state["snapshot"] = [_row("HOS", prev_c=0.30, day_o=10.30, price=10.33)]
    state["universe"] = ["HOS"]
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        for _ in range(5):
            trk.refresh()
    assert len([r for r in caplog.records if "basis REJECTED" in r.message]) == 1


def test_mid_session_rebase_of_a_CORROBORATED_basis_is_frozen_and_reported(
    tracker, caplog
):
    """A prev close is fixed for the session. Movement is the corruption
    signature (KXIN/CDTG/CAST all show the implied basis moving intraday).

    Two agreeing reads first — an UNcorroborated value is not worth freezing
    (see the next test), so the freeze only applies once the basis has been
    confirmed.
    """
    trk, state = tracker
    state["snapshot"] = [_row("JZ", prev_c=1.70, day_o=2.20, price=2.60)]
    state["universe"] = ["JZ"]
    trk.refresh()
    trk.refresh()  # second agreeing read → CORROBORATED
    assert trk.baseline_for("JZ") == 1.70

    # Next 20s refresh: the vendor hands back a different close for the same day.
    state["snapshot"] = [
        _row("JZ", prev_c=3.70, day_o=2.20, price=2.60, prev_h=3.9, prev_l=3.5)
    ]
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        trk.refresh()

    assert trk.baseline_for("JZ") == 1.70, "the lane silently re-based mid-session"
    assert any("basis DRIFTED" in r.message for r in caplog.records)


def test_an_UNCORROBORATED_first_read_loses_to_the_second(tracker, caplog):
    """THE CORRUPT VALUE CAN ARRIVE FIRST.

    HOS was wrong from its very first observation (06:39:23 PT,
    move_pct=3462.37), so "freeze whatever you saw first" cements corruption
    just as readily as it prevents it — and for the ~2x class the magnitude
    bound admits (JZ 2026-08-20, implied basis ~3.7 against a true 1.70) it
    would convert a transient vendor row that re-reads every 20s and could
    self-heal into a locked-wrong basis for the whole session.

    So a single read is PROVISIONAL. The newer value wins until two agree.
    """
    trk, state = tracker
    # First read: the wrong one (3.70 against JZ's true 1.70 close).
    state["snapshot"] = [
        _row("JZ", prev_c=3.70, day_o=2.20, price=2.60, prev_h=3.9, prev_l=3.5)
    ]
    state["universe"] = ["JZ"]
    trk.refresh()
    assert trk.baseline_for("JZ") == 3.70

    # Second read: the correct one. It must WIN, not be frozen out.
    state["snapshot"] = [_row("JZ", prev_c=1.70, day_o=2.20, price=2.60)]
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        trk.refresh()

    assert trk.baseline_for("JZ") == 1.70, "an unconfirmed first read was frozen"
    assert any("basis UNCORROBORATED" in r.message for r in caplog.records)
    assert not any("basis DRIFTED" in r.message for r in caplog.records)

    # A third agreeing read corroborates 1.70; now it is held.
    trk.refresh()
    assert trk.baseline_for("JZ") == 1.70


def test_a_SUSTAINED_disagreement_rebases_rather_than_pinning_the_stale_close(
    tracker, caplog, monkeypatch
):
    """The freeze is BOUNDED.

    A corporate action, or a vendor correcting a bad row, produces a new value
    that keeps arriving. Holding the pre-adjustment close for the rest of the
    session is the fail-open direction the guard's own docstring names as the
    worse error, so the freeze surrenders after `_BASIS_REBASE_AFTER_S`.
    """
    trk, state = tracker
    state["snapshot"] = [_row("JZ", prev_c=1.70, day_o=2.20, price=2.60)]
    state["universe"] = ["JZ"]
    trk.refresh()
    trk.refresh()  # CORROBORATED at 1.70

    state["snapshot"] = [
        _row("JZ", prev_c=3.70, day_o=2.20, price=2.60, prev_h=3.9, prev_l=3.5)
    ]
    trk.refresh()
    assert trk.baseline_for("JZ") == 1.70, "the freeze did not engage"

    # Wind the disagreement clock past the bound without sleeping.
    trk._basis_disagree_since["JZ"] = (
        trk._basis_disagree_since["JZ"] - IL._BASIS_REBASE_AFTER_S - 1.0
    )
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        trk.refresh()

    assert trk.baseline_for("JZ") == 3.70, "the stale close was pinned for the session"
    assert any("basis RE-BASED" in r.message for r in caplog.records)


def test_basis_memory_resets_on_the_et_date_rollover(tracker):
    """A prev close legitimately changes BETWEEN sessions — and MUST NOT change
    within one.

    Both halves are asserted from the same corroborated starting state, so the
    ET-date rollover branch is the only thing that can satisfy this test. The
    earlier version asserted only the second half, which passed on origin/main
    for the wrong reason: with no basis memory at all the tracker re-read the new
    value anyway.
    """
    trk, state = tracker
    state["snapshot"] = [_row("JZ", prev_c=1.70, day_o=2.20, price=2.60)]
    state["universe"] = ["JZ"]
    trk.refresh()
    trk.refresh()  # CORROBORATED at 1.70

    new_close = [
        _row("JZ", prev_c=2.60, day_o=3.00, price=3.20, prev_h=2.7, prev_l=2.5)
    ]

    # HALF ONE — same session: the new close is refused.
    state["snapshot"] = new_close
    trk.refresh()
    assert trk.baseline_for("JZ") == 1.70, "re-based inside a single session"

    # HALF TWO — same input, across the ET date rollover: it is adopted.
    trk._basis_session_date = "1999-01-01"
    trk.refresh()
    assert trk.baseline_for("JZ") == 2.60, "the rollover did not clear the memory"


# ── the site that ADMITS and RANKS, not just the ones that stamp and log ─────
#
# The first pass of this repair guarded the ignition tracker's baseline cache and
# `scan_premarket_gaps` — both DOWNSTREAM stamping/emission sites — and left
# `build_equity_universe` untouched. That is where the vendor's own
# `todaysChangePerc` decides the +5% admission floor, the `rank_score =
# pos * log1p(chg)` ordering into the capped pool, and (through the batch change
# list) the adaptive `_hot_mover_bars` / `_adaptive_adv_floor` percentiles for
# every OTHER name. Suppressing the emitted value while leaving the computation
# that produced the wrong priority in place fixes the symptom, not the root.


def _universe_row(ticker, *, prev_c, day_o, price, change_pct, shares=5_000_000,
                  prev_h=None, prev_l=None):
    """A full-market snapshot row shaped the way `build_equity_universe` reads it."""
    return {
        "ticker": ticker,
        "todaysChangePerc": change_pct,
        "lastTrade": {"p": price},
        "day": {"o": day_o, "h": price, "l": price * 0.9, "c": price, "v": shares},
        "min": {"av": shares, "c": price},
        "prevDay": {
            "c": prev_c,
            "v": 4_000_000,
            "h": prev_h if prev_h is not None else prev_c * 1.05,
            "l": prev_l if prev_l is not None else prev_c * 0.95,
        },
    }


@pytest.fixture
def offline_universe(monkeypatch):
    """No DB, no IQFeed: the $-volume fallback query is the only external call."""
    from app.services.trading.momentum_neural import universe as U

    monkeypatch.setattr(U, "_iqfeed_dollar_volumes", lambda _t: {}, raising=False)
    # Module-level once-per-name alarm memory must not leak between tests.
    monkeypatch.setattr(U, "_BASIS_ALARMED_DATE", None, raising=False)
    monkeypatch.setattr(U, "_BASIS_ALARMED", set(), raising=False)
    return U


def test_hos_shaped_row_is_not_ADMITTED_to_the_universe(offline_universe, caplog):
    """The HOS row is dropped from the pool, not merely left unstamped.

    price 10.33, prevDay.c 0.30, todaysChangePerc 3462 — the exact 2026-09-02
    shape. On origin/main this row clears the +5% floor on a fictional change and
    takes the top `rank_score` in the pool.
    """
    U = offline_universe
    snap = [
        _universe_row("HOS", prev_c=0.30, day_o=10.30, price=10.33, change_pct=3342.0),
        _universe_row("REAL", prev_c=5.00, day_o=7.00, price=7.50, change_pct=50.0),
    ]
    with caplog.at_level(logging.WARNING, logger=U.__name__):
        out = U.build_equity_universe(U.EQUITY_ROSS_SMALLCAP, snapshot=snap)

    assert "HOS" not in out, "a 3,342% fiction was admitted to the watch set"
    assert out == ["REAL"]
    assert any("basis REJECTED" in r.message for r in caplog.records)


def test_the_fiction_cannot_take_the_top_rank_slot(offline_universe):
    """`rank_score = pos * log1p(chg)`: HOS's 3342 gives log1p 8.11 against 3.93
    for a real +50% mover — a ~2x rank inflation into the capped pool. Ordering
    is the harm the brief named ("a fictional value is a fictional priority"),
    so assert the ORDER, not only the membership."""
    U = offline_universe
    snap = [
        _universe_row("HOS", prev_c=0.30, day_o=10.30, price=10.33, change_pct=3342.0),
        _universe_row("A", prev_c=5.00, day_o=7.00, price=7.50, change_pct=50.0),
        _universe_row("B", prev_c=5.00, day_o=6.50, price=6.75, change_pct=35.0),
    ]
    out = U.build_equity_universe(U.EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert out and out[0] != "HOS"
    assert out == ["A", "B"]


def test_the_fiction_cannot_move_the_adaptive_bars_for_other_names(offline_universe):
    """`_hot_mover_bars` and `_adaptive_adv_floor` take percentiles over the
    batch's change list, so ONE fictional value shifts the bar every other name
    is judged against. Dropping the row (rather than nulling its stamp) is what
    keeps the batch's own statistics honest."""
    U = offline_universe
    real = [
        _universe_row(f"R{i}", prev_c=5.00, day_o=6.0 + i * 0.1,
                      price=6.0 + i * 0.1, change_pct=20.0 + i)
        for i in range(6)
    ]
    clean = U.build_equity_universe(U.EQUITY_ROSS_SMALLCAP, snapshot=list(real))
    poisoned = U.build_equity_universe(
        U.EQUITY_ROSS_SMALLCAP,
        snapshot=list(real)
        + [_universe_row("HOS", prev_c=0.30, day_o=10.30, price=10.33,
                         change_pct=3342.0)],
    )
    assert poisoned == clean, "the fiction perturbed the batch's own ordering"


def test_the_ARM_GATE_refuses_the_fiction_rather_than_admitting_it(offline_universe):
    """`ross_smallcap_profile_evidence` is the live auto-arm admission check and
    the snapshot change takes PRECEDENCE over the signal's stamped value, so
    guarding only the stamping site left this gate reading the raw vendor field.
    `risk_evaluator._ross_universe_ok` re-enters here with a fresh snapshot row
    precisely when the stamped value was withheld, which routed straight back
    around the guard."""
    U = offline_universe
    row = _universe_row("HOS", prev_c=0.30, day_o=10.30, price=10.33,
                        change_pct=3342.0)
    ok, reason, debug = U.ross_smallcap_profile_evidence("HOS", snapshot_row=row)
    assert ok is False
    assert reason == "ross_universe_missing_change_pct", (
        "the arm gate admitted a name on a fictional day change"
    )
    assert debug["snapshot_basis_rejected"] == "implausible_vs_session"
    assert debug["change_pct"] is None


def test_a_real_gapper_still_passes_the_arm_gate(offline_universe):
    """No-regression control. SGLD 2026-09-02: prev close 5.08, +525.6% — 6.3x,
    inside the bound and correctly admitted."""
    U = offline_universe
    row = _universe_row("SGLD", prev_c=5.08, day_o=17.0, price=18.0,
                        change_pct=254.3, shares=2_000_000)
    ok, reason, _debug = U.ross_smallcap_profile_evidence("SGLD", snapshot_row=row)
    assert ok is True
    assert reason == "ross_universe_profile_ok"


def test_the_TOO_HIGH_class_is_a_STATED_uncaught_boundary(offline_universe):
    """JZ 2026-08-20, PINNED — this is what the repair does NOT fix.

    True prev close 1.70; the lane's own NBBO tape (mid 2.21-3.265) implies the
    basis it actually read was ~3.7, i.e. 2.2x too HIGH, which read a +221% name
    as one down 10-41% all session and produced 318 of the 1,909
    `ross_universe_change_below_profile` rejections. A stale-but-internally-
    consistent `prevDay` bar does not contradict itself, and no magnitude bound
    tight enough to catch 2.2x is safe against SGLD's genuine 6.3x or WVVIP's
    10.5x. So this row is STILL admitted-then-rejected on the wrong number, and
    this test records that as a boundary rather than leaving it untested.

    Closing it needs a genuine second source (`massive_client._get_prev_close`
    hits `/v2/aggs/ticker/{t}/prev`, one call per screened name per session).
    That is not in this change and is not claimed.
    """
    U = offline_universe
    row = _universe_row(
        "JZ", prev_c=3.70, day_o=2.20, price=2.60,
        change_pct=-29.7,  # (2.60 - 3.70)/3.70 — the fiction, computed from 3.70
        prev_h=3.90, prev_l=3.50,
    )
    ok, reason, debug = U.ross_smallcap_profile_evidence("JZ", snapshot_row=row)

    assert debug["snapshot_basis_rejected"] is None, (
        "if this now flags, the magnitude bound moved — re-check SGLD/WVVIP"
    )
    assert ok is False
    assert reason == "ross_universe_change_below_profile"
    # And the true basis would have admitted it: (2.60-1.70)/1.70 = +52.9%.
    true_row = dict(row)
    true_row["todaysChangePerc"] = 52.9
    true_row["prevDay"] = {"c": 1.70, "v": 4_000_000, "h": 1.80, "l": 1.55}
    ok2, reason2, _ = U.ross_smallcap_profile_evidence("JZ", snapshot_row=true_row)
    assert ok2 is True and reason2 == "ross_universe_profile_ok"


def test_premarket_change_fallback_refuses_a_rejected_basis(offline_universe):
    """`_premarket_change_pct`'s `base = prev.c or day.o` is the surviving TWIN
    of the line already fixed in the ignition tracker — same expression, same
    field, but on the admission path. It must fall through to the open rather
    than divide by fiction."""
    U = offline_universe
    row = _universe_row("HOS", prev_c=0.30, day_o=10.30, price=10.33,
                        change_pct=None)
    row.pop("todaysChangePerc")
    got = U._premarket_change_pct(row)
    assert got == pytest.approx((10.33 - 10.30) / 10.30 * 100.0, abs=1e-6), (
        "the fictional 0.30 basis was used for the premarket fallback"
    )


# ── the second producer: scan_premarket_gaps sorts, then truncates ───────────


def test_premarket_gap_scan_drops_the_fictional_row(monkeypatch, caplog):
    """The persisted HOS row came through THIS path, and because the list is
    sorted by |gap_pct| then truncated, one fiction evicts real gappers."""
    from app.services.trading import intraday_signals as IS

    quotes = {
        "HOS": {"price": 10.33, "previous_close": 0.30},
        "REAL1": {"price": 7.00, "previous_close": 5.00},
        "REAL2": {"price": 6.00, "previous_close": 5.00},
    }
    monkeypatch.setattr(
        IS, "fetch_quote", lambda t: quotes.get(t), raising=False
    )
    import app.services.trading.market_data as MD

    monkeypatch.setattr(MD, "fetch_quote", lambda t: quotes.get(t), raising=False)

    with caplog.at_level(logging.WARNING, logger=IS.__name__):
        out = IS.scan_premarket_gaps(
            tickers=["HOS", "REAL1", "REAL2"], min_gap_pct=5.0, max_signals=2
        )

    tickers = [r["ticker"] for r in out]
    assert "HOS" not in tickers, "a 3,345% fiction took rank #1"
    assert tickers == ["REAL1", "REAL2"]
    assert any("basis REJECTED" in r.message for r in caplog.records)
