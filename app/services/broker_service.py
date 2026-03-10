"""Robinhood read-only portfolio integration via robin_stocks.

Supports two MFA flows:
  1. TOTP (automatic) — if ROBINHOOD_TOTP_SECRET is set in .env
  2. SMS  (manual)    — two-step: login_step1_sms() triggers SMS,
                        login_step2_verify(code) completes auth

Uses robin_stocks' internal HTTP helpers for the raw API calls during
SMS verification, since the library's login() uses input() which is
unusable in a web server.

No orders are ever placed through this module.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings

logger = logging.getLogger(__name__)

_login_lock = threading.Lock()
_logged_in = False
_last_login: float = 0
_LOGIN_TTL = 3600

# SMS flow state preserved between step 1 and step 2
_sms_state: dict[str, Any] = {}

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300


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

    if not _credentials_configured():
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
                store_session=False,
            )
            _logged_in = True
            _last_login = time.time()
            logger.info("[broker] Robinhood login successful (TOTP)")
            return True
        except Exception as e:
            _logged_in = False
            logger.error(f"[broker] Robinhood TOTP login failed: {e}")
            return False


# ── Login: SMS two-step ──────────────────────────────────────────────────

def login_step1_sms() -> dict[str, Any]:
    """Step 1: send credentials, trigger Robinhood's verification flow.

    Returns {"status": "sms_sent"} when the SMS challenge is issued,
    {"status": "connected"} if no MFA was needed or TOTP succeeded,
    or {"status": "error", "message": ...} on failure.
    """
    global _logged_in, _last_login, _sms_state

    if not _credentials_configured():
        return {"status": "error", "message": "Set ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD in .env"}

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
    """Set session state after a successful login response."""
    global _logged_in, _last_login
    from robin_stocks.robinhood.helper import update_session
    from robin_stocks.robinhood.authentication import set_login_state

    token = f"{data['token_type']} {data['access_token']}"
    update_session("Authorization", token)
    set_login_state(True)
    _logged_in = True
    _last_login = time.time()
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
    if not _credentials_configured():
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
            updated += 1
        else:
            trade = Trade(
                user_id=user_id,
                ticker=ticker,
                direction="long",
                entry_price=avg_price,
                quantity=qty,
                status="open",
                broker_source="robinhood",
                tags="robinhood-sync",
                notes=f"Auto-synced from Robinhood on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            )
            db.add(trade)
            created += 1

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
        trade.status = "closed"
        trade.exit_date = datetime.utcnow()
        trade.notes = (trade.notes or "") + f"\nAuto-closed: position no longer on Robinhood ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')})"
        closed += 1

    db.commit()
    logger.info(f"[broker] Position sync: {created} created, {updated} updated, {closed} closed")
    return {"created": created, "updated": updated, "closed": closed}


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


def clear_cache() -> None:
    _cache.clear()
