"""L11 — paper setup-quality gate (viability.score_viability_explicit).

Dati, ang `paper_eligible` ay fail-open: `True` para sa lahat maliban sa
leveraged/inverse-ETF veto. Sukat sa prod 2026-08-04: 610 distinct eligible
symbols sa 24h vs 117 live-eligible; 437 ng paper-only na 493 ay may "Below Ross
explosiveness floor". Ang band na iyon ang cause #3 ng IQFeed subscription
resolver, at nang humati ang rail-governor sa 312 slots ay inubos nito ang buong
budget — 100% ng ross band (109 distinct, 29,677 evictions) ang na-evict.

Ang gate ay SETUP-QUALITY lang. Ang live-money COST/RISK na knock-down (spread
ceiling) ay nananatiling live-only, kaya buhay pa rin ang deployment ladder.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from app.config import settings as runtime_settings
from app.services.trading.momentum_neural import viability as viability_module
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import (
    ViabilityExternalInputs,
    ViabilitySettingsProjection,
    score_viability_explicit,
)


def _family():
    family = get_family("impulse_breakout")
    assert family is not None
    return family


def _context(*, meta=None):
    return build_momentum_regime_context(
        now=datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc),
        atr_pct=0.015,
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


def _projection(**overrides) -> ViabilitySettingsProjection:
    projection = ViabilitySettingsProjection.from_runtime(runtime_settings)
    return replace(projection, **overrides) if overrides else projection


def _score(symbol="TEST", *, meta=None, features_meta=None, projection=None, external=None):
    return score_viability_explicit(
        symbol,
        _family(),
        _context(meta=meta),
        ExecutionReadinessFeatures.from_meta(
            features_meta if features_meta is not None else {"spread_bps": 18.0, "product_tradable": True}
        ),
        settings=projection or _projection(),
        external=external or _external(),
    )


def test_ross_floor_vetoes_paper_too():
    # Ang 437/610 na klase: "not a live setup" — hindi rin dapat i-rehearse ng paper.
    result = _score(meta={"ross_below_floor": ["TEST"]})
    assert result.live_eligible is False
    assert result.paper_eligible is False
    assert any("Paper setup-quality gate: ross_explosiveness_floor" in w for w in result.warnings)


def test_not_tradable_vetoes_paper_too():
    result = _score(features_meta={"spread_bps": 18.0, "product_tradable": False})
    assert result.live_eligible is False
    assert result.paper_eligible is False


def test_kill_switch_restores_fail_open_paper():
    result = _score(
        meta={"ross_below_floor": ["TEST"]},
        projection=_projection(chili_momentum_paper_setup_quality_gate_enabled=False),
    )
    assert result.live_eligible is False
    assert result.paper_eligible is True  # ang dating fail-open na ugali
    assert not any("Paper setup-quality gate" in w for w in result.warnings)


def test_clean_setup_stays_eligible_sa_parehong_lane():
    result = _score()
    assert result.paper_eligible is True
    assert not any("Paper setup-quality gate" in w for w in result.warnings)


def test_deployment_ladder_buhay_pa_rin_sa_cost_only_knockdown():
    # LIVE-MONEY COST gate (spread ceiling) — live-only ito: dapat mag-paper pa rin
    # ang isang TOTOONG setup na mahal lang para sa live. Ito ang buong punto ng
    # deployment ladder (deployment_ladder_service.py:122).
    result = _score(
        features_meta={"spread_bps": 900.0, "product_tradable": True},
        projection=_projection(
            chili_momentum_live_eligible_max_spread_bps=25.0,
            chili_momentum_thin_spread_squeeze_lane_enabled=False,
        ),
    )
    assert result.live_eligible is False, "malapad na spread ay dapat pumigil sa live"
    assert result.paper_eligible is True, "hindi dapat masira ang paper rehearsal ng cost gate"


def test_leveraged_etf_veto_hindi_nagbago():
    result = _score(external=_external(leveraged_etf=True))
    assert result.paper_eligible is False
    assert result.live_eligible is False
    assert result.regime_fit == "leveraged_inverse_etf_vetoed"
