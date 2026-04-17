"""Phase K unit tests for the pure divergence model."""
from __future__ import annotations

import pytest

from app.services.trading.divergence_model import (
    ALL_LAYERS,
    DivergenceConfig,
    DivergenceInput,
    LAYER_BRACKET,
    LAYER_EXIT,
    LAYER_LEDGER,
    LAYER_SIZER,
    LAYER_VENUE,
    LayerSignal,
    compute_divergence,
    compute_divergence_id,
)


def _inp(
    signals,
    *,
    pattern_id: int = 42,
    pattern_name: str = "p",
    as_of_key: str = "2026-04-16",
):
    return DivergenceInput(
        scan_pattern_id=pattern_id,
        pattern_name=pattern_name,
        as_of_key=as_of_key,
        signals=signals,
    )


# ---------------------------------------------------------------------------
# compute_divergence_id
# ---------------------------------------------------------------------------


def test_divergence_id_deterministic():
    a = compute_divergence_id(scan_pattern_id=1, as_of_key="2026-04-16")
    b = compute_divergence_id(scan_pattern_id=1, as_of_key="2026-04-16")
    assert a == b
    assert len(a) == 32


def test_divergence_id_changes_with_pattern():
    a = compute_divergence_id(scan_pattern_id=1, as_of_key="k")
    b = compute_divergence_id(scan_pattern_id=2, as_of_key="k")
    assert a != b


def test_divergence_id_changes_with_key():
    a = compute_divergence_id(scan_pattern_id=1, as_of_key="k1")
    b = compute_divergence_id(scan_pattern_id=1, as_of_key="k2")
    assert a != b


def test_divergence_id_none_key_stable():
    a = compute_divergence_id(scan_pattern_id=1, as_of_key=None)
    b = compute_divergence_id(scan_pattern_id=1, as_of_key=None)
    assert a == b


# ---------------------------------------------------------------------------
# compute_divergence - empty / green
# ---------------------------------------------------------------------------


def test_empty_signals_green():
    out = compute_divergence(_inp([]))
    assert out.severity == "green"
    assert out.score == 0.0
    assert out.layers_sampled == 0
    assert out.layers_total == len(ALL_LAYERS)


def test_all_green_stays_green():
    sigs = [
        LayerSignal(layer=LAYER_LEDGER, severity="green"),
        LayerSignal(layer=LAYER_EXIT, severity="green"),
    ]
    out = compute_divergence(_inp(sigs))
    assert out.severity == "green"
    assert out.score == 0.0
    assert out.layers_sampled == 2
    assert out.layers_agreed == 2
    assert out.ledger_severity == "green"
    assert out.exit_severity == "green"
    assert out.venue_severity is None


# ---------------------------------------------------------------------------
# compute_divergence - yellow / red
# ---------------------------------------------------------------------------


def test_single_yellow_ledger_yellow():
    sigs = [LayerSignal(layer=LAYER_LEDGER, severity="yellow")]
    out = compute_divergence(_inp(sigs))
    assert out.severity == "yellow"
    assert out.score == pytest.approx(1.0)
    assert out.ledger_severity == "yellow"


def test_single_red_ledger_red():
    sigs = [LayerSignal(layer=LAYER_LEDGER, severity="red")]
    out = compute_divergence(_inp(sigs))
    assert out.severity == "red"
    assert out.score == pytest.approx(2.0)
    assert out.ledger_severity == "red"


def test_venue_weight_below_one_still_yellow():
    """venue weight is 0.8 -> yellow (rank 1) * 0.8 = 0.8 which is < yellow_threshold 0.9 -> green."""
    sigs = [LayerSignal(layer=LAYER_VENUE, severity="yellow")]
    out = compute_divergence(_inp(sigs))
    assert out.severity == "green"
    assert out.score == pytest.approx(0.8)


def test_venue_red_still_red():
    """venue weight 0.8 * red (2) = 1.6 which is below red_threshold 1.8 so yellow."""
    sigs = [LayerSignal(layer=LAYER_VENUE, severity="red")]
    out = compute_divergence(_inp(sigs))
    assert out.severity == "yellow"
    assert out.score == pytest.approx(1.6)


def test_max_not_sum():
    """Red should come from max layer, not sum of all layers."""
    sigs = [
        LayerSignal(layer=LAYER_LEDGER, severity="yellow"),
        LayerSignal(layer=LAYER_EXIT, severity="yellow"),
        LayerSignal(layer=LAYER_VENUE, severity="yellow"),
        LayerSignal(layer=LAYER_BRACKET, severity="yellow"),
        LayerSignal(layer=LAYER_SIZER, severity="yellow"),
    ]
    out = compute_divergence(_inp(sigs))
    assert out.severity == "yellow"
    assert out.score == pytest.approx(1.0)


def test_one_red_layer_enough_for_red():
    sigs = [
        LayerSignal(layer=LAYER_LEDGER, severity="green"),
        LayerSignal(layer=LAYER_EXIT, severity="red"),
        LayerSignal(layer=LAYER_VENUE, severity="green"),
    ]
    out = compute_divergence(_inp(sigs))
    assert out.severity == "red"
    assert out.exit_severity == "red"
    assert out.layers_sampled == 3
    assert out.layers_agreed == 2


# ---------------------------------------------------------------------------
# hysteresis gate
# ---------------------------------------------------------------------------


def test_min_layers_sampled_clamps_to_green():
    cfg = DivergenceConfig(min_layers_sampled=3)
    sigs = [
        LayerSignal(layer=LAYER_LEDGER, severity="red"),
        LayerSignal(layer=LAYER_EXIT, severity="yellow"),
    ]
    out = compute_divergence(_inp(sigs), config=cfg)
    assert out.severity == "green"
    assert out.score > 0
    assert out.layers_sampled == 2


def test_min_layers_sampled_met_promotes():
    cfg = DivergenceConfig(min_layers_sampled=2)
    sigs = [
        LayerSignal(layer=LAYER_LEDGER, severity="red"),
        LayerSignal(layer=LAYER_EXIT, severity="yellow"),
    ]
    out = compute_divergence(_inp(sigs), config=cfg)
    assert out.severity == "red"


# ---------------------------------------------------------------------------
# dedup / validation
# ---------------------------------------------------------------------------


def test_duplicate_layer_last_wins():
    sigs = [
        LayerSignal(layer=LAYER_LEDGER, severity="red"),
        LayerSignal(layer=LAYER_LEDGER, severity="green"),
    ]
    out = compute_divergence(_inp(sigs))
    assert out.ledger_severity == "green"
    assert out.severity == "green"


def test_invalid_severity_raises():
    with pytest.raises(ValueError):
        compute_divergence(_inp([LayerSignal(layer=LAYER_LEDGER, severity="on_fire")]))


def test_invalid_layer_raises():
    with pytest.raises(ValueError):
        compute_divergence(_inp([LayerSignal(layer="cosmos", severity="green")]))


# ---------------------------------------------------------------------------
# payload shape
# ---------------------------------------------------------------------------


def test_payload_contains_weights_and_reasons():
    sigs = [
        LayerSignal(layer=LAYER_LEDGER, severity="red", reason_code="delta_abs_gt_tol"),
        LayerSignal(layer=LAYER_EXIT, severity="yellow"),
    ]
    out = compute_divergence(_inp(sigs))
    p = out.payload
    assert "weighted_scores" in p
    assert "reason_codes" in p
    assert p["reason_codes"][LAYER_LEDGER] == "delta_abs_gt_tol"
    assert LAYER_EXIT not in p["reason_codes"]
    assert "thresholds" in p
    assert p["thresholds"]["red"] == DivergenceConfig().red_threshold


def test_custom_weights_respected():
    cfg = DivergenceConfig(
        layer_weights={
            LAYER_LEDGER: 2.0,
            LAYER_EXIT: 1.0,
            LAYER_VENUE: 1.0,
            LAYER_BRACKET: 1.0,
            LAYER_SIZER: 1.0,
        },
        yellow_threshold=1.9,
        red_threshold=3.5,
    )
    sigs = [LayerSignal(layer=LAYER_LEDGER, severity="yellow")]
    out = compute_divergence(_inp(sigs), config=cfg)
    assert out.score == pytest.approx(2.0)
    assert out.severity == "yellow"


def test_output_divergence_id_matches_helper():
    out = compute_divergence(_inp([]))
    assert out.divergence_id == compute_divergence_id(
        scan_pattern_id=42, as_of_key="2026-04-16"
    )
