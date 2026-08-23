"""ROSS TOP-GAINER CONCENTRATION (2026-08-23) — arm-slot doctrine tests.

Ross 08-20 recap: momentum setups work ONLY on the day's top-2/3 leading gainers;
dispersed-attention names lose (the SDOT/COIW midday churn class). Outside premarket,
NEW equity live arms concentrate on the top-N market-wide %-gainers, with two
starvation guards (CLRO lesson / #1036): the board TOP-2 always pass, and a
structural A-setup at/above the board's p90 viability bypasses membership.

Pure unit tests — no DB fixture (module-level helpers on stub rows).
"""
from __future__ import annotations

from datetime import datetime, timezone

import app.services.trading.momentum_neural.auto_arm as aa
from app.config import settings


class _Row:
    """Minimal viability-row stub (symbol / viability_score / execution_readiness_json)."""

    def __init__(self, symbol: str, viability: float = 0.5, extra: dict | None = None):
        self.symbol = symbol
        self.viability_score = viability
        self.execution_readiness_json = {"extra": extra} if extra is not None else {}
        self.variant_id = 1


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# 2026-08-20 was a regular Thursday session.
_PREMARKET = _utc(2026, 8, 20, 12, 30)  # 08:30 ET
_REGULAR = _utc(2026, 8, 20, 14, 30)    # 10:30 ET (the midday churn window)


# ---------------------------------------------------------------- knob + clock

def test_knob_defaults_on_at_3():
    """LIVE+ON: the shipped default concentrates on the top-3 (no dark flag)."""
    assert settings.chili_momentum_top_gainer_concentration_n == 3
    assert aa._top_gainer_concentration_n() == 3


def test_knob_zero_is_kill_switch(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_top_gainer_concentration_n", 0, raising=False)
    assert aa._top_gainer_concentration_n() == 0
    assert aa._top_gainer_concentration_active(now=_REGULAR) is False


def test_knob_negative_clamps_to_zero(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_top_gainer_concentration_n", -2, raising=False)
    assert aa._top_gainer_concentration_n() == 0


def test_active_outside_premarket_only():
    """Premarket (the measured profitable window) is exempt; regular session gates."""
    assert aa._top_gainer_concentration_active(now=_PREMARKET) is False
    assert aa._top_gainer_concentration_active(now=_REGULAR) is True


# ---------------------------------------------------------- membership sources

def test_top_gainers_from_snapshot_ranked_by_change():
    snapshot = {
        "AAA": {"todays_change_perc": 120.0},
        "BBB": {"todays_change_perc": 80.0},
        "CCC": {"todays_change_perc": 30.0},
        "DDD": {"todays_change_perc": -40.0},  # a dump is not a gainer
        "EEE-USD": {"todays_change_perc": 500.0},  # crypto excluded (equity doctrine)
    }
    top, source = aa._market_top_gainers([], n=2, snapshot_rows=snapshot)
    assert source == "snapshot"
    assert top == frozenset({"AAA", "BBB"})


def _own_sig_row(symbol: str, change_pct: float, viability: float = 0.5) -> _Row:
    """A row shaped like production persistence: ross_signals SUBSETTED to the row's
    own symbol (persistence._row_execution_readiness)."""
    return _Row(
        symbol,
        viability=viability,
        extra={"ross_signals": {symbol.upper(): {"daily_change_pct": change_pct, "vol_ratio": 20.0}}},
    )


def test_top_gainers_from_board_signals_fallback():
    """No snapshot (e.g. the displacement hook) -> each row's OWN persisted signal is
    aggregated ACROSS the board and ranked by the same momentum pillar pipeline uses."""
    rows = [
        _own_sig_row("SDOT", 12.0),
        _own_sig_row("JXG", 150.0),
        _own_sig_row("COIW", 9.0),
    ]
    top, source = aa._market_top_gainers(rows, n=2, snapshot_rows=None)
    assert source == "board_signals"
    assert top == frozenset({"JXG", "SDOT"})


def test_single_row_board_never_yields_one_name_lock():
    """REGRESSION (persistence subsets ross_signals to the row's own symbol): a 1-row
    board must NOT produce a 1-name top set (accidental leader-only lock) — it falls
    through to the membership meta / unavailable (fail-open)."""
    top, source = aa._market_top_gainers([_own_sig_row("NINE", 40.0)], n=3, snapshot_rows=None)
    assert top is None and source == "unavailable"


def test_top_gainers_membership_meta_last_resort():
    rows = [_Row("XYZ", extra={"top_market_gainers": ["aaa", "BBB"]})]
    top, source = aa._market_top_gainers(rows, n=2, snapshot_rows=None)
    assert source == "membership_meta"
    assert top == frozenset({"AAA", "BBB"})


def test_top_gainers_unavailable_fails_open():
    top, source = aa._market_top_gainers([_Row("XYZ")], n=3, snapshot_rows=None)
    assert top is None and source == "unavailable"
    blocked, reason = aa._top_gainer_concentration_blocks(
        _Row("XYZ"), top_gainers=None, exempt_syms=frozenset(), board_p90_viability=0.9
    )
    assert blocked is False and reason == "unknown_top_gainers_fail_open"


def test_top_gainers_disabled_n_zero():
    assert aa._market_top_gainers([], n=0, snapshot_rows={"AAA": {"todays_change_perc": 9}}) == (
        None,
        "disabled",
    )


# ------------------------------------------------------------- gate decisions

_TOP = frozenset({"JXG", "HUIZ", "SGLY"})


def test_member_passes():
    blocked, reason = aa._top_gainer_concentration_blocks(
        _Row("JXG"), top_gainers=_TOP, exempt_syms=frozenset(), board_p90_viability=0.9
    )
    assert blocked is False and reason == "top_gainer"


def test_non_member_blocked():
    """The SDOT/COIW class: mid-pack bare-rel_vol name outside the top gainers."""
    blocked, reason = aa._top_gainer_concentration_blocks(
        _Row("SDOT", viability=0.6),
        top_gainers=_TOP,
        exempt_syms=frozenset(),
        board_p90_viability=0.9,
    )
    assert blocked is True and reason == "not_top_gainer"


def test_board_top2_never_starved():
    """CONCENTRATION, not starvation (#1036 / CLRO lesson): the hoisted leader and the
    armed-first it displaced (board TOP-2 via _board_exempt_syms) always pass, even
    when the market-wide gainer set disagrees (e.g. a WILDCARD-regime hoist)."""
    cands = [_Row("RDAC", viability=0.55), _Row("YJ", viability=0.80), _Row("SDOT")]
    exempt = aa._board_exempt_syms(cands)
    assert exempt == frozenset({"RDAC", "YJ"})
    for sym in ("RDAC", "YJ"):
        blocked, reason = aa._top_gainer_concentration_blocks(
            _Row(sym), top_gainers=_TOP, exempt_syms=exempt, board_p90_viability=0.99
        )
        assert blocked is False and reason == "board_top_exempt"


def test_structural_p90_bypass(monkeypatch):
    """A structural A-setup (persisted Ross/5-Pillars shape evidence) at/above the
    board's p90 viability arms even outside the top-N."""
    monkeypatch.setattr(aa, "_candidate_ross_tick_evidence", lambda c: (True, "tick_first_pullback_watch", {}))
    blocked, reason = aa._top_gainer_concentration_blocks(
        _Row("CANF", viability=0.95),
        top_gainers=_TOP,
        exempt_syms=frozenset(),
        board_p90_viability=0.90,
    )
    assert blocked is False and reason == "structural_p90_bypass"


def test_structural_below_p90_still_blocked(monkeypatch):
    monkeypatch.setattr(aa, "_candidate_ross_tick_evidence", lambda c: (True, "tick_first_pullback_watch", {}))
    blocked, reason = aa._top_gainer_concentration_blocks(
        _Row("CANF", viability=0.50),
        top_gainers=_TOP,
        exempt_syms=frozenset(),
        board_p90_viability=0.90,
    )
    assert blocked is True and reason == "not_top_gainer"


def test_high_viability_without_structure_blocked(monkeypatch):
    """Bare rel_vol / high score without shape evidence is exactly the churn class the
    doctrine removes — viability alone never bypasses."""
    monkeypatch.setattr(aa, "_candidate_ross_tick_evidence", lambda c: (False, "no_evidence", {}))
    blocked, reason = aa._top_gainer_concentration_blocks(
        _Row("COIW", viability=0.99),
        top_gainers=_TOP,
        exempt_syms=frozenset(),
        board_p90_viability=0.50,
    )
    assert blocked is True and reason == "not_top_gainer"


def test_board_p90():
    rows = [_Row(f"S{i}", viability=v) for i, v in enumerate([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])]
    p90 = aa._board_viability_p90(rows)
    assert p90 is not None and 0.9 <= p90 <= 1.0
    assert aa._board_viability_p90([]) is None
