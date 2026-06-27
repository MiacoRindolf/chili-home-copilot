"""Adversarial tests for the ADAPTIVE spread-cost entry veto/derate
(app/services/trading/momentum_neural/spread_cost_veto.py).

THE TRAP (project_momentum_zero_fills_root_cause): Ross low-float movers
INHERENTLY trade wide spreads. A FLAT bps spread veto re-creates the documented
0-fills over-restriction (it rejects the explosive names the lane exists to
trade). The non-negotiable property these tests prove: a wide-but-TYPICAL
low-float spread with a good R PASSES unaffected (mult=1.0), so the gate cannot
re-introduce the 0-fills regression. Hard veto fires ONLY at the extreme (an
EXTREME outlier vs the name's OWN p90 distribution AND the cost eats more than the
documented max fraction of R).

Run:
  TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
    conda run -n chili-env pytest tests/test_momentum_adaptive_spread_cost_veto.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.services.trading.momentum_neural.spread_cost_veto import (
    adaptive_spread_cost_veto_derate,
    name_spread_percentiles,
)


# ── a tiny fake Session: returns a canned (p50,p75,p90,n) percentile row so the
# pure-logic cases don't need a live DB. The module's percentile query is the ONLY
# DB call; everything else is pure arithmetic. ───────────────────────────────────
class _FakePercentileResult:
    def __init__(self, row: Optional[tuple]) -> None:
        self._row = row

    def fetchone(self) -> Optional[tuple]:
        return self._row


class _FakeDB:
    """Fake Session.execute() that returns one canned percentile row (or raises)."""

    def __init__(self, row: Optional[tuple], *, raise_exc: bool = False) -> None:
        self._row = row
        self._raise = raise_exc

    def execute(self, *_a: Any, **_k: Any) -> _FakePercentileResult:
        if self._raise:
            raise RuntimeError("boom")
        return _FakePercentileResult(self._row)


def _derate(symbol: str, entry_price: float, spread_bps: float, stop_distance: float,
            *, p50=None, p75=None, p90=None, n=50, raise_db=False, flag=True):
    """Helper: build a fake-DB distribution row and call the gate."""
    row: Optional[tuple]
    if p50 is None:
        row = None  # insufficient history
    else:
        row = (p50, p75 if p75 is not None else p50, p90 if p90 is not None else p50, n)
    db = _FakeDB(row, raise_exc=raise_db)
    return adaptive_spread_cost_veto_derate(
        symbol=symbol, entry_price=entry_price, current_spread_bps=spread_bps,
        stop_distance=stop_distance, db=db, flag_enabled=flag,
    )


# ── 0. FLAG OFF => byte-identical pass-through (the no-op guarantee) ───────────
def test_flag_off_is_byte_identical_passthrough() -> None:
    allow, mult, reason, meta = _derate(
        "PAVS", entry_price=5.0, spread_bps=9999.0, stop_distance=0.01,
        p50=50, p75=60, p90=70, flag=False,
    )
    assert allow is True and mult == 1.0 and reason == "flag_off" and meta == {}


# ── 1. THE CRITICAL NO-0-FILLS PROOF: a wide-but-TYPICAL low-float name PASSES ─
def test_wide_but_typical_low_float_name_passes_unaffected() -> None:
    """PAVS-class: a low-float runner whose live spread (300bps) IS its normal
    spread (p50=300), with a good Ross-style R (entry $5, stop_distance $2.00 => the
    typical wide structural stop on an explosive name). cost_of_r = (0.03*5)/2.00 =
    0.075 — well inside the dead-band (engage_frac 0.5 * 0.25 cap = 0.125). anomaly_
    ratio = 1.0 (NOT wide for IT). MUST PASS at mult=1.0 — proving the gate does NOT
    over-restrict the inherently-wide Ross names (the no-0-fills guarantee)."""
    allow, mult, reason, meta = _derate(
        "PAVS", entry_price=5.0, spread_bps=300.0, stop_distance=2.00,
        p50=300, p75=340, p90=380,
    )
    assert allow is True
    assert mult == 1.0
    assert reason == "pass"
    assert meta["decision"] == "pass"
    assert meta["anomaly_ratio"] == pytest.approx(1.0, abs=0.01)


def test_wide_but_typical_with_ample_R_passes_even_at_p75() -> None:
    """A name trading right at its own p75 (slightly wide for it) but with a large R
    so the cost is in the dead-band still PASSES — never a veto, never a derate."""
    allow, mult, reason, meta = _derate(
        "RUNNER", entry_price=4.0, spread_bps=340.0, stop_distance=1.20,
        p50=300, p75=340, p90=380,  # exactly at p75
    )
    # cost_of_r = (0.034*4)/1.20 = 0.113 < 0.125 engage point -> pass; at-p75 not "above"
    assert allow is True
    assert reason == "pass"
    assert mult == 1.0


def test_typical_wide_name_with_higher_cost_only_mildly_derates() -> None:
    """A typical-for-the-name wide spread (300bps vs p50=300) but a tighter R so the
    cost climbs to 15% of R (0.15, just into the upper band): the gate ONLY mildly
    size-derates (never vetoes, never floors a normal trade) — graceful, not a cliff.
    This bounds the over-restriction: even a costlier-but-typical name keeps most size."""
    allow, mult, reason, meta = _derate(
        "PAVS2", entry_price=5.0, spread_bps=300.0, stop_distance=1.00,
        p50=300, p75=340, p90=380,
    )
    assert allow is True
    assert mult > 0.8  # mild trim only — far from the floor; the name still trades big
    assert reason == "cost_of_r"


# ── 2. ANOMALOUSLY-WIDE vs the name's OWN norm => veto or heavy derate ─────────
def test_anomalous_spread_vs_own_norm_derates_heavily() -> None:
    """Same name normally 50bps (p50=50,p90=80) but the live spread is 300bps — a
    6x its-own-median anomaly (toxic / thinning book). With a healthy R the cost is
    not extreme, so this DERATES heavily (sizes down) rather than hard-vetoing."""
    allow, mult, reason, meta = _derate(
        "TIGHTNAME", entry_price=5.0, spread_bps=300.0, stop_distance=2.0,
        p50=50, p75=70, p90=80,
    )
    # cost_of_r = (0.03*5)/2.0 = 0.075 < 0.25 -> not extreme cost -> derate, not veto
    assert allow is True
    assert mult < 1.0
    assert "anomaly_wide_for_name" in reason


def test_extreme_anomaly_AND_high_cost_hard_vetoes() -> None:
    """The ONLY hard-veto path: an EXTREME outlier vs the name's OWN p90 (300bps vs
    p90=80, well past 1.5x p90=120) AND the round-trip cost eats > max fraction of R
    (tiny R). Both conditions => allow=False."""
    allow, mult, reason, meta = _derate(
        "TOXIC", entry_price=5.0, spread_bps=300.0, stop_distance=0.30,
        p50=50, p75=70, p90=80,
    )
    # cost_of_r = (0.03*5)/0.30 = 0.50 > 0.25 AND 300 >= 80*1.5=120 -> HARD VETO
    assert allow is False
    assert meta["decision"] == "hard_veto"
    assert reason == "extreme_spread_anomaly_and_cost"


# ── 3. SPREAD EATS MOST OF R => derate (or veto if also anomalous) ────────────
def test_spread_eats_most_of_R_derates_to_floor() -> None:
    """A name whose spread is TYPICAL for it (300bps vs p50=300, NOT anomalous) but
    the trade has a tiny R ($0.30 stop) so the round-trip cost eats 50% of R. This is
    NOT an extreme anomaly (typical for the name), so it DERATES toward the floor — it
    does NOT hard-veto a name that is simply trading its normal spread."""
    allow, mult, reason, meta = _derate(
        "THINR", entry_price=5.0, spread_bps=300.0, stop_distance=0.30,
        p50=300, p75=340, p90=380,
    )
    # cost_of_r = 0.50 > 0.25, but anomaly_ratio=1.0 (typical) -> NOT extreme -> derate
    assert allow is True
    assert mult == pytest.approx(settings.chili_momentum_spread_cost_derate_floor, abs=1e-6)
    assert "cost_of_r" in reason


def test_moderate_cost_gives_partial_derate() -> None:
    """Cost-of-R in the upper band (between the engage point and the cap) => a
    graceful partial derate strictly between floor and 1.0, proving the size-down is
    smooth, not a cliff."""
    # cost_of_r = (0.015*5)/0.5 = 0.15 ; engage=0.125, cap=0.25 -> partial derate
    allow, mult, reason, meta = _derate(
        "MODNAME", entry_price=5.0, spread_bps=150.0, stop_distance=0.5,
        p50=150, p75=170, p90=190,  # typical for it -> only the cost lever acts
    )
    assert allow is True
    floor = settings.chili_momentum_spread_cost_derate_floor
    assert floor < mult < 1.0
    assert reason == "cost_of_r"


# ── 4. TIGHT spread => passes unaffected ──────────────────────────────────────
def test_tight_spread_passes_unaffected() -> None:
    """A liquid name with a tight 10bps spread and a normal R is untouched."""
    allow, mult, reason, meta = _derate(
        "LIQUID", entry_price=10.0, spread_bps=10.0, stop_distance=0.30,
        p50=12, p75=15, p90=20,
    )
    assert allow is True and mult == 1.0 and reason == "pass"


# ── 5. FAIL-OPEN: thin history / unusable inputs / DB error never block ────────
def test_insufficient_history_never_hard_vetoes() -> None:
    """No distribution (None row) => the name can NEVER be hard-vetoed (extreme
    anomaly is unreachable without a p90). Even an enormous spread only cost-derates."""
    allow, mult, reason, meta = _derate(
        "NEWNAME", entry_price=5.0, spread_bps=2000.0, stop_distance=0.30,
        p50=None,  # insufficient history
    )
    assert allow is True  # cannot hard-veto without the name's distribution
    assert meta.get("name_dist") == "insufficient_history"


def test_too_few_samples_returns_none_distribution() -> None:
    db = _FakeDB((300.0, 340.0, 380.0, 3))  # n=3 < default min_samples=8
    assert name_spread_percentiles(db, "FEW", lookback_days=20.0) is None


def test_unusable_inputs_fail_open() -> None:
    # no spread
    a, m, r, _ = _derate("X", entry_price=5.0, spread_bps=0.0, stop_distance=1.0, p50=50)
    assert a is True and m == 1.0 and r == "no_spread"
    # no stop distance
    a, m, r, _ = _derate("X", entry_price=5.0, spread_bps=100.0, stop_distance=0.0, p50=50)
    assert a is True and m == 1.0 and r == "no_stop_distance"
    # no entry price
    a, m, r, _ = _derate("X", entry_price=0.0, spread_bps=100.0, stop_distance=1.0, p50=50)
    assert a is True and m == 1.0 and r == "no_entry_price"


def test_db_error_fails_open_to_cost_only() -> None:
    """A percentile-read failure must not block: the name-relative anomaly simply
    can't be computed (no hard veto possible), and only the cost lever can derate."""
    allow, mult, reason, meta = _derate(
        "ERR", entry_price=5.0, spread_bps=100.0, stop_distance=2.0, raise_db=True,
    )
    assert allow is True  # cost_of_r=(0.01*5)/2=0.025 in the dead-band -> passes
    assert mult == 1.0
    assert reason == "pass"
    assert meta.get("name_dist") == "insufficient_history"


# ── 6. CONFIG: flag default OFF (byte-identical guarantee at the config layer) ─
def test_flag_default_is_off() -> None:
    assert settings.chili_momentum_adaptive_spread_cost_veto_enabled is False


# ── 7. INTEGRATION: the real DB percentile read off momentum_nbbo_spread_tape ─
def _ensure_table(db: Session) -> None:
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS momentum_nbbo_spread_tape ("
        " id BIGSERIAL PRIMARY KEY, symbol VARCHAR(32) NOT NULL,"
        " observed_at TIMESTAMPTZ NOT NULL DEFAULT now(), bid DOUBLE PRECISION,"
        " ask DOUBLE PRECISION, mid DOUBLE PRECISION, spread_bps DOUBLE PRECISION,"
        " day_volume DOUBLE PRECISION, source VARCHAR(24) NOT NULL DEFAULT 'massive_snapshot')"
    ))
    db.execute(text("DELETE FROM momentum_nbbo_spread_tape"))
    db.commit()


def _seed_spreads(db: Session, sym: str, spreads: list[float]) -> None:
    for s in spreads:
        db.execute(text(
            "INSERT INTO momentum_nbbo_spread_tape (symbol, observed_at, spread_bps) "
            "VALUES (:s, now() - interval '1 hour', :sp)"
        ), {"s": sym, "sp": s})
    db.commit()


def test_real_db_percentiles_and_typical_name_passes(db: Session) -> None:
    """End-to-end against the real tape: a name with a wide-but-CONSISTENT spread
    history passes when its live spread sits in its own distribution."""
    _ensure_table(db)
    # 30 rows all ~300bps -> p50~300, p90~300; a chronically-wide low-float name.
    _seed_spreads(db, "PAVS", [290, 295, 300, 305, 310] * 6)
    pct = name_spread_percentiles(db, "PAVS", lookback_days=20.0)
    assert pct is not None
    assert 290 <= pct["p50"] <= 310
    # live 300bps with a good Ross-style R ($2 stop) -> PASS (no 0-fills regression
    # on the real path: the inherently-wide name trades full size at its own norm)
    allow, mult, reason, meta = adaptive_spread_cost_veto_derate(
        symbol="PAVS", entry_price=5.0, current_spread_bps=300.0,
        stop_distance=2.0, db=db, flag_enabled=True,
    )
    assert allow is True and mult == 1.0 and reason == "pass"


def test_real_db_extreme_anomaly_vetoes(db: Session) -> None:
    """Same name on the real tape, but the live spread is an extreme outlier vs its
    own history AND eats most of a tiny R -> hard veto."""
    _ensure_table(db)
    _seed_spreads(db, "TIGHT", [45, 50, 55, 48, 52] * 6)  # p50~50, p90~55
    allow, mult, reason, meta = adaptive_spread_cost_veto_derate(
        symbol="TIGHT", entry_price=5.0, current_spread_bps=400.0,
        stop_distance=0.30, db=db, flag_enabled=True,
    )
    assert allow is False
    assert reason == "extreme_spread_anomaly_and_cost"


def test_real_db_thin_history_fails_open(db: Session) -> None:
    """Fewer than min_samples tape rows -> no distribution -> never hard-veto."""
    _ensure_table(db)
    _seed_spreads(db, "NEW", [300, 320])  # 2 rows < 8
    assert name_spread_percentiles(db, "NEW", lookback_days=20.0) is None
    allow, mult, reason, meta = adaptive_spread_cost_veto_derate(
        symbol="NEW", entry_price=5.0, current_spread_bps=2000.0,
        stop_distance=0.30, db=db, flag_enabled=True,
    )
    assert allow is True
    assert meta.get("name_dist") == "insufficient_history"
