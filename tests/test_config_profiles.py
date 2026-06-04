"""Tests for config profile presets (P5)."""
from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

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


def test_bracket_watchdog_stale_after_sec_rejects_out_of_range_env(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("CHILI_BRACKET_WATCHDOG_STALE_AFTER_SEC", "29")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://x:x@localhost/test")

    monkeypatch.setenv("CHILI_BRACKET_WATCHDOG_STALE_AFTER_SEC", "3601")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://x:x@localhost/test")


def test_bracket_watchdog_stale_after_sec_accepts_documented_bounds(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("CHILI_BRACKET_WATCHDOG_STALE_AFTER_SEC", "30")
    lower = Settings(_env_file=None, database_url="postgresql://x:x@localhost/test")
    assert lower.chili_bracket_watchdog_stale_after_sec == 30

    monkeypatch.setenv("CHILI_BRACKET_WATCHDOG_STALE_AFTER_SEC", "3600")
    upper = Settings(_env_file=None, database_url="postgresql://x:x@localhost/test")
    assert upper.chili_bracket_watchdog_stale_after_sec == 3600


def test_settings_has_no_duplicate_field_declarations():
    tree = ast.parse(Path("app/config.py").read_text(encoding="utf-8"))
    settings_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    seen: dict[str, list[int]] = {}
    for stmt in settings_class.body:
        name: str | None = None
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
        elif isinstance(stmt, ast.Assign):
            names = [target.id for target in stmt.targets if isinstance(target, ast.Name)]
            name = names[0] if names else None
        if name is not None:
            seen.setdefault(name, []).append(stmt.lineno)

    duplicates = {name: lines for name, lines in seen.items() if len(lines) > 1}
    assert duplicates == {}
