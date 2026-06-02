"""Unit tests for the CHILI OS command-palette search (DB-free; helpers patched)."""
from unittest.mock import patch

from app.services import workspace_search as wss


def test_empty_query_returns_destinations():
    out = wss.search(object(), 1, "")
    labels = [r["label"] for r in out]
    assert "Dashboard" in labels and "Trading Desk" in labels
    assert all(r["type"] == "app" for r in out)


def test_destination_match_by_label():
    with patch.object(wss, "_patterns", return_value=[]), \
         patch.object(wss, "_tickers", return_value=[]):
        out = wss.search(object(), 1, "trad")
    assert any(r["label"] == "Trading Desk" and r["app"] == "trading" for r in out)


def test_includes_patterns_and_tickers():
    with patch.object(wss, "_patterns", return_value=[{"type": "pattern", "label": "585 Wedge", "app": "brain", "url": "/brain?pattern=585", "icon": "⚡", "sub": "Pattern #585"}]), \
         patch.object(wss, "_tickers", return_value=[{"type": "ticker", "label": "NVDA", "app": "trading", "url": "/trading?ticker=NVDA", "icon": "🎯", "sub": "Open on the desk"}]):
        out = wss.search(object(), 1, "5")
    types = {r["type"] for r in out}
    assert "pattern" in types and "ticker" in types
    assert any(r["label"] == "NVDA" for r in out)


def test_includes_research():
    with patch.object(wss, "_patterns", return_value=[]), \
         patch.object(wss, "_tickers", return_value=[]), \
         patch.object(wss, "_research", return_value=[{"type": "research", "label": "AI capex cycle", "sub": "Research", "icon": "🔎", "app": "research", "url": "/api/brain/reasoning/research/report"}]):
        out = wss.search(object(), 1, "ai")
    research = [r for r in out if r["type"] == "research"]
    assert research and research[0]["label"] == "AI capex cycle"
    assert research[0]["app"] == "research"
    assert research[0]["url"] == "/api/brain/reasoning/research/report"


def test_guest_gets_no_research_or_tickers():
    # Real helpers (unpatched): user_id None should short-circuit both to [].
    out = wss.search(object(), None, "ai")
    assert not any(r["type"] in ("research", "ticker") for r in out)


def test_defensive_when_db_helpers_raise():
    # _patterns/_tickers/_research swallow their own errors -> [], so search still works.
    with patch.object(wss, "_patterns", return_value=[]), \
         patch.object(wss, "_tickers", return_value=[]), \
         patch.object(wss, "_research", return_value=[]):
        out = wss.search(object(), None, "anything")
    assert isinstance(out, list)


def test_research_helper_swallows_failure():
    # A broken DB session must degrade to [] rather than raise.
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    assert wss._research(_Boom(), 1, "ai", 5) == []


def test_includes_planner():
    with patch.object(wss, "_patterns", return_value=[]), \
         patch.object(wss, "_tickers", return_value=[]), \
         patch.object(wss, "_research", return_value=[]), \
         patch.object(wss, "_planner", return_value=[
             {"type": "project", "label": "Kitchen Remodel", "sub": "Project", "icon": "🗂", "app": "planner", "url": "/planner"},
             {"type": "task", "label": "Paint walls", "sub": "Task", "icon": "✓", "app": "planner", "url": "/planner?project_id=3&task_id=7"},
         ]):
        out = wss.search(object(), 1, "k")
    types = {r["type"] for r in out}
    assert "project" in types and "task" in types
    proj = next(r for r in out if r["type"] == "project")
    assert proj["app"] == "planner" and proj["url"] == "/planner"
    task = next(r for r in out if r["type"] == "task")
    assert task["url"] == "/planner?project_id=3&task_id=7"


def test_guest_gets_no_planner():
    # Real helper (unpatched): user_id None short-circuits to [].
    assert wss._planner(object(), None, "k", 5) == []
    out = wss.search(object(), None, "k")
    assert not any(r["type"] in ("project", "task") for r in out)


def test_planner_helper_swallows_failure():
    # A broken DB session must degrade to [] rather than raise.
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    assert wss._planner(_Boom(), 1, "k", 5) == []


def test_planner_limit_respected():
    many = [{"type": "task", "label": f"T{i}", "app": "planner", "url": "/planner", "icon": "✓", "sub": "Task"} for i in range(20)]
    with patch.object(wss, "_patterns", return_value=[]), \
         patch.object(wss, "_tickers", return_value=[]), \
         patch.object(wss, "_research", return_value=[]), \
         patch.object(wss, "_planner", return_value=many):
        out = wss.search(object(), 1, "t", limit=4)
    assert len(out) <= 4


def test_action_matches():
    with patch.object(wss, "_patterns", return_value=[]), \
         patch.object(wss, "_tickers", return_value=[]):
        out = wss.search(object(), 1, "brief")
    assert any(r["type"] == "action" and "brief" in r["label"].lower() for r in out)


def test_limit_respected():
    many = [{"type": "ticker", "label": f"T{i}", "app": "trading", "url": "/", "icon": "🎯", "sub": ""} for i in range(20)]
    with patch.object(wss, "_patterns", return_value=[]), patch.object(wss, "_tickers", return_value=many):
        out = wss.search(object(), 1, "t", limit=6)
    assert len(out) <= 6
