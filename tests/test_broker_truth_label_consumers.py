"""Broker-truth label routing of the REMAINING momentum learning consumers (mig309).

Companion to test_broker_truth_reconcile.py. That file proves the accessor +
WRITE pass; this file proves the OTHER learning/decision readers were routed
through ``authoritative_label_for_outcome`` with the same
flag-OFF-byte-identical / flag-ON-skip-unreconciled contract:

  * family_regime_stats.aggregate_family_regime_performance  (arming prefilter gate)
  * ab_test.compare_peer_variants                            (A/B winner decision)
  * viability._symbol_family_memory_adjust                   (symbol×family viability nudge)

And that the read-model brief (feedback_query._outcome_brief) SURFACES the broker
label additively (flag-independent) WITHOUT dropping rows — a desk read-model must
keep the lane-vs-broker divergence visible, never hide it.

NON-NEGOTIABLE properties:
(a) PARITY  — flag OFF => every consumer is byte-identical to the legacy
              return_bps path (paper + live both counted, no row dropped).
(b) SWITCH  — flag ON => reconciled-live rows use the broker-true return_bps and
              unreconciled rows (incl. never-reconciled / paper) are EXCLUDED,
              which can flip a win-rate / mean / A-vs-B winner / viability sign.
(c) AUDIT   — the read-model brief always carries the raw broker_* columns
              alongside the untouched legacy fields, flag state irrelevant.

Uses the truncating ``db`` fixture (TEST_DATABASE_URL, _test DB). No broker calls —
the broker_* label columns are seeded directly to simulate a completed WRITE pass.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app.config import settings
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import ab_test
from app.services.trading.momentum_neural import family_regime_stats as frs
from app.services.trading.momentum_neural import feedback_query
from app.services.trading.momentum_neural import viability

_seq = 0


def _mk_outcome(
    db,
    *,
    family="reclaim",
    symbol="CAST",
    mode="live",
    execution_family="robinhood_spot",
    legacy_bps=50.0,
    legacy_pnl=10.0,
    broker_status=None,
    broker_bps=None,
    broker_pnl=None,
    broker_win=None,
    broker_divergence=None,
    vol="normal",
    session_label="us",
    variant_id=None,
):
    """Seed one variant (unless variant_id given) + outcome with controllable legacy
    AND broker_* label fields. broker_status=None => never reconciled (flag-ON excluded)."""
    global _seq
    _seq += 1
    if variant_id is None:
        v = MomentumStrategyVariant(
            family=family, variant_key=f"lbl_{_seq}", label="label test", params_json={},
        )
        db.add(v)
        db.flush()
        variant_id = v.id
    # session_id is a non-null unique FK to trading_automation_sessions — seed a parent.
    sess = TradingAutomationSession(
        user_id=None, venue="test", execution_family=execution_family, mode=mode,
        symbol=symbol, variant_id=variant_id, state="live_finished",
        risk_snapshot_json={}, correlation_id=f"corr-lbl-{_seq}",
    )
    db.add(sess)
    db.flush()
    o = MomentumAutomationOutcome(
        session_id=sess.id, user_id=None, variant_id=variant_id, symbol=symbol,
        mode=mode, execution_family=execution_family, terminal_state="live_finished",
        terminal_at=_dt.datetime.utcnow(), outcome_class="success",
        realized_pnl_usd=legacy_pnl, return_bps=legacy_bps,
        entry_regime_snapshot_json={"volatility_regime": vol, "session_label": session_label},
        broker_recon_status=broker_status,
        broker_return_bps=broker_bps,
        broker_realized_pnl_usd=broker_pnl,
        broker_win=broker_win,
        broker_divergence_usd=broker_divergence,
    )
    db.add(o)
    db.flush()
    return o, variant_id


@pytest.fixture(autouse=True)
def _label_flag_off():
    orig = settings.chili_momentum_broker_truth_label_enabled
    settings.chili_momentum_broker_truth_label_enabled = False
    yield
    settings.chili_momentum_broker_truth_label_enabled = orig


# ── family_regime_stats.aggregate_family_regime_performance ───────────────────
def _seed_family_regime(db):
    # 3 rows, same family×vol×session. 2 legacy-wins (50, 30) + 1 legacy-loss (-40);
    # the +30 row is NEVER reconciled (broker_status=None) — flag-ON it drops out.
    _mk_outcome(db, legacy_bps=50.0, broker_status="reconciled", broker_bps=80.0, broker_win=True)
    _mk_outcome(db, legacy_bps=-40.0, broker_status="reconciled", broker_bps=-60.0, broker_win=False)
    _mk_outcome(db, legacy_bps=30.0, broker_status=None)  # unreconciled / paper-like
    db.commit()


def test_family_regime_flag_off_byte_identical(db):
    _seed_family_regime(db)
    rows = frs.aggregate_family_regime_performance(db, days=90)
    assert len(rows) == 1
    r = rows[0]
    assert r["n"] == 3  # all three counted (legacy path)
    assert r["win_rate"] == pytest.approx(2 / 3)  # +50, +30 win; -40 loss
    assert r["mean_return_bps"] == pytest.approx((50.0 - 40.0 + 30.0) / 3.0)


def test_family_regime_flag_on_uses_broker_truth_and_excludes_unreconciled(db):
    _seed_family_regime(db)
    settings.chili_momentum_broker_truth_label_enabled = True
    rows = frs.aggregate_family_regime_performance(db, days=90)
    assert len(rows) == 1
    r = rows[0]
    assert r["n"] == 2  # the unreconciled +30 row dropped
    assert r["win_rate"] == pytest.approx(0.5)  # +80 win, -60 loss
    assert r["mean_return_bps"] == pytest.approx((80.0 - 60.0) / 2.0)  # broker-true, not legacy


# ── ab_test.compare_peer_variants ─────────────────────────────────────────────
def _seed_ab(db):
    # Variant A: legacy mean +10, broker mean +100. Variant B: legacy mean +200,
    # broker mean +20. => legacy picks B, broker truth picks A (winner FLIPS).
    _, a = _mk_outcome(db, family="A", legacy_bps=10.0, broker_status="reconciled", broker_bps=100.0)
    _mk_outcome(db, family="A", legacy_bps=10.0, broker_status="reconciled", broker_bps=100.0, variant_id=a)
    _mk_outcome(db, family="A", legacy_bps=10.0, broker_status="reconciled", broker_bps=100.0, variant_id=a)
    # A 4th A-row that is NEVER reconciled — counts flag-OFF, drops flag-ON.
    _mk_outcome(db, family="A", legacy_bps=10.0, broker_status=None, variant_id=a)
    _, b = _mk_outcome(db, family="B", legacy_bps=200.0, broker_status="reconciled", broker_bps=20.0)
    _mk_outcome(db, family="B", legacy_bps=200.0, broker_status="reconciled", broker_bps=20.0, variant_id=b)
    _mk_outcome(db, family="B", legacy_bps=200.0, broker_status="reconciled", broker_bps=20.0, variant_id=b)
    db.commit()
    return a, b


def test_ab_flag_off_byte_identical(db):
    a, b = _seed_ab(db)
    out = ab_test.compare_peer_variants(db, variant_a_id=a, variant_b_id=b, min_sessions=2)
    assert out["a_n"] == 4 and out["b_n"] == 3  # all rows counted incl. unreconciled
    assert out["a_mean_bps"] == pytest.approx(10.0)
    assert out["b_mean_bps"] == pytest.approx(200.0)
    assert out["winner"] == "b"


def test_ab_flag_on_flips_winner_and_drops_unreconciled(db):
    a, b = _seed_ab(db)
    settings.chili_momentum_broker_truth_label_enabled = True
    out = ab_test.compare_peer_variants(db, variant_a_id=a, variant_b_id=b, min_sessions=2)
    assert out["a_n"] == 3  # the unreconciled A row dropped
    assert out["b_n"] == 3
    assert out["a_mean_bps"] == pytest.approx(100.0)  # broker-true
    assert out["b_mean_bps"] == pytest.approx(20.0)
    assert out["winner"] == "a"  # winner flipped vs legacy


# ── viability._symbol_family_memory_adjust ────────────────────────────────────
def _seed_viability(db):
    # legacy track record => 3 wins / 5 (wr 0.6, n>=5) => positive BOOST.
    # broker truth (only 4 reconciled) => 1 win / 4 (wr 0.25) => negative PENALTY.
    _mk_outcome(db, symbol="CAST", family="reclaim", legacy_bps=50.0, broker_status="reconciled", broker_bps=-10.0)
    _mk_outcome(db, symbol="CAST", family="reclaim", legacy_bps=50.0, broker_status="reconciled", broker_bps=-10.0)
    _mk_outcome(db, symbol="CAST", family="reclaim", legacy_bps=50.0, broker_status="reconciled", broker_bps=-10.0)
    _mk_outcome(db, symbol="CAST", family="reclaim", legacy_bps=-10.0, broker_status="reconciled", broker_bps=50.0)
    _mk_outcome(db, symbol="CAST", family="reclaim", legacy_bps=-10.0, broker_status=None)  # unreconciled
    db.commit()


def test_viability_memory_flag_off_byte_identical_boost(db):
    _seed_viability(db)
    adj = viability._symbol_family_memory_adjust(db, "CAST", "reclaim")
    # n=5, wr=0.6>0.55 => boost = min(0.08, 0.05*(0.6-0.55)) = 0.0025
    assert adj == pytest.approx(min(0.08, 0.05 * (0.6 - 0.55)))
    assert adj > 0.0


def test_viability_memory_flag_on_flips_to_penalty(db):
    _seed_viability(db)
    settings.chili_momentum_broker_truth_label_enabled = True
    adj = viability._symbol_family_memory_adjust(db, "CAST", "reclaim")
    # broker truth: 4 reconciled, wr=0.25<0.5 => penalty = -0.1*(0.5-0.25) = -0.025
    assert adj == pytest.approx(-max(0.0, 0.1 * (0.5 - 0.25)))
    assert adj < 0.0


# ── feedback_query._outcome_brief — additive broker audit, never hides ────────
def test_outcome_brief_surfaces_broker_label_additively(db):
    o, _ = _mk_outcome(
        db, legacy_bps=50.0, legacy_pnl=4.0,
        broker_status="reconciled", broker_bps=65.0, broker_pnl=6.5,
        broker_win=True, broker_divergence=2.5,
    )
    db.commit()
    brief = feedback_query._outcome_brief(o)
    # legacy fields untouched (the drift baseline stays visible)
    assert brief["realized_pnl_usd"] == pytest.approx(4.0)
    assert brief["return_bps"] == pytest.approx(50.0)
    # broker-truth label surfaced alongside, flag-INDEPENDENT (flag is OFF here)
    assert settings.chili_momentum_broker_truth_label_enabled is False
    assert brief["broker_recon_status"] == "reconciled"
    assert brief["broker_realized_pnl_usd"] == pytest.approx(6.5)
    assert brief["broker_return_bps"] == pytest.approx(65.0)
    assert brief["broker_divergence_usd"] == pytest.approx(2.5)


def test_outcome_brief_keeps_unreconciled_row_visible(db):
    # A read-model must NOT drop unreconciled rows (that would hide the divergence).
    o, _ = _mk_outcome(db, legacy_bps=30.0, legacy_pnl=3.0, broker_status=None)
    db.commit()
    brief = feedback_query._outcome_brief(o)
    assert brief["return_bps"] == pytest.approx(30.0)  # still present
    assert brief["broker_recon_status"] is None  # honestly shown as unreconciled
