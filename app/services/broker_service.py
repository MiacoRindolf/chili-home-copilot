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
from .trading.broker_position_sync import (
    acquire_broker_position_sync_lock,
    collapse_open_broker_position_duplicates,
    dedupe_positions_by_ticker,
)

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
_last_login_attempt: float = 0
_LOGIN_ATTEMPT_COOLDOWN = 300  # seconds between TOTP login attempts
_session_connected_at: float | None = None  # exposed to runtime_status
_last_sync_ts: float | None = None          # exposed to runtime_status
_LOGIN_TTL = int(getattr(settings, "broker_login_ttl_seconds", 3600))


def _reset_rh_session() -> None:
    """Wipe robin_stocks' module-level SESSION cookies + Authorization header.

    The robin_stocks library keeps a long-lived ``requests.Session()`` at
    ``robin_stocks.robinhood.helper.SESSION``. Every login attempt within a
    single uvicorn process accumulates cookies on that session. After 2-3
    failed Step1 attempts, Robinhood starts silently dropping further
    requests from that cookie jar — robin_stocks' ``request_post`` then
    returns ``None`` instead of the expected verification_workflow body.

    Symptom: ``Step1 response keys: None`` in logs, even with valid
    credentials. A fresh subprocess (e.g. a diagnostic script) hits the
    same endpoint and gets a normal response, proving the issue is local
    session state, not server-side block.

    Call this at the top of every interactive login flow so each click
    starts from a clean slate.
    """
    if not _rh_available:
        return
    try:
        from robin_stocks.robinhood import helper as _rh_helper

        _rh_helper.SESSION.cookies.clear()
        # Drop any leftover Authorization header from a prior attempt.
        for h in ("Authorization", "X-Hyper-Ex"):
            _rh_helper.SESSION.headers.pop(h, None)
        try:
            _rh_helper.set_login_state(False)
        except Exception:
            pass
        logger.debug("[broker] robin_stocks SESSION cookies/headers reset for fresh login")
    except Exception as e:
        logger.debug("[broker] could not reset robin_stocks SESSION: %s", e)


# SMS flow state preserved between step 1 and step 2
_sms_state: dict[str, Any] = {}

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = int(getattr(settings, "broker_cache_ttl_seconds", 300))

# ── Configurable execution timeouts ──────────────────────────────────────
_ORDER_POLL_TIMEOUT = int(getattr(settings, "broker_order_poll_timeout", 30))
_ORDER_POLL_INTERVAL = float(getattr(settings, "broker_order_poll_interval", 2.0))
_CHALLENGE_POLL_TIMEOUT = int(getattr(settings, "broker_challenge_poll_timeout", 15))
_RECONCILE_CONFIRM_WINDOW = int(getattr(settings, "broker_reconcile_confirm_seconds", 300))


# ── DB-backed session token storage ──────────────────────────────────────

def _save_session_to_db(broker: str, username: str, token_data: dict, device_token: str | None = None) -> None:
    """Persist a broker session token to PostgreSQL (upsert)."""
    try:
        from ..db import SessionLocal
        from ..models.core import BrokerSession
        db = SessionLocal()
        try:
            existing = (
                db.query(BrokerSession)
                .filter(BrokerSession.broker == broker, BrokerSession.username == username)
                .first()
            )
            if existing:
                existing.token_data = token_data
                existing.device_token = device_token
                existing.updated_at = datetime.utcnow()
            else:
                db.add(BrokerSession(
                    broker=broker,
                    username=username,
                    token_data=token_data,
                    device_token=device_token,
                ))
            db.commit()
            logger.info("[broker] Session token persisted to DB for %s/%s", broker, username)
        finally:
            db.close()
    except Exception as e:
        logger.warning("[broker] Failed to persist session to DB: %s", e)


def _load_session_from_db(broker: str, username: str) -> dict | None:
    """Load a stored broker session token from PostgreSQL."""
    try:
        from ..db import SessionLocal
        from ..models.core import BrokerSession
        db = SessionLocal()
        try:
            row = (
                db.query(BrokerSession)
                .filter(BrokerSession.broker == broker, BrokerSession.username == username)
                .first()
            )
            if row and row.token_data:
                data = dict(row.token_data)
                if row.device_token:
                    data["device_token"] = row.device_token
                return data
            return None
        finally:
            db.close()
    except Exception as e:
        logger.debug("[broker] Failed to load session from DB: %s", e)
        return None


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
    global _logged_in, _last_login, _session_connected_at

    if not _rh_available or not _credentials_configured():
        return False

    if _logged_in and not force and (time.time() - _last_login) < _LOGIN_TTL:
        return True

    if not _has_totp():
        return False

    with _login_lock:
        if _logged_in and not force and (time.time() - _last_login) < _LOGIN_TTL:
            return True

        # Try DB session first (avoids rate-limited TOTP re-auth)
        if not force and _try_load_db_session():
            logger.info("[broker] TOTP login skipped — valid session loaded from DB")
            return True

        # Rate-limit TOTP attempts to avoid Robinhood 429s
        global _last_login_attempt
        if not force and (time.time() - _last_login_attempt) < _LOGIN_ATTEMPT_COOLDOWN:
            logger.debug("[broker] TOTP login attempt skipped — cooldown active")
            return False
        _last_login_attempt = time.time()

        try:
            import pyotp
            import robin_stocks.robinhood as rh

            totp = pyotp.TOTP(settings.robinhood_totp_secret)
            result = rh.login(
                settings.robinhood_username,
                settings.robinhood_password,
                mfa_code=totp.now(),
                store_session=False,
            )
            if not result or not isinstance(result, dict) or not result.get("access_token"):
                logger.warning("[broker] TOTP login returned no access token")
                return False

            _logged_in = True
            _last_login = time.time()
            _session_connected_at = _last_login
            _save_session_to_db(
                broker="robinhood",
                username=settings.robinhood_username or "default",
                token_data=result,
                device_token=result.get("device_token"),
            )
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
    global _logged_in, _last_login, _session_connected_at
    if not _rh_available or not _credentials_configured():
        return False

    with _login_lock:
        if _logged_in:
            return True

        # Load from PostgreSQL only — never trigger a fresh rh.login() at
        # startup.  TOTP re-auth happens lazily in login() / is_connected()
        # when an API call is actually needed.  This avoids Robinhood 429
        # rate limits from repeated container restarts.
        if _try_load_db_session():
            return True

        logger.info("[broker] No valid session in DB — user must re-authenticate via web UI")
        return False


def _refresh_oauth_token(refresh_token: str, scope: str | None = None) -> dict | None:
    """POST /oauth2/token/ with grant_type=refresh_token.

    Returns the new token dict on success (containing access_token,
    refresh_token, expires_in, etc.), or None if Robinhood rejected the
    refresh (typical after ~5 days of idle — refresh tokens age out).
    """
    import requests
    REFRESH_URL = "https://api.robinhood.com/oauth2/token/"
    payload = {
        "client_id": "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS",
        "expires_in": 86400,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope or "internal",
        "token_request_path": "/login",
    }
    try:
        r = requests.post(REFRESH_URL, data=payload, timeout=10)
        if not r.ok:
            try:
                err = r.json()
            except Exception:
                err = r.text[:200]
            logger.info(
                "[broker] refresh_token rejected by Robinhood (status=%s body=%s)",
                r.status_code, err,
            )
            return None
        body = r.json()
        if not isinstance(body, dict) or not body.get("access_token"):
            logger.info("[broker] refresh response missing access_token: %s", body)
            return None
        return body
    except Exception as e:
        logger.info("[broker] refresh_token request failed: %s", e)
        return None


def _try_load_db_session() -> bool:
    """Load a stored session from PostgreSQL and validate with a lightweight
    API call. If the access_token has expired (401), use the refresh_token
    to mint a new one without re-MFA, then save the new token blob and retry.
    Sets module login state on success.
    """
    global _logged_in, _last_login, _session_connected_at
    try:
        from robin_stocks.robinhood.helper import (
            set_login_state,
            update_session,
            request_get,
        )
        from robin_stocks.robinhood.urls import positions_url

        username = settings.robinhood_username or "default"
        data = _load_session_from_db("robinhood", username)
        if not data:
            logger.debug("[broker] No stored session in DB for robinhood/%s", username)
            return False

        access_token = data.get("access_token")
        token_type = data.get("token_type", "Bearer")
        auth_header = data.get("Authorization")

        if not access_token and not auth_header:
            logger.debug("[broker] DB session row exists but has no token")
            return False

        def _apply_token_to_session(td: dict) -> None:
            ah = td.get("Authorization")
            if ah:
                update_session("Authorization", ah)
            else:
                update_session(
                    "Authorization",
                    f"{td.get('token_type', 'Bearer')} {td['access_token']}",
                )
            set_login_state(True)

        _apply_token_to_session(data)

        # First validation attempt with the stored access_token.
        try:
            res = request_get(
                positions_url(), "pagination",
                {"nonzero": "true"}, jsonify_data=False,
            )
            res.raise_for_status()
            _logged_in = True
            _last_login = time.time()
            _session_connected_at = _last_login
            logger.info("[broker] Robinhood session restored from DB (no re-auth)")
            return True
        except Exception as first_err:
            # Most common: HTTP 401 from expired access_token. Try the refresh
            # flow before giving up — that's why Robinhood gave us a refresh
            # token in the first place.
            logger.debug(
                "[broker] stored access_token failed validation (%s); trying refresh",
                first_err,
            )

        refresh_tok = data.get("refresh_token")
        if not refresh_tok:
            logger.info("[broker] no refresh_token in saved session; manual login required")
            return False

        new_blob = _refresh_oauth_token(refresh_tok, scope=data.get("scope"))
        if not new_blob:
            # Refresh token has aged out / been revoked. Operator must MFA once.
            return False

        # Merge: keep old fields like user_uuid, replace token-y fields.
        merged = dict(data)
        merged.update(new_blob)
        # The Authorization JWT in the original blob is separate from the
        # refresh-issued access_token. Drop it so _apply_token_to_session
        # falls back to the new "Bearer <access_token>" path.
        merged.pop("Authorization", None)

        _apply_token_to_session(merged)

        # Re-validate.
        try:
            res = request_get(
                positions_url(), "pagination",
                {"nonzero": "true"}, jsonify_data=False,
            )
            res.raise_for_status()
        except Exception as e2:
            logger.warning(
                "[broker] refresh succeeded but validation still failed: %s", e2,
            )
            return False

        # Persist the freshly-refreshed token blob so subsequent restarts
        # use it directly.
        try:
            _save_session_to_db(
                broker="robinhood",
                username=username,
                token_data=merged,
                device_token=merged.get("device_token") or data.get("device_token"),
            )
        except Exception as e3:
            logger.warning("[broker] failed to persist refreshed token: %s", e3)

        _logged_in = True
        _last_login = time.time()
        _session_connected_at = _last_login
        logger.info("[broker] Robinhood session restored via refresh_token (no MFA)")
        return True

    except Exception as e:
        logger.debug("[broker] DB session restore failed: %s", e)
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
            _reset_rh_session()
            try:
                import pyotp
                import robin_stocks.robinhood as rh

                totp = pyotp.TOTP(totp_secret)
                result = rh.login(username, password, mfa_code=totp.now(), store_session=False)
                if not result or not isinstance(result, dict) or not result.get("access_token"):
                    return {"status": "error", "message": "TOTP login returned no token"}
                _logged_in = True
                _last_login = time.time()
                _session_connected_at = time.time()
                _save_session_to_db(
                    broker="robinhood", username=username,
                    token_data=result, device_token=result.get("device_token"),
                )
                logger.info("[broker] Robinhood login successful (TOTP, user creds)")
                return {"status": "connected", "message": "Connected via TOTP"}
            except Exception as e:
                _logged_in = False
                logger.error("[broker] Robinhood TOTP login failed (user creds): %s", e)
                return {"status": "error", "message": f"TOTP login failed: {e}"}

    with _login_lock:
        _reset_rh_session()
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

            # Same diagnostic discrimination as login_step1_sms (see below).
            if not data:
                return {
                    "status": "error",
                    "message": (
                        "Robinhood did not respond (transport failure or rate "
                        "limit). Wait 30 seconds and try again."
                    ),
                }
            if isinstance(data, dict) and "detail" in data:
                return {
                    "status": "error",
                    "message": f"Robinhood rejected the login: {data.get('detail')}",
                }
            return {
                "status": "error",
                "message": (
                    "Robinhood returned an unexpected response (no access_token, "
                    "no verification_workflow). Keys: "
                    + (", ".join(data.keys()) if isinstance(data, dict) else type(data).__name__)
                ),
            }
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
        # Q1.T8 fix — clear cookies/state from any prior failed attempt so
        # Robinhood treats this as a fresh login. Without this, repeated
        # clicks of "Connect" from the same uvicorn process produce silent
        # None responses after the first 1-2 attempts.
        _reset_rh_session()
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

            # Distinguish "Robinhood didn't respond" from "Robinhood said no".
            # request_post returns None on transport / 4xx-with-non-JSON / rate
            # limit. The previous catch-all "Check credentials" message was
            # misleading because it fires whether or not credentials are wrong.
            if not data:
                return {
                    "status": "error",
                    "message": (
                        "Robinhood did not respond (transport failure or rate "
                        "limit). Wait 30 seconds and try again. If this keeps "
                        "happening, check internet connectivity or whether your "
                        "account is locked."
                    ),
                }

            # No MFA needed — already logged in
            if data and "access_token" in data:
                _complete_login(data)
                return {"status": "connected", "message": "Connected (no MFA required)"}

            # Robinhood-side error (wrong password, account locked, etc.)
            if isinstance(data, dict) and "detail" in data:
                detail = str(data.get("detail") or "").strip()
                # Map known Robinhood detail strings to actionable messages.
                _DETAIL_HINTS = {
                    "Unable to log in with provided credentials.":
                        "Robinhood says: invalid username or password. Verify the "
                        "values in your .env are correct.",
                    "Account temporarily locked.":
                        "Robinhood has temporarily locked the account (likely "
                        "from too many failed login attempts). Wait 15 minutes "
                        "and try again, or unlock via the Robinhood app.",
                }
                hint = _DETAIL_HINTS.get(detail, detail or "credentials issue")
                return {
                    "status": "error",
                    "message": f"Robinhood rejected the login: {hint}",
                }

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
                    _session_connected_at = _last_login
                    # Capture the current robin_stocks headers as session data
                    try:
                        from robin_stocks.robinhood.helper import request_headers
                        _hdr = request_headers() if callable(request_headers) else request_headers
                        if _hdr and _hdr.get("Authorization"):
                            _save_session_to_db(
                                broker="robinhood",
                                username=settings.robinhood_username or "default",
                                token_data={"Authorization": _hdr["Authorization"]},
                            )
                    except Exception:
                        pass
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
    """Set session state after a successful login response and persist to DB."""
    global _logged_in, _last_login, _session_connected_at
    from robin_stocks.robinhood.helper import update_session
    from robin_stocks.robinhood.authentication import set_login_state

    token = f"{data['token_type']} {data['access_token']}"
    update_session("Authorization", token)
    set_login_state(True)
    _logged_in = True
    _last_login = time.time()
    _session_connected_at = _last_login

    _save_session_to_db(
        broker="robinhood",
        username=settings.robinhood_username or "default",
        token_data={"Authorization": token, **data},
        device_token=data.get("device_token"),
    )

    logger.info("[broker] Robinhood session established and persisted to DB")


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
    global _logged_in, _last_login, _sms_state, _session_connected_at

    if not _sms_state or _sms_state.get("challenge_type") != "prompt":
        return {"status": "error", "message": "No pending app approval."}

    try:
        from robin_stocks.robinhood.helper import request_get, request_post
        from robin_stocks.robinhood.authentication import login_url

        challenge_id = _sms_state["challenge_id"]
        inquiries_url = _sms_state["inquiries_url"]
        login_payload = _sms_state["login_payload"]

        # Check prompt status (may return None if rate-limited)
        prompt_url = f"https://api.robinhood.com/push/{challenge_id}/get_prompts_status/"
        resp = request_get(url=prompt_url)
        status_validated = resp and resp.get("challenge_status") == "validated"
        logger.info("[broker] Prompt poll: %s (validated=%s)", resp, status_validated)

        # Track retries: after N polls, try the continuation anyway in case
        # the approval went through but the status endpoint is 429'd.
        poll_count = _sms_state.get("_poll_count", 0) + 1
        _sms_state["_poll_count"] = poll_count
        should_try_continue = status_validated or (poll_count >= 3 and poll_count % 3 == 0)

        if should_try_continue:
            login_result = _try_finalize_approval(inquiries_url, login_payload)
            if login_result:
                _sms_state = {}
                return login_result

            if status_validated:
                _sms_state = {}
                return {"status": "error", "message": "Approval succeeded but login failed. Try connecting again."}

        return {"status": "pending", "message": "Waiting for approval in Robinhood app..."}

    except Exception as e:
        logger.error("[broker] App approval poll failed: %s", e)
        return {"status": "pending", "message": "Still waiting..."}


def _try_finalize_approval(inquiries_url: str, login_payload: dict) -> dict | None:
    """Attempt to finalize a device-approval login.  Returns a status dict
    on success, None if the approval hasn't cleared yet."""
    global _logged_in, _last_login, _session_connected_at
    try:
        from robin_stocks.robinhood.helper import request_post
        from robin_stocks.robinhood.authentication import login_url

        # Continue the workflow
        try:
            cont_payload = {"sequence": 0, "user_input": {"status": "continue"}}
            request_post(url=inquiries_url, payload=cont_payload, json=True)
            time.sleep(1)
        except Exception:
            pass

        data = request_post(login_url(), login_payload)
        if data and "access_token" in data:
            _complete_login(data)
            return {"status": "approved", "message": "Connected successfully!"}

        # Fallback: check if robin_stocks session is already live
        try:
            import robin_stocks.robinhood as rh
            profile = rh.load_account_profile()
            if profile:
                _logged_in = True
                _last_login = time.time()
                _session_connected_at = _last_login
                try:
                    from robin_stocks.robinhood.helper import request_headers
                    _hdr = request_headers() if callable(request_headers) else request_headers
                    if _hdr and _hdr.get("Authorization"):
                        _save_session_to_db(
                            broker="robinhood",
                            username=settings.robinhood_username or "default",
                            token_data={"Authorization": _hdr["Authorization"]},
                        )
                except Exception:
                    pass
                return {"status": "approved", "message": "Connected successfully!"}
        except Exception:
            pass

    except Exception as e:
        logger.debug("[broker] Finalize approval attempt failed: %s", e)

    return None


# ── Connection status ────────────────────────────────────────────────────

def is_connected() -> bool:
    """Check if we have an active Robinhood session."""
    if not _rh_available or not _credentials_configured():
        return False
    if _logged_in and (time.time() - _last_login) < _LOGIN_TTL:
        return True
    # Try DB session (works for both TOTP and non-TOTP accounts)
    if _try_load_db_session():
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

        # phoenix.robinhood.com/accounts/unified has been intermittently
        # unreachable (TLS handshake rejected by AWS edge). It's only a
        # liveness probe — the real data comes from api.robinhood.com
        # endpoints below. Treat phoenix failure as advisory, not fatal,
        # so portfolio / buying-power stay fresh during phoenix outages.
        try:
            rh.load_phoenix_account()
        except Exception as phx_err:
            logger.warning(
                "[broker] phoenix precheck failed (continuing with api.robinhood.com): %s",
                str(phx_err)[:200],
            )

        account_info = rh.load_account_profile()
        portfolio_info = rh.load_portfolio_profile()
        if not account_info and not portfolio_info:
            return {}

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
    from .trading.management_scope import MANAGEMENT_SCOPE_BROKER_SYNC
    from .trading.runtime_surface_state import upsert_runtime_surface_state

    if not is_connected():
        return {"created": 0, "updated": 0, "closed": 0}

    acquire_broker_position_sync_lock(db, broker_source="robinhood", user_id=user_id)
    cleanup = collapse_open_broker_position_duplicates(
        db, broker_source="robinhood", user_id=user_id,
    )

    positions = get_positions()
    crypto = get_crypto_positions()
    all_positions = dedupe_positions_by_ticker(positions + crypto)

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
            if not existing.management_scope:
                existing.management_scope = MANAGEMENT_SCOPE_BROKER_SYNC
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
                management_scope=MANAGEMENT_SCOPE_BROKER_SYNC,
                indicator_snapshot=snapshot,
                last_broker_sync=datetime.utcnow(),
                stop_model="atr_crypto_breakout" if is_crypto else "atr_swing",
                notes=f"Auto-synced from Robinhood on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            )
            db.add(trade)
            db.flush()
            created += 1

    # Link open trades to recent pattern-imminent alerts (if not already linked).
    # 14-day window: positions can be held well beyond the alert timestamp.
    # Applies to any open trade source (Robinhood, manual, other brokers) so Monitor can score health.
    try:
        from ..models.trading import BreakoutAlert
        _link_cutoff = datetime.utcnow() - timedelta(days=14)
        _open_unlinked = (
            db.query(Trade)
            .filter(
                Trade.user_id == user_id,
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
        # This prevents premature closes during transient API glitches AND
        # during RH's fractional-share settlement delay (small orders can
        # take a minute to appear in the positions endpoint after placement).
        #
        # Use whichever reference timestamp is MOST RECENT — a fresh trade
        # created by the autotrader has ``last_broker_sync = NULL`` but a
        # valid ``entry_date`` / ``submitted_at``; without this fallback the
        # first sync after creation auto-closes the trade before RH has even
        # reflected the position. Before the fix this killed every
        # autotrader entry within seconds of placement.
        refs = [
            getattr(trade, "last_broker_sync", None),
            getattr(trade, "submitted_at", None),
            getattr(trade, "entry_date", None),
        ]
        ref_ts = max((r for r in refs if r is not None), default=None)
        if ref_ts is not None and (
            (datetime.utcnow() - ref_ts).total_seconds() < _RECONCILE_CONFIRM_WINDOW
        ):
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
        if not trade.management_scope:
            trade.management_scope = MANAGEMENT_SCOPE_BROKER_SYNC
        closed += 1

    db.commit()
    global _last_sync_ts
    _last_sync_ts = time.time()
    upsert_runtime_surface_state(
        db,
        surface="broker",
        state="ok",
        source="sync_positions_to_db",
        as_of=datetime.utcnow(),
        details={
            "broker": "robinhood",
            "user_id": int(user_id) if user_id is not None else None,
            "created": int(created),
            "updated": int(updated),
            "closed": int(closed),
            "deduped": int(cleanup["cancelled"]),
        },
        updated_by="broker_service",
    )
    db.commit()
    logger.info(
        "[broker] Position sync: %d created, %d updated, %d closed, %d duplicates cancelled",
        created,
        updated,
        closed,
        cleanup["cancelled"],
    )
    return {
        "created": created,
        "updated": updated,
        "closed": closed,
        "deduped": cleanup["cancelled"],
        "_live_tickers": rh_tickers,
    }


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


def get_position_for_ticker(ticker: str) -> dict[str, Any] | None:
    """Return the live broker position for *ticker*, or None if not held.

    Checks both stock and crypto positions from Robinhood (cached).
    """
    if not is_connected():
        return None

    ticker_up = ticker.upper().strip()
    for pos in get_positions() + get_crypto_positions():
        pos_ticker = pos.get("ticker", "").upper().strip()
        if pos_ticker == ticker_up:
            qty = pos.get("quantity", 0)
            if qty and qty > 0:
                return pos
    return None


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


def _rh_order_session_kwargs(
    *,
    market_hours_override: str | None = None,
    extended_hours_override: bool | None = None,
) -> dict[str, Any]:
    """Return Robinhood session flags for stock order placement."""
    if market_hours_override is not None:
        return {
            "extendedHours": bool(
                extended_hours_override
                if extended_hours_override is not None
                else market_hours_override != "regular_hours"
            ),
            "market_hours": str(market_hours_override or "regular_hours"),
        }
    allow_ext = bool(getattr(settings, "chili_autotrader_allow_extended_hours", False))
    if not allow_ext:
        return {"extendedHours": False, "market_hours": "regular_hours"}
    try:
        from .trading.pattern_imminent_alerts import (
            us_stock_extended_session_open,
            us_stock_session_open,
        )

        if us_stock_session_open():
            return {"extendedHours": False, "market_hours": "regular_hours"}
        if us_stock_extended_session_open():
            return {"extendedHours": True, "market_hours": "all_day_hours"}
    except Exception:
        logger.debug("[broker] extended-hours session probe failed", exc_info=True)
    return {"extendedHours": False, "market_hours": "regular_hours"}


def place_buy_order(
    ticker: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
    *,
    market_hours_override: str | None = None,
    extended_hours_override: bool | None = None,
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
        session_kwargs = _rh_order_session_kwargs(
            market_hours_override=market_hours_override,
            extended_hours_override=extended_hours_override,
        )

        def _do_buy():
            # RH rejects timeInForce='gtc' on market orders ("Invalid Good
            # Til Canceled order"). GTC is valid only for limit orders; use
            # 'gfd' (Good-For-Day) for markets. Fractional-share + market
            # orders especially fail with gtc. Verified against RH's own
            # error body: the sell path produced
            # {'non_field_errors': ['Invalid Good Til Canceled order.']}
            # on a straightforward 80-share market sell.
            tif = "gtc" if order_type == "limit" else "gfd"
            return rh.orders.order(
                symbol=ticker,
                quantity=quantity,
                side="buy",
                limitPrice=round(limit_price, 2) if order_type == "limit" and limit_price else None,
                timeInForce=tif,
                extendedHours=session_kwargs["extendedHours"],
                market_hours=session_kwargs["market_hours"],
                jsonify=True,
            )

        result = _retry_api_call(_do_buy, label=f"BUY {ticker}")

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            # Reject dict responses that lack a real order_id. RH's endpoint
            # sometimes returns a JSON body describing a rejection
            # (``{"detail": "Fractional order validation failed", ...}``)
            # and the caller's contract was treating any dict as success —
            # which wrote ``ok=True, order_id=""`` to the idempotency store
            # and the audit row, silently stranding the trade. A genuine
            # submission always includes ``id``; anything else is a failure.
            if not order_id:
                error_msg = (
                    result.get("detail")
                    or result.get("error")
                    or result.get("message")
                    or "Robinhood returned no order_id"
                )
                logger.error(
                    f"[broker] BUY rejected (no order_id): {ticker} x{quantity} "
                    f"response={result}"
                )
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
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
    *,
    market_hours_override: str | None = None,
    extended_hours_override: bool | None = None,
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
        session_kwargs = _rh_order_session_kwargs(
            market_hours_override=market_hours_override,
            extended_hours_override=extended_hours_override,
        )

        def _do_sell():
            # Same TIF rule as place_buy_order: market orders use 'gfd',
            # limits use 'gtc'. See the comment there for the RH error
            # that surfaced this.
            tif = "gtc" if order_type == "limit" else "gfd"
            return rh.orders.order(
                symbol=ticker,
                quantity=quantity,
                side="sell",
                limitPrice=round(limit_price, 2) if order_type == "limit" and limit_price else None,
                timeInForce=tif,
                extendedHours=session_kwargs["extendedHours"],
                market_hours=session_kwargs["market_hours"],
                jsonify=True,
            )

        result = _retry_api_call(_do_sell, label=f"SELL {ticker}")

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            # Same guard as place_buy_order: a dict response without an
            # ``id`` is an error body, not a successful submission. Treating
            # it as success wrote the trade + exit intent to the DB even
            # though the broker never accepted the order, permanently
            # stranding the position.
            if not order_id:
                error_msg = (
                    result.get("detail")
                    or result.get("error")
                    or result.get("message")
                    or "Robinhood returned no order_id"
                )
                logger.error(
                    f"[broker] SELL rejected (no order_id): {ticker} x{quantity} "
                    f"response={result}"
                )
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
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


# ── Crypto order placement (Task KK) ───────────────────────────────────
#
# Robinhood crypto trades 24/7 with no PDT constraint. The `rh.orders`
# crypto endpoints are separate from equity (different URL family;
# stock `instrument` URL vs crypto `currency_pair_id`). Symbol convention:
# bare base currency ('BTC', 'ETH', 'SOL') — NOT 'BTC-USD'.
#
# Strategy mirrors place_buy_order/place_sell_order but skips the
# market-hours / extended-hours plumbing because crypto sessions are
# always open. We still enforce the duplicate-id idempotency guard at
# the adapter level (RobinhoodSpotAdapter.place_market_order); this
# layer is a thin shim over the robin_stocks call.


def _to_crypto_base(ticker: str) -> str:
    """Normalize 'BTC-USD' → 'BTC'. Idempotent for already-bare bases."""
    s = (ticker or "").strip().upper()
    if s.endswith("-USD"):
        s = s[:-4]
    return s


def _is_crypto_supported_on_robinhood(base: str) -> bool:
    """Pre-flight check: does Robinhood actually list this crypto base?

    The brain's pattern scanner looks at a much wider crypto universe (e.g.
    Coinbase / CoinGecko) than Robinhood lists. When the autotrader tries
    to submit a crypto order for a symbol RH doesn't trade — say a low-cap
    altcoin like SPX-USD — robin_stocks crashes deep in its order builder
    with a cryptic ``TypeError: float() argument must be a string or a
    real number, not 'NoneType'``. That's because RH's crypto-pair lookup
    returns None and the library doesn't guard. This pre-flight catches it
    cleanly so callers see ``crypto_not_supported_on_robinhood`` instead.
    """
    q = get_crypto_quote(base)
    if not q:
        return False
    # A real RH crypto quote always carries a numeric mark/bid/ask. An
    # empty/synthetic response should also fail closed.
    px = _safe_float(q.get("mark_price")) or _safe_float(q.get("bid_price")) or _safe_float(q.get("ask_price"))
    return bool(px and px > 0)


def place_crypto_buy_order(
    ticker: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
) -> dict[str, Any]:
    """Place a crypto buy via Robinhood. Returns the same envelope shape as
    place_buy_order so call sites can dispatch on ``_is_crypto(ticker)``
    without branching the response handling.

    ``ticker`` accepts either ``BTC-USD`` (autotrader convention) or
    bare ``BTC``; both normalize to the bare symbol the RH crypto API
    expects.
    """
    if not _rh_available:
        return {"ok": False, "error": "robin_stocks not installed"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Robinhood"}

    base = _to_crypto_base(ticker)
    if not base:
        return {"ok": False, "error": f"empty crypto base from {ticker!r}"}

    # Pre-flight: confirm the symbol is tradeable on Robinhood. Saves a
    # noisy stack trace inside robin_stocks for low-cap altcoins the brain
    # scans but RH doesn't list (SPX-USD, DAI-USD, etc.).
    if not _is_crypto_supported_on_robinhood(base):
        return {
            "ok": False,
            "error": f"crypto_not_supported_on_robinhood:{base}",
        }

    try:
        import robin_stocks.robinhood as rh

        def _do_buy():
            if order_type == "limit":
                if not limit_price or limit_price <= 0:
                    raise ValueError("limit_price required for crypto limit order")
                return rh.orders.order_buy_crypto_limit(
                    symbol=base,
                    quantity=quantity,
                    limitPrice=round(float(limit_price), 2),
                    timeInForce="gtc",
                    jsonify=True,
                )
            return rh.orders.order_buy_crypto_by_quantity(
                symbol=base,
                quantity=quantity,
                jsonify=True,
            )

        result = _retry_api_call(_do_buy, label=f"BUY-CRYPTO {base}")

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            if not order_id:
                error_msg = (
                    result.get("detail")
                    or result.get("error")
                    or result.get("message")
                    or "Robinhood crypto endpoint returned no order_id"
                )
                logger.error(
                    "[broker] BUY-CRYPTO rejected (no order_id): %s x%s response=%s",
                    base, quantity, result,
                )
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
            logger.info(
                "[broker] BUY-CRYPTO order placed: %s x%s (%s) -> %s",
                base, quantity, order_type, state,
            )
            _cache.pop("crypto_positions", None)
            _cache.pop("portfolio", None)
            return {"ok": True, "order_id": order_id, "state": state, "raw": result}

        error_msg = str(result) if result else "Empty response from Robinhood crypto"
        logger.error("[broker] BUY-CRYPTO order failed for %s: %s", base, error_msg)
        return {"ok": False, "error": error_msg}

    except Exception as e:
        logger.error(
            "[broker] BUY-CRYPTO order exception for %s: %s", base, e, exc_info=True,
        )
        return {"ok": False, "error": str(e)}


def place_crypto_sell_order(
    ticker: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
) -> dict[str, Any]:
    """Mirror of :func:`place_crypto_buy_order` for the sell side."""
    if not _rh_available:
        return {"ok": False, "error": "robin_stocks not installed"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Robinhood"}

    base = _to_crypto_base(ticker)
    if not base:
        return {"ok": False, "error": f"empty crypto base from {ticker!r}"}

    if not _is_crypto_supported_on_robinhood(base):
        return {
            "ok": False,
            "error": f"crypto_not_supported_on_robinhood:{base}",
        }

    try:
        import robin_stocks.robinhood as rh

        def _do_sell():
            if order_type == "limit":
                if not limit_price or limit_price <= 0:
                    raise ValueError("limit_price required for crypto limit order")
                return rh.orders.order_sell_crypto_limit(
                    symbol=base,
                    quantity=quantity,
                    limitPrice=round(float(limit_price), 2),
                    timeInForce="gtc",
                    jsonify=True,
                )
            return rh.orders.order_sell_crypto_by_quantity(
                symbol=base,
                quantity=quantity,
                jsonify=True,
            )

        result = _retry_api_call(_do_sell, label=f"SELL-CRYPTO {base}")

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            if not order_id:
                error_msg = (
                    result.get("detail")
                    or result.get("error")
                    or result.get("message")
                    or "Robinhood crypto endpoint returned no order_id"
                )
                logger.error(
                    "[broker] SELL-CRYPTO rejected (no order_id): %s x%s response=%s",
                    base, quantity, result,
                )
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
            logger.info(
                "[broker] SELL-CRYPTO order placed: %s x%s (%s) -> %s",
                base, quantity, order_type, state,
            )
            _cache.pop("crypto_positions", None)
            _cache.pop("portfolio", None)
            return {"ok": True, "order_id": order_id, "state": state, "raw": result}

        error_msg = str(result) if result else "Empty response from Robinhood crypto"
        logger.error("[broker] SELL-CRYPTO order failed for %s: %s", base, error_msg)
        return {"ok": False, "error": error_msg}

    except Exception as e:
        logger.error(
            "[broker] SELL-CRYPTO order exception for %s: %s", base, e, exc_info=True,
        )
        return {"ok": False, "error": str(e)}


def get_crypto_quote(ticker: str) -> dict[str, Any] | None:
    """Return the raw crypto quote dict from Robinhood, or ``None`` on failure.

    Used by the venue adapter when callers ask for a crypto price — the
    equity ``rh.stocks.get_quotes`` path returns garbage for crypto bases.
    """
    if not _rh_available or not is_connected():
        return None
    base = _to_crypto_base(ticker)
    if not base:
        return None
    try:
        import robin_stocks.robinhood as rh
        q = rh.crypto.get_crypto_quote(base)
        if isinstance(q, dict) and q:
            return q
        return None
    except Exception as e:
        logger.warning("[broker] get_crypto_quote(%s) failed: %s", base, e)
        return None


# ── Options order placement (Task MM) ──────────────────────────────────
#
# Robinhood options live at api.robinhood.com/options/ — same equity-scope
# OAuth token as stocks (NOT a separate scope like crypto's nummus). If the
# RH session is connected for stocks, options endpoints work too, modulo
# account-level options approval (Level 2 buy / Level 3 spreads / etc).
# Approval is operator-side; we surface broker rejections cleanly via the
# existing audit row plumbing, same as KK did for crypto-not-supported.
#
# robin_stocks options API used here:
#   rh.options.order_buy_option_limit(...)    — single-leg long buy
#   rh.options.order_sell_option_limit(...)   — single-leg long sell
#   rh.options.find_options_by_expiration_and_strike(...) — locate contract
#   rh.options.get_option_market_data_by_id(option_id) — quote + IV + greeks
#
# Multi-leg strategies (verticals, iron condors) need separate orchestration
# at the strategy layer — submit each leg sequentially with combined coid
# prefix. Out of scope for Phase 1.


def find_option_contract(
    underlying: str,
    expiration: str,
    strike: float,
    option_type: str,
) -> dict[str, Any] | None:
    """Look up a specific option contract by (underlying, expiration, strike, type).

    Args:
        underlying: equity ticker, e.g. 'AAPL'
        expiration: ISO date 'YYYY-MM-DD' (e.g. '2026-05-16')
        strike: strike price as float
        option_type: 'call' or 'put'

    Returns the raw RH instrument dict (with id/url/state/tradability) or
    ``None`` if no match. None signals the contract doesn't exist or RH
    auth lacks options scope; the venue adapter should clean-error rather
    than crash on this.
    """
    if not _rh_available or not is_connected():
        return None
    side = (option_type or "").strip().lower()
    if side not in ("call", "put"):
        return None
    sym = (underlying or "").strip().upper()
    if not sym:
        return None
    try:
        import robin_stocks.robinhood as rh
        results = rh.options.find_options_by_expiration_and_strike(
            sym, expiration, strike, side,
        )
        if not results:
            return None
        # The library returns a list; take the first matching tradable contract.
        for c in results:
            if isinstance(c, dict) and c.get("tradability") in ("tradable", None):
                return c
        # Fall back to first if none flagged tradable.
        first = results[0]
        return first if isinstance(first, dict) else None
    except Exception as e:
        logger.warning(
            "[broker] find_option_contract(%s %s %s %s) failed: %s",
            sym, expiration, strike, side, e,
        )
        return None


def get_option_quote(option_id: str) -> dict[str, Any] | None:
    """Return market data for a specific option contract by RH id, or None.

    The dict carries bid_price, ask_price, mark_price, implied_volatility,
    delta/gamma/theta/vega/rho, open_interest, volume — the operator-visible
    fields the autotrader uses for sizing + slippage gates.
    """
    if not _rh_available or not is_connected():
        return None
    if not option_id:
        return None
    try:
        import robin_stocks.robinhood as rh
        # robin_stocks returns a list (one entry per requested id). Normalize
        # to a single dict.
        data = rh.options.get_option_market_data_by_id(option_id)
        if isinstance(data, list) and data:
            first = data[0]
            return first if isinstance(first, dict) else None
        if isinstance(data, dict):
            return data
        return None
    except Exception as e:
        logger.warning("[broker] get_option_quote(%s) failed: %s", option_id, e)
        return None


def place_option_buy_order(
    underlying: str,
    expiration: str,
    strike: float,
    option_type: str,
    quantity: int,
    limit_price: float,
    *,
    time_in_force: str = "gtc",
) -> dict[str, Any]:
    """Place a single-leg long-call or long-put BUY via Robinhood.

    Args mirror the contract-identification triple plus order details:

      underlying:  'AAPL'
      expiration:  '2026-05-16' (ISO YYYY-MM-DD; same format RH uses)
      strike:      150.0 (the strike price in dollars)
      option_type: 'call' or 'put'
      quantity:    integer number of contracts (each = 100 shares of underlying)
      limit_price: per-contract premium in dollars (NOT × 100)
      time_in_force: 'gtc' or 'gfd' (default gtc for limit orders)

    Returns the same envelope as place_buy_order for stocks/crypto so the
    autotrader doesn't need a third response shape. The pre-flight verifies
    the contract exists at the broker before submitting; if RH options
    approval is missing on the account the order will be rejected by RH
    and surfaced as ``error`` in the response.
    """
    if not _rh_available:
        return {"ok": False, "error": "robin_stocks not installed"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Robinhood"}

    sym = (underlying or "").strip().upper()
    side = (option_type or "").strip().lower()
    if not sym or side not in ("call", "put"):
        return {"ok": False, "error": f"bad inputs: underlying={underlying!r} type={option_type!r}"}
    if quantity <= 0 or limit_price <= 0:
        return {"ok": False, "error": f"bad qty/price: qty={quantity} price={limit_price}"}

    # Pre-flight: confirm the contract exists. Catches typos in
    # expiration / strike before the broker does, gives a clean error
    # message instead of an opaque RH 400.
    contract = find_option_contract(sym, expiration, strike, side)
    if not contract:
        return {
            "ok": False,
            "error": f"option_contract_not_found:{sym}_{expiration}_{strike}_{side}",
        }

    try:
        import robin_stocks.robinhood as rh

        def _do_buy():
            return rh.options.order_buy_option_limit(
                positionEffect="open",
                creditOrDebit="debit",  # buying-to-open is always a debit
                price=round(float(limit_price), 2),
                symbol=sym,
                quantity=int(quantity),
                expirationDate=expiration,
                strike=float(strike),
                optionType=side,
                timeInForce=time_in_force,
                jsonify=True,
            )

        result = _retry_api_call(_do_buy, label=f"BUY-OPT {sym} {expiration} {strike}{side}")

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            if not order_id:
                error_msg = (
                    result.get("detail")
                    or result.get("error")
                    or result.get("message")
                    or "Robinhood options endpoint returned no order_id"
                )
                logger.error(
                    "[broker] BUY-OPT rejected (no order_id): %s %s %s%s qty=%d response=%s",
                    sym, expiration, strike, side, quantity, result,
                )
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
            logger.info(
                "[broker] BUY-OPT order placed: %s %s %s%s qty=%d limit=%.2f -> %s",
                sym, expiration, strike, side, quantity, limit_price, state,
            )
            return {"ok": True, "order_id": order_id, "state": state, "raw": result}

        error_msg = str(result) if result else "Empty response from Robinhood options"
        logger.error("[broker] BUY-OPT order failed for %s: %s", sym, error_msg)
        return {"ok": False, "error": error_msg}

    except Exception as e:
        logger.error("[broker] BUY-OPT exception for %s: %s", sym, e, exc_info=True)
        return {"ok": False, "error": str(e)}


def place_option_sell_order(
    underlying: str,
    expiration: str,
    strike: float,
    option_type: str,
    quantity: int,
    limit_price: float,
    *,
    position_effect: str = "close",
    time_in_force: str = "gtc",
) -> dict[str, Any]:
    """Place a single-leg SELL via Robinhood. Mirrors place_option_buy_order.

    ``position_effect``:
      - 'close' (default) — closing a long call/put we already own
      - 'open' — opening a short call/put (covered call, naked put, etc.).
        Requires Level 3+ approval and far higher margin/cash than longs.

    For Phase 1 we default to 'close' so the autotrader can exit a long
    position without operator-supervised gating. Opening shorts is opt-in
    by passing ``position_effect='open'`` explicitly.
    """
    if not _rh_available:
        return {"ok": False, "error": "robin_stocks not installed"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Robinhood"}

    sym = (underlying or "").strip().upper()
    side = (option_type or "").strip().lower()
    if not sym or side not in ("call", "put"):
        return {"ok": False, "error": f"bad inputs: underlying={underlying!r} type={option_type!r}"}
    if quantity <= 0 or limit_price <= 0:
        return {"ok": False, "error": f"bad qty/price: qty={quantity} price={limit_price}"}
    pe = (position_effect or "close").strip().lower()
    if pe not in ("open", "close"):
        return {"ok": False, "error": f"bad position_effect: {position_effect!r}"}

    # Closing: contract must exist (we should already own it).
    # Opening short: contract must exist + RH approval level handles the
    # margin check. Either way the existence check is correct.
    contract = find_option_contract(sym, expiration, strike, side)
    if not contract:
        return {
            "ok": False,
            "error": f"option_contract_not_found:{sym}_{expiration}_{strike}_{side}",
        }

    try:
        import robin_stocks.robinhood as rh

        # close-of-long is a credit (we receive premium); open-of-short
        # is also a credit. Both sells go through as credit transactions.
        cod = "credit"

        def _do_sell():
            return rh.options.order_sell_option_limit(
                positionEffect=pe,
                creditOrDebit=cod,
                price=round(float(limit_price), 2),
                symbol=sym,
                quantity=int(quantity),
                expirationDate=expiration,
                strike=float(strike),
                optionType=side,
                timeInForce=time_in_force,
                jsonify=True,
            )

        result = _retry_api_call(_do_sell, label=f"SELL-OPT {sym} {expiration} {strike}{side}")

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            if not order_id:
                error_msg = (
                    result.get("detail")
                    or result.get("error")
                    or result.get("message")
                    or "Robinhood options endpoint returned no order_id"
                )
                logger.error(
                    "[broker] SELL-OPT rejected (no order_id): %s %s %s%s qty=%d response=%s",
                    sym, expiration, strike, side, quantity, result,
                )
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
            logger.info(
                "[broker] SELL-OPT order placed: %s %s %s%s qty=%d limit=%.2f effect=%s -> %s",
                sym, expiration, strike, side, quantity, limit_price, pe, state,
            )
            return {"ok": True, "order_id": order_id, "state": state, "raw": result}

        error_msg = str(result) if result else "Empty response from Robinhood options"
        logger.error("[broker] SELL-OPT order failed for %s: %s", sym, error_msg)
        return {"ok": False, "error": error_msg}

    except Exception as e:
        logger.error("[broker] SELL-OPT exception for %s: %s", sym, e, exc_info=True)
        return {"ok": False, "error": str(e)}


def get_open_option_positions() -> list[dict[str, Any]]:
    """Current open option legs (long + short). Each entry carries
    quantity, average_price (premium paid/received), trade_value,
    and chain_id — enough to reconcile against trade rows.
    """
    if not _rh_available or not is_connected():
        return []
    try:
        import robin_stocks.robinhood as rh
        positions = rh.options.get_open_option_positions() or []
        return [p for p in positions if isinstance(p, dict)]
    except Exception as e:
        logger.warning("[broker] get_open_option_positions failed: %s", e)
        return []


def cancel_option_order(order_id: str) -> dict[str, Any]:
    """Cancel an open option order by id. Returns ok/error envelope."""
    if not _rh_available:
        return {"ok": False, "error": "robin_stocks not installed"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Robinhood"}
    if not order_id:
        return {"ok": False, "error": "empty order_id"}
    try:
        import robin_stocks.robinhood as rh
        result = rh.options.cancel_option_order(order_id)
        return {"ok": True, "raw": result or {}}
    except Exception as e:
        logger.warning("[broker] cancel_option_order(%s) failed: %s", order_id, e)
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
    from .trading.robinhood_exit_execution import sync_pending_exit_order

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
    open_with_pending_exit = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_source == "robinhood",
            Trade.pending_exit_order_id.isnot(None),
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
                # Safety gate: refuse to mark cancelled if the order actually
                # has fills. RH's state field can briefly report a terminal
                # status during transient routing/canceling of unfilled
                # portions, even while ``cumulative_quantity`` shows the
                # order executed. We saw autotrader-placed WGS/GH/INFQ get
                # silently cancelled in our DB while RH confirmed them
                # filled — the resulting "phantom cancel" broke
                # autotrader_open_count and forced broker_sync to re-import
                # the real positions as fresh broker_sync rows.
                cum = 0.0
                try:
                    cum = float(rh_order.get("cumulative_quantity") or 0)
                except (TypeError, ValueError):
                    cum = 0.0
                if cum > 0:
                    logger.warning(
                        "[broker] Order %s for %s reports state=%s but cumulative_quantity=%s — "
                        "treating as FILLED (RH state anomaly)",
                        trade.broker_order_id, trade.ticker, rh_state, cum,
                    )
                    trade.status = "open"
                    trade.broker_status = "filled"
                    filled += 1
                else:
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

    for trade in open_with_pending_exit:
        try:
            rh_order = get_order_by_id(trade.pending_exit_order_id)
            if not rh_order:
                errors += 1
                continue
            prefix = "desk_close" if (trade.pending_exit_reason or "").lower() == "desk_close_now" else "monitor_exit"
            sync_out = sync_pending_exit_order(
                db,
                trade,
                order={**rh_order, "id": trade.pending_exit_order_id},
                audit_decision_prefix=prefix,
            )
            st = str(sync_out.get("state") or "").lower()
            if st == "filled":
                filled += 1
            elif st in ("cancelled", "canceled", "rejected", "failed", "expired"):
                cancelled += 1
            synced += 1
        except Exception as e:
            logger.warning(f"[broker] Pending exit sync failed for {trade.ticker}: {e}")
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
                # Same safety as the working-trades path above: if the
                # order shows fills, trust cumulative_quantity over the
                # state field. A filled order whose state later reads
                # "cancelled" shouldn't be flipped to cancelled locally.
                cum = 0.0
                try:
                    cum = float(rh_order.get("cumulative_quantity") or 0)
                except (TypeError, ValueError):
                    cum = 0.0
                if cum > 0:
                    logger.warning(
                        "[broker] Reconcile: %s reports state=%s but cum=%s — leaving as open (RH state anomaly)",
                        trade.ticker, rh_state, cum,
                    )
                    # Do not call apply_execution_event_to_trade-derived
                    # mutation; record_execution_event above already wrote
                    # the audit row. Leave trade.status=open untouched.
                else:
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
    try:
        from .trading.runtime_surface_state import upsert_runtime_surface_state

        upsert_runtime_surface_state(
            db,
            surface="broker",
            state="ok",
            source="sync_orders_to_db",
            as_of=datetime.utcnow(),
            details={
                "broker": "robinhood",
                "user_id": int(user_id) if user_id is not None else None,
                "synced": int(synced),
                "filled": int(filled),
                "cancelled": int(cancelled),
                "errors": int(errors),
            },
            updated_by="broker_service",
        )
        db.commit()
    except Exception:
        logger.debug("[broker] failed to persist broker runtime surface", exc_info=True)

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
