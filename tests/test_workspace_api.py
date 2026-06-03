"""Contract tests for the CHILI OS JSON API endpoints (WS-33).

Two read-only JSON surfaces power the OS workspace:

* ``GET /api/workspace/desktop`` -> ``desktop_live.build_live`` — the live
  cockpit view-model (P/L, counts, safety status, market state).
* ``GET /api/workspace/search``  -> ``workspace_search.search`` — the command
  palette (⌘K) destinations + ranked results.

These tests guard the *shape* of those payloads, not their data: the front-end
binds to these keys, so a silent rename would break the OS without a test
failing anywhere else. They use the guest ``client`` fixture — both endpoints
resolve identity from the cookie and degrade to defensive/empty data for a
guest while still returning 200.
"""
from __future__ import annotations

# Apps a search result may declare via its optional ``app`` key (opens that
# surface as an OS window). Mirrors workspace_search._DESTINATIONS.
_VALID_APPS = {"dashboard", "chat", "trading", "brain", "research", "planner"}

# The exact top-level key set returned by desktop_live.build_live.
_DESKTOP_TOP_LEVEL_KEYS = {
    "ok",
    "net_pnl",
    "net_pnl_fmt",
    "net_pnl_up",
    "win_rate_fmt",
    "open_positions",
    "closes_today",
    "top_patterns",
    "positions",
    "closes",
    "unrealized_total_fmt",
    "unrealized_total_up",
    "unrealized_priced",
    "total_pnl_fmt",
    "equity_curve",
    "kill_switch",
    "breaker",
    "market",
    "last_trade_iso",
    "data_fresh_iso",
}


class TestWorkspaceDesktop:
    def test_desktop_returns_200_and_ok(self, client):
        resp = client.get("/api/workspace/desktop")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_desktop_has_all_documented_top_level_keys(self, client):
        body = client.get("/api/workspace/desktop").json()
        assert set(body.keys()) == _DESKTOP_TOP_LEVEL_KEYS

    def test_desktop_positions_and_closes_are_lists(self, client):
        body = client.get("/api/workspace/desktop").json()
        assert isinstance(body["positions"], list)
        assert isinstance(body["closes"], list)

    def test_desktop_safety_subobjects_are_dicts_with_ok(self, client):
        body = client.get("/api/workspace/desktop").json()
        for key in ("kill_switch", "breaker", "market"):
            section = body[key]
            assert isinstance(section, dict), key
            assert "ok" in section, key

    def test_desktop_iso_timestamps_are_str_or_none(self, client):
        body = client.get("/api/workspace/desktop").json()
        for key in ("last_trade_iso", "data_fresh_iso"):
            assert body[key] is None or isinstance(body[key], str), key

    def test_desktop_count_fields_are_ints(self, client):
        body = client.get("/api/workspace/desktop").json()
        for key in ("open_positions", "closes_today", "top_patterns"):
            assert isinstance(body[key], int), key


class TestWorkspaceSearch:
    def test_empty_query_returns_destinations(self, client):
        resp = client.get("/api/workspace/search")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        results = body["results"]
        assert isinstance(results, list) and results, "empty q must return destinations"
        for r in results:
            assert isinstance(r, dict)
            assert {"type", "label", "url"} <= set(r.keys())

    def test_empty_query_apps_are_valid(self, client):
        results = client.get("/api/workspace/search").json()["results"]
        for r in results:
            if "app" in r:
                assert r["app"] in _VALID_APPS, r

    def test_query_returns_list_with_consistent_shape(self, client):
        # Guest: live-data groups short-circuit to [], so results may be empty,
        # but the contract (list of dicts with type/label/url) must hold.
        body = client.get("/api/workspace/search", params={"q": "trading"}).json()
        assert body["ok"] is True
        results = body["results"]
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, dict)
            assert {"type", "label", "url"} <= set(r.keys())
            if "app" in r:
                assert r["app"] in _VALID_APPS, r

    def test_no_match_query_returns_list(self, client):
        body = client.get(
            "/api/workspace/search", params={"q": "zzz-no-such-thing-zzz"}
        ).json()
        assert body["ok"] is True
        assert isinstance(body["results"], list)

    def test_long_query_does_not_500(self, client):
        # Defensive contract: the router caps q at max_length=80, so an 80-char
        # query is the longest valid input and must still return a clean 200.
        resp = client.get("/api/workspace/search", params={"q": "a" * 80})
        assert resp.status_code == 200
        assert isinstance(resp.json()["results"], list)
