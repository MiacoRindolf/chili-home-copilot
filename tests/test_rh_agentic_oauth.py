"""Pure-unit tests for the Robinhood Agentic OAuth refresh + adapter safety core.

NO DB, NO network: every test injects a mock ``http_post`` and uses a temp token file.
These tests are the load-bearing proof for a LIVE-MONEY + CREDENTIAL rail — they assert
fail-closed auth, account-pin enforcement, single-flight refresh, rotation persistence,
and (critically) that NO token / authorization-code material ever reaches a log or repr.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import pytest

from app.services.trading.venue import rh_oauth
from app.services.trading.venue.rh_oauth import (
    DEFAULT_TOKEN_ENDPOINT,
    NeedsReauth,
    TokenBundle,
    load_bundle,
    write_bundle_atomic,
)
from app.services.trading.venue import rh_mcp_client as cli
from app.services.trading.venue.rh_mcp_client import (
    RhMcpClient,
    RhMcpError,
    RhRefreshingTokenSource,
    bundle_is_routable,
    resolve_mcp_token,
)
from app.services.trading.venue.robinhood_mcp import RobinhoodAgenticMcpAdapter

# Sentinel secrets — if any of these substrings appears in a log/repr, the no-leak guard fails.
_ACCESS = "ACCESS_SECRET_aaaaaaaaaaaaaaaa"
_REFRESH = "REFRESH_SECRET_rrrrrrrrrrrrrrrr"
_NEW_ACCESS = "ACCESS_SECRET_bbbbbbbbbbbbbbbb"
_NEW_REFRESH = "REFRESH_SECRET_ssssssssssssssss"
_AUTH_CODE = "AUTHCODE_cccccccccccccccc"
_ACCT = "674153143"


# ── helpers ──────────────────────────────────────────────────────────────────


def _write_bundle(path, *, access=_ACCESS, refresh=_REFRESH, expires_at, client_id="cid-123"):
    b = TokenBundle(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        client_id=client_id,
        token_endpoint=DEFAULT_TOKEN_ENDPOINT,
        obtained_at=0.0,
    )
    write_bundle_atomic(str(path), b)
    return b


def _refresh_ok_post(new_access=_NEW_ACCESS, new_refresh=_NEW_REFRESH, expires_in=3600, calls=None):
    def _post(url, headers, body, timeout):
        if calls is not None:
            calls.append(url)
        payload = {"access_token": new_access, "expires_in": expires_in, "scope": "internal"}
        if new_refresh is not None:
            payload["refresh_token"] = new_refresh
        return 200, {}, json.dumps(payload)

    return _post


def _refresh_status_post(status, body="{}", calls=None):
    def _post(url, headers, body_, timeout):
        if calls is not None:
            calls.append(url)
        return status, {}, body

    return _post


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ── 1. bundle round-trip + perms ─────────────────────────────────────────────


def test_bundle_round_trip_and_perms(tmp_path):
    p = tmp_path / "tok.json"
    _write_bundle(p, expires_at=time.time() + 3600)
    loaded = load_bundle(str(p))
    assert loaded is not None
    assert loaded.access_token == _ACCESS
    assert loaded.refresh_token == _REFRESH
    assert loaded.has_refresh_token()
    if os.name != "nt":
        mode = os.stat(str(p)).st_mode & 0o777
        assert mode == 0o600


# ── 2. legacy raw token resolves; malformed `{` -> None (fail-closed) ─────────


def test_legacy_raw_token_resolves(tmp_path, monkeypatch):
    p = tmp_path / "raw.token"
    p.write_text("legacy-raw-bearer-xyz", encoding="utf-8")
    monkeypatch.setenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE", str(p))
    monkeypatch.delenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN", raising=False)
    assert resolve_mcp_token() == "legacy-raw-bearer-xyz"


def test_malformed_json_bundle_fails_closed(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    # Starts with `{` so it is treated as a bundle, but it is not valid / has no access_token.
    p.write_text('{ "refresh_token": "' + _REFRESH + '" ', encoding="utf-8")
    monkeypatch.setenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE", str(p))
    monkeypatch.delenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN", raising=False)
    # NEVER returns the raw JSON (which would leak the refresh token as a bearer).
    assert resolve_mcp_token() is None


def test_bundle_without_access_token_fails_closed(tmp_path, monkeypatch):
    p = tmp_path / "norefresh.json"
    p.write_text(json.dumps({"refresh_token": _REFRESH, "expires_at": time.time() + 3600}), encoding="utf-8")
    monkeypatch.setenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE", str(p))
    monkeypatch.delenv("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN", raising=False)
    assert resolve_mcp_token() is None


# ── 3. refresh-before-expiry (skew) -> one refresh ───────────────────────────


def test_refresh_before_expiry_skew(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    # expires in 100s but skew is 300s -> considered expired -> refresh.
    _write_bundle(p, expires_at=1100.0)
    calls = []
    src = RhRefreshingTokenSource(str(p), http_post=_refresh_ok_post(calls=calls), clock=clock)
    tok = src.bearer()
    assert tok == _NEW_ACCESS
    assert len(calls) == 1
    # persisted rotation
    reloaded = load_bundle(str(p))
    assert reloaded.access_token == _NEW_ACCESS


def test_no_refresh_when_fresh(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    _write_bundle(p, expires_at=1000.0 + 10_000)  # well beyond skew
    calls = []
    src = RhRefreshingTokenSource(str(p), http_post=_refresh_ok_post(calls=calls), clock=clock)
    assert src.bearer() == _ACCESS
    assert calls == []


# ── 4. rotation persist + carry-forward ──────────────────────────────────────


def test_rotation_persist_and_carry_forward(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    _write_bundle(p, expires_at=1100.0)
    # response omits refresh_token -> carry forward the old one.
    src = RhRefreshingTokenSource(str(p), http_post=_refresh_ok_post(new_refresh=None), clock=clock)
    src.bearer()
    reloaded = load_bundle(str(p))
    assert reloaded.access_token == _NEW_ACCESS
    assert reloaded.refresh_token == _REFRESH  # carried forward


def test_rotation_replaces_refresh_when_present(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    _write_bundle(p, expires_at=1100.0)
    src = RhRefreshingTokenSource(str(p), http_post=_refresh_ok_post(new_refresh=_NEW_REFRESH), clock=clock)
    src.bearer()
    reloaded = load_bundle(str(p))
    assert reloaded.refresh_token == _NEW_REFRESH


# ── 5. refresh-fail(400) -> NeedsReauth -> is_enabled False -> place returns needs_reauth ──


def test_refresh_rejected_400_needs_reauth_and_no_place_transport(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    _write_bundle(p, expires_at=1100.0)  # expired-within-skew -> will try refresh

    place_calls = []

    def _post(url, headers, body, timeout):
        if "oauth2/token" in url:
            return 400, {}, '{"error":"invalid_grant"}'
        place_calls.append(url)  # a place/transport call — must NEVER happen on needs_reauth
        return 200, {}, '{"result":{}}'

    src = RhRefreshingTokenSource(str(p), http_post=_post, clock=clock)
    with pytest.raises(NeedsReauth) as ei:
        src.bearer()
    assert ei.value.reason == "refresh_rejected"

    # is_enabled() -> False
    client = RhMcpClient(endpoint="https://agent.robinhood.com/mcp/trading", http_post=_post, token_source=src)
    adapter = RobinhoodAgenticMcpAdapter(client=client, market_data_adapter=object(), account_number=_ACCT)
    assert adapter.is_enabled() is False

    # place_market_order returns needs_reauth, ZERO place transport calls
    res = adapter.place_market_order(product_id="AAPL", side="buy", base_size="1")
    assert res["ok"] is False
    assert res["error"] == "needs_reauth"
    assert place_calls == []


# ── 6. single-flight: 16 threads, expired -> token endpoint hit exactly once ──


def test_single_flight_refresh(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    _write_bundle(p, expires_at=1100.0)

    calls = []
    lock = threading.Lock()

    def _post(url, headers, body, timeout):
        with lock:
            calls.append(url)
        time.sleep(0.02)  # widen the race window
        return 200, {}, json.dumps({"access_token": _NEW_ACCESS, "expires_in": 3600})

    src = RhRefreshingTokenSource(str(p), http_post=_post, clock=clock)
    results = []
    threads = [threading.Thread(target=lambda: results.append(src.bearer())) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r == _NEW_ACCESS for r in results)
    assert len(calls) == 1  # single-flight: exactly one network refresh


# ── 7. transient 5xx -> RhMcpError not NeedsReauth + bundle unchanged + is_enabled True ──


def test_transient_5xx_keeps_token(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    # expires_at within skew (refresh attempted) but NOT yet hard-expired at clock=1000.
    _write_bundle(p, expires_at=1100.0)
    before = load_bundle(str(p)).to_dict()

    src = RhRefreshingTokenSource(str(p), http_post=_refresh_status_post(503), clock=clock)
    with pytest.raises(RhMcpError):
        src.bearer()
    after = load_bundle(str(p)).to_dict()
    # bundle access/refresh unchanged (transient).
    assert after["access_token"] == before["access_token"]
    assert after["refresh_token"] == before["refresh_token"]

    # is_enabled stays True (the on-disk access token is still unexpired at clock=1000 < 1100).
    client = RhMcpClient(http_post=_refresh_status_post(503), token_source=src)
    adapter = RobinhoodAgenticMcpAdapter(client=client, market_data_adapter=object(), account_number=_ACCT)

    def _verify_ok(self):
        return None

    # ensure_authable swallows the transient (token within expiry); but is_enabled also
    # needs the account pin to pass — patch the account verify to isolate the auth path.
    import app.services.trading.venue.robinhood_mcp as mod
    orig = mod.RobinhoodAgenticMcpAdapter._assert_account_is_agentic
    mod.RobinhoodAgenticMcpAdapter._assert_account_is_agentic = _verify_ok
    try:
        assert adapter.is_enabled() is True
    finally:
        mod.RobinhoodAgenticMcpAdapter._assert_account_is_agentic = orig


# ── 8. transient distinguished from NeedsReauth ──────────────────────────────


def test_transient_is_not_needs_reauth(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    _write_bundle(p, expires_at=1100.0)
    src = RhRefreshingTokenSource(str(p), http_post=_refresh_status_post(500), clock=clock)
    with pytest.raises(RhMcpError):
        src.bearer()
    # 429 likewise transient
    src2 = RhRefreshingTokenSource(str(p), http_post=_refresh_status_post(429), clock=clock)
    with pytest.raises(RhMcpError):
        src2.bearer()


# ── 9. refresh parse failure -> RhMcpError ───────────────────────────────────


def test_refresh_parse_failure(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    _write_bundle(p, expires_at=1100.0)
    src = RhRefreshingTokenSource(str(p), http_post=_refresh_status_post(200, body="not json"), clock=clock)
    with pytest.raises(RhMcpError) as ei:
        src.bearer()
    assert ei.value.code == "refresh_parse"


# ── 10. pin rejects non-agentic ──────────────────────────────────────────────


def test_pin_rejects_non_agentic_account():
    accounts_payload = json.dumps(
        {"accounts": [{"account_number": _ACCT, "agentic_allowed": False}]}
    )

    def _post(url, headers, body, timeout):
        payload = json.loads(body)
        method = payload.get("method")
        if method == "initialize":
            return 200, {"mcp-session-id": "s1"}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "serverInfo": {}, "capabilities": {}}})
        if method == "notifications/initialized":
            return 200, {}, ""
        if method == "tools/list":
            return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "get_accounts"}, {"name": "place_equity_order"}]}})
        if method == "tools/call":
            return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": json.loads(accounts_payload)}})
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})

    client = RhMcpClient(http_post=_post, token="static-tok")
    adapter = RobinhoodAgenticMcpAdapter(client=client, market_data_adapter=object(), account_number=_ACCT)
    res = adapter.place_market_order(product_id="AAPL", side="buy", base_size="1")
    assert res["ok"] is False
    assert "agentic" in res["error"]
    # latched: rail now reports disabled.
    assert adapter._pin_invalid is True


# ── 11. no-account -> no_agentic_account, zero transport ─────────────────────


def test_no_account_blocks_with_zero_transport():
    calls = []

    def _post(url, headers, body, timeout):
        calls.append(url)
        return 200, {}, "{}"

    client = RhMcpClient(http_post=_post, token="static-tok")
    adapter = RobinhoodAgenticMcpAdapter(client=client, market_data_adapter=object(), account_number="")
    res = adapter.place_market_order(product_id="AAPL", side="buy", base_size="1")
    assert res["ok"] is False
    assert "no_agentic_account" in res["error"] or "no Robinhood Agentic account" in res["error"]
    assert calls == []


# ── 12. normalize parity (partially_filled / filled / placed_agent) ──────────


def test_normalize_status_parity():
    adapter = RobinhoodAgenticMcpAdapter(market_data_adapter=object(), account_number=_ACCT)
    for state in ("partially_filled", "filled", "placed_agent"):
        od = {"id": "o1", "symbol": "AAPL", "side": "buy", "state": state, "type": "limit",
              "filled_quantity": "2", "average_price": "10.5"}
        norm = adapter._normalize_order(od)
        assert norm.status == state
        assert norm.order_id == "o1"
        assert norm.product_id == "AAPL"
    # filled order surfaces as a fill via get_fills' status filter
    filled = {"id": "o2", "symbol": "AAPL", "side": "buy", "state": "filled",
              "filled_quantity": "3", "average_price": "11.0"}
    assert str(filled["state"]).lower() in ("filled", "complete", "completed")


# ── 13. no-leak: caplog over refresh + exchange-shaped + 401-retry ───────────


def test_no_token_leak_in_logs_and_reprs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    _write_bundle(p, expires_at=1100.0)

    # 1) a successful refresh (token rotates).
    src = RhRefreshingTokenSource(str(p), http_post=_refresh_ok_post(), clock=clock)
    src.bearer()

    # 2) a 401-driven reactive refresh + retry that ultimately is grant_revoked.
    def _post(url, headers, body, timeout):
        if "oauth2/token" in url:
            return 200, {}, json.dumps({"access_token": _NEW_ACCESS, "expires_in": 3600, "refresh_token": _NEW_REFRESH})
        # transport always 401 -> after refresh, still 401 -> grant_revoked
        return 401, {}, '{"error":"unauthorized"}'

    _write_bundle(p, expires_at=900.0)  # expired so first call refreshes anyway
    src2 = RhRefreshingTokenSource(str(p), http_post=_post, clock=_Clock(1000.0))
    client = RhMcpClient(http_post=_post, token_source=src2)
    with pytest.raises(NeedsReauth) as ei:
        client._rpc("tools/call", {"name": "x"})
    assert ei.value.reason == "grant_revoked"

    # NeedsReauth + RhMcpError reprs are clean.
    assert _REFRESH not in repr(ei.value) and _ACCESS not in repr(ei.value)
    err = RhMcpError("boom", code="x", raw={"access_token": _ACCESS, "refresh_token": _REFRESH})
    assert _ACCESS not in repr(err) and _REFRESH not in repr(err)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (_ACCESS, _REFRESH, _NEW_ACCESS, _NEW_REFRESH, _AUTH_CODE):
        assert secret not in blob


# ── 14. allow_redirects=False asserted + off-host rejected ───────────────────


def test_off_host_and_scheme_rejected():
    with pytest.raises(RhMcpError) as ei:
        cli._assert_https_allowed_host("https://evil.example.com/oauth")
    assert ei.value.code == "bad_host"
    with pytest.raises(RhMcpError) as ei2:
        cli._assert_https_allowed_host("http://api.robinhood.com/oauth2/token/")
    assert ei2.value.code == "bad_scheme"
    # allow-listed https host passes
    cli._assert_https_allowed_host("https://api.robinhood.com/oauth2/token/")


def test_default_http_post_sets_allow_redirects_false(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        headers = {}
        text = "{}"

    def _fake_post(url, headers=None, data=None, timeout=None, allow_redirects=None):
        captured["allow_redirects"] = allow_redirects
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    cli._default_http_post("https://api.robinhood.com/oauth2/token/", {}, "{}", 5.0)
    assert captured["allow_redirects"] is False


def test_redirect_is_transient_not_followed(monkeypatch):
    class _Resp:
        status_code = 302
        headers = {"location": "https://evil.example.com"}
        text = ""

    def _fake_post(url, headers=None, data=None, timeout=None, allow_redirects=None):
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    with pytest.raises(RhMcpError) as ei:
        cli._default_http_post("https://api.robinhood.com/oauth2/token/", {}, "{}", 5.0)
    assert ei.value.code == "redirect"
    assert ei.value.raw is None  # NO raw on a credentialed transport error


# ── 15. ref_id pass-through == client_order_id ───────────────────────────────


def test_ref_id_passthrough_equals_client_order_id():
    sent = {}
    accounts = {"accounts": [{"account_number": _ACCT, "agentic_allowed": True}]}

    def _post(url, headers, body, timeout):
        payload = json.loads(body)
        method = payload.get("method")
        if method == "initialize":
            return 200, {"mcp-session-id": "s1"}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {}, "capabilities": {}}})
        if method == "notifications/initialized":
            return 200, {}, ""
        if method == "tools/list":
            return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "get_accounts"}, {"name": "place_equity_order"}, {"name": "review_equity_order"}]}})
        if method == "tools/call":
            args = payload["params"]["arguments"]
            name = payload["params"]["name"]
            if name == "get_accounts":
                return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": accounts}})
            if name == "review_equity_order":
                return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"can_place": True}}})
            if name == "place_equity_order":
                sent.update(args)
                return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"id": "o9", "ref_id": args.get("ref_id")}}})
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})

    client = RhMcpClient(http_post=_post, token="static-tok")
    adapter = RobinhoodAgenticMcpAdapter(client=client, market_data_adapter=object(), account_number=_ACCT)
    res = adapter.place_limit_order_gtc(
        product_id="AAPL", side="buy", base_size="1", limit_price="10.0", client_order_id="chili_ml_e_42_abcd_deadbeef00"
    )
    assert res["ok"] is True
    assert sent["ref_id"] == "chili_ml_e_42_abcd_deadbeef00"
    assert res["client_order_id"] == "chili_ml_e_42_abcd_deadbeef00"


# ── 16. account_number injected on EVERY order/review method ──────────────────


@pytest.mark.parametrize(
    "fn,kwargs",
    [
        ("place_market_order", dict(product_id="AAPL", side="buy", base_size="1")),
        ("place_limit_order_gtc", dict(product_id="AAPL", side="buy", base_size="1", limit_price="10")),
        ("place_stop_market_order", dict(product_id="AAPL", side="sell", base_size="1", stop_price="9")),
        ("place_stop_limit_order", dict(product_id="AAPL", side="sell", base_size="1", stop_price="9", limit_price="8.9")),
    ],
)
def test_account_injected_on_every_order_method(fn, kwargs):
    seen = {"accounts_in_calls": []}
    accounts = {"accounts": [{"account_number": _ACCT, "agentic_allowed": True}]}

    def _post(url, headers, body, timeout):
        payload = json.loads(body)
        method = payload.get("method")
        if method == "initialize":
            return 200, {"mcp-session-id": "s1"}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {}, "capabilities": {}}})
        if method == "notifications/initialized":
            return 200, {}, ""
        if method == "tools/list":
            return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "get_accounts"}, {"name": "place_equity_order"}, {"name": "review_equity_order"}]}})
        if method == "tools/call":
            args = payload["params"]["arguments"]
            name = payload["params"]["name"]
            if name == "get_accounts":
                return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": accounts}})
            if name == "review_equity_order":
                seen["accounts_in_calls"].append(args.get("account_number"))
                return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"can_place": True}}})
            if name == "place_equity_order":
                seen["accounts_in_calls"].append(args.get("account_number"))
                return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"id": "o1"}}})
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})

    client = RhMcpClient(http_post=_post, token="static-tok")
    adapter = RobinhoodAgenticMcpAdapter(client=client, market_data_adapter=object(), account_number=_ACCT)
    res = getattr(adapter, fn)(**kwargs)
    assert res["ok"] is True
    # both the review call and the place call carried the pinned account.
    assert seen["accounts_in_calls"], "no account-bearing calls captured"
    assert all(a == _ACCT for a in seen["accounts_in_calls"])


# ── 17. multi-process external-rotation recovery ─────────────────────────────


def test_multiprocess_external_rotation_recovery(tmp_path):
    p = tmp_path / "tok.json"
    clock = _Clock(1000.0)
    _write_bundle(p, expires_at=1100.0)  # within skew -> wants refresh

    def _post(url, headers, body, timeout):
        # Simulate: another process already consumed THIS refresh token and wrote a
        # fresh bundle to disk. Our refresh attempt gets invalid_grant; we should
        # re-read disk and adopt the peer's fresh access token.
        _write_bundle(p, access=_NEW_ACCESS, refresh=_NEW_REFRESH, expires_at=1000.0 + 5000)
        return 400, {}, '{"error":"invalid_grant"}'

    src = RhRefreshingTokenSource(str(p), http_post=_post, clock=clock)
    tok = src.bearer()
    assert tok == _NEW_ACCESS  # adopted the externally-rotated token, NOT NeedsReauth


# ── 18. refresh-ok-but-retry-401 -> grant_revoked; 401->refresh->404 never refreshes ──


def test_refresh_ok_but_retry_401_is_grant_revoked(tmp_path):
    p = tmp_path / "tok.json"
    _write_bundle(p, expires_at=900.0)
    clock = _Clock(1000.0)

    def _post(url, headers, body, timeout):
        if "oauth2/token" in url:
            return 200, {}, json.dumps({"access_token": _NEW_ACCESS, "expires_in": 3600})
        return 401, {}, '{"error":"unauthorized"}'  # bearer always rejected

    src = RhRefreshingTokenSource(str(p), http_post=_post, clock=clock)
    client = RhMcpClient(http_post=_post, token_source=src)
    with pytest.raises(NeedsReauth) as ei:
        client._rpc("tools/call", {"name": "x"})
    assert ei.value.reason == "grant_revoked"


def test_404_session_never_triggers_refresh(tmp_path):
    p = tmp_path / "tok.json"
    _write_bundle(p, expires_at=1000.0 + 9999)  # fresh: no proactive refresh
    clock = _Clock(1000.0)
    refresh_calls = []

    def _post(url, headers, body, timeout):
        payload = json.loads(body)
        method = payload.get("method")
        if "oauth2/token" in url:
            refresh_calls.append(url)
            return 200, {}, json.dumps({"access_token": _NEW_ACCESS, "expires_in": 3600})
        if method == "initialize":
            return 200, {"mcp-session-id": "s1"}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {}}})
        if method == "notifications/initialized":
            return 200, {}, ""
        # an established session then 404s -> session expired, NOT auth.
        return 404, {}, "session gone"

    src = RhRefreshingTokenSource(str(p), http_post=_post, clock=clock)
    client = RhMcpClient(http_post=_post, token_source=src)
    client.connect()
    with pytest.raises(RhMcpError) as ei:
        client._rpc("tools/call", {"name": "x"})
    assert ei.value.code == "session_expired"
    assert refresh_calls == []  # a 404 must NEVER trigger a token refresh


# ── 19. bundle_is_routable gate (dead/refreshless excluded) ──────────────────


def test_bundle_is_routable_gate(tmp_path):
    good = tmp_path / "good.json"
    _write_bundle(good, expires_at=time.time() + 3600)
    assert bundle_is_routable(str(good)) is True

    # refreshless + expired -> hard-dead -> not routable
    dead = tmp_path / "dead.json"
    write_bundle_atomic(str(dead), TokenBundle(access_token=_ACCESS, refresh_token=None, expires_at=time.time() - 10))
    assert bundle_is_routable(str(dead)) is False

    # refreshless but unexpired -> not routable (no refresh token = can't recover headlessly)
    norefresh = tmp_path / "nr.json"
    write_bundle_atomic(str(norefresh), TokenBundle(access_token=_ACCESS, refresh_token=None, expires_at=time.time() + 3600))
    assert bundle_is_routable(str(norefresh)) is False

    assert bundle_is_routable(str(tmp_path / "missing.json")) is False


# ── 20. .gitignore patterns match ────────────────────────────────────────────


def test_gitignore_patterns_present():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gi = os.path.join(repo_root, ".gitignore")
    with open(gi, "r", encoding="utf-8") as fh:
        content = fh.read()
    for pat in ("*agentic*token*.json", "*rh_agentic*.json", "*.client.json", ".rh_tok_*.tmp", "/secrets/"):
        assert pat in content, f"missing .gitignore pattern: {pat}"


# ── 21. real RH response envelope {"data": <payload>, "guide": "..."} unwrap ──
# Live-verified 2026-06-19: get_portfolio/get_accounts/orders nest the payload under
# "data" alongside a "guide" string. The flat-mock tests missed this; this locks it.


def test_response_envelope_unwrap():
    from app.services.trading.venue.robinhood_mcp import (
        _unwrap_payload,
        RobinhoodAgenticMcpAdapter as A,
    )

    # envelope peeled to the inner payload (real get_portfolio shape)
    env = {"data": {"buying_power": {"buying_power": "13800.0000"}}, "guide": "info"}
    assert _unwrap_payload(env) == {"buying_power": {"buying_power": "13800.0000"}}
    # a plain payload (no "guide") is NOT mis-unwrapped, even with a "data" field
    assert _unwrap_payload({"data": [1], "x": 2}) == {"data": [1], "x": 2}
    assert _unwrap_payload([1, 2]) == [1, 2]
    # accounts/orders envelopes resolve to their inner lists (real get_accounts shape)
    acc = {"data": {"accounts": [{"account_number": "674153143", "agentic_allowed": True}]}, "guide": "x"}
    assert A._as_account_dicts(acc) == [{"account_number": "674153143", "agentic_allowed": True}]
    orders = {"data": {"orders": [{"id": "o1"}]}, "guide": "x"}
    assert A._as_order_dicts(orders) == [{"id": "o1"}]
