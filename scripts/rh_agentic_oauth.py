"""Operator tool: ONE-time interactive OAuth consent for the Robinhood Agentic rail.

Run this ONCE on the desktop (where a browser is available) to mint a refreshable
token BUNDLE for headless operation in the scheduler container. PKCE S256, public
client (``token_endpoint_auth_method=none``), authorization_code + refresh_token.

Flow:
  1. Dynamic client registration (RFC 7591) -> persist ONLY the ``client_id`` to a
     sidecar ``<bundle>.client.json`` (client_secret / registration tokens are scrubbed).
     ``--register`` forces a fresh registration; otherwise a cached client_id is reused.
  2. PKCE pair + state (in memory only — NEVER printed or written).
  3. Print the authorization URL (no secret in it) for the operator to open.
  4. Capture the callback on 127.0.0.1:<port> (loopback http.server, request line
     never logged). Headless fallback: ``--paste`` reads the callback URL from STDIN.
  5. Exchange the code at the token endpoint (PKCE; no Authorization header).
  6. Write the bundle via ``rh_oauth.write_bundle_atomic`` (0600 / owner-only ACL).
     Prints ONLY ``bundle.redacted()``.

SECURITY: prints NO token material and NO authorization code, ever — not the
access_token, refresh_token, auth code, code_verifier, state, or client_secret.

Usage:
    conda run -n chili-env python scripts/rh_agentic_oauth.py
    conda run -n chili-env python scripts/rh_agentic_oauth.py --register --port 8765
    conda run -n chili-env python scripts/rh_agentic_oauth.py --paste   # headless
"""

from __future__ import annotations

# stdlib
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

# Bootstrap: allow running as `python scripts/rh_agentic_oauth.py` from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import requests  # noqa: E402  (third-party; stdlib bootstrap must precede app imports)

from app.services.trading.venue.rh_oauth import (  # noqa: E402
    ALLOWED_OAUTH_HOSTS,
    DEFAULT_AUTHORIZATION_ENDPOINT,
    DEFAULT_REGISTRATION_ENDPOINT,
    DEFAULT_RESOURCE,
    DEFAULT_SCOPE,
    DEFAULT_TOKEN_ENDPOINT,
    MAX_PLAUSIBLE_EXPIRES_IN,
    PKCE_METHOD,
    TOKEN_HTTP_TIMEOUT,
    TokenBundle,
    default_bundle_path,
    make_pkce_pair,
    make_state,
    write_bundle_atomic,
)

_CLIENT_NAME = "chili-trading-brain"


def _assert_https_allowed(url: str) -> None:
    parts = urlparse(url)
    if parts.scheme != "https":
        raise SystemExit(f"[rh_agentic_oauth] refusing non-https URL scheme={parts.scheme!r}")
    if (parts.hostname or "").lower() not in ALLOWED_OAUTH_HOSTS:
        raise SystemExit("[rh_agentic_oauth] refusing off-host URL (not in RH allow-list)")


def _bundle_path() -> str:
    try:
        from app.config import settings

        p = getattr(settings, "chili_robinhood_agentic_mcp_token_file", "") or ""
        if p:
            return p
    except Exception:
        pass
    return default_bundle_path()


def _sidecar_path(bundle_path: str) -> str:
    base, _ext = os.path.splitext(bundle_path)
    return base + ".client.json"


def _load_cached_client_id(sidecar: str) -> str:
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("client_id") or "")
    except Exception:
        return ""


def _register_client(redirect_uri: str) -> str:
    """Dynamic registration (RFC 7591). Returns client_id; persists ONLY client_id."""
    _assert_https_allowed(DEFAULT_REGISTRATION_ENDPOINT)
    body = {
        "client_name": _CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": DEFAULT_SCOPE,
    }
    resp = requests.post(
        DEFAULT_REGISTRATION_ENDPOINT,
        json=body,
        headers={"Accept": "application/json"},
        timeout=TOKEN_HTTP_TIMEOUT,
        allow_redirects=False,
    )
    if 300 <= resp.status_code < 400:
        raise SystemExit("[rh_agentic_oauth] registration redirected — refusing to follow")
    if resp.status_code >= 400:
        raise SystemExit(f"[rh_agentic_oauth] registration failed status={resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        raise SystemExit("[rh_agentic_oauth] registration response was not JSON")
    client_id = str(data.get("client_id") or "")
    if not client_id:
        raise SystemExit("[rh_agentic_oauth] registration returned no client_id")
    return client_id


def _persist_client_id(sidecar: str, client_id: str) -> None:
    """Persist ONLY client_id — scrub client_secret / registration_access_token / uri."""
    os.makedirs(os.path.dirname(os.path.abspath(sidecar)) or ".", exist_ok=True)
    with open(sidecar, "w", encoding="utf-8") as fh:
        json.dump({"client_id": client_id}, fh)


class _CallbackHandler(BaseHTTPRequestHandler):
    captured_query: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        type(self).captured_query = parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"CHILI: authorization received. You may close this tab.")

    def log_message(self, *_args, **_kwargs) -> None:  # noqa: D401 - silence request logging
        # NEVER echo the request line (it carries the authorization code).
        return


def _capture_via_loopback(port: int, expected_state: str, timeout_s: float) -> str:
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1.0
    _CallbackHandler.captured_query = {}
    deadline = time.time() + timeout_s
    print(f"[rh_agentic_oauth] waiting for the browser callback on 127.0.0.1:{port} ...")
    while time.time() < deadline and not _CallbackHandler.captured_query:
        server.handle_request()
    server.server_close()
    q = _CallbackHandler.captured_query
    if not q:
        raise SystemExit("[rh_agentic_oauth] timed out waiting for the callback")
    return _extract_code(q, expected_state)


def _capture_via_paste(expected_state: str) -> str:
    print("[rh_agentic_oauth] paste the full callback URL (http://127.0.0.1.../callback?...):")
    line = input().strip()  # never argv — keeps the code out of the process table
    q = parse_qs(urlparse(line).query)
    return _extract_code(q, expected_state)


def _extract_code(query: dict, expected_state: str) -> str:
    got_state = (query.get("state") or [""])[0]
    if got_state != expected_state:
        raise SystemExit("[rh_agentic_oauth] state mismatch — aborting (possible CSRF)")
    code = (query.get("code") or [""])[0]
    if not code:
        err = (query.get("error") or ["unknown"])[0]
        raise SystemExit(f"[rh_agentic_oauth] no authorization code in callback (error={err})")
    return code


def _exchange_code(*, code: str, redirect_uri: str, client_id: str, verifier: str) -> dict:
    _assert_https_allowed(DEFAULT_TOKEN_ENDPOINT)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": DEFAULT_RESOURCE,
    }
    resp = requests.post(
        DEFAULT_TOKEN_ENDPOINT,
        data=form,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=TOKEN_HTTP_TIMEOUT,
        allow_redirects=False,
    )
    if 300 <= resp.status_code < 400:
        raise SystemExit("[rh_agentic_oauth] token exchange redirected — refusing to follow")
    if resp.status_code >= 400:
        # Do NOT print the body (could echo error detail with sensitive context).
        raise SystemExit(f"[rh_agentic_oauth] token exchange failed status={resp.status_code}")
    try:
        return resp.json()
    except Exception:
        raise SystemExit("[rh_agentic_oauth] token response was not JSON")


def main() -> int:
    parser = argparse.ArgumentParser(description="One-time RH Agentic OAuth consent (PKCE).")
    parser.add_argument("--port", type=int, default=8765, help="loopback callback port")
    parser.add_argument("--register", action="store_true", help="force fresh client registration")
    parser.add_argument("--paste", action="store_true", help="headless: paste the callback URL via STDIN")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the callback")
    args = parser.parse_args()

    bundle_path = _bundle_path()
    sidecar = _sidecar_path(bundle_path)
    redirect_uri = f"http://127.0.0.1:{args.port}/callback"

    # 1) client_id (register or reuse).
    client_id = "" if args.register else _load_cached_client_id(sidecar)
    if not client_id:
        client_id = _register_client(redirect_uri)
        _persist_client_id(sidecar, client_id)
        print(f"[rh_agentic_oauth] registered client (id tail=...{client_id[-6:]}); sidecar={sidecar}")
    else:
        print(f"[rh_agentic_oauth] reusing cached client (id tail=...{client_id[-6:]})")

    # 2) PKCE + state (memory only).
    verifier, challenge = make_pkce_pair()
    state = make_state()

    # 3) print the authorization URL (no secret).
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": DEFAULT_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": PKCE_METHOD,
        "resource": DEFAULT_RESOURCE,
    }
    auth_url = f"{DEFAULT_AUTHORIZATION_ENDPOINT}?{urlencode(auth_params)}"
    print("\n[rh_agentic_oauth] open this URL in your browser and approve:\n")
    print(auth_url)
    print()

    # 4) capture the callback.
    if args.paste:
        code = _capture_via_paste(state)
    else:
        code = _capture_via_loopback(args.port, state, args.timeout)

    # 5) exchange.
    token_data = _exchange_code(
        code=code, redirect_uri=redirect_uri, client_id=client_id, verifier=verifier
    )

    access = token_data.get("access_token")
    if not access:
        raise SystemExit("[rh_agentic_oauth] token response had no access_token")
    refresh = token_data.get("refresh_token")
    try:
        expires_in = float(token_data.get("expires_in") or 0.0)
    except (TypeError, ValueError):
        expires_in = 0.0
    if expires_in <= 0 or expires_in > MAX_PLAUSIBLE_EXPIRES_IN:
        expires_in = 0.0
    now = time.time()

    bundle = TokenBundle(
        access_token=str(access),
        refresh_token=(str(refresh) if refresh else None),
        expires_at=now + expires_in,
        scope=str(token_data.get("scope") or DEFAULT_SCOPE),
        client_id=client_id,
        token_endpoint=DEFAULT_TOKEN_ENDPOINT,
        obtained_at=now,
    )

    # 6) write (atomic, 0600). Print ONLY the redacted view.
    write_bundle_atomic(bundle_path, bundle)
    print(f"\n[rh_agentic_oauth] wrote token bundle -> {bundle_path}")
    print(f"[rh_agentic_oauth] {bundle.redacted()}")

    if not refresh:
        print(
            "\n[rh_agentic_oauth] WARNING: no refresh_token in the response — HEADLESS "
            "REFRESH WILL NOT WORK. Re-run consent (the token will expire and the rail "
            "will report needs_reauth).",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
