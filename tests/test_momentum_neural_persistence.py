"""Phase 2: neural momentum durable tables (PostgreSQL + migrations)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSessionBinding,
    TradingAutomationSimulatedFill,
)
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.persistence import (
    append_trading_automation_event,
    append_trading_automation_simulated_fill,
    build_runtime_snapshot_values,
    create_trading_automation_session,
    default_session_binding,
    ensure_momentum_strategy_variants,
    persist_neural_momentum_tick,
    VIABILITY_PERSISTENCE_LOCK_KEY,
    _strategy_variants_by_key,
    upsert_trading_automation_runtime_snapshot,
    upsert_trading_automation_session_binding,
)
from app.services.trading.momentum_neural.pipeline import _hydrate_recent_ross_evidence
from app.services.trading.momentum_neural.pipeline import run_momentum_neural_tick
from app.services.trading.momentum_neural.viability import score_viability
from app.services.trading.momentum_neural.variants import get_family


class _FakeQuery:
    def __init__(self, rows: list[MomentumStrategyVariant]) -> None:
        self.rows = rows
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def all(self) -> list[MomentumStrategyVariant]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[MomentumStrategyVariant]) -> None:
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None

    def query(self, model: object) -> _FakeQuery:
        assert model is MomentumStrategyVariant
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def test_strategy_variants_by_key_batches_registry_lookup() -> None:
    row = MomentumStrategyVariant(family="impulse_breakout", variant_key="impulse_breakout", version=1)
    db = _FakeSession([row])

    result = _strategy_variants_by_key(
        db,  # type: ignore[arg-type]
        [
            SimpleNamespace(family_id="impulse_breakout", version=1),
            SimpleNamespace(family_id="pullback_reversal", version=2),
        ],
    )

    assert result[("impulse_breakout", "impulse_breakout", 1)] is row
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_strategy_variants_by_key_skips_empty_registry_lookup() -> None:
    db = _FakeSession([])

    assert _strategy_variants_by_key(db, []) == {}  # type: ignore[arg-type]
    assert db.query_calls == 0


def test_models_and_migration_tables_exist(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    n = db.query(MomentumStrategyVariant).count()
    assert n >= 10


def test_ensure_variants_idempotent(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    a = db.query(MomentumStrategyVariant).count()
    ensure_momentum_strategy_variants(db)
    db.commit()
    b = db.query(MomentumStrategyVariant).count()
    assert a == b


def test_viability_upsert_updates_row(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    fam = get_family("impulse_breakout")
    assert fam is not None
    ctx = build_momentum_regime_context(
        now=datetime(2026, 4, 7, 16, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={"spread_regime": "normal"},
    )
    feats = ExecutionReadinessFeatures(spread_bps=5.0)
    vr = score_viability("SOL-USD", fam, ctx, feats)
    row = vr.to_public_dict()
    row["label"] = fam.label
    row["entry_style"] = fam.entry_style
    row["default_stop_logic"] = fam.default_stop_logic
    row["default_exit_logic"] = fam.default_exit_logic

    n1 = persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="corr-a",
        source_node_id="nm_momentum_crypto_intel",
    )
    assert n1 == 1
    db.commit()

    r1 = db.query(MomentumSymbolViability).filter(MomentumSymbolViability.symbol == "SOL-USD").one()
    id1 = r1.id
    assert r1.correlation_id == "corr-a"
    assert r1.source_node_id == "nm_momentum_crypto_intel"
    assert r1.paper_eligible is True
    assert r1.execution_readiness_json.get("spread_bps") == 5.0

    row2 = dict(row)
    row2["viability"] = 0.99
    persist_neural_momentum_tick(
        db,
        row_dicts=[row2],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="corr-b",
        source_node_id="nm_momentum_crypto_intel",
    )
    db.commit()

    r2 = db.query(MomentumSymbolViability).filter(MomentumSymbolViability.symbol == "SOL-USD").one()
    assert r2.id == id1
    assert r2.viability_score == 0.99
    assert r2.correlation_id == "corr-b"
    assert "rationale" in (r2.explain_json or {})


def test_persist_viability_uses_deterministic_lock_order(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    ctx = build_momentum_regime_context(
        now=datetime(2026, 4, 7, 16, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={"spread_regime": "normal"},
    )
    feats = ExecutionReadinessFeatures(spread_bps=5.0)
    rows = [
        {
            "symbol": "ZZZ",
            "family_id": "impulse_breakout",
            "family_version": 1,
            "viability": 0.4,
            "paper_eligible": True,
            "live_eligible": False,
        },
        {
            "symbol": "AAA",
            "family_id": "impulse_breakout",
            "family_version": 1,
            "viability": 0.6,
            "paper_eligible": True,
            "live_eligible": False,
        },
        {
            "symbol": "BEZ",
            "family_id": "impulse_breakout",
            "family_version": 1,
            "viability": 0.5,
            "paper_eligible": True,
            "live_eligible": False,
        },
    ]
    seen_symbols: list[str] = []

    def _capture_viability_insert(_conn, _cursor, statement, parameters, _context, _executemany) -> None:
        if "INSERT INTO momentum_symbol_viability" not in statement:
            return
        if isinstance(parameters, dict) and parameters.get("symbol"):
            seen_symbols.append(str(parameters["symbol"]))

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", _capture_viability_insert)
    try:
        persist_neural_momentum_tick(
            db,
            row_dicts=rows,
            regime_snapshot=ctx.to_public_dict(),
            features=feats,
            correlation_id="lock-order",
            source_node_id="nm_momentum_crypto_intel",
        )
    finally:
        event.remove(bind, "before_cursor_execute", _capture_viability_insert)

    assert seen_symbols == ["AAA", "BEZ", "ZZZ"]


def test_persist_viability_skips_when_writer_lock_busy(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    ensure_momentum_strategy_variants(db)
    db.commit()
    ctx = build_momentum_regime_context(
        now=datetime(2026, 4, 7, 16, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={"spread_regime": "normal"},
    )
    feats = ExecutionReadinessFeatures(spread_bps=5.0)
    row = {
        "symbol": "BEZ",
        "family_id": "impulse_breakout",
        "family_version": 1,
        "viability": 0.5,
        "paper_eligible": True,
        "live_eligible": False,
    }

    with bind.connect() as conn:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": VIABILITY_PERSISTENCE_LOCK_KEY},
            ).scalar()
        )
        if not acquired:
            return
        try:
            written = persist_neural_momentum_tick(
                db,
                row_dicts=[row],
                regime_snapshot=ctx.to_public_dict(),
                features=feats,
                correlation_id="lock-busy",
                source_node_id="nm_momentum_crypto_intel",
            )
        finally:
            conn.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": VIABILITY_PERSISTENCE_LOCK_KEY},
            )

    assert written == 0
    assert db.query(MomentumSymbolViability).filter(MomentumSymbolViability.symbol == "BEZ").count() == 0


def test_symbol_refresh_hydrates_recent_ross_evidence(db: Session) -> None:
    """A ticker-only refresh must not erase fresh Ross/pillar evidence."""
    ensure_momentum_strategy_variants(db)
    db.commit()
    fam = get_family("impulse_breakout")
    assert fam is not None
    symbol = "ZZHY"
    signal = {
        "ticker": symbol,
        "rvol_pace": 12.0,
        "rvol_basis": "actual_cum_over_expected_cum",
        "daily_change_pct": 42.0,
        "gap_pct": 42.0,
        "float_shares": 1_200_000,
        "scanner_source": "Ross's 5 Pillars Alert (Online)",
    }
    ctx = build_momentum_regime_context(
        now=datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={"ross_scores": {symbol: 0.88}},
    )
    feats = ExecutionReadinessFeatures.from_meta(
        {
            "tickers": [symbol],
            "ross_signals": {symbol: signal},
            "ross_scores": {symbol: 0.88},
        }
    )
    vr = score_viability(symbol, fam, ctx, feats)
    row = vr.to_public_dict()
    row["label"] = fam.label
    row["entry_style"] = fam.entry_style
    row["default_stop_logic"] = fam.default_stop_logic
    row["default_exit_logic"] = fam.default_exit_logic

    persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="ross-rich",
        source_node_id="nm_momentum_crypto_intel",
    )
    db.commit()

    hydrated = _hydrate_recent_ross_evidence(db, symbols=[symbol], meta={"tickers": [symbol]})

    assert hydrated["ross_signals"][symbol]["scanner_source"] == "Ross's 5 Pillars Alert (Online)"
    assert hydrated["ross_signals"][symbol]["rvol_pace"] == 12.0
    assert hydrated["ross_evidence_hydrated_from_recent_viability"] == [symbol]

    explicit = _hydrate_recent_ross_evidence(
        db,
        symbols=[symbol],
        meta={"tickers": [symbol], "ross_signals": {symbol: {"ticker": symbol, "rvol_pace": 99.0}}},
    )
    assert explicit["ross_signals"][symbol]["rvol_pace"] == 99.0


def test_persistence_demotes_generic_equity_live_eligible_in_ross_lane(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.config.settings.chili_momentum_auto_arm_equity_only",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        "app.config.settings.chili_momentum_ross_equity_universe_required",
        True,
        raising=False,
    )
    ensure_momentum_strategy_variants(db)
    db.commit()
    fam = get_family("impulse_breakout")
    assert fam is not None
    ctx = build_momentum_regime_context(
        now=datetime(2026, 7, 2, 14, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={},
    )
    feats = ExecutionReadinessFeatures.from_meta({"tickers": ["META"]})
    row = {
        "symbol": "META",
        "family_id": fam.family_id,
        "family_version": fam.version,
        "viability": 0.99,
        "paper_eligible": True,
        "live_eligible": True,
        "label": fam.label,
        "entry_style": fam.entry_style,
        "default_stop_logic": fam.default_stop_logic,
        "default_exit_logic": fam.default_exit_logic,
        "regime_fit": "normal",
        "freshness_hint": "mesh_tick",
    }

    persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="generic-equity",
        source_node_id="nm_momentum_crypto_intel",
    )
    db.commit()

    via = db.query(MomentumSymbolViability).filter(MomentumSymbolViability.symbol == "META").one()
    assert via.live_eligible is False
    assert via.paper_eligible is True
    assert via.explain_json["ross_live_eligible_demoted_by"] == "persistence_ross_equity_universe"


def test_persistence_demotes_generic_equity_when_ross_required_even_if_not_equity_only(
    db: Session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.config.settings.chili_momentum_auto_arm_equity_only",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        "app.config.settings.chili_momentum_ross_equity_universe_required",
        True,
        raising=False,
    )
    ensure_momentum_strategy_variants(db)
    db.commit()
    fam = get_family("impulse_breakout")
    assert fam is not None
    ctx = build_momentum_regime_context(
        now=datetime(2026, 7, 2, 14, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={},
    )
    feats = ExecutionReadinessFeatures.from_meta({"tickers": ["META"]})
    row = {
        "symbol": "META",
        "family_id": fam.family_id,
        "family_version": fam.version,
        "viability": 0.99,
        "paper_eligible": True,
        "live_eligible": True,
        "label": fam.label,
        "entry_style": fam.entry_style,
        "default_stop_logic": fam.default_stop_logic,
        "default_exit_logic": fam.default_exit_logic,
        "regime_fit": "normal",
        "freshness_hint": "mesh_tick",
    }

    persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="generic-equity-ross-required",
        source_node_id="nm_momentum_crypto_intel",
    )
    db.commit()

    via = db.query(MomentumSymbolViability).filter(MomentumSymbolViability.symbol == "META").one()
    assert via.live_eligible is False
    assert via.explain_json["ross_live_eligible_demoted_by"] == "persistence_ross_equity_universe"
    assert via.explain_json["ross_live_eligible_demoted_reason"] == "ross_universe_missing_price"


def test_persistence_keeps_profile_proven_ross_equity_live_eligible(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.config.settings.chili_momentum_auto_arm_equity_only",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        "app.config.settings.chili_momentum_ross_equity_universe_required",
        True,
        raising=False,
    )
    ensure_momentum_strategy_variants(db)
    db.commit()
    fam = get_family("impulse_breakout")
    assert fam is not None
    symbol = "JEM"
    signal = {
        "ticker": symbol,
        "price": 2.8,
        "todays_change_perc": 42.0,
        "dollar_volume": 8_000_000,
        "source": "tape_delta_ignite",
        "signal_type": "running_up_ignite",
    }
    ctx = build_momentum_regime_context(
        now=datetime(2026, 7, 2, 14, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={"ross_scores": {symbol: 0.9}},
    )
    feats = ExecutionReadinessFeatures.from_meta(
        {"tickers": [symbol], "ross_signals": {symbol: signal}, "ross_scores": {symbol: 0.9}}
    )
    row = {
        "symbol": symbol,
        "family_id": fam.family_id,
        "family_version": fam.version,
        "viability": 0.99,
        "paper_eligible": True,
        "live_eligible": True,
        "label": fam.label,
        "entry_style": fam.entry_style,
        "default_stop_logic": fam.default_stop_logic,
        "default_exit_logic": fam.default_exit_logic,
        "regime_fit": "normal",
        "freshness_hint": "mesh_tick",
    }

    persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="ross-equity",
        source_node_id="nm_momentum_crypto_intel",
    )
    db.commit()

    via = db.query(MomentumSymbolViability).filter(MomentumSymbolViability.symbol == symbol).one()
    assert via.live_eligible is True
    assert "ross_live_eligible_demoted_by" not in (via.explain_json or {})


def test_pipeline_rolls_back_after_viability_persistence_failure(db: Session, monkeypatch) -> None:
    from app.services.trading.momentum_neural import persistence as persistence_mod

    def _raise_deadlock_like_failure(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("simulated viability persistence deadlock")

    rollback_calls = 0
    original_rollback = db.rollback

    def _tracking_rollback() -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        original_rollback()

    monkeypatch.setattr(persistence_mod, "persist_neural_momentum_tick", _raise_deadlock_like_failure)
    monkeypatch.setattr(db, "rollback", _tracking_rollback)

    result = run_momentum_neural_tick(db, meta={}, correlation_id="deadlock-regression")

    assert result["persistence_ok"] is False
    assert rollback_calls >= 1
    assert db.execute(text("SELECT 1")).scalar() == 1


def test_automation_session_and_event(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    sess = create_trading_automation_session(
        db,
        symbol="BTC-USD",
        variant_id=v.id,
        state="idle",
        correlation_id="sess-corr",
        source_node_id="nm_test",
    )
    db.commit()
    ev = append_trading_automation_event(
        db,
        sess.id,
        "session_created",
        {"hello": True},
        correlation_id="evt-corr",
        source_node_id="nm_test",
    )
    db.commit()
    assert ev.session_id == sess.id
    assert ev.correlation_id == "evt-corr"
    assert ev.source_node_id == "nm_test"


def test_runtime_snapshot_binding_and_fill_tables(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    sess = create_trading_automation_session(
        db,
        symbol="ETH-USD",
        variant_id=v.id,
        state="queued",
        mode="paper",
        correlation_id="rt-corr",
        source_node_id="nm_test",
        risk_snapshot_json={"momentum_risk": {"allowed": True}},
    )
    values = build_runtime_snapshot_values(sess, variant=v, trade_count=0)
    upsert_trading_automation_runtime_snapshot(db, session_id=sess.id, values=values)
    upsert_trading_automation_session_binding(
        db,
        session_id=sess.id,
        values=default_session_binding(
            venue=sess.venue,
            mode=sess.mode,
            execution_family=sess.execution_family,
            quote_source="massive",
        ),
    )
    append_trading_automation_simulated_fill(
        db,
        session_id=sess.id,
        symbol=sess.symbol,
        lane="simulation",
        action="enter_long",
        fill_type="entry",
        quantity=1.25,
        price=2500.0,
        reference_price=2498.0,
        fees_usd=1.0,
        position_state_before="flat",
        position_state_after="long",
        reason="test_entry",
    )
    db.commit()

    snap = db.query(TradingAutomationRuntimeSnapshot).filter_by(session_id=sess.id).one()
    binding = db.query(TradingAutomationSessionBinding).filter_by(session_id=sess.id).one()
    fill = db.query(TradingAutomationSimulatedFill).filter_by(session_id=sess.id).one()
    assert snap.symbol == "ETH-USD"
    assert snap.lane == "simulation"
    assert binding.discovery_provider == "massive"
    assert fill.action == "enter_long"
