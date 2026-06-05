"""Realized-only stat reads on decision paths (legacy-conflation audit fixes).

The legacy ScanPattern columns (win_rate / avg_return_pct / trade_count) are
overwritten by mining + backtest writers with no provenance, so a decision must
never treat them as realized. ``get_realized_pattern_stats`` is the realized-only
accessor (corrected_* -> raw_realized_* -> missing, NEVER legacy), and the live
consumers route through it.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.models.core import User
from app.models.trading import ScanPattern


def _pat(**kw):
    base = dict(
        corrected_trade_count=None, corrected_win_rate=None, corrected_avg_return_pct=None,
        raw_realized_trade_count=None, raw_realized_win_rate=None, raw_realized_avg_return_pct=None,
        win_rate=None, avg_return_pct=None, trade_count=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── accessor: corrected -> raw_realized -> missing, never legacy ──────────

def test_realized_accessor_prefers_corrected():
    from app.services.trading.pattern_stats_accessor import get_realized_pattern_stats
    s = get_realized_pattern_stats(_pat(
        corrected_win_rate=0.7, corrected_trade_count=9, corrected_avg_return_pct=2.0,
        raw_realized_win_rate=0.6, win_rate=0.3,
    ))
    assert s.win_rate == 0.7
    assert s.source_win_rate == "corrected"


def test_realized_accessor_falls_back_to_raw_realized_not_legacy():
    from app.services.trading.pattern_stats_accessor import get_realized_pattern_stats
    s = get_realized_pattern_stats(_pat(
        raw_realized_win_rate=0.6, raw_realized_trade_count=12, raw_realized_avg_return_pct=2.66,
        win_rate=0.9, avg_return_pct=-5.0, trade_count=7,  # legacy must be ignored
    ))
    assert s.win_rate == 0.6
    assert s.avg_return_pct == 2.66
    assert s.trade_count == 12
    assert s.source_win_rate == "raw_realized"
    assert s.source_avg_return_pct == "raw_realized"


def test_realized_accessor_never_returns_legacy():
    from app.services.trading.pattern_stats_accessor import get_realized_pattern_stats
    # Only legacy populated (e.g. a backtest-only pattern) -> missing, NOT legacy.
    s = get_realized_pattern_stats(_pat(win_rate=0.95, avg_return_pct=9.0, trade_count=50))
    assert s.win_rate is None
    assert s.avg_return_pct is None
    assert s.trade_count is None
    assert s.source_win_rate == "missing"


def test_corrected_accessor_still_allows_legacy_for_display():
    # The display accessor keeps the legacy fallback (unchanged contract).
    from app.services.trading.pattern_stats_accessor import get_corrected_pattern_stats
    s = get_corrected_pattern_stats(_pat(win_rate=0.42))
    assert s.win_rate == 0.42
    assert s.source_win_rate == "legacy"


# ── live_drift research baseline: no legacy fallback (#H) ─────────────────

def test_baseline_research_expectancy_ignores_legacy():
    from app.services.trading.live_drift import _baseline_research_expectancy_pct
    # OOS absent, legacy avg_return_pct present -> must NOT use legacy (masks drift).
    assert _baseline_research_expectancy_pct(_pat(oos_avg_return_pct=None, avg_return_pct=-5.0)) is None
    # OOS present -> used.
    assert _baseline_research_expectancy_pct(_pat(oos_avg_return_pct=3.2, avg_return_pct=-5.0)) == 3.2


# ── evolve_patterns deactivation: realized-only spare (#D) ────────────────

def _evolve_pat(db, name, **kw):
    p = ScanPattern(name=name, timeframe="1d", rules_json={}, origin="mined", active=True)
    p.parent_id = None
    p.evidence_count = 5
    p.confidence = 0.1  # < min_confidence 0.2
    for k, v in kw.items():
        setattr(p, k, v)
    db.add(p)
    db.flush()
    return p


def test_evolve_patterns_deactivates_on_legacy_only_winrate(db):
    db.add(User(name="evo"))
    # legacy backtest WR 0.6 but NO clean realized -> must deactivate (no reprieve).
    p_legacy = _evolve_pat(db, "legacy_wr", win_rate=0.6)
    # clean corrected WR 0.6 -> genuine realized performance spares it.
    p_real = _evolve_pat(db, "real_wr", win_rate=0.6, corrected_win_rate=0.6, corrected_trade_count=8)
    db.commit()

    from app.services.trading.learning import evolve_patterns
    evolve_patterns(db)
    db.refresh(p_legacy)
    db.refresh(p_real)

    assert p_legacy.active is False, "backtest-only win_rate must not spare a low-confidence pattern"
    assert p_real.active is True, "clean realized win_rate must spare it"
