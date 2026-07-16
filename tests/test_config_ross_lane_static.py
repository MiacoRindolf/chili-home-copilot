from __future__ import annotations

from pathlib import Path


def test_ross_live_same_session_reentry_setting_declared_once() -> None:
    config_src = Path("app/config.py").read_text(encoding="utf-8")

    assert config_src.count("chili_momentum_live_same_session_reentry_enabled:") == 1
