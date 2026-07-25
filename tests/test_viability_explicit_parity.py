from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import settings as runtime_settings
from app.services.trading.momentum_neural import viability as viability_module
from app.services.trading.momentum_neural.context import (
    build_momentum_regime_context,
)
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import (
    ViabilityExternalInputs,
    ViabilitySettingsProjection,
    score_viability,
    score_viability_explicit,
)


def _family():
    family = get_family("impulse_breakout")
    assert family is not None
    return family


def _context(*, meta=None, atr_pct=0.015):
    return build_momentum_regime_context(
        now=datetime(2026, 7, 18, 16, 0, tzinfo=timezone.utc),
        atr_pct=atr_pct,
        meta=meta or {},
    )


def _external(**changes) -> ViabilityExternalInputs:
    base = ViabilityExternalInputs(
        leveraged_etf=False,
        excluded_fund=False,
        symbol_family_memory_adjust=0.0,
        dilution_history_derate=0.0,
        ross_rvol=None,
        ross_change_pct=None,
        ross_float_shares=None,
        squeeze_fuel_rank_pct=None,
        below_explosive_floor=False,
        catalyst_delta=0.0,
        catalyst_grade_delta=0.0,
        fake_catalyst_delta=0.0,
        sympathy_delta=0.0,
        theme_sympathy_delta=0.0,
        close_strength_delta=0.0,
        thick_tape_delta=0.0,
        nonmonotonic_volume_delta=0.0,
        ross_quality_viability_tilt=0.20,
    )
    return replace(base, **changes)


def test_default_wrapper_and_resolved_explicit_core_are_identical(monkeypatch) -> None:
    monkeypatch.setattr(viability_module, "symbol_is_leveraged_etf", lambda _symbol: False)
    monkeypatch.setattr(viability_module, "symbol_is_excluded_fund", lambda _symbol: False)
    family = _family()
    context = _context(
        meta={
            "ross_scores": {"VEEE": 0.91},
            "ross_signals": {
                "VEEE": {
                    "rvol": 8.0,
                    "daily_change_pct": 35.0,
                    "float_shares": 2_000_000.0,
                    "squeeze_fuel_rank_pct": 0.95,
                }
            },
        }
    )
    features = ExecutionReadinessFeatures.from_meta(
        {
            "spread_bps": 18.0,
            "ofi": 0.6,
            "micro_price_edge": 8.0,
            "trade_flow": 0.8,
            "product_tradable": True,
            "ross_signals": context.meta["ross_signals"],
        }
    )
    projection = ViabilitySettingsProjection.from_runtime(runtime_settings)
    external = viability_module._resolve_viability_external_inputs(
        "VEEE",
        family,
        context,
        features,
        db=None,
        settings_projection=projection,
    )

    wrapped = score_viability("VEEE", family, context, features, db=None)
    explicit = score_viability_explicit(
        "VEEE",
        family,
        context,
        features,
        settings=projection,
        external=external,
    )

    assert wrapped == explicit
    assert wrapped.to_public_dict() == explicit.to_public_dict()


def test_legacy_wrapper_preserves_leveraged_veto_short_circuit(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("post-veto external dependency was consulted")

    monkeypatch.setattr(viability_module, "symbol_is_leveraged_etf", lambda _symbol: True)
    monkeypatch.setattr(viability_module, "symbol_is_excluded_fund", forbidden)
    monkeypatch.setattr(viability_module, "_symbol_family_memory_adjust", forbidden)

    result = score_viability(
        "SOXL",
        _family(),
        _context(),
        ExecutionReadinessFeatures.from_meta({"spread_bps": 7.0}),
        db=object(),
    )

    assert result.viability == 0.0
    assert result.paper_eligible is False
    assert result.live_eligible is False
    assert result.regime_fit == "leveraged_inverse_etf_vetoed"


def test_explicit_core_does_not_read_runtime_settings_classifiers_or_db(
    monkeypatch,
) -> None:
    class ForbiddenSettings:
        def __getattr__(self, name):
            raise AssertionError(f"runtime settings fallback attempted: {name}")

    def forbidden(*args, **kwargs):
        raise AssertionError("classifier/database fallback attempted")

    family = _family()
    context = _context(meta={"ross_scores": {"VEEE": 0.9}})
    features = ExecutionReadinessFeatures.from_meta(
        {"spread_bps": 7.0, "product_tradable": True}
    )
    projection = ViabilitySettingsProjection.from_runtime(runtime_settings)
    external = _external(
        ross_rvol=8.0,
        ross_change_pct=35.0,
        ross_float_shares=2_000_000.0,
    )
    expected = score_viability_explicit(
        "VEEE",
        family,
        context,
        features,
        settings=projection,
        external=external,
    )
    monkeypatch.setattr(viability_module, "settings", ForbiddenSettings())
    monkeypatch.setattr(viability_module, "symbol_is_leveraged_etf", forbidden)
    monkeypatch.setattr(viability_module, "symbol_is_excluded_fund", forbidden)
    monkeypatch.setattr(viability_module, "_symbol_family_memory_adjust", forbidden)

    actual = score_viability_explicit(
        "VEEE",
        family,
        context,
        features,
        settings=projection,
        external=external,
    )

    assert actual == expected


def test_explicit_core_does_not_mutate_enriched_inputs() -> None:
    family = _family()
    context = _context(
        meta={
            "ross_scores": {"VEEE": 0.9},
            "catalyst_symbols": {"VEEE"},
            "top_market_gainers": {"VEEE"},
        }
    )
    features = ExecutionReadinessFeatures.from_meta(
        {
            "spread_bps": 9.0,
            "book_imbalance": 0.2,
            "ross_signals": {"VEEE": {"rvol": 8.0}},
        }
    )
    before_context = deepcopy(context.meta)
    before_features = deepcopy(features.meta)

    score_viability_explicit(
        "VEEE",
        family,
        context,
        features,
        settings=ViabilitySettingsProjection.from_runtime(runtime_settings),
        external=_external(catalyst_delta=0.04),
    )

    assert context.meta == before_context
    assert features.meta == before_features


def test_every_direct_core_setting_is_in_the_explicit_projection() -> None:
    source = Path(
        "app/services/trading/momentum_neural/viability.py"
    ).read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    core = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "score_viability_explicit"
    )
    consulted = {
        node.args[1].value
        for node in ast.walk(core)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "settings"
        and isinstance(node.args[1], ast.Constant)
    }
    projected = {field.name for field in fields(ViabilitySettingsProjection)}

    assert consulted == projected


def test_every_external_core_fact_is_in_the_explicit_projection() -> None:
    source = Path(
        "app/services/trading/momentum_neural/viability.py"
    ).read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    core = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "score_viability_explicit"
    )
    consulted = {
        node.attr
        for node in ast.walk(core)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "external"
    }
    projected = {field.name for field in fields(ViabilityExternalInputs)}

    assert consulted == projected


def test_explicit_core_has_no_import_logging_db_or_legacy_helper_call() -> None:
    source = Path(
        "app/services/trading/momentum_neural/viability.py"
    ).read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    core = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "score_viability_explicit"
    )
    names = {node.id for node in ast.walk(core) if isinstance(node, ast.Name)}
    calls = {
        node.func.id
        for node in ast.walk(core)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(core))
    assert not names.intersection({"db", "logger"})
    assert not calls.intersection(
        {
            "symbol_is_leveraged_etf",
            "symbol_is_excluded_fund",
            "_symbol_family_memory_adjust",
            "dilution_history_derate",
            "catalyst_viability_delta",
            "catalyst_grade_selection_delta",
            "fake_catalyst_viability_delta",
            "sympathy_viability_delta",
            "theme_sympathy_viability_delta",
            "close_strength_viability_delta",
            "thick_tape_discount",
            "nonmonotonic_volume_rolloff",
            "_extract_pillars",
            "below_explosive_floor",
        }
    )


def test_explicit_memory_and_classification_inputs_control_same_core_path() -> None:
    family = _family()
    context = _context()
    features = ExecutionReadinessFeatures.from_meta(
        {"spread_bps": 7.0, "product_tradable": True}
    )
    projection = ViabilitySettingsProjection.from_runtime(runtime_settings)
    base = score_viability_explicit(
        "VEEE",
        family,
        context,
        features,
        settings=projection,
        external=_external(),
    )
    adjusted = score_viability_explicit(
        "VEEE",
        family,
        context,
        features,
        settings=projection,
        external=_external(
            symbol_family_memory_adjust=0.08,
            dilution_history_derate=0.03,
            excluded_fund=True,
        ),
    )

    expected_delta = 0.08 - 0.03
    if bool(projection.chili_momentum_exclude_fund_structures_enabled):
        expected_delta -= 0.12
    assert adjusted.viability == pytest.approx(
        max(0.0, min(1.0, base.viability + expected_delta))
    )
