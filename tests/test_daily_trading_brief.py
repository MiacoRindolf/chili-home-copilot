"""DB-free tests for the daily trading brief orchestration module.

The DB and the summary builder are mocked; ``build_brief`` and
``generate_report`` run for real (they're pure). Output is written to pytest's
``tmp_path`` so nothing touches a real filesystem location or the database.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.trading import daily_trading_brief as mod


# A representative summary dict, shaped like build_trading_summary's output.
SAMPLE_SUMMARY = {
    "date": "2026-06-01",
    "net_pnl": 340.12,
    "win_rate": 0.5,
    "closes": [
        {"ticker": "AAPL", "pnl": 200.0, "pattern": "Breakout", "reason": "target"},
        {"ticker": "TSLA", "pnl": -33.0, "pattern": "Pullback", "reason": "stop"},
    ],
    "open_positions": [{"ticker": "BTC", "side": "long"}],
    "top_patterns": [
        {"id": "Breakout", "pnl": 200.0, "trades": 1, "payoff": 4.97},
    ],
}


def _fake_db():
    return MagicMock(name="db")


# ---------------------------------------------------------------------------
# generate_user_brief_html
# ---------------------------------------------------------------------------

def test_generate_user_brief_html_returns_html(monkeypatch):
    monkeypatch.setattr(mod, "build_trading_summary", lambda *a, **k: SAMPLE_SUMMARY)

    html = mod.generate_user_brief_html(_fake_db(), user_id=1)

    assert isinstance(html, str)
    assert "<!DOCTYPE html>" in html
    assert "Daily Trading Brief" in html
    # Real brief content from the sample summary should appear.
    assert "AAPL" in html
    assert "Breakout" in html


# ---------------------------------------------------------------------------
# persist_user_brief
# ---------------------------------------------------------------------------

def test_persist_user_brief_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "build_trading_summary", lambda *a, **k: SAMPLE_SUMMARY)
    out_dir = tmp_path / "briefs"

    path = mod.persist_user_brief(_fake_db(), user_id=7, out_dir=str(out_dir))

    assert path is not None
    assert path.endswith(".html")
    assert "brief_user_7" in path
    import os
    assert os.path.exists(path)
    content = open(path, encoding="utf-8").read()
    assert "Daily Trading Brief" in content


def test_persist_user_brief_swallows_errors(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(mod, "build_trading_summary", _boom)

    # Must NOT raise; returns None on failure.
    path = mod.persist_user_brief(_fake_db(), user_id=9, out_dir=str(tmp_path))

    assert path is None


# ---------------------------------------------------------------------------
# _active_user_ids
# ---------------------------------------------------------------------------

def test_active_user_ids_maps_rows_to_ints():
    db = MagicMock()
    db.query.return_value.all.return_value = [(1,), (2,), (3,)]

    assert mod._active_user_ids(db) == [1, 2, 3]


def test_active_user_ids_returns_empty_on_error():
    db = MagicMock()
    db.query.side_effect = RuntimeError("db down")

    assert mod._active_user_ids(db) == []


# ---------------------------------------------------------------------------
# run_daily_brief_for_all_users
# ---------------------------------------------------------------------------

def test_run_daily_brief_counts_success_and_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_active_user_ids", lambda db: [1, 2, 3])

    def _persist(db, user_id, out_dir, window_hours=24):
        if user_id == 2:
            return None  # simulated failure
        return str(tmp_path / f"brief_user_{user_id}.html")

    monkeypatch.setattr(mod, "persist_user_brief", _persist)

    result = mod.run_daily_brief_for_all_users(_fake_db(), str(tmp_path))

    assert result["generated"] == 2
    assert result["failed"] == 1
    assert result["paths"] == [
        str(tmp_path / "brief_user_1.html"),
        str(tmp_path / "brief_user_3.html"),
    ]


def test_run_daily_brief_empty_user_list(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_active_user_ids", lambda db: [])

    result = mod.run_daily_brief_for_all_users(_fake_db(), str(tmp_path))

    assert result["generated"] == 0
    assert result["failed"] == 0
    assert result["paths"] == []
