"""The ignition-governor derivation script's constants must not rot.

``scripts/derive_ignition_governors.py`` mirrors four ``app/config.py`` defaults
so it can run from a bare checkout where ``app.config`` cannot be imported (it
raises ``ValidationError: database_url Field required`` unless DATABASE_URL is
in the environment, and the script offers its own ``--database-url`` flag
precisely so an operator need not export one). That fallback path is SILENT, so
a stale literal produces a WRONG RECOMMENDATION rather than an error.

This is not hypothetical: ``FALLBACK_MAX_CONCURRENT_LIVE`` shipped as 3 against
a real default of 5, which bounds ``derive_admits_per_minute`` and would have
published "bounded by max_concurrent_live_sessions=3" — understating the
admits/minute cap by 40% in a seam whose whole measured problem is that
admission is already too slow.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.config import Settings

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "derive_ignition_governors.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("_derive_governors", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("fallback_name", "setting_name"),
    [
        ("FALLBACK_ADMITS_PER_MINUTE", "chili_momentum_ignition_admits_per_minute"),
        ("FALLBACK_DEDUP_TTL_S", "chili_momentum_ignition_dedup_ttl_seconds"),
        (
            "FALLBACK_MAX_CONCURRENT_LIVE",
            "chili_momentum_risk_max_concurrent_live_sessions",
        ),
        ("FALLBACK_MAX_WATCH_SECONDS", "chili_momentum_auto_arm_max_watch_seconds"),
    ],
)
def test_every_fallback_equals_the_real_config_default(fallback_name, setting_name):
    module = _load_script()
    assert getattr(module, fallback_name) == Settings.model_fields[setting_name].default


def test_admits_per_minute_is_bounded_by_the_real_live_slot_count():
    """The bound that the stale literal was silently shrinking."""
    module = _load_script()
    out = module.derive_admits_per_minute(
        [6] * 100, max_concurrent_live_sessions=5, current_value=6
    )
    assert out["recommended_value"] == 5

    # And the wrong literal would have produced a different, smaller answer —
    # so this test genuinely discriminates.
    stale = module.derive_admits_per_minute(
        [6] * 100, max_concurrent_live_sessions=3, current_value=6
    )
    assert stale["recommended_value"] == 3


def test_thin_samples_recommend_nothing():
    """Below the documented sample floor the script must refuse to recommend."""
    module = _load_script()
    out = module.derive_admits_per_minute(
        [6] * 3, max_concurrent_live_sessions=5, current_value=6
    )
    assert out["recommended_value"] is None
