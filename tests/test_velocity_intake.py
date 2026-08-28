"""VELOCITY INTAKE — ang XLAB blindspot ng 2026-08-28.

SINUKAT: ang #1 play ni Ross (XLAB, SPAC day-2 listing) ay −2.4% sa araw pero
+12% sa loob ng minutos. LAHAT ng intake noon ay day-change-based (universe
min_change_pct=5, hot-mover 20, ignite floors 10) — ZERO ticks, zero viability,
ganap na invisible, habang si Ross ay kumita sa dalawang leg mula sa "Running
Up" scanner niya na puro short-horizon velocity.

Ang intake: (a) snapshot-delta admission sa `_UniverseTracker.refresh()` bilang
monotonic OR-leg (nakakadagdag lamang), at (b) pang-apat na ignite axis sa
`_ross_threshold_crossed`. Iisa ang floor knob para walang drift.

Runnable: pytest tests/test_velocity_intake.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.ignition_loop import _UniverseTracker


def _row(sym, price, day_o=None, prev_c=None, vol=2_000_000, prev_v=5_000_000):
    return {
        "ticker": sym,
        "day": {"o": day_o, "v": 0, "c": price},
        "min": {"c": price, "av": vol},
        "prevDay": {"c": prev_c, "v": prev_v},
        "lastTrade": {"p": price},
        "todaysChangePerc": (
            (price / prev_c - 1.0) * 100.0 if prev_c else None
        ),
    }


def _refresh(tracker, snapshot):
    """Diretsong i-inject ang snapshot (walang network) gaya ng tests ng screen."""
    import app.services.trading.momentum_neural.ignition_loop as il

    orig = il.build_equity_universe
    try:
        il.build_equity_universe = lambda profile, snapshot=None: []
        # ang universe screen ay walang ibinabalik — lahat ng admission ay
        # mula LAMANG sa velocity leg, kaya malinis ang attribution
        import app.services.massive_client as mc

        orig_snap = mc.get_full_market_snapshot
        mc.get_full_market_snapshot = lambda **kw: snapshot
        try:
            return tracker.refresh()
        finally:
            mc.get_full_market_snapshot = orig_snap
    finally:
        il.build_equity_universe = orig


def test_xlab_class_negative_day_burst_is_admitted():
    """ANG EKSAKTONG XLAB: −2.4% sa araw, 9.42→10.53 (+11.8%) sa isang refresh.
    Dati: invisible. Ngayon: nasa watch set at may velocity value."""
    tr = _UniverseTracker()
    _refresh(tr, [_row("XLAB", 9.42, prev_c=9.92)])
    got = _refresh(tr, [_row("XLAB", 10.53, prev_c=9.92)])
    assert "XLAB" in got
    v = tr.velocity_for("XLAB")
    assert v is not None and v > 11.0
    # ang baseline/rvol/shares loop ay dapat nag-stamp din para sa admitted name
    assert tr.baseline_for("XLAB") == 9.92
    assert tr.shares_for("XLAB") == 2_000_000.0


def test_below_the_bar_is_not_admitted():
    tr = _UniverseTracker()
    _refresh(tr, [_row("SLOW", 5.00, prev_c=5.10)])
    got = _refresh(tr, [_row("SLOW", 5.20, prev_c=5.10)])  # +4% < 7%
    assert "SLOW" not in got


def test_falling_names_never_qualify():
    """May tanda ang velocity: pagbagsak ay hindi admission (long lane)."""
    tr = _UniverseTracker()
    _refresh(tr, [_row("DUMP", 10.00, prev_c=9.00)])
    got = _refresh(tr, [_row("DUMP", 8.50, prev_c=9.00)])  # −15%
    assert "DUMP" not in got


def test_price_band_and_dollar_volume_hygiene(monkeypatch):
    from app.config import settings

    # Ang sub-$1 ay pinamamahalaan na ng paper-lane flag (2026-08-28) — dito
    # OFF para ang lumang buong-exclusion na gawi ang sinusubok.
    monkeypatch.setattr(
        settings, "chili_momentum_subdollar_paper_enabled", False, raising=False
    )
    tr = _UniverseTracker()
    _refresh(tr, [
        _row("PENNY", 0.40, prev_c=0.40),
        _row("BIG", 55.0, prev_c=50.0),
        _row("THIN", 5.00, prev_c=5.00, vol=50_000),  # $250k < $1M floor
    ])
    got = _refresh(tr, [
        _row("PENNY", 0.48, prev_c=0.40),   # +20% pero sub-$1
        _row("BIG", 62.0, prev_c=50.0),     # +12.7% pero >$20
        _row("THIN", 5.60, prev_c=5.00, vol=50_000),  # +12% pero manipis
    ])
    assert got == set()


def test_subdollar_velocity_admitted_when_paper_flag_on(monkeypatch):
    """SUB-$1 PAPER LANE (default ON): ang FNGR/CHAI-class na sub-dollar
    velocity mover ay pumapasok na sa watch set."""
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_subdollar_paper_enabled", True, raising=False
    )
    tr = _UniverseTracker()
    _refresh(tr, [_row("CHAI", 0.40, prev_c=0.40, vol=6_000_000)])
    got = _refresh(tr, [_row("CHAI", 0.48, prev_c=0.40, vol=6_000_000)])  # +20%, $2.9M vol
    assert "CHAI" in got
    v = tr.velocity_for("CHAI")
    assert v is not None and v > 15.0


def test_warrant_class_symbols_are_excluded():
    tr = _UniverseTracker()
    _refresh(tr, [_row("ABCDW", 2.00, prev_c=2.00)])
    got = _refresh(tr, [_row("ABCDW", 2.40, prev_c=2.00)])
    assert "ABCDW" not in got


def test_stale_history_beyond_the_window_is_ignored(monkeypatch):
    """Ang paghahambing ay naka-bound sa window — ang lumang presyo ay hindi
    puwedeng pagmulan ng multong velocity."""
    import time as _time

    tr = _UniverseTracker()
    _t = {"v": 1000.0}
    monkeypatch.setattr(_time, "monotonic", lambda: _t["v"])
    _refresh(tr, [_row("AGED", 5.00, prev_c=5.00)])
    _t["v"] += 400.0  # lampas sa 180s window
    got = _refresh(tr, [_row("AGED", 5.60, prev_c=5.00)])
    assert "AGED" not in got


def test_flag_off_is_byte_identical(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_velocity_intake_enabled", False, raising=False
    )
    tr = _UniverseTracker()
    _refresh(tr, [_row("XLAB", 9.42, prev_c=9.92)])
    got = _refresh(tr, [_row("XLAB", 10.53, prev_c=9.92)])
    assert got == set()
    assert tr.velocity_for("XLAB") is None


def test_velocity_is_kept_for_daychange_members_too():
    """Ang pangalang pumasok na sa day-change screen ay may velocity value pa
    rin — ito ang nagpapagana ng axis para sa positive-day na bumubulusok."""
    import app.services.trading.momentum_neural.ignition_loop as il

    tr = _UniverseTracker()
    orig = il.build_equity_universe
    import app.services.massive_client as mc

    orig_snap = mc.get_full_market_snapshot
    try:
        il.build_equity_universe = lambda profile, snapshot=None: ["HOTT"]
        mc.get_full_market_snapshot = lambda **kw: [_row("HOTT", 4.00, prev_c=3.00)]
        tr.refresh()
        mc.get_full_market_snapshot = lambda **kw: [_row("HOTT", 4.40, prev_c=3.00)]
        got = tr.refresh()
    finally:
        il.build_equity_universe = orig
        mc.get_full_market_snapshot = orig_snap
    assert "HOTT" in got
    v = tr.velocity_for("HOTT")
    assert v is not None and v > 9.0


# ─────────────── ang predicate: pang-apat na axis ───────────────


def test_velocity_axis_ignites_at_the_floor():
    from app.services.trading.momentum_neural.nbbo_tape import (
        _ross_threshold_crossed,
    )

    # XLAB-tulad: move% vs baseline +6.1 (ilalim ng 10% day floor), pero
    # velocity +11.8 — dapat mag-ignite na ngayon
    assert _ross_threshold_crossed(
        "XLAB", move_pct=6.1, gap_pct=6.1, price=10.53, velocity_pct=11.8
    )
    # walang velocity — dating gawi: hindi nag-i-ignite sa +6.1
    assert not _ross_threshold_crossed(
        "XLAB", move_pct=6.1, gap_pct=6.1, price=10.53
    )
    # velocity sa ilalim ng floor — hindi rin
    assert not _ross_threshold_crossed(
        "XLAB", move_pct=6.1, gap_pct=6.1, price=10.53, velocity_pct=4.0
    )


def test_velocity_axis_respects_the_band_gates():
    from app.services.trading.momentum_neural.nbbo_tape import (
        _ross_threshold_crossed,
    )

    # lampas sa price band — ang velocity ay hindi makakalusot
    assert not _ross_threshold_crossed(
        "BIGG", move_pct=2.0, gap_pct=2.0, price=45.0, velocity_pct=15.0
    )
    # manipis na $-volume — hindi rin
    assert not _ross_threshold_crossed(
        "THIN", move_pct=2.0, gap_pct=2.0, price=5.0,
        dollar_volume=200_000.0, velocity_pct=15.0,
    )


def test_negative_velocity_never_fires():
    from app.services.trading.momentum_neural.nbbo_tape import (
        _ross_threshold_crossed,
    )

    assert not _ross_threshold_crossed(
        "DUMP", move_pct=None, gap_pct=None, price=5.0, velocity_pct=-12.0
    )


# ─────────────── ang arm gate: velocity leg sa evidence ───────────────


def test_evidence_gate_velocity_leg_saves_the_xlab_class():
    """P1 ng review: ang XLAB (−2.4% day, +11.8 velocity) ay dating namamatay
    sa ross_universe_change_below_profile sa BAWAT arm route. Ngayon: pasado
    sa velocity leg — parehong 'already-moving' semantics, mas maikling horizon."""
    from app.services.trading.momentum_neural.universe import (
        ross_smallcap_profile_evidence,
    )

    sig = {
        "ticker": "XLAB", "price": 10.53, "volume": 2_000_000,
        "todays_change_perc": -2.4, "velocity_pct": 11.8,
    }
    ok, reason, debug = ross_smallcap_profile_evidence("XLAB", signal=sig)
    assert ok, (reason, debug)
    assert debug.get("change_leg") == "velocity"


def test_evidence_gate_velocity_leg_with_snapshot_change_present():
    """Kahit may snapshot row na may negatibong todaysChangePerc (ang
    pinipiling source), ang velocity leg ay dapat pumasa pa rin."""
    from app.services.trading.momentum_neural.universe import (
        ross_smallcap_profile_evidence,
    )

    sig = {"ticker": "XLAB", "price": 10.53, "volume": 2_000_000,
           "velocity_pct": 11.8}
    snap = {"ticker": "XLAB", "todaysChangePerc": -2.4,
            "day": {"o": 9.9, "v": 2_000_000}, "min": {"av": 2_000_000},
            "lastTrade": {"p": 10.53}, "prevDay": {"c": 9.92}}
    ok, reason, debug = ross_smallcap_profile_evidence(
        "XLAB", signal=sig, snapshot_row=snap
    )
    assert ok, (reason, debug)
    assert debug.get("change_leg") == "velocity"


def test_evidence_gate_velocity_below_floor_keeps_old_failure():
    from app.services.trading.momentum_neural.universe import (
        ross_smallcap_profile_evidence,
    )

    sig = {"ticker": "SLOW", "price": 5.0, "volume": 2_000_000,
           "todays_change_perc": -2.0, "velocity_pct": 4.0}
    ok, reason, _ = ross_smallcap_profile_evidence("SLOW", signal=sig)
    assert not ok
    assert reason == "ross_universe_change_below_profile"


def test_evidence_gate_missing_change_with_velocity_passes():
    """Day-1 listing: walang change data pero may velocity — pasado (dating
    ross_universe_missing_change_pct)."""
    from app.services.trading.momentum_neural.universe import (
        ross_smallcap_profile_evidence,
    )

    sig = {"ticker": "NEWI", "price": 5.0, "volume": 2_000_000,
           "velocity_pct": 9.0}
    ok, reason, debug = ross_smallcap_profile_evidence("NEWI", signal=sig)
    assert ok, (reason, debug)
    assert debug.get("change_leg") == "velocity"


def test_evidence_gate_velocity_never_bypasses_price_or_volume():
    from app.services.trading.momentum_neural.universe import (
        ross_smallcap_profile_evidence,
    )

    # walang volume — fail-closed pa rin bago pa ang change/velocity legs
    sig = {"ticker": "XLAB", "price": 10.53, "velocity_pct": 11.8}
    ok, reason, _ = ross_smallcap_profile_evidence("XLAB", signal=sig)
    assert not ok
    assert reason == "ross_universe_missing_dollar_volume"


def test_evidence_gate_passing_change_path_is_byte_identical():
    """Regression: ang pangalang pumapasa sa day-change ay hindi dumadaan sa
    velocity leg (walang change_leg key)."""
    from app.services.trading.momentum_neural.universe import (
        ross_smallcap_profile_evidence,
    )

    sig = {"ticker": "HOTT", "price": 4.4, "volume": 2_000_000,
           "todays_change_perc": 46.7, "velocity_pct": 10.0}
    ok, reason, debug = ross_smallcap_profile_evidence("HOTT", signal=sig)
    assert ok
    assert "change_leg" not in debug


# ─────────────── tapat na stamping sa scorer ───────────────


def _capture_score(monkeypatch, tracker_vel, move_pct):
    """Patakbuhin ang _score_symbol na naka-capture ang ross_signals meta."""
    import app.services.trading.momentum_neural.ignition_loop as il
    from app.services.trading.momentum_neural import pipeline as pipeline_mod

    captured = {}

    def _fake_tick(db, **kw):
        captured.update(kw.get("meta") or {})
        for k in ("ross_signals",):
            if k in kw:
                captured[k] = kw[k]
        return {"ok": True}

    monkeypatch.setattr(
        pipeline_mod, "run_momentum_neural_tick", _fake_tick, raising=False
    )
    loop = il.IgnitionScoringLoop.__new__(il.IgnitionScoringLoop)
    loop._tracker = _UniverseTracker()
    loop._inflight = set()
    loop._inflight_lock = __import__("threading").Lock()
    monkeypatch.setattr(
        il.IgnitionScoringLoop, "_bridge_arm", lambda self, s: "off",
        raising=False,
    )
    loop._score_symbol("XLAB", move_pct, 10.53, tracker_vel)
    return captured


def test_unknown_day_move_is_not_stamped(monkeypatch):
    """P2 ng review: ang pineke na todays_change_perc=0.0 ay nagbe-bench sa
    below_explosive_floor. Ngayon: hindi itina-stamp ang hindi alam."""
    cap = _capture_score(monkeypatch, tracker_vel=11.8, move_pct=None)
    sig = _dig_signal(cap)
    assert "todays_change_perc" not in sig
    assert sig.get("velocity_pct") == 11.8


def test_known_day_move_still_stamped(monkeypatch):
    cap = _capture_score(monkeypatch, tracker_vel=None, move_pct=6.1)
    sig = _dig_signal(cap)
    assert sig.get("todays_change_perc") == 6.1
    assert "velocity_pct" not in sig


def _dig_signal(captured):
    """Hanapin ang XLAB ross_signals dict saanman ito ipinasa."""
    def _walk(o):
        if isinstance(o, dict):
            if "XLAB" in o and isinstance(o["XLAB"], dict) and (
                o["XLAB"].get("source") == "ws_ignition"
            ):
                return o["XLAB"]
            for v in o.values():
                r = _walk(v)
                if r is not None:
                    return r
        return None

    sig = _walk(captured)
    assert sig is not None, f"walang ross_signals sa captured: {list(captured)[:8]}"
    return sig
