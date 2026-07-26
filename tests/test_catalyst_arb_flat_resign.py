"""Ross-parity L3 arb-flat candidate and its default-off containment.

The pure candidate classifier remains available for isolated experiments.  The
legacy strong/weak classifier remains unchanged unless a future sealed,
target-bound news experiment earns promotion.
"""
from __future__ import annotations

from datetime import datetime, timezone
import inspect
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.services.trading.momentum_neural import pipeline, viability
from app.services.trading.momentum_neural.catalyst import (
    _catalyst_tilt,
    _is_arb_flat_catalyst,
    _is_strong_catalyst,
    _is_weak_catalyst,
)
from app.services.trading.momentum_neural.context import (
    build_momentum_regime_context,
)
from app.services.trading.momentum_neural.features import (
    ExecutionReadinessFeatures,
)
from app.services.trading.momentum_neural.variants import get_family


# ── Unflagged SEC-form promotion remains quarantined ─────────────────────────

def test_sec_form_only_headlines_keep_legacy_neutral_classification():
    assert _is_weak_catalyst("Acme Files Form S-1 With SEC") is False
    assert _is_weak_catalyst("Acme S-3 Registration Declared Effective") is False
    assert _is_weak_catalyst("Acme Files Form F-1 With SEC") is False
    assert _is_weak_catalyst("Acme Files 424B5 Prospectus Supplement") is False


def test_collision_safe_bare_forms_not_weak():
    # bare "s-1"/"f-1" collide with ordinary hyphenations — must NOT classify
    assert _is_weak_catalyst("Acme recalls Class-1 medical device") is False
    assert _is_weak_catalyst("Acme wins F-15 maintenance contract") is False
    assert _is_weak_catalyst("Acme launches Model S-10 sensor line") is False


# ── ARB-FLAT class (confirmed buyout target) ─────────────────────────────────

def test_buyout_target_headlines_are_arb_flat():
    assert _is_arb_flat_catalyst("Acme to be acquired by BigCo for $12.00 per share") is True
    assert _is_arb_flat_catalyst("BigCo announces buyout of Acme") is True
    assert _is_arb_flat_catalyst("Acme agrees to takeover by BigCo") is True
    assert _is_arb_flat_catalyst("BigCo commences tender offer for Acme shares") is True


def test_default_off_path_retains_legacy_strong_classification():
    assert _is_strong_catalyst("Acme to be acquired by BigCo for $12.00 per share") is True
    assert _is_strong_catalyst("BigCo announces buyout of Acme") is True
    assert _is_strong_catalyst("Acme agrees to takeover by BigCo") is True
    assert _is_strong_catalyst("BigCo commences tender offer for Acme shares") is True


def test_acquirer_side_deal_making_stays_strong():
    # the buyER can run — acquirer-side phrasings remain strong catalysts
    assert _is_strong_catalyst("BigCo to acquire Acme in $2B merger") is True
    assert _is_strong_catalyst("BigCo enters definitive agreement for acquisition") is True
    assert _is_strong_catalyst("BigCo and Acme announce merger") is True


def test_arb_flat_not_weak_class():
    # arb-flat is its OWN class — the weak-keyed refinements must never touch it
    assert _is_weak_catalyst("Acme to be acquired by BigCo for $12.00 per share") is False
    assert _is_weak_catalyst("BigCo commences tender offer for Acme shares") is False


def test_empty_or_none_title_not_arb_flat():
    assert _is_arb_flat_catalyst("") is False
    assert _is_arb_flat_catalyst(None) is False


def test_dual_match_headline_is_both_strong_and_arb_flat():
    # precedence is enforced at the CONSUMER (viability): here both classifiers may
    # legitimately match a dual headline — document the contract
    t = "Acme enters definitive agreement to be acquired by BigCo"
    assert _is_arb_flat_catalyst(t) is True
    assert _is_strong_catalyst(t) is True  # "definitive agreement" — consumer must let arb-flat win


def test_arb_flat_candidate_is_opt_in_and_missing_fallback_is_off():
    assert (
        Settings.model_fields[
            "chili_momentum_catalyst_arb_flat_gate_enabled"
        ].default
        is False
    )
    for module in (pipeline, viability):
        source = inspect.getsource(module)
        assert (
            'getattr(settings, "chili_momentum_catalyst_arb_flat_gate_enabled", True)'
            not in source
        )


def _viability_context(*, arb: bool) -> object:
    meta = {
        "spread_regime": "tight",
        "strong_catalyst_symbols": ["ACME"],
    }
    if arb:
        meta["arb_flat_catalyst_symbols"] = ["ACME"]
    return build_momentum_regime_context(
        now=datetime(2026, 7, 25, 17, 0, tzinfo=timezone.utc),
        atr_pct=0.018,
        meta=meta,
    )


def _features() -> ExecutionReadinessFeatures:
    return ExecutionReadinessFeatures(
        spread_bps=4.0,
        slippage_estimate_bps=4.0,
        fee_to_target_ratio=0.08,
    )


def test_explicit_arb_candidate_reaches_viability_and_owns_precedence():
    family = get_family("vwap_reclaim_continuation")
    assert family is not None
    with patch.object(
        viability.settings,
        "chili_momentum_catalyst_arb_flat_gate_enabled",
        True,
    ):
        neutral = viability.score_viability(
            "ACME",
            family,
            build_momentum_regime_context(
                now=datetime(2026, 7, 25, 17, 0, tzinfo=timezone.utc),
                atr_pct=0.018,
                meta={"spread_regime": "tight"},
            ),
            _features(),
        )
        result = viability.score_viability(
            "ACME",
            family,
            _viability_context(arb=True),
            _features(),
        )
    assert result.live_eligible is False
    assert result.viability == pytest.approx(
        neutral.viability - abs(float(_catalyst_tilt()))
    )
    assert any("arb-flat catalyst" in warning.lower() for warning in result.warnings)
    assert not any("strong catalyst" in warning.lower() for warning in result.warnings)


def test_default_off_arb_candidate_is_noop_and_legacy_strong_wins():
    family = get_family("vwap_reclaim_continuation")
    assert family is not None
    with patch.object(
        viability.settings,
        "chili_momentum_catalyst_arb_flat_gate_enabled",
        False,
    ):
        result = viability.score_viability(
            "ACME",
            family,
            _viability_context(arb=True),
            _features(),
        )
    assert result.live_eligible is True
    assert any("strong catalyst" in warning.lower() for warning in result.warnings)
    assert not any("arb-flat catalyst" in warning.lower() for warning in result.warnings)


def test_pipeline_projects_arb_candidate_into_viability_context():
    source = inspect.getsource(pipeline)
    projection = source[source.index("ctx_meta = {"):source.index(
        "ctx = build_momentum_regime_context"
    )]
    assert '"arb_flat_catalyst_symbols"' in projection


@pytest.mark.parametrize(
    ("parent_enabled", "child_enabled"),
    ((False, False), (False, True), (True, False)),
)
def test_pipeline_arb_fetch_is_zero_io_when_either_gate_is_off(
    parent_enabled,
    child_enabled,
):
    provider = MagicMock(return_value={"ACME"})
    meta = {}
    with patch.object(
        pipeline.settings,
        "chili_momentum_catalyst_grade_gate_enabled",
        parent_enabled,
    ), patch.object(
        pipeline.settings,
        "chili_momentum_catalyst_arb_flat_gate_enabled",
        child_enabled,
    ), patch(
        "app.services.trading.momentum_neural.catalyst.arb_flat_catalyst_symbols",
        provider,
    ):
        pipeline._attach_arb_flat_catalysts(meta)
    provider.assert_not_called()
    assert "arb_flat_catalyst_symbols" not in meta


def test_pipeline_arb_fetch_projects_only_when_both_gates_are_on():
    provider = MagicMock(return_value={"BETA", "ACME"})
    meta = {}
    with patch.object(
        pipeline.settings,
        "chili_momentum_catalyst_grade_gate_enabled",
        True,
    ), patch.object(
        pipeline.settings,
        "chili_momentum_catalyst_arb_flat_gate_enabled",
        True,
    ), patch(
        "app.services.trading.momentum_neural.catalyst.arb_flat_catalyst_symbols",
        provider,
    ):
        pipeline._attach_arb_flat_catalysts(meta)
    provider.assert_called_once_with()
    assert meta["arb_flat_catalyst_symbols"] == ["ACME", "BETA"]
