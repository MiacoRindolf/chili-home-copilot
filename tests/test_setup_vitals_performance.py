from __future__ import annotations

import math

from app.services.trading import setup_vitals


def _polyfit_equivalent_normalized_slope(values: list[float | None]) -> float:
    ys = [float(v) for v in values if v is not None and not math.isnan(v)]
    n = len(ys)
    if n < 2:
        return 0.0
    mean_y = sum(ys) / n
    variance = sum((y - mean_y) ** 2 for y in ys) / n
    if variance < 1e-24:
        return 0.0
    mean_x = (n - 1) / 2.0
    denom = sum((i - mean_x) ** 2 for i in range(n))
    slope = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys)) / denom
    return float(max(-1.0, min(1.0, slope / max(math.sqrt(variance), 1e-6))))


def test_normalized_slope_matches_closed_form_reference() -> None:
    samples = [
        [40.0, 42.0, 44.0, 46.0, 48.0],
        [70.0, 68.0, 65.0, 64.0, 61.0],
        [None, 1.0, 2.0, None, 3.0],
        [1.0, 1.0, 1.0, 1.0],
    ]

    for values in samples:
        assert setup_vitals._normalized_slope(values) == _polyfit_equivalent_normalized_slope(values)


def test_normalized_slope_avoids_numpy_polyfit(monkeypatch) -> None:
    def fail_polyfit(*_args, **_kwargs):
        raise AssertionError("_normalized_slope should use closed-form least squares")

    monkeypatch.setattr(setup_vitals.np, "polyfit", fail_polyfit)

    assert setup_vitals._normalized_slope([10.0, 11.0, 12.0, 13.0]) > 0
