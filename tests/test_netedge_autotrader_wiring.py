"""Phase D of evidence-fidelity (f-netedge-live-wiring) — wiring tests.

Verifies that the autotrader's parallel NetEdge shadow-call writes a
NetEdgeScoreLog row with non-null ``scan_pattern_id`` and a non-empty
``regime`` (i.e. NOT ``unknown``), and that a failure in
``net_edge_ranker.score(...)`` does NOT propagate up and block the
autotrader.

These tests exercise the small extracted helper
:func:`auto_trader._emit_netedge_shadow_score` directly. The full
``_process_one_alert`` path is covered by other autotrader tests; here
we focus on the wiring contract: write-only side effect, never raise.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.trading import BreakoutAlert, NetEdgeScoreLog, ScanPattern
from app.services.trading import auto_trader, net_edge_ranker as ner
from app.trading_brain.infrastructure.net_edge_ops_log import (
    MODE_OFF,
    MODE_SHADOW,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_netedge_state():
    """Clear NetEdge calibrator cache + diagnostic rate-limit between tests."""
    ner._CACHE.clear()
    auto_trader._NETEDGE_DIAG_LAST_EMIT_TS = 0.0
    yield
    ner._CACHE.clear()
    auto_trader._NETEDGE_DIAG_LAST_EMIT_TS = 0.0


@pytest.fixture
def shadow_mode(monkeypatch):
    monkeypatch.setattr(settings, "brain_net_edge_ranker_mode", MODE_SHADOW)
    monkeypatch.setattr(settings, "brain_net_edge_min_samples", 50)
    monkeypatch.setattr(settings, "brain_net_edge_ops_log_enabled", True)
    yield


def _mk_pattern(
    db,
    *,
    asset_class: str = "stock",
    corrected_win_rate: float | None = 0.62,
    timeframe: str = "1d",
) -> ScanPattern:
    sp = ScanPattern(
        name="netedge_wiring_pat",
        rules_json={},
        origin="user",
        asset_class=asset_class,
        timeframe=timeframe,
        corrected_win_rate=corrected_win_rate,
        corrected_trade_count=120 if corrected_win_rate is not None else None,
        lifecycle_stage="live",
    )
    db.add(sp)
    db.flush()
    return sp


def _mk_alert(
    db,
    *,
    pattern_id: int | None,
    asset_type: str = "stock",
    stop_loss: float | None = 97.0,
    target_price: float | None = 106.0,
    regime_at_alert: str | None = "risk_on",
    timeframe: str | None = "1d",
) -> BreakoutAlert:
    a = BreakoutAlert(
        ticker="AAPL",
        asset_type=asset_type,
        alert_tier="strong",
        score_at_alert=85.0,
        price_at_alert=100.0,
        stop_loss=stop_loss,
        target_price=target_price,
        scan_pattern_id=pattern_id,
        regime_at_alert=regime_at_alert,
        timeframe=timeframe,
        alerted_at=datetime.utcnow(),
        outcome="pending",
    )
    db.add(a)
    db.flush()
    return a


# ── Tests ─────────────────────────────────────────────────────────────────


def test_shadow_score_writes_row_with_non_null_pattern_and_regime(db, shadow_mode):
    """The wiring fix: scan_pattern_id and regime must be non-null on the row."""
    pat = _mk_pattern(db, asset_class="stock", corrected_win_rate=0.62)
    alert = _mk_alert(db, pattern_id=pat.id, regime_at_alert="risk_on")

    before = db.query(NetEdgeScoreLog).count()
    auto_trader._emit_netedge_shadow_score(db, alert, entry_price=100.0)
    after = db.query(NetEdgeScoreLog).count()

    assert after == before + 1
    row = db.query(NetEdgeScoreLog).order_by(NetEdgeScoreLog.id.desc()).first()
    assert row is not None
    assert row.scan_pattern_id == pat.id
    assert row.regime == "risk_on"
    assert row.ticker == "AAPL"
    assert row.asset_class == "stock"
    assert row.mode == MODE_SHADOW
    # raw_prob 0.62 maps through cold-start identity calibration.
    assert row.calibrated_prob == pytest.approx(0.62, rel=1e-6)


def test_shadow_score_routes_crypto_asset_class(db, shadow_mode):
    pat = _mk_pattern(db, asset_class="crypto", corrected_win_rate=0.55, timeframe="4h")
    alert = _mk_alert(
        db,
        pattern_id=pat.id,
        asset_type="crypto",
        regime_at_alert="risk_off",
        timeframe="4h",
    )

    auto_trader._emit_netedge_shadow_score(db, alert, entry_price=50_000.0)

    row = db.query(NetEdgeScoreLog).order_by(NetEdgeScoreLog.id.desc()).first()
    assert row is not None
    assert row.asset_class == "crypto"
    assert row.regime == "risk_off"
    assert row.scan_pattern_id == pat.id


def test_shadow_score_routes_option_asset_alias_to_options_bucket(monkeypatch):
    pat = SimpleNamespace(
        id=123,
        corrected_win_rate=0.57,
        corrected_trade_count=50,
        win_rate=None,
        trade_count=None,
    )

    class _FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def one_or_none(self):
            return pat

    class _FakeDb:
        def query(self, *_args, **_kwargs):
            return _FakeQuery()

    alert = SimpleNamespace(
        ticker="SPY",
        asset_type="robinhood_options",
        stop_loss=1.0,
        target_price=2.0,
        scan_pattern_id=pat.id,
        regime_at_alert="risk_on",
        timeframe="1h",
        direction="long",
    )
    captured = {}

    monkeypatch.setattr(ner, "mode_is_active", lambda: True)
    monkeypatch.setattr(
        ner,
        "score",
        lambda _db, ctx: captured.setdefault("ctx", ctx),
    )

    auto_trader._emit_netedge_shadow_score(_FakeDb(), alert, entry_price=1.25)

    ctx = captured["ctx"]
    assert ctx.asset_class == "options"
    assert ctx.scan_pattern_id == 123
    assert ctx.regime == "risk_on"
    assert ctx.raw_prob == pytest.approx(0.57)


def test_shadow_score_never_raises_when_scorer_blows_up(db, shadow_mode, monkeypatch):
    """Even if net_edge_ranker.score raises, the helper MUST swallow it."""
    pat = _mk_pattern(db)
    alert = _mk_alert(db, pattern_id=pat.id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated scorer failure")

    monkeypatch.setattr(ner, "score", _boom)

    # The contract: never raises. Failure inside score is logged at DEBUG and the
    # caller (autotrader) proceeds to broker placement.
    auto_trader._emit_netedge_shadow_score(db, alert, entry_price=100.0)


def test_shadow_score_is_noop_when_mode_off(db, monkeypatch):
    monkeypatch.setattr(settings, "brain_net_edge_ranker_mode", MODE_OFF)
    pat = _mk_pattern(db)
    alert = _mk_alert(db, pattern_id=pat.id)

    before = db.query(NetEdgeScoreLog).count()
    auto_trader._emit_netedge_shadow_score(db, alert, entry_price=100.0)
    after = db.query(NetEdgeScoreLog).count()
    assert after == before  # mode_is_active() == False -> no write


def test_shadow_score_skipped_when_pattern_has_no_corrected_win_rate(db, shadow_mode):
    """Without a usable raw_prob we don't even build a context — no row written."""
    pat = _mk_pattern(db, corrected_win_rate=None)
    # Wipe the legacy fallback too: the pattern has none.
    pat.win_rate = None
    db.flush()
    alert = _mk_alert(db, pattern_id=pat.id)

    before = db.query(NetEdgeScoreLog).count()
    auto_trader._emit_netedge_shadow_score(db, alert, entry_price=100.0)
    after = db.query(NetEdgeScoreLog).count()
    assert after == before


def test_shadow_score_falls_back_to_raw_realized_win_rate_when_corrected_null(db, shadow_mode):
    """Realized-only contract (PR #366 / ff9cc05): corrected_* preferred, raw_realized_*
    is the valid fallback. The conflated legacy ``win_rate`` is NEVER used for live
    decisions (get_realized_pattern_stats), so a present legacy value must be ignored."""
    pat = _mk_pattern(db, corrected_win_rate=None)
    pat.raw_realized_win_rate = 0.58
    pat.raw_realized_trade_count = 120
    pat.win_rate = 0.99  # legacy must be ignored — never seeds raw_prob
    db.flush()
    alert = _mk_alert(db, pattern_id=pat.id, regime_at_alert="range")

    auto_trader._emit_netedge_shadow_score(db, alert, entry_price=100.0)

    row = db.query(NetEdgeScoreLog).order_by(NetEdgeScoreLog.id.desc()).first()
    assert row is not None
    assert row.scan_pattern_id == pat.id
    assert row.calibrated_prob == pytest.approx(0.58, rel=1e-6)


def test_regime_diagnostic_warns_when_majority_unknown(db, shadow_mode, caplog):
    """If >50% of recent NetEdge rows have unknown/empty regime, emit a WARNING."""
    import logging

    # Seed 12 rows: 10 unknown + 2 risk_on. >50% unknown -> warning expected.
    pat = _mk_pattern(db)
    base_ctx_kwargs = dict(
        ticker="X", asset_class="stock", scan_pattern_id=pat.id,
        raw_prob=0.55, entry_price=100.0, stop_price=97.0, target_price=104.0,
    )
    for _ in range(10):
        ner.score(db, ner.NetEdgeSignalContext(regime=None, **base_ctx_kwargs))
    for _ in range(2):
        ner.score(db, ner.NetEdgeSignalContext(regime="risk_on", **base_ctx_kwargs))

    caplog.set_level(logging.WARNING, logger="app.services.trading.auto_trader")
    auto_trader._maybe_emit_regime_diagnostic(db)

    assert any(
        "regime-snapshot diagnostic" in rec.getMessage() for rec in caplog.records
    ), "Expected regime-snapshot diagnostic WARNING when >50% rows are unknown"


def test_regime_diagnostic_silent_when_regimes_are_populated(db, shadow_mode, caplog):
    """No warning when most rows have a real regime label."""
    import logging

    pat = _mk_pattern(db)
    base_ctx_kwargs = dict(
        ticker="X", asset_class="stock", scan_pattern_id=pat.id,
        raw_prob=0.55, entry_price=100.0, stop_price=97.0, target_price=104.0,
    )
    for _ in range(11):
        ner.score(db, ner.NetEdgeSignalContext(regime="risk_on", **base_ctx_kwargs))

    caplog.set_level(logging.WARNING, logger="app.services.trading.auto_trader")
    auto_trader._maybe_emit_regime_diagnostic(db)

    assert not any(
        "regime-snapshot diagnostic" in rec.getMessage() for rec in caplog.records
    )


def test_regime_diagnostic_rate_limited(db, shadow_mode, caplog):
    """After emitting once, the diagnostic stays quiet until the cooldown lapses."""
    import logging

    pat = _mk_pattern(db)
    base_ctx_kwargs = dict(
        ticker="X", asset_class="stock", scan_pattern_id=pat.id,
        raw_prob=0.55, entry_price=100.0, stop_price=97.0, target_price=104.0,
    )
    for _ in range(10):
        ner.score(db, ner.NetEdgeSignalContext(regime=None, **base_ctx_kwargs))

    caplog.set_level(logging.WARNING, logger="app.services.trading.auto_trader")
    auto_trader._maybe_emit_regime_diagnostic(db)
    first_count = sum(
        1 for r in caplog.records if "regime-snapshot diagnostic" in r.getMessage()
    )
    auto_trader._maybe_emit_regime_diagnostic(db)
    second_count = sum(
        1 for r in caplog.records if "regime-snapshot diagnostic" in r.getMessage()
    )
    assert first_count == 1
    assert second_count == 1  # second call within cooldown -> no new warning
