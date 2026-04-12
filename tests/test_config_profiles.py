"""Tests for config profile presets (P5)."""
from __future__ import annotations

import os
from unittest.mock import patch

from app.config import CONFIG_PROFILES


def _make_settings(env: dict[str, str] | None = None):
    """Instantiate a fresh Settings with given env overrides."""
    base = {"DATABASE_URL": "postgresql://x:x@localhost/test"}
    if env:
        base.update(env)
    with patch.dict(os.environ, base, clear=False):
        # Re-import to get a fresh class (avoids cached singleton)
        from app.config import Settings
        return Settings(**{k: v for k, v in base.items() if k.islower()}, **{
            k: v for k, v in base.items() if k == k.upper() and k != "DATABASE_URL"
        })


def _make(profile: str = "default", **extra: str):
    """Build Settings with a specific profile."""
    from app.config import Settings
    env = {"DATABASE_URL": "postgresql://x:x@localhost/test"}
    env.update(extra)
    with patch.dict(os.environ, env, clear=False):
        overrides: dict = {"brain_config_profile": profile, "database_url": "postgresql://x:x@localhost/test"}
        return Settings(**overrides)


def test_default_profile_is_noop():
    s = _make("default")
    assert s.brain_config_profile == "default"
    assert s.brain_backtest_parallel == 18  # unchanged default


def test_conservative_profile_applies():
    s = _make("conservative")
    assert s.brain_backtest_parallel == 6
    assert s.brain_research_integrity_strict is True


def test_aggressive_profile_applies():
    s = _make("aggressive")
    assert s.brain_backtest_parallel == 24


def test_env_override_wins_over_profile():
    """Explicit value should beat profile default."""
    from app.config import Settings
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x:x@localhost/test"}, clear=False):
        s = Settings(
            brain_config_profile="conservative",
            brain_backtest_parallel=99,
            database_url="postgresql://x:x@localhost/test",
        )
    assert s.brain_backtest_parallel == 99  # not 6


def test_unknown_profile_is_noop():
    s = _make("nonexistent")
    assert s.brain_backtest_parallel == 18  # default


def test_research_profile():
    s = _make("research")
    assert s.brain_research_integrity_strict is True
    assert s.brain_research_integrity_enabled is True


def test_config_profiles_dict_has_expected_keys():
    assert set(CONFIG_PROFILES.keys()) == {"default", "conservative", "aggressive", "research"}


def test_get_active_profile_info():
    from app.config import get_active_profile_info
    info = get_active_profile_info()
    assert "profile" in info
    assert "overrides" in info
    assert isinstance(info["overrides"], dict)
