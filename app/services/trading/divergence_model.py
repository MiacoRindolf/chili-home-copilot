"""Phase K - canonical divergence panel (pure functions).

Given a set of :class:`LayerSignal` rows sourced from the five existing
substrate log tables (Phase A ledger parity, Phase B exit parity,
Phase F venue truth, Phase G bracket reconciliation, Phase H position
sizer), the divergence panel computes:

1. **Per-layer severity** - the severity we saw at each layer for this
   pattern, or ``None`` when the layer had no sample.
2. **Weighted composite score** in ``[0.0, 1.0]`` - the ``max`` of the
   per-layer weighted scores (a single red layer should flag the
   pattern loudly; averaging would dilute it).
3. **Overall severity** - ``{green, yellow, red}`` with hysteresis on
   ``min_layers_sampled`` so small samples never promote past yellow.

The pure model has **no side effects**: no DB, no logging, no config
reads. All callers wrap it with a service-layer writer that handles
mode gating and persistence to ``trading_pattern_divergence_log``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Sequence

_VALID_SEVERITIES = ("green", "yellow", "red")
_SEVERITY_RANK = {"green": 0, "yellow": 1, "red": 2}

LAYER_LEDGER = "ledger"
LAYER_EXIT = "exit"
LAYER_VENUE = "venue"
LAYER_BRACKET = "bracket"
LAYER_SIZER = "sizer"
ALL_LAYERS = (LAYER_LEDGER, LAYER_EXIT, LAYER_VENUE, LAYER_BRACKET, LAYER_SIZER)


@dataclass(frozen=True)
class DivergenceConfig:
    """Tuning knobs for the divergence scorer.

    * ``layer_weights`` - multiplicative weight per layer; default 1.0
      for every layer. The composite score is ``max_over_layers(
      severity_rank * weight)``.
    * ``min_layers_sampled`` - minimum number of layers with a signal
      needed for the overall severity to reach yellow or red. Below
      this gate the output is clamped to green.
    * ``red_threshold`` - composite score threshold for red. Defaults
      to ``1.8`` (one red layer at weight 1.0 plus one yellow at any
      weight, or one layer at weight >= 1.8).
    * ``yellow_threshold`` - composite score threshold for yellow.
    """

    layer_weights: dict[str, float] = field(
        default_factory=lambda: {
            LAYER_LEDGER: 1.0,
            LAYER_EXIT: 1.0,
            LAYER_VENUE: 0.8,
            LAYER_BRACKET: 1.0,
            LAYER_SIZER: 1.0,
        }
    )
    min_layers_sampled: int = 1
    yellow_threshold: float = 0.9
    red_threshold: float = 1.8


@dataclass(frozen=True)
class LayerSignal:
    """One layer's contribution to the divergence scorer.

    ``severity`` must be in :data:`_VALID_SEVERITIES`. ``reason_code``
    is an optional short tag describing the most recent offending row
    (e.g. ``"delta_abs_gt_tol"`` for ledger parity, ``"kind=qty_drift"``
    for bracket reconciliation).
    """

    layer: str
    severity: str
    reason_code: str | None = None
    sample_size: int = 0
    source_row_id: int | None = None


@dataclass(frozen=True)
class DivergenceInput:
    """Inputs for a single :func:`compute_divergence` call.

    ``signals`` may contain at most one entry per layer in
    :data:`ALL_LAYERS`. Duplicate layers cause the later entry to win.
    """

    scan_pattern_id: int
    pattern_name: str | None
    as_of_key: str | None
    signals: Sequence[LayerSignal]


@dataclass(frozen=True)
class DivergenceOutput:
    """Pure result of :func:`compute_divergence`.

    All fields map 1:1 onto columns of the
    ``trading_pattern_divergence_log`` table.
    """

    divergence_id: str
    scan_pattern_id: int
    pattern_name: str | None
    as_of_key: str | None
    ledger_severity: str | None
    exit_severity: str | None
    venue_severity: str | None
    bracket_severity: str | None
    sizer_severity: str | None
    severity: str
    score: float
    layers_sampled: int
    layers_agreed: int
    layers_total: int
    payload: dict = field(default_factory=dict)


def _validate_severity(sev: str) -> str:
    if sev not in _VALID_SEVERITIES:
        raise ValueError(f"divergence_model: invalid severity {sev!r}")
    return sev


def _validate_layer(layer: str) -> str:
    if layer not in ALL_LAYERS:
        raise ValueError(f"divergence_model: invalid layer {layer!r}")
    return layer


def compute_divergence_id(
    *, scan_pattern_id: int, as_of_key: str | None,
) -> str:
    """Deterministic hash for ``(pattern, as_of_key)`` dedupe."""
    basis = f"{int(scan_pattern_id)}|{as_of_key or 'no_key'}"
    return hashlib.blake2b(
        basis.encode("utf-8"), digest_size=16,
    ).hexdigest()


def _classify(
    *, score: float, layers_sampled: int, cfg: DivergenceConfig,
) -> str:
    if layers_sampled < cfg.min_layers_sampled:
        return "green"
    if score >= cfg.red_threshold:
        return "red"
    if score >= cfg.yellow_threshold:
        return "yellow"
    return "green"


def compute_divergence(
    inputs: DivergenceInput,
    *,
    config: DivergenceConfig | None = None,
) -> DivergenceOutput:
    """Pure divergence scoring for a single pattern.

    Empty ``signals`` returns a fully green snapshot with zero score.
    No exceptions on missing layers; invalid severity or layer values
    raise :class:`ValueError`.
    """
    cfg = config or DivergenceConfig()
    per_layer: dict[str, LayerSignal] = {}
    for sig in inputs.signals:
        _validate_severity(sig.severity)
        _validate_layer(sig.layer)
        per_layer[sig.layer] = sig

    weighted_scores: dict[str, float] = {}
    for layer, sig in per_layer.items():
        weight = float(cfg.layer_weights.get(layer, 1.0))
        weighted_scores[layer] = _SEVERITY_RANK[sig.severity] * weight

    max_score = max(weighted_scores.values(), default=0.0)
    layers_sampled = len(per_layer)
    layers_agreed = sum(
        1 for sig in per_layer.values() if sig.severity == "green"
    )
    severity = _classify(
        score=max_score,
        layers_sampled=layers_sampled,
        cfg=cfg,
    )

    payload = {
        "weighted_scores": weighted_scores,
        "reason_codes": {
            layer: sig.reason_code
            for layer, sig in per_layer.items()
            if sig.reason_code
        },
        "sample_sizes": {
            layer: int(sig.sample_size)
            for layer, sig in per_layer.items()
        },
        "layer_weights": dict(cfg.layer_weights),
        "thresholds": {
            "yellow": cfg.yellow_threshold,
            "red": cfg.red_threshold,
            "min_layers_sampled": cfg.min_layers_sampled,
        },
    }

    return DivergenceOutput(
        divergence_id=compute_divergence_id(
            scan_pattern_id=inputs.scan_pattern_id,
            as_of_key=inputs.as_of_key,
        ),
        scan_pattern_id=int(inputs.scan_pattern_id),
        pattern_name=inputs.pattern_name,
        as_of_key=inputs.as_of_key,
        ledger_severity=(
            per_layer[LAYER_LEDGER].severity if LAYER_LEDGER in per_layer else None
        ),
        exit_severity=(
            per_layer[LAYER_EXIT].severity if LAYER_EXIT in per_layer else None
        ),
        venue_severity=(
            per_layer[LAYER_VENUE].severity if LAYER_VENUE in per_layer else None
        ),
        bracket_severity=(
            per_layer[LAYER_BRACKET].severity if LAYER_BRACKET in per_layer else None
        ),
        sizer_severity=(
            per_layer[LAYER_SIZER].severity if LAYER_SIZER in per_layer else None
        ),
        severity=severity,
        score=round(float(max_score), 6),
        layers_sampled=layers_sampled,
        layers_agreed=layers_agreed,
        layers_total=len(ALL_LAYERS),
        payload=payload,
    )


__all__ = [
    "ALL_LAYERS",
    "DivergenceConfig",
    "DivergenceInput",
    "DivergenceOutput",
    "LAYER_BRACKET",
    "LAYER_EXIT",
    "LAYER_LEDGER",
    "LAYER_SIZER",
    "LAYER_VENUE",
    "LayerSignal",
    "compute_divergence",
    "compute_divergence_id",
]
