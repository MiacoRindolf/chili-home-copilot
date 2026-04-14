"""Robinhood portfolio integration via robin_stocks.

Supports two MFA flows:
  1. TOTP (automatic) — if ROBINHOOD_TOTP_SECRET is set in .env
  2. SMS  (manual)    — two-step: login_step1_sms() triggers SMS,
                        login_step2_verify(code) completes auth

Uses robin_stocks' internal HTTP helpers for the raw API calls during
SMS verification, since the library's login() uses input() which is
unusable in a web server.

Includes order placement for approved strategy proposals.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings

logger = logging.getLogger(__name__)

# Check robin_stocks availability at module level (import is lazy elsewhere)
_rh_available = True
try:
    import robin_stocks.robinhood  # noqa: F401
except ImportError:
    _rh_available = False
    logger.info("[broker] robin_stocks not installed — Robinhood integration disabled")

_login_lock = threading.Lock()
_logged_in = False
_last_login: float = 0
_session_connected_at: float | None = None  # exposed to runtime_status
_last_sync_ts: float | None = None          # exposed to runtime_status
_LOGIN_TTL = int(getattr(settings, "broker_login_ttl_seconds", 3600))

# SMS flow state preserved between step 1 and step 2
_sms_state: dict[str, Any] = {}

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = int(getattr(settings, "broker_cache_ttl_seconds", 300))

# ── Configurable execution timeouts ──────────────────────────────────────
_ORDER_POLL_TIMEOUT = int(getattr(settings, "broker_order_poll_timeout", 30))
_ORDER_POLL_INTERVAL = float(getattr(settings, "broker_order_poll_interval", 2.0))
_CHALLENGE_POLL_TIMEOUT = int(getattr(settings, "broker_challenge_poll_timeout", 15))
_RECONCILE_CONFIRM_WINDOW = int(getattr(settings, "broker_reconcile_confirm_seconds", 300))


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)


def _credentials_configured() -> bool:
    return bool(settings.robinhood_username and settings.robinhood_password)


def _has_totp() -> bool:
    return bool(settings.robinhood_totp_secret)


# ── Login: TOTP (automatic) ─────────────────────────────────────────────

def login(force: bool = False) -> bool:
    """Auto-login with TOTP, or check if an active session exists."""
    global _logged_in, _last_login

    if not _rh_available or not _credentials_configured():
        return False

    # If session still valid, reuse it
    if _logged_in and not force and (time.time() - _last_login) < _LOGIN_TTL:
        return True

    if not _has_totp():
        return False

    with _login_lock:
        if _logged_in and not force and (time.time() - _last_login) < _LOGIN_TTL:
            return True

        try:
            import pyotp
            import robin_stocks.robinhood as rh

            totp = pyotp.TOTP(settings.robinhood_totp_secret)
            rh.login(
                settings.robinhood_username,
                settings.robinhood_password,
                mfa_code=totp.now(),
                store_session=True,
            )
            _logged_in = True
            _last_login = time.time()
            _session_connected_at = _last_login
            logger.info("[broker] Robinhood login successful (TOTP)")
            return True
        except Exception as e:
            _logged_in = False
            logger.error(f"[broker] Robinhood TOTP login failed: {e}")
            return False


# ── Session restore (startup) ────────────────────────────────────────────

def try_restore_session() -> bool:
    """Attempt to restore a persisted Robinhood session from disk.

    robin_stocks saves a pickle in ~/.tokens/ when store_session=True.
    If a valid token file exists, this reuses it without re-authenticating.
    Called once at server startup.
    """
    global _logged_in, _last_login
    if not _rh_available or not _credentials_configured():
        return False

    with _login_lock:
        if _logged_in:
            return True
        try:
            import robin_stocks.robinhood as rh

            if _has_totp():
                import pyotp
                totp = pyotp.TOTP(settings.robinhood_totp_secret)
                rh.login(
                    settings.robinhood_username,
                    settings.robinhood_password,
                    mfa_code=totp.now(),
                    store_session=True,
                )
            else:
                rh.login(
                    settings.robinhood_username,
                    settings.robinhood_password,
                    store_session=True,
                )

            _logged_in = True
            _last_login = time.time()
            logger.info("[broker] Robinhood session restored from disk")
            return True
        except Exception as e:
            logger.info(f"[broker] No saved Robinhood session to restore: {e}")
            return False


# ── Login with explicit credentials (per-user from DB) ──────────────────

def login_with_credentials(username: str, password: str, totp_secret: str | None = None) -> dict[str, Any]:
    """Connect to Robinhood using explicitly provided credentials (from DB vault).

    Returns the same status dict as login_step1_sms().
    """
    global _logged_in, _last_login, _sms_state

    if not _rh_available:
        return {"status": "error", "message": "robin_stocks is not installed"}
    if not username or not password:
        return {"status": "error", "message": "Username and password are required"}

    if totp_secret:
        with _login_lock:
            try:
                import pyotp
                import robin_stocks.robinhood as rh

                totp = pyotp.TOTP(totp_secret)
                rh.login(username, password, mfa_code=totp.now(), store_session=True)
                _logged_in = True
                _last_login = time.time()
                logger.info("[broker] Robinhood login successful (TOTP, user creds)")
                return {"status": "connected", "message": "Connected via TOTP"}
            except Exception as e:
                _logged_in = False
                logger.error(f"[broker] Robinhood TOTP login failed (user creds): {e}")
                return {"status": "error", "message": f"TOTP login failed: {e}"}

    with _login_lock:
        try:
            from robin_stocks.robinhood.authentication import (
                generate_device_token, login_url,
            )
            from robin_stocks.robinhood.helper import request_post

            device_token = generate_device_token()
            payload = {
                "client_id": "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS",
                "expires_in": 86400,
                "grant_type": "password",
                "password": password,
                "scope": "internal",
                "username": username,
                "device_token": device_token,
                "try_passkeys": False,
                "token_request_path": "/login",
                "create_read_only_secondary_token": True,
            }

            data = request_post(login_url(), payload)

            if data and "access_token" in data:
                _complete_login(data)
                return {"status": "connected", "message": "Connected (no MFA required)"}

            if data and "verification_workflow" in data:
                workflow_id = data["verification_workflow"]["id"]
                pathfinder_url = "https://api.robinhood.com/pathfinder/user_machine/"
                machine_payload = {
                    "device_id": device_token,
                    "flow": "suv",
                    "input": {"workflow_id": workflow_id},
                }
                machine_data = request_post(url=pathfinder_url, payload=machine_payload, json=True)
                machine_id = _extract_machine_id(machine_data)
                if not machine_id:
                    return {"status": "error", "message": "Could not start verification flow"}

                inquiries_url = f"https://api.robinhood.com/pathfinder/inquiries/{machine_id}/user_view/"
                challenge_info = _poll_for_challenge(inquiries_url)

                if challenge_info:
                    _sms_state = {
                        "device_token": device_token,
                        "login_payload": payload,
                        "machine_id": machine_id,
                        "challenge_id": challenge_info["id"],
                        "challenge_type": challenge_info["type"],
                        "inquiries_url": inquiries_url,
                    }
                    ctype = challenge_info["type"]
                    if ctype == "prompt":
                        return {"status": "app_approval", "message": "Check your Robinhood app and approve the login request."}
                    return {"status": "sms_sent", "message": f"A verification code was sent via {ctype.upper()}. Enter it below."}

                return {"status": "error", "message": "Could not determine verification method. Try again."}

            return {"status": "error", "message": "Unexpected response from Robinhood. Check credentials."}
        except Exception as e:
            logger.error(f"[broker] Login failed (user creds): {e}", exc_info=True)
            return {"status": "error", "message": f"Login failed: {e}"}


# ── Login: SMS two-step ──────────────────────────────────────────────────

def login_step1_sms() -> dict[str, Any]:
    """Step 1: send credentials, trigger Robinhood's verification flow.

    Returns {"status": "sms_sent"} when the SMS challenge is issued,
    {"status": "connected"} if no MFA was needed or TOTP succeeded,
    or {"status": "error", "message": ...} on failure.
    """
    global _logged_in, _last_login, _sms_state

    if not _rh_available:
        return {"status": "error", "message": "robin_stocks is not installed. Run: pip install robin_stocks"}
    if not _credentials_configured():
        return {"status": "needs_credentials", "message": "Click to set up your Robinhood account."}

    # If TOTP is available, use the fast path
    if _has_totp():
        if login(force=True):
            return {"status": "connected", "message": "Connected via TOTP"}
        return {"status": "error", "message": "TOTP login failed"}

    with _login_lock:
        try:
            from robin_stocks.robinhood.authentication import (
                generate_device_token, login_url,
            )
            from robin_stocks.robinhood.helper import request_post, request_get

            device_token = generate_device_token()
            payload = {
                "client_id": "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS",
                "expires_in": 86400,
                "grant_type": "password",
                "password": settings.robinhood_password,
                "scope": "internal",
                "username": settings.robinhood_username,
                "device_token": device_token,
                "try_passkeys": False,
                "token_request_path": "/login",
                "create_read_only_secondary_token": True,
            }

            data = request_post(login_url(), payload)
            logger.info(f"[broker] Step1 response keys: {list(data.keys()) if data else 'None'}")

            # No MFA needed — already logged in
            if data and "access_token" in data:
                _complete_login(data)
                return {"status": "connected", "message": "Connected (no MFA required)"}

            # Verification workflow triggered
            if data and "verification_workflow" in data:
                workflow_id = data["verification_workflow"]["id"]
                logger.info(f"[broker] Verification workflow: {workflow_id}")

                # Start the pathfinder machine
                pathfinder_url = "https://api.robinhood.com/pathfinder/user_machine/"
                machine_payload = {
                    "device_id": device_token,
                    "flow": "suv",
                    "input": {"workflow_id": workflow_id},
                }
                machine_data = request_post(url=pathfinder_url, payload=machine_payload, json=True)

                machine_id = _extract_machine_id(machine_data)
                if not machine_id:
                    return {"status": "error", "message": "Could not start verification flow"}

                # Poll for the challenge to be issued
                inquiries_url = f"https://api.robinhood.com/pathfinder/inquiries/{machine_id}/user_view/"
                challenge_info = _poll_for_challenge(inquiries_url)

                if challenge_info:
                    _sms_state = {
                        "device_token": device_token,
                        "login_payload": payload,
                        "machine_id": machine_id,
                        "challenge_id": challenge_info["id"],
                        "challenge_type": challenge_info["type"],
                        "inquiries_url": inquiries_url,
                    }
                    ctype = challenge_info["type"]
                    logger.info(f"[broker] {ctype.upper()} challenge issued (id={challenge_info['id']})")

                    if ctype == "prompt":
                        return {
                            "status": "app_approval",
                            "message": "Check your Robinhood app and approve the login request.",
                        }
                    else:
                        return {
                            "status": "sms_sent",
                            "message": f"A verification code was sent via {ctype.upper()}. Enter it below.",
                        }

                _sms_state = {}
                return {"status": "error", "message": "Could not determine verification method. Try again."}

            return {"status": "error", "message": "Unexpected response from Robinhood. Check credentials."}

        except Exception as e:
            logger.error(f"[broker] SMS step 1 failed: {e}", exc_info=True)
            return {"status": "error", "message": f"Login failed: {e}"}


def login_step2_verify(sms_code: str) -> dict[str, Any]:
    """Step 2: submit the verification code the user received."""
    global _logged_in, _last_login, _sms_state

    if not _sms_state:
        return {"status": "error", "message": "No pending verification. Click Connect first."}

    code = (sms_code or "").strip()
    if not code or len(code) < 4:
        return {"status": "error", "message": "Please enter a valid code."}

    with _login_lock:
        try:
            from robin_stocks.robinhood.helper import request_post, request_get
            from robin_stocks.robinhood.authentication import login_url

            challenge_id = _sms_state["challenge_id"]
            machine_id = _sms_state["machine_id"]
            inquiries_url = _sms_state["inquiries_url"]
            login_payload = _sms_state["login_payload"]

            # Respond to the challenge
            challenge_url = f"https://api.robinhood.com/challenge/{challenge_id}/respond/"
            resp = request_post(url=challenge_url, payload={"response": code})
            logger.info(f"[broker] Challenge response status: {resp.get('status') if resp else 'None'}")

            if not resp or resp.get("status") != "validated":
                return {"status": "error", "message": "Invalid code. Please try again."}

            # Poll the workflow for approval
            start = time.time()
            while time.time() - start < 30:
                try:
                    cont_payload = {"sequence": 0, "user_input": {"status": "continue"}}
                    inq_resp = request_post(url=inquiries_url, payload=cont_payload, json=True)

                    if inq_resp and "type_context" in inq_resp:
                        result = inq_resp["type_context"].get("result", "")
                        if "approved" in result:
                            break
                except Exception:
                    pass
                time.sleep(2)

            # Re-attempt login with the original payload (now challenge is cleared)
            data = request_post(login_url(), login_payload)

            if data and "access_token" in data:
                _complete_login(data)
                _sms_state = {}
                return {"status": "connected", "message": "Connected successfully!"}

            # Sometimes the session is already set from the challenge flow
            try:
                import robin_stocks.robinhood as rh
                profile = rh.load_account_profile()
                if profile:
                    _logged_in = True
                    _last_login = time.time()
                    _sms_state = {}
                    return {"status": "connected", "message": "Connected successfully!"}
            except Exception:
                pass

            _sms_state = {}
            return {"status": "error", "message": "Verification succeeded but login failed. Try connecting again."}

        except Exception as e:
            logger.error(f"[broker] SMS step 2 failed: {e}", exc_info=True)
            _sms_state = {}
            return {"status": "error", "message": f"Verification error: {e}"}


def _complete_login(data: dict) -> None:
    """Set session state after a successful login response and persist to disk."""
    global _logged_in, _last_login
    from robin_stocks.robinhood.helper import update_session
    from robin_stocks.robinhood.authentication import set_login_state

    token = f"{data['token_type']} {data['access_token']}"
    update_session("Authorization", token)
    set_login_state(True)
    _logged_in = True
    _last_login = time.time()

    try:
        import pickle
        from pathlib import Path
        token_dir = Path.home() / ".tokens"
        token_dir.mkdir(exist_ok=True)
        token_file = token_dir / f"{settings.robinhood_username}.pickle"
        with open(token_file, "wb") as f:
            pickle.dump({"Authorization": token, **data}, f)
        logger.info("[broker] Robinhood session persisted to disk")
    except Exception as e:
        logger.warning(f"[broker] Could not persist session: {e}")

    logger.info("[broker] Robinhood session established")


def _extract_machine_id(machine_data: Any) -> str | None:
    """Extract the machine/inquiry ID from pathfinder response."""
    if not machine_data:
        return None
    # Can be nested in various ways
    if isinstance(machine_data, dict):
        if "id" in machine_data:
            return machine_data["id"]
        if "type_context" in machine_data:
            ctx = machine_data["type_context"]
            if isinstance(ctx, dict) and "id" in ctx:
                return ctx["id"]
    return None


def _poll_for_challenge(inquiries_url: str, timeout: int = 15) -> dict | None:
    """Poll the inquiries endpoint until a challenge is issued."""
    from robin_stocks.robinhood.helper import request_get

    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = request_get(inquiries_url)
            if resp and "context" in resp and "sheriff_challenge" in resp["context"]:
                challenge = resp["context"]["sheriff_challenge"]
                ctype = challenge.get("type", "")
                status = challenge.get("status", "")
                cid = challenge.get("id", "")

                if ctype in ("sms", "email") and status == "issued":
                    return {"id": cid, "type": ctype, "status": status}
                if ctype == "prompt":
                    return {"id": cid, "type": "prompt", "status": status}
        except Exception as e:
            logger.warning(f"[broker] Poll error: {e}")
        time.sleep(2)

    return None


def poll_app_approval() -> dict[str, Any]:
    """Poll Robinhood to see if the user approved the login in the app.

    Returns {"status": "approved"}, {"status": "pending"}, or {"status": "error"}.
    """
    global _logged_in, _last_login, _sms_state

    if not _sms_state or _sms_state.get("challenge_type") != "prompt":
        return {"status": "error", "message": "No pending app approval."}

    try:
        from robin_stocks.robinhood.helper import request_get, request_post
        from robin_stocks.robinhood.authentication import login_url

        challenge_id = _sms_state["challenge_id"]
        prompt_url = f"https://api.robinhood.com/push/{challenge_id}/get_prompts_status/"

        resp = request_get(url=prompt_url)
        logger.info(f"[broker] Prompt poll: {resp}")

        if resp and resp.get("challenge_status") == "validated":
            # Approved! Now finalize the workflow and re-login
            inquiries_url = _sms_state["inquiries_url"]
            login_payload = _sms_state["login_payload"]

            try:
                cont_payload = {"sequence": 0, "user_input": {"status": "continue"}}
                request_post(url=inquiries_url, payload=cont_payload, json=True)
                time.sleep(1)
            except Exception:
                pass

            data = request_post(login_url(), login_payload)
            if data and "access_token" in data:
                _complete_login(data)
                _sms_state = {}
                return {"status": "approved", "message": "Connected successfully!"}

            # Try checking if session is already valid
            try:
                import robin_stocks.robinhood as rh
                profile = rh.load_account_profile()
                if profile:
                    _logged_in = True
                    _last_login = time.time()
                    _sms_state = {}
                    return {"status": "approved", "message": "Connected successfully!"}
            except Exception:
                pass

            _sms_state = {}
            return {"status": "error", "message": "Approval succeeded but login failed. Try again."}

        return {"status": "pending", "message": "Waiting for approval in Robinhood app..."}

    except Exception as e:
        logger.error(f"[broker] App approval poll failed: {e}")
        return {"status": "pending", "message": "Still waiting..."}


# ── Connection status ────────────────────────────────────────────────────

def is_connected() -> bool:
    """Check if we have an active Robinhood session."""
    if not _rh_available or not _credentials_configured():
        return False
    if _logged_in and (time.time() - _last_login) < _LOGIN_TTL:
        return True
    if _has_totp():
        return login()
    return False


def get_connection_status() -> dict[str, Any]:
    """Connection status for the UI."""
    configured = _credentials_configured()
    connected = _logged_in and (time.time() - _last_login) < _LOGIN_TTL

    return {
        "configured": configured,
        "connected": connected,
        "rh_available": _rh_available,
        "awaiting_code": bool(_sms_state),
        "auth_method": "totp" if _has_totp() else "sms",
        "username": settings.robinhood_username if configured else None,
        "last_login": datetime.fromtimestamp(_last_login).isoformat() if _last_login else None,
    }


# ── Data fetching ────────────────────────────────────────────────────────

def get_portfolio() -> dict[str, Any]:
    """Account equity, buying power, cash, total value. Cached 5 min."""
    cached = _cache_get("portfolio")
    if cached is not None:
        return cached

    if not is_connected():
        return {}

    try:
        import robin_stocks.robinhood as rh

        profile = rh.load_phoenix_account()
        if not profile:
            return {}

        account_info = rh.load_account_profile()
        portfolio_info = rh.load_portfolio_profile()

        result = {
            "equity": _safe_float(portfolio_info.get("equity")),
            "extended_hours_equity": _safe_float(portfolio_info.get("extended_hours_equity")),
            "market_value": _safe_float(portfolio_info.get("market_value")),
            "buying_power": _safe_float(account_info.get("buying_power")),
            "cash": _safe_float(account_info.get("cash")),
            "withdrawable_amount": _safe_float(portfolio_info.get("withdrawable_amount")),
            "last_updated": datetime.utcnow().isoformat(),
        }
        _cache_set("portfolio", result)
        return result
    except Exception as e:
        logger.error(f"[broker] Failed to fetch portfolio: {e}")
        return {}


def get_positions() -> list[dict[str, Any]]:
    """Current holdings with quantity, avg cost, equity, change. Cached 5 min."""
    cached = _cache_get("positions")
    if cached is not None:
        return cached

    if not is_connected():
        return []

    try:
        import robin_stocks.robinhood as rh

        holdings = rh.build_holdings()
        if not holdings:
            return []

        positions = []
        for ticker, data in holdings.items():
            positions.append({
                "ticker": ticker,
                "quantity": _safe_float(data.get("quantity")),
                "average_buy_price": _safe_float(data.get("average_buy_price")),
                "equity": _safe_float(data.get("equity")),
                "current_price": _safe_float(data.get("price")),
                "percent_change": _safe_float(data.get("percent_change")),
                "equity_change": _safe_float(data.get("equity_change")),
                "name": data.get("name", ""),
            })

        positions.sort(key=lambda p: p.get("equity", 0), reverse=True)
        _cache_set("positions", positions)
        return positions
    except Exception as e:
        logger.error(f"[broker] Failed to fetch positions: {e}")
        return []


def get_crypto_positions() -> list[dict[str, Any]]:
    """Current crypto holdings. Cached 5 min."""
    cached = _cache_get("crypto_positions")
    if cached is not None:
        return cached

    if not is_connected():
        return []

    try:
        import robin_stocks.robinhood as rh

        crypto_pos = rh.get_crypto_positions()
        if not crypto_pos:
            return []

        positions = []
        for pos in crypto_pos:
            qty = _safe_float(pos.get("quantity"))
            if qty and qty > 0:
                cost_bases = pos.get("cost_bases", [{}])
                avg_cost = _safe_float(cost_bases[0].get("direct_cost_basis")) if cost_bases else 0
                if avg_cost and qty:
                    avg_cost = avg_cost / qty

                currency = pos.get("currency", {})
                positions.append({
                    "ticker": currency.get("code", "???") + "-USD",
                    "quantity": qty,
                    "average_buy_price": round(avg_cost, 4) if avg_cost else 0,
                    "name": currency.get("name", ""),
                    "type": "crypto",
                })

        _cache_set("crypto_positions", positions)
        return positions
    except Exception as e:
        logger.error(f"[broker] Failed to fetch crypto positions: {e}")
        return []


def get_recent_orders(limit: int = 20) -> list[dict[str, Any]]:
    """Recent stock order history. Cached 5 min."""
    cached = _cache_get("recent_orders")
    if cached is not None:
        return cached[:limit]

    if not is_connected():
        return []

    try:
        import robin_stocks.robinhood as rh

        raw_orders = rh.get_all_stock_orders()
        if not raw_orders:
            return []

        orders = []
        for o in raw_orders[:limit]:
            orders.append({
                "id": o.get("id", ""),
                "ticker": _resolve_instrument_ticker(o.get("instrument", "")),
                "side": o.get("side", ""),
                "quantity": _safe_float(o.get("quantity")),
                "price": _safe_float(o.get("average_price") or o.get("price")),
                "state": o.get("state", ""),
                "created_at": o.get("created_at", ""),
                "type": o.get("type", ""),
            })

        _cache_set("recent_orders", orders)
        return orders[:limit]
    except Exception as e:
        logger.error(f"[broker] Failed to fetch orders: {e}")
        return []


def _compute_trade_snapshot(ticker: str, entry_price: float) -> str | None:
    """Compute ATR-based stop/target for a trade and return JSON snapshot."""
    import json
    try:
        from .trading.scanner import _score_ticker
        result = _score_ticker(ticker)
        if result and result.get("stop_loss") and result.get("take_profit"):
            return json.dumps({
                "stop_loss": result["stop_loss"],
                "take_profit": result["take_profit"],
                "score": result.get("score"),
                "signal": result.get("signal"),
            })
    except Exception as e:
        logger.debug(f"[broker] Could not compute snapshot for {ticker}: {e}")
    return None


def _get_exit_price(ticker: str, fallback_entry: float | None) -> float:
    """Fetch current market price for *ticker*, falling back to entry price."""
    try:
        from .trading.market_data import fetch_quote
        quote = fetch_quote(ticker)
        if quote and quote.get("price"):
            return float(quote["price"])
    except Exception as exc:
        logger.debug(f"[broker] Could not fetch exit quote for {ticker}: {exc}")
    return float(fallback_entry or 0.0)


def sync_positions_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Sync Robinhood positions into local Trade model."""
    from ..models.trading import Trade

    if not is_connected():
        return {"created": 0, "updated": 0, "closed": 0}

    positions = get_positions()
    crypto = get_crypto_positions()
    all_positions = positions + crypto

    created = updated = closed = 0

    rh_tickers = set()
    for pos in all_positions:
        ticker = pos["ticker"]
        rh_tickers.add(ticker)
        qty = pos.get("quantity", 0)
        avg_price = pos.get("average_buy_price", 0)

        if not qty or qty <= 0:
            continue

        existing = (
            db.query(Trade)
            .filter(
                Trade.user_id == user_id,
                Trade.ticker == ticker,
                Trade.broker_source == "robinhood",
                Trade.status == "open",
            )
            .first()
        )

        if existing:
            existing.quantity = qty
            existing.entry_price = avg_price
            existing.last_broker_sync = datetime.utcnow()
            if not existing.indicator_snapshot:
                existing.indicator_snapshot = _compute_trade_snapshot(ticker, avg_price)
            updated += 1
        else:
            snapshot = _compute_trade_snapshot(ticker, avg_price)
            is_crypto = ticker.upper().endswith("-USD")
            trade = Trade(
                user_id=user_id,
                ticker=ticker,
                direction="long",
                entry_price=avg_price,
                quantity=qty,
                status="open",
                broker_source="robinhood",
                tags="robinhood-sync",
                indicator_snapshot=snapshot,
                last_broker_sync=datetime.utcnow(),
                stop_model="atr_crypto_breakout" if is_crypto else "atr_swing",
                notes=f"Auto-synced from Robinhood on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            )
            db.add(trade)
            created += 1

    # Link open trades to recent pattern-imminent alerts (if not already linked).
    try:
        from ..models.trading import BreakoutAlert
        _link_cutoff = datetime.utcnow() - timedelta(hours=48)
        _open_unlinked = (
            db.query(Trade)
            .filter(
                Trade.user_id == user_id,
                Trade.broker_source == "robinhood",
                Trade.status == "open",
                Trade.related_alert_id.is_(None),
            )
            .all()
        )
        for _t in _open_unlinked:
            _best = (
                db.query(BreakoutAlert)
                .filter(
                    BreakoutAlert.ticker == _t.ticker,
                    BreakoutAlert.alert_tier == "pattern_imminent",
                    BreakoutAlert.alerted_at >= _link_cutoff,
                )
                .order_by(BreakoutAlert.score_at_alert.desc())
                .first()
            )
            if _best:
                _t.related_alert_id = _best.id
                if not _t.scan_pattern_id and _best.scan_pattern_id:
                    _t.scan_pattern_id = _best.scan_pattern_id
    except Exception:
        logger.debug("[broker] alert-position link failed", exc_info=True)

    stale = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_source == "robinhood",
            Trade.status == "open",
            Trade.ticker.notin_(rh_tickers) if rh_tickers else True,
        )
        .all()
    )

    for trade in stale:
        # Reconciliation confirmation window: only auto-close if the position
        # has been missing for longer than _RECONCILE_CONFIRM_WINDOW seconds.
        # This prevents premature closes during transient API glitches.
        last_sync = getattr(trade, "last_broker_sync", None)
        if last_sync and (datetime.utcnow() - last_sync).total_seconds() < _RECONCILE_CONFIRM_WINDOW:
            logger.debug(
                "[broker] %s missing from RH but within %ds confirm window — skipping",
                trade.ticker, _RECONCILE_CONFIRM_WINDOW,
            )
            continue

        trade.status = "closed"
        trade.exit_date = datetime.utcnow()
        entry = trade.entry_price or 0.0
        qty = trade.quantity or 0.0
        exit_price = _get_exit_price(trade.ticker, entry)
        trade.exit_price = exit_price
        if trade.direction == "short":
            trade.pnl = round((entry - exit_price) * qty, 2)
        else:
            trade.pnl = round((exit_price - entry) * qty, 2)
        trade.notes = (
            (trade.notes or "")
            + f"\nAuto-closed: position no longer on Robinhood "
            f"({datetime.utcnow().strftime('%Y-%m-%d %H:%M')}). "
            f"Exit ~${exit_price:.2f} (market quote)."
        )
        try:
            from .trading.tca_service import apply_tca_on_trade_close

            trade.tca_reference_exit_price = exit_price
            apply_tca_on_trade_close(trade)
        except Exception:
            pass
        try:
            from .trading.brain_work.execution_hooks import on_broker_reconciled_close

            on_broker_reconciled_close(db, trade, source="sync_positions_to_db")
        except Exception:
            pass
        closed += 1

    db.commit()
    logger.info(f"[broker] Position sync: {created} created, {updated} updated, {closed} closed")
    return {"created": created, "updated": updated, "closed": closed, "_live_tickers": rh_tickers}


def cleanup_manual_trades(
    db: Session, user_id: int | None, live_tickers: set[str],
) -> dict[str, int]:
    """Auto-close manual-only trades whose ticker has no matching RH position.

    A manual trade is one with no broker_order_id AND broker_source is
    either NULL or 'manual'.
    """
    from ..models.trading import Trade
    from sqlalchemy import or_

    manual_open = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "open",
            Trade.broker_order_id.is_(None),
            or_(Trade.broker_source.is_(None), Trade.broker_source == "manual"),
        )
        .all()
    )

    closed_manual = 0
    for trade in manual_open:
        if trade.ticker not in live_tickers:
            trade.status = "closed"
            trade.exit_date = datetime.utcnow()
            entry = trade.entry_price or 0.0
            qty = trade.quantity or 0.0
            exit_price = _get_exit_price(trade.ticker, entry)
            trade.exit_price = exit_price
            if trade.direction == "short":
                trade.pnl = round((entry - exit_price) * qty, 2)
            else:
                trade.pnl = round((exit_price - entry) * qty, 2)
            trade.notes = (
                (trade.notes or "")
                + f"\nAuto-closed during RH sync (no matching Robinhood position) "
                f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')}. "
                f"Exit ~${exit_price:.2f} (market quote)."
            )
            try:
                from .trading.tca_service import apply_tca_on_trade_close

                trade.tca_reference_exit_price = exit_price
                apply_tca_on_trade_close(trade)
            except Exception:
                pass
            try:
                from .trading.brain_work.execution_hooks import on_broker_reconciled_close

                on_broker_reconciled_close(db, trade, source="cleanup_manual_trades")
            except Exception:
                pass
            closed_manual += 1

    if closed_manual:
        db.commit()
        logger.info(f"[broker] Manual cleanup: {closed_manual} manual trade(s) auto-closed")

    return {"closed_manual": closed_manual}


def backfill_closed_trade_pnl(db: Session, user_id: int | None) -> int:
    """One-time patch: set exit_price and pnl on closed trades that are missing them."""
    from ..models.trading import Trade

    missing = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.pnl.is_(None),
        )
        .all()
    )
    if not missing:
        return 0

    patched = 0
    for trade in missing:
        entry = trade.entry_price or 0.0
        qty = trade.quantity or 0.0
        exit_price = trade.exit_price or _get_exit_price(trade.ticker, entry)
        trade.exit_price = exit_price
        if trade.direction == "short":
            trade.pnl = round((entry - exit_price) * qty, 2)
        else:
            trade.pnl = round((exit_price - entry) * qty, 2)
        patched += 1

    if patched:
        db.commit()
        logger.info(f"[broker] Backfilled exit_price/pnl for {patched} closed trade(s)")
    return patched


def build_portfolio_context() -> str:
    """Build a text summary of the Robinhood portfolio for AI context."""
    if not is_connected():
        return ""

    portfolio = get_portfolio()
    positions = get_positions()
    crypto = get_crypto_positions()

    if not portfolio and not positions:
        return ""

    lines = ["## REAL PORTFOLIO (Robinhood)"]

    if portfolio:
        equity = portfolio.get("equity", 0)
        buying_power = portfolio.get("buying_power", 0)
        cash = portfolio.get("cash", 0)
        lines.append(
            f"Account value: ${equity:,.2f} | "
            f"Buying power: ${buying_power:,.2f} | "
            f"Cash: ${cash:,.2f}"
        )

    all_pos = positions + crypto
    if all_pos:
        lines.append("POSITIONS:")
        total_pnl = 0.0
        total_equity = 0.0

        for p in all_pos:
            ticker = p["ticker"]
            qty = p.get("quantity", 0)
            avg = p.get("average_buy_price", 0)
            current = p.get("current_price", avg)
            equity = p.get("equity", qty * current if current else 0)
            pct = p.get("percent_change", 0)
            change = p.get("equity_change", (current - avg) * qty if avg and current else 0)

            total_pnl += change if change else 0
            total_equity += equity if equity else 0

            pct_str = f"+{pct:.1f}%" if pct and pct >= 0 else f"{pct:.1f}%" if pct else "N/A"
            change_str = f"+${change:,.2f}" if change and change >= 0 else f"-${abs(change):,.2f}" if change else "N/A"

            lines.append(
                f"  - {ticker}: {qty} shares @ ${avg:,.2f} avg → "
                f"${current:,.2f} now ({pct_str}, {change_str})"
            )

        if total_equity > 0:
            total_pct = (total_pnl / (total_equity - total_pnl)) * 100 if (total_equity - total_pnl) > 0 else 0
            pnl_sign = "+" if total_pnl >= 0 else ""
            lines.append(f"Total P&L: {pnl_sign}${total_pnl:,.2f} ({pnl_sign}{total_pct:.2f}%)")

    return "\n".join(lines)


# ── Retry helper ──────────────────────────────────────────────────────

def _retry_api_call(
    fn,
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    label: str = "api_call",
    **kwargs,
) -> Any:
    """Execute *fn* with exponential backoff retries.

    Only retries on transient exceptions (network errors, timeouts).
    ValueError / KeyError / other logic errors are not retried.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except (ConnectionError, TimeoutError, OSError) as e:
            last_exc = e
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "[broker] %s attempt %d/%d failed: %s — retrying in %.1fs",
                label, attempt + 1, max_retries, e, delay,
            )
            time.sleep(delay)
        except Exception:
            raise  # non-transient — don't retry
    raise last_exc  # type: ignore[misc]


# ── Order Placement ───────────────────────────────────────────────────


def place_buy_order(
    ticker: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
) -> dict[str, Any]:
    """Place a buy order via Robinhood.

    Returns:
        {"ok": True, "order_id": "...", "state": "...", "raw": {...}} on success,
        {"ok": False, "error": "..."} on failure.
    """
    if not _rh_available:
        return {"ok": False, "error": "robin_stocks not installed"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Robinhood"}

    try:
        import robin_stocks.robinhood as rh

        def _do_buy():
            return rh.orders.order(
                symbol=ticker,
                quantity=quantity,
                side="buy",
                limitPrice=round(limit_price, 2) if order_type == "limit" and limit_price else None,
                timeInForce="gtc",
                extendedHours=False,
                jsonify=True,
            )

        result = _retry_api_call(_do_buy, label=f"BUY {ticker}")

        if result and isinstance(result, dict):
            order_id = result.get("id", "")
            state = result.get("state", "unknown")
            logger.info(f"[broker] BUY order placed: {ticker} x{quantity} ({order_type}) -> {state}")
            _cache.pop("positions", None)
            _cache.pop("portfolio", None)
            _cache.pop("recent_orders", None)

            # Post-order poll: wait for fill on market orders
            if order_type == "market" and order_id and state not in _RH_TERMINAL_STATES:
                state = _poll_order_until_terminal(order_id, label=f"BUY {ticker}")
                result["state"] = state

            return {"ok": True, "order_id": order_id, "state": state, "raw": result}
        else:
            error_msg = str(result) if result else "Empty response from Robinhood"
            logger.error(f"[broker] BUY order failed for {ticker}: {error_msg}")
            return {"ok": False, "error": error_msg}

    except Exception as e:
        logger.error(f"[broker] BUY order exception for {ticker}: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


def place_sell_order(
    ticker: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
) -> dict[str, Any]:
    """Place a sell order via Robinhood.

    Returns:
        {"ok": True, "order_id": "...", "state": "...", "raw": {...}} on success,
        {"ok": False, "error": "..."} on failure.
    """
    if not _rh_available:
        return {"ok": False, "error": "robin_stocks not installed"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Robinhood"}

    try:
        import robin_stocks.robinhood as rh

        def _do_sell():
            return rh.orders.order(
                symbol=ticker,
                quantity=quantity,
                side="sell",
                limitPrice=round(limit_price, 2) if order_type == "limit" and limit_price else None,
                timeInForce="gtc",
                extendedHours=False,
                jsonify=True,
            )

        result = _retry_api_call(_do_sell, label=f"SELL {ticker}")

        if result and isinstance(result, dict):
            order_id = result.get("id", "")
            state = result.get("state", "unknown")
            logger.info(f"[broker] SELL order placed: {ticker} x{quantity} ({order_type}) -> {state}")
            _cache.pop("positions", None)
            _cache.pop("portfolio", None)
            _cache.pop("recent_orders", None)

            # Post-order poll: wait for fill on market orders
            if order_type == "market" and order_id and state not in _RH_TERMINAL_STATES:
                state = _poll_order_until_terminal(order_id, label=f"SELL {ticker}")
                result["state"] = state

            return {"ok": True, "order_id": order_id, "state": state, "raw": result}
        else:
            error_msg = str(result) if result else "Empty response from Robinhood"
            logger.error(f"[broker] SELL order failed for {ticker}: {error_msg}")
            return {"ok": False, "error": error_msg}

    except Exception as e:
        logger.error(f"[broker] SELL order exception for {ticker}: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


def _poll_order_until_terminal(order_id: str, *, label: str = "") -> str:
    """Poll a Robinhood order until it reaches a terminal state or times out.

    Returns the final state string. Does not raise.
    """
    start = time.time()
    last_state = "queued"
    while (time.time() - start) < _ORDER_POLL_TIMEOUT:
        try:
            order = get_order_by_id(order_id)
            if order:
                st = (order.get("state") or "").lower()
                last_state = st
                if st in _RH_TERMINAL_STATES:
                    logger.info("[broker] %s order %s reached terminal state: %s", label, order_id, st)
                    return st
        except Exception:
            logger.warning("[broker] %s order %s poll error (will retry)", label, order_id, exc_info=True)
        time.sleep(_ORDER_POLL_INTERVAL)
    logger.warning("[broker] %s order %s poll timed out after %ds (last: %s)", label, order_id, _ORDER_POLL_TIMEOUT, last_state)
    return last_state


# ── Helpers ──

_instrument_cache: dict[str, str] = {}


def _resolve_instrument_ticker(instrument_url: str) -> str:
    if not instrument_url:
        return "???"
    cached = _instrument_cache.get(instrument_url)
    if cached:
        return cached
    try:
        import robin_stocks.robinhood as rh
        data = rh.get_instrument_by_url(instrument_url)
        ticker = data.get("symbol", "???") if data else "???"
        _instrument_cache[instrument_url] = ticker
        return ticker
    except Exception:
        return "???"


def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ── Robinhood → Chili status mapping ──────────────────────────────────

# Robinhood order states from robin_stocks:
#   queued, unconfirmed, confirmed, partially_filled, filled,
#   cancelled, rejected, failed
_RH_TO_CHILI_STATUS = {
    "queued":           "working",
    "unconfirmed":      "working",
    "confirmed":        "working",
    "partially_filled": "working",
    "filled":           "open",       # fully filled → open position
    "cancelled":        "cancelled",
    "canceled":         "cancelled",  # alternate spelling
    "rejected":         "rejected",
    "failed":           "rejected",
}

_RH_TERMINAL_STATES = {"filled", "cancelled", "canceled", "rejected", "failed"}


def map_rh_status(rh_state: str | None) -> str:
    """Map a raw Robinhood order state to a Chili Trade status."""
    if not rh_state:
        return "working"
    return _RH_TO_CHILI_STATUS.get(rh_state.lower(), "working")


def is_rh_terminal(rh_state: str | None) -> bool:
    """True if the Robinhood order is in a final state (filled/cancelled/rejected)."""
    return (rh_state or "").lower() in _RH_TERMINAL_STATES


# ── Order sync (Robinhood → local DB) ────────────────────────────────

def get_order_by_id(order_id: str) -> dict[str, Any] | None:
    """Fetch a single order from Robinhood by ID."""
    if not is_connected() or not order_id:
        return None
    try:
        import robin_stocks.robinhood as rh
        data = rh.orders.get_stock_order_info(order_id)
        if data and isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug(f"[broker] get_order_by_id({order_id}) failed: {e}")
    return None


def sync_orders_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Reconcile local trades (with broker_order_id) against Robinhood.

    For each local trade that has a broker_order_id and is still in a
    non-terminal state, we look up the order on Robinhood and update
    local status, fill price, and timestamps accordingly.
    """
    from ..models.trading import Trade, StrategyProposal
    from .trading.execution_audit import normalize_robinhood_order_event, record_execution_event

    if not is_connected():
        return {"synced": 0, "filled": 0, "cancelled": 0, "errors": 0}

    # Sync trades that are still "working" (limit order pending)
    working_trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_source == "robinhood",
            Trade.broker_order_id.isnot(None),
            Trade.status.in_(["working"]),
        )
        .all()
    )
    # Also reconcile "open" trades that have broker_order_id (e.g. manually marked
    # open but order was cancelled on RH — correct local state from RH)
    open_with_order_id = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_order_id.isnot(None),
            Trade.status == "open",
        )
        .all()
    )

    synced = filled = cancelled = errors = 0

    for trade in working_trades:
        try:
            rh_order = get_order_by_id(trade.broker_order_id)
            if not rh_order:
                errors += 1
                continue

            rh_state = (rh_order.get("state") or "").lower()
            now = datetime.utcnow()
            normalized = normalize_robinhood_order_event(
                order={**rh_order, "id": trade.broker_order_id},
                trade=trade,
                event_type="status",
            )
            normalized.setdefault("submitted_at", getattr(trade, "submitted_at", None) or now)
            normalized.setdefault("acknowledged_at", now)
            record_execution_event(
                db,
                user_id=trade.user_id,
                ticker=trade.ticker,
                trade=trade,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                **normalized,
            )
            trade.last_broker_sync = now

            if rh_state == "filled":
                try:
                    from .trading.tca_service import apply_tca_on_trade_fill

                    apply_tca_on_trade_fill(trade)
                except Exception:
                    pass
                filled += 1
                logger.info(
                    f"[broker] Order {trade.broker_order_id} for {trade.ticker} FILLED "
                    f"@ ${trade.avg_fill_price} x{trade.quantity}"
                )

                # Update linked proposal if any
                if trade.notes:
                    _update_proposal_on_fill(db, trade)

            elif rh_state in ("cancelled", "canceled", "rejected", "failed"):
                trade.status = "cancelled"
                cancelled += 1
                logger.info(
                    f"[broker] Order {trade.broker_order_id} for {trade.ticker} {rh_state}"
                )
                _update_proposal_on_cancel(db, trade, rh_state)

            synced += 1

        except Exception as e:
            logger.warning(f"[broker] Order sync failed for {trade.ticker}: {e}")
            errors += 1

    # Reconcile "open" trades that have broker_order_id: if RH says cancelled, fix local
    for trade in open_with_order_id:
        try:
            rh_order = get_order_by_id(trade.broker_order_id)
            if not rh_order:
                continue
            rh_state = (rh_order.get("state") or "").lower()
            trade.last_broker_sync = datetime.utcnow()
            normalized = normalize_robinhood_order_event(
                order={**rh_order, "id": trade.broker_order_id},
                trade=trade,
                event_type="status",
            )
            record_execution_event(
                db,
                user_id=trade.user_id,
                ticker=trade.ticker,
                trade=trade,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                **normalized,
            )
            if rh_state in ("cancelled", "canceled", "rejected", "failed"):
                cancelled += 1
                synced += 1
                logger.info(
                    f"[broker] Reconcile: {trade.ticker} (open locally) was {rh_state} on RH -> set cancelled"
                )
                _update_proposal_on_cancel(db, trade, rh_state)
        except Exception as e:
            logger.debug(f"[broker] Reconcile open trade {trade.ticker}: {e}")

    if synced:
        db.commit()

    global _last_sync_ts
    _last_sync_ts = time.time()

    logger.info(
        f"[broker] Order sync: {synced} checked, {filled} filled, "
        f"{cancelled} cancelled, {errors} errors"
    )
    return {"synced": synced, "filled": filled, "cancelled": cancelled, "errors": errors}


def _update_proposal_on_fill(db: Session, trade) -> None:
    """When an order fills, update the linked StrategyProposal to 'executed'."""
    from ..models.trading import StrategyProposal

    if not trade.broker_order_id:
        return
    proposal = (
        db.query(StrategyProposal)
        .filter(StrategyProposal.broker_order_id == trade.broker_order_id)
        .first()
    )
    if proposal and proposal.status == "working":
        proposal.status = "executed"
        proposal.executed_at = datetime.utcnow()


def _update_proposal_on_cancel(db: Session, trade, rh_state: str) -> None:
    """When an order is cancelled/rejected, revert proposal to 'approved'."""
    from ..models.trading import StrategyProposal

    if not trade.broker_order_id:
        return
    proposal = (
        db.query(StrategyProposal)
        .filter(StrategyProposal.broker_order_id == trade.broker_order_id)
        .first()
    )
    if proposal and proposal.status == "working":
        proposal.status = "approved"
        proposal.reviewed_at = datetime.utcnow()
        logger.info(
            f"[broker] Proposal #{proposal.id} reverted to 'approved' "
            f"after order {rh_state}"
        )


def clear_cache() -> None:
    _cache.clear()
