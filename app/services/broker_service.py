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
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from ..config import settings
from .trading.broker_position_sync import (
    acquire_broker_position_sync_lock,
    collapse_open_broker_position_duplicates,
    dedupe_positions_by_ticker,
)
from .trading.tick_normalizer import normalize_price, normalize_quantity

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
# f-equity-reconcile-partial-list-guard (2026-05-08): minimum number of
# consecutive sync_positions_to_db cycles a position must be missing
# from ``rh_tickers`` before the stale-close path may close it. Default
# 2 -- one missing cycle increments the streak; the second consecutive
# miss confirms. Setting CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN=0
# disables the guard without a code revert.
_RECONCILE_PARTIAL_LIST_STREAK_MIN = int(
    getattr(settings, "chili_reconcile_partial_list_streak_min", 2)
)


# ── f-equity-broker-reconcile-wipeout-protection (2026-05-08) ────────────
#
# Wipeout-burst breaker trip. The 2026-04-30 incident showed that when
# the equity reconciler manufactures multiple synthetic closes in
# rapid succession (broker auth flap or transient API failure that R32
# didn't catch), the consecutive-loss breaker (R31) excludes them on
# PnL grounds — but the row-burst pattern itself IS the wipeout
# signature. We trip on cardinality, not realized PnL.
#
# The threshold is 3-in-5s: under healthy operation the reconciler
# closes 0–1 stale rows per cycle. Three closes inside one 5s window
# is a wipeout-class event the operator must investigate manually
# (auth lapse, broker outage, etc). Tripping is conservative — better
# to spuriously freeze entries for the operator to reset than to let
# the cascade continue.
_WIPEOUT_BURST_BUCKET_S = 5
_WIPEOUT_BURST_THRESHOLD = 3
_wipeout_burst_buckets: dict[int, int] = {}
_wipeout_burst_tripped_buckets: set[int] = set()
_WIPEOUT_BURST_BUCKET_RETENTION_S = 300  # 5min; bound the dict
_RECONCILE_CLOSE_TOTAL = 0  # observability counter; module-grep'd by ops


def _record_reconcile_close_burst(
    ticker: str,
    trade_id: int | None,
    *,
    _now: float | None = None,
    _breaker_persister=None,
) -> None:
    """Bucket the current ``broker_reconcile_position_gone`` close and
    trip the drawdown breaker if cardinality crosses
    ``_WIPEOUT_BURST_THRESHOLD`` inside a single
    ``_WIPEOUT_BURST_BUCKET_S`` second window.

    Called from the stale-close loop in ``sync_positions_to_db``. The
    function is single-process bound — module-level dict is fine for
    the broker-sync worker (one process, one tick at a time). If a
    follow-up brief moves broker-sync to multi-worker, replace the
    dict with a Redis SETNX or DB-backed counter.

    The two leading-underscore kwargs are injection seams for tests:
    ``_now`` substitutes the wall clock (production callers leave it
    None → ``time.time()``); ``_breaker_persister`` replaces the lazy
    import of :func:`portfolio_risk._persist_breaker_state` so the
    burst-trip logic can be asserted without DB IO.
    """
    global _RECONCILE_CLOSE_TOTAL
    _RECONCILE_CLOSE_TOTAL += 1
    now = float(_now) if _now is not None else time.time()
    bucket = int(now // _WIPEOUT_BURST_BUCKET_S)
    _wipeout_burst_buckets[bucket] = _wipeout_burst_buckets.get(bucket, 0) + 1

    # GC old buckets so the dict can't grow without bound.
    cutoff = bucket - (_WIPEOUT_BURST_BUCKET_RETENTION_S // _WIPEOUT_BURST_BUCKET_S)
    for old in list(_wipeout_burst_buckets.keys()):
        if old < cutoff:
            _wipeout_burst_buckets.pop(old, None)
            _wipeout_burst_tripped_buckets.discard(old)

    if (
        _wipeout_burst_buckets[bucket] >= _WIPEOUT_BURST_THRESHOLD
        and bucket not in _wipeout_burst_tripped_buckets
    ):
        _wipeout_burst_tripped_buckets.add(bucket)
        reason = (
            f"wipeout_burst_{_WIPEOUT_BURST_THRESHOLD}_in_"
            f"{_WIPEOUT_BURST_BUCKET_S}s"
        )
        logger.critical(
            "[broker_sync] WIPEOUT BURST DETECTED — %d reconcile-closes in "
            "<=%ds (latest: ticker=%s trade_id=%s); TRIPPING DRAWDOWN BREAKER "
            "with reason=%r",
            _wipeout_burst_buckets[bucket], _WIPEOUT_BURST_BUCKET_S,
            ticker, trade_id, reason,
        )
        try:
            persister = _breaker_persister
            if persister is None:
                from .trading.portfolio_risk import _persist_breaker_state
                persister = _persist_breaker_state
            persister(True, reason)
        except Exception:
            logger.warning(
                "[broker_sync] failed to persist breaker trip from wipeout "
                "burst — operator should manually trip and investigate",
                exc_info=True,
            )


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
            # FIX 46 pattern (rollback before close).
            try:
                db.rollback()
            except Exception:
                pass
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
            # FIX 46 pattern (canonical: scanner.py:1064-1074): explicit rollback
            # to end the implicit read-only transaction. session.close() alone
            # leaves the connection 'idle in transaction' in pg_stat_activity.
            # Lower volume than the market_data anchor leak but the same shape.
            try:
                db.rollback()
            except Exception:
                pass
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


def list_open_sell_orders_for_ticker(ticker: str) -> list[dict[str, Any]]:
    """Return open SELL orders for *ticker* WITHOUT cancelling.

    bracket-writer-respect-upside-targets (2026-05-04): the writer's
    pending-decision surface needs to expose the covering orders to the
    operator so they can decide keep-vs-replace. Mirrors
    ``cancel_open_sell_orders_for_ticker``'s iteration shape but takes
    no action.

    Each row in the returned list carries the broker fields the
    pending-decision JSON consumes: ``order_id``, ``type``,
    ``side``, ``quantity``, ``price``, ``stop_price``. Best-effort --
    individual instrument-lookup failures are skipped silently.
    Returns an empty list if the broker session is down.
    """
    if not is_connected():
        return []
    sym = (ticker or "").upper().strip()
    if not sym:
        return []
    out: list[dict[str, Any]] = []
    try:
        import robin_stocks.robinhood as rh

        all_open = rh.orders.get_all_open_stock_orders() or []
        for o in all_open:
            try:
                if (o.get("side") or "").lower() != "sell":
                    continue
                instr_url = o.get("instrument")
                inst = rh.helper.request_get(instr_url) if instr_url else {}
                p_sym = ((inst or {}).get("symbol") or "").upper().strip()
                if p_sym != sym:
                    continue
                order_id = o.get("id")
                if not order_id:
                    continue
                out.append({
                    "order_id": str(order_id),
                    "type": o.get("type"),
                    "side": "sell",
                    "quantity": _safe_float(o.get("quantity")),
                    "price": _safe_float(o.get("price")),
                    "stop_price": _safe_float(o.get("stop_price")),
                })
            except Exception:
                continue
    except Exception as exc:
        logger.warning(
            "[broker] list_open_sell_orders_for_ticker(%s) failed: %s", sym, exc,
        )
    return out


def cancel_open_sell_orders_for_ticker(ticker: str) -> int:
    """Cancel every open SELL order for *ticker*. Returns the count cancelled.

    FIX 57 (2026-05-01): when an existing limit-sell (target take-profit)
    fully commits the position's shares, a SELL_STOP can't be placed
    ("Not enough shares to sell"). The user explicitly opted into
    "cancel-and-replace": cancel the covering sell order(s) first, then
    bracket_writer_g2 places the stop on the now-free shares. Trade-off:
    the original take-profit ceiling is lost, but downside protection
    (which the user prioritizes) is gained.

    Iterates ``get_all_open_stock_orders()``, filters to side=sell for the
    given ticker, calls ``cancel_stock_order`` on each. Best-effort —
    individual cancellation failures don't abort the loop. Returns 0 if
    the broker session is down.
    """
    if not is_connected():
        return 0
    sym = (ticker or "").upper().strip()
    if not sym:
        return 0
    try:
        import robin_stocks.robinhood as rh

        all_open = rh.orders.get_all_open_stock_orders() or []
        cancelled = 0
        for o in all_open:
            try:
                if (o.get("side") or "").lower() != "sell":
                    continue
                instr_url = o.get("instrument")
                inst = rh.helper.request_get(instr_url) if instr_url else {}
                p_sym = ((inst or {}).get("symbol") or "").upper().strip()
                if p_sym != sym:
                    continue
                order_id = o.get("id")
                if not order_id:
                    continue
                try:
                    rh.orders.cancel_stock_order(order_id)
                    cancelled += 1
                    logger.warning(
                        "[broker] cancelled covering sell order ticker=%s id=%s "
                        "type=%s qty=%s price=%s stop=%s — to free shares for "
                        "SELL_STOP placement",
                        sym, order_id, o.get("type"), o.get("quantity"),
                        o.get("price"), o.get("stop_price"),
                    )
                except Exception as exc:
                    logger.warning(
                        "[broker] cancel of covering order %s failed: %s",
                        order_id, exc,
                    )
            except Exception:
                continue

        # Bust the held cache so callers re-querying see the post-cancel state.
        _cache.pop(f"held_for_sells::{sym}", None)
        return cancelled
    except Exception as exc:
        logger.warning(
            "[broker] cancel_open_sell_orders_for_ticker(%s) failed: %s", sym, exc,
        )
        return 0


def get_open_position_quantity(ticker: str) -> float | None:
    """TOTAL shares currently held for *ticker* per Robinhood truth.

    0.0 = a SUCCESSFUL fetch confirming the position is flat (safe to reconcile
    an exit against). None = unknown (session down / API error) — callers must
    fail SAFE and never treat unknown as flat. (2026-06-11 INDP: the exit-retry
    loop flattened a phantom position 8x because only Coinbase had a
    broker-zero reconcile.)"""
    if not is_connected():
        return None
    sym = (ticker or "").upper().strip()
    if not sym:
        return None
    try:
        import robin_stocks.robinhood as rh

        positions = rh.get_open_stock_positions() or []
        for p in positions:
            try:
                qty_total = float(p.get("quantity") or 0)
                instr_url = p.get("instrument")
                inst = rh.helper.request_get(instr_url) if instr_url else {}
                p_sym = ((inst or {}).get("symbol") or "").upper().strip()
                if p_sym == sym:
                    return qty_total
            except Exception:
                continue
        return 0.0  # successful fetch, symbol absent -> confirmed flat
    except Exception as e:
        logger.debug(f"[broker] get_open_position_quantity({sym}) failed: {e}")
        return None


def get_position_held_for_sells(ticker: str) -> float | None:
    """Return shares already committed to existing sell orders for *ticker*.

    FIX 55 (2026-05-01): the bracket writer needs to know whether a position
    is already protected by a resting sell order (target/take-profit limit
    or otherwise) before submitting a new SELL_STOP. ``build_holdings``
    doesn't expose this — only ``get_open_stock_positions`` does, via
    ``shares_held_for_sells``.

    Returns ``None`` if the broker session is down, the ticker is not held,
    or the API call raises. The caller treats ``None`` as "unknown — defer
    to the broker" rather than skipping (we don't want to block legitimate
    placements just because a single API hiccup denied us the held data).

    Cache TTL: 60s. Stop-loss placement runs once per minute per intent
    (post FIX 53), so a 60s cache means at most one extra API call per
    placement decision.
    """
    if not is_connected():
        return None
    sym = (ticker or "").upper().strip()
    if not sym:
        return None

    cache_key = f"held_for_sells::{sym}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached is not False else None

    try:
        import robin_stocks.robinhood as rh

        positions = rh.get_open_stock_positions() or []
        result: float | None = None
        for p in positions:
            try:
                qty_total = float(p.get("quantity") or 0)
                if qty_total <= 0:
                    continue
                instr_url = p.get("instrument")
                inst = rh.helper.request_get(instr_url) if instr_url else {}
                p_sym = ((inst or {}).get("symbol") or "").upper().strip()
                if p_sym == sym:
                    held = p.get("shares_held_for_sells")
                    result = float(held) if held is not None else 0.0
                    break
            except Exception:
                continue
        # Cache the negative outcome too (False marker) so we don't churn the API
        # for tickers that aren't currently held. _cache_set uses the module's
        # default TTL (_CACHE_TTL).
        _cache_set(cache_key, result if result is not None else False)
        return result
    except Exception as exc:
        logger.warning("[broker] get_position_held_for_sells(%s) failed: %s", sym, exc)
        return None


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
                ticker_full = currency.get("code", "???") + "-USD"
                # Phase 1 (2026-05-01): preserve crypto's 8-decimal precision.
                # The previous round(avg_cost, 4) silently truncated cost basis
                # for sub-penny coins, then the reconciler classified
                # price_drift against the unrounded broker truth.
                positions.append({
                    "ticker": ticker_full,
                    "quantity": qty,
                    "average_buy_price": (
                        normalize_price(avg_cost, ticker_full, asset_class="crypto")
                        if avg_cost else 0
                    ),
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
    """Fetch current market price for *ticker*. Falls back to entry price.

    NOTE: Use :func:`_resolve_close_exit_price` for broker-reconcile-close
    paths -- that variant returns None when no real price is recoverable
    instead of synthesizing entry_price (which fakes PnL=$0).
    """
    try:
        from .trading.market_data import fetch_quote
        quote = fetch_quote(ticker)
        if quote and quote.get("price"):
            return float(quote["price"])
    except Exception as exc:
        logger.debug(f"[broker] Could not fetch exit quote for {ticker}: {exc}")
    return float(fallback_entry or 0.0)


def _resolve_close_exit_price(ticker: str) -> float | None:
    """Resolve the actual exit price when a position has disappeared from
    the broker. Tries (in order):

      1. Robinhood's recent order history for a filled SELL on this ticker
         within the last 4 days. Uses ``average_price``, the broker-truth
         exit value.
      2. Current market quote via fetch_quote (may not match the actual
         fill but is closer than entry_price).
      3. Returns ``None`` when neither is available -- caller MUST treat
         None as "exit price unknown" and store pnl=NULL.

    Per the no-hardcoded-fallback principle (operator feedback 2026-04-29):
    do NOT silently substitute entry_price as the exit. That stamps the
    trade as flat (PnL=$0) and corrupts the brain's learning signal.
    """
    # 1. Most-reliable: Robinhood order history
    try:
        recent = get_recent_orders(limit=80) or []
        ticker_up = (ticker or "").upper()
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        cutoff = _dt.now(_tz.utc) - _td(days=4)
        for o in recent:
            if (o.get("ticker") or "").upper() != ticker_up:
                continue
            if (o.get("side") or "").lower() != "sell":
                continue
            if (o.get("state") or "").lower() not in ("filled", "partially_filled"):
                continue
            # parse created_at; filter to last 4 days only
            ts_raw = o.get("created_at") or ""
            try:
                ts = _dt.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_tz.utc)
            except (TypeError, ValueError):
                ts = None
            if ts is not None and ts < cutoff:
                continue
            px = o.get("price")
            try:
                if px is not None and float(px) > 0:
                    return float(px)
            except (TypeError, ValueError):
                pass
    except Exception:
        logger.debug(
            "[broker] _resolve_close_exit_price: order-history lookup failed for %s",
            ticker, exc_info=True,
        )

    # 2. Fallback to current market quote
    try:
        from .trading.market_data import fetch_quote
        quote = fetch_quote(ticker)
        if quote and quote.get("price"):
            return float(quote["price"])
    except Exception:
        logger.debug(
            "[broker] _resolve_close_exit_price: fetch_quote failed for %s",
            ticker, exc_info=True,
        )

    # 3. Unknown -- propagate None per no-hardcoded-fallback rule
    return None


# ── position-identity-phase-1 (2026-05-04) shadow-mode helpers ────────
#
# Per docs/DESIGN/POSITION_IDENTITY.md § 8.1: the position layer ships
# in shadow mode -- broker_sync writes to trading_positions /
# trading_position_events alongside today's trading_trades writes; NO
# READERS depend on the new tables for decisions in Phase 1. Failures
# in this code path log + continue; they NEVER raise to the surrounding
# sync_positions_to_db loop (the additive-shadow contract).
#
# Sync-gap detection threshold: 2 × the broker_sync cron interval.
# The broker_sync cron is configured at trading_scheduler.py:3317 with
# ``minute="*/2"`` -> 120 seconds between cycles. The 2× multiplier is
# the next-cycle-plus-tolerance derivation from design doc § 11.1
# Decision B. If the operator changes the broker_sync cron in
# trading_scheduler.py, this constant must move in lockstep -- comment
# above the source-of-truth and a test guard catch divergence.
_BROKER_SYNC_CRON_INTERVAL_SECONDS = 120
_SYNC_GAP_TOLERANCE_MULTIPLIER = 2
_SYNC_GAP_THRESHOLD_SECONDS = (
    _BROKER_SYNC_CRON_INTERVAL_SECONDS * _SYNC_GAP_TOLERANCE_MULTIPLIER
)


def _resolve_account_type_for_position(broker_source: str, ticker: str) -> str:
    """Resolve account_type for a broker-observed position.

    f-account-type-coinbase-retrofit (mig 250, 2026-05-18): Coinbase
    positions live in 'spot' accounts (Coinbase's API convention; the
    'cash' label was a Phase 1 placeholder). All other brokers continue
    to map to 'cash' until the autopilot routing layer (Phase 7)
    refines per-account-type rules.

    Mig 250 retrofitted existing Coinbase rows; this function ensures
    new fills get the correct value going forward.
    """
    if (broker_source or "").strip().lower() == "coinbase":
        return "spot"
    return "cash"


def _resolve_direction_for_position(broker_payload: Any) -> str:
    """Resolve direction for a broker-observed position. Robinhood
    retail does not surface short positions in get_positions(); all
    observations default to 'long'. Future perps venues
    (Hyperliquid/dYdX/Kraken Futures) will signal short via the
    payload shape; that integration adds a per-broker resolver.
    """
    return "long"


def _infer_asset_kind_for_position(ticker: str) -> str | None:
    """Mirror the auto-derivation logic at app/models/trading.py:151
    for trade.asset_kind. Returns 'crypto' for -USD tickers, 'equity'
    otherwise. Position layer keeps this for query convenience; not
    authoritative."""
    t = (ticker or "").upper().strip()
    if not t:
        return None
    if t.endswith("-USD"):
        return "crypto"
    return "equity"


def _phase1_record_position_observation(
    db: Session,
    *,
    user_id: int | None,
    broker_source: str,
    account_type: str,
    ticker: str,
    direction: str,
    asset_kind: str | None,
    broker_qty: float,
    broker_avg: float | None,
    broker_payload: Any | None,
) -> None:
    """Phase 1 shadow-mode write. Idempotent. Never raises -- caller's
    try/except wraps; failures inside log at debug.

    Per design doc § 8.1, on a single broker observation:
      1. Look up existing trading_positions row by natural key.
      2. If not found: INSERT + write 'opened' event.
      3. If found and state='closed': flip to 'open' + write 're_opened' event.
      4. If found and qty differs from snapshot: write 'qty_change' event.
      5. If found and identical: bump last_observed_at only (no event).
    Sync-gap detection (per § 11.1 Decision B): if the prior event for
    this position was older than _SYNC_GAP_THRESHOLD_SECONDS, write a
    'sync_gap' event BEFORE the current observation event.
    """
    from datetime import datetime, timedelta
    import json as _json

    payload_json = None
    if broker_payload is not None:
        try:
            payload_json = _json.dumps(broker_payload, default=str)
        except Exception:
            payload_json = None

    # 1. Look up existing position row.
    row = db.execute(
        text(
            "SELECT id, state, current_quantity, current_avg_price, last_observed_at "
            "FROM trading_positions "
            "WHERE COALESCE(user_id, -1) = COALESCE(:uid, -1) "
            "  AND broker_source = :bs "
            "  AND account_type = :at "
            "  AND ticker = :tk "
            "  AND direction = :dir"
        ),
        {
            "uid": user_id, "bs": broker_source, "at": account_type,
            "tk": ticker, "dir": direction,
        },
    ).first()

    now = datetime.utcnow()

    if row is None:
        # 2. INSERT + opened event.
        new_id = db.execute(
            text(
                "INSERT INTO trading_positions ("
                "  user_id, broker_source, account_type, ticker, direction, "
                "  asset_kind, current_quantity, current_avg_price, state, "
                "  last_observed_at, last_state_transition_at "
                ") VALUES ("
                "  :uid, :bs, :at, :tk, :dir, "
                "  :ak, :q, :avg, 'open', :now, :now"
                ") RETURNING id"
            ),
            {
                "uid": user_id, "bs": broker_source, "at": account_type,
                "tk": ticker, "dir": direction, "ak": asset_kind,
                "q": broker_qty, "avg": broker_avg, "now": now,
            },
        ).scalar_one()
        db.execute(
            text(
                "INSERT INTO trading_position_events ("
                "  position_id, event_type, transition_reason, quantity, "
                "  avg_price, broker_payload, observed_at "
                ") VALUES ("
                "  :pid, 'opened', 'broker_sync_first_observation', :q, "
                "  :avg, CAST(:p AS JSONB), :now"
                ")"
            ),
            {
                "pid": int(new_id), "q": broker_qty, "avg": broker_avg,
                "p": payload_json, "now": now,
            },
        )
        db.commit()
        return

    pos_id = int(row[0])
    prev_state = row[1] or "unknown"
    prev_qty = row[2]
    prev_observed = row[4]

    # Sync-gap detection: if last_observed_at is older than the threshold,
    # emit a sync_gap event capturing the missed window.
    if prev_observed is not None:
        gap_seconds = (now - prev_observed).total_seconds()
        if gap_seconds > _SYNC_GAP_THRESHOLD_SECONDS:
            db.execute(
                text(
                    "INSERT INTO trading_position_events ("
                    "  position_id, event_type, transition_reason, "
                    "  observed_at "
                    ") VALUES ("
                    "  :pid, 'sync_gap', 'sync_gap', :now"
                    ")"
                ),
                {"pid": pos_id, "now": prev_observed + timedelta(
                    seconds=_BROKER_SYNC_CRON_INTERVAL_SECONDS,
                )},
            )

    # 3. re_opened from closed.
    if prev_state == "closed":
        db.execute(
            text(
                "UPDATE trading_positions "
                "SET state='open', current_quantity=:q, current_avg_price=:avg, "
                "    asset_kind=COALESCE(asset_kind, :ak), "
                "    last_observed_at=:now, last_state_transition_at=:now, "
                "    updated_at=:now "
                "WHERE id=:pid"
            ),
            {"pid": pos_id, "q": broker_qty, "avg": broker_avg,
             "ak": asset_kind, "now": now},
        )
        db.execute(
            text(
                "INSERT INTO trading_position_events ("
                "  position_id, event_type, transition_reason, quantity, "
                "  avg_price, broker_payload, observed_at "
                ") VALUES ("
                "  :pid, 're_opened', 'broker_sync_position_reappeared', :q, "
                "  :avg, CAST(:p AS JSONB), :now"
                ")"
            ),
            {"pid": pos_id, "q": broker_qty, "avg": broker_avg,
             "p": payload_json, "now": now},
        )
        db.commit()
        return

    # 4. qty_change.
    qty_diff = (
        prev_qty is None
        or abs(float(prev_qty or 0) - float(broker_qty or 0)) > 1e-9
    )
    if qty_diff:
        db.execute(
            text(
                "UPDATE trading_positions "
                "SET current_quantity=:q, current_avg_price=:avg, "
                "    asset_kind=COALESCE(asset_kind, :ak), "
                "    last_observed_at=:now, updated_at=:now "
                "WHERE id=:pid"
            ),
            {"pid": pos_id, "q": broker_qty, "avg": broker_avg,
             "ak": asset_kind, "now": now},
        )
        db.execute(
            text(
                "INSERT INTO trading_position_events ("
                "  position_id, event_type, transition_reason, quantity, "
                "  avg_price, broker_payload, observed_at "
                ") VALUES ("
                "  :pid, 'qty_change', 'broker_sync_qty_observation', :q, "
                "  :avg, CAST(:p AS JSONB), :now"
                ")"
            ),
            {"pid": pos_id, "q": broker_qty, "avg": broker_avg,
             "p": payload_json, "now": now},
        )
        db.commit()
        return

    # 5. Identical -- bump last_observed_at only, no event.
    db.execute(
        text(
            "UPDATE trading_positions SET last_observed_at=:now, "
            "updated_at=:now WHERE id=:pid"
        ),
        {"pid": pos_id, "now": now},
    )
    db.commit()


def _phase1_close_dropped_positions(
    db: Session,
    *,
    user_id: int | None,
    broker_source: str,
    observed_tickers: set[str],
) -> None:
    """Phase 1 shadow-mode close-detection. Per design doc § 8.1: for
    each position currently state='open' whose ticker is NOT in this
    cycle's observed_tickers, write 'closed' event + flip state.

    Scoped by (user_id, broker_source) so a Robinhood broker_sync cycle
    doesn't touch Coinbase positions and vice-versa. Idempotent.
    Never raises -- caller's try/except wraps.
    """
    from datetime import datetime
    if not observed_tickers:
        # All-empty broker response is the R32 case; caller decides
        # whether to mass-close. Phase 1 mirrors that decision: when
        # observed_tickers is empty, do not close-drop here either.
        return
    now = datetime.utcnow()
    rows = db.execute(
        text(
            "SELECT id, ticker FROM trading_positions "
            "WHERE COALESCE(user_id, -1) = COALESCE(:uid, -1) "
            "  AND broker_source = :bs "
            "  AND state = 'open'"
        ),
        {"uid": user_id, "bs": broker_source},
    ).fetchall()
    for row in rows:
        pos_id = int(row[0])
        ticker = row[1]
        if ticker in observed_tickers:
            continue
        # Broker dropped this position. Close it in shadow mode.
        db.execute(
            text(
                "UPDATE trading_positions "
                "SET state='closed', current_quantity=0, "
                "    last_state_transition_at=:now, updated_at=:now "
                "WHERE id=:pid"
            ),
            {"pid": pos_id, "now": now},
        )
        db.execute(
            text(
                "INSERT INTO trading_position_events ("
                "  position_id, event_type, transition_reason, quantity, "
                "  observed_at "
                ") VALUES ("
                "  :pid, 'closed', 'broker_sync_position_gone', 0, :now"
                ")"
            ),
            {"pid": pos_id, "now": now},
        )
    db.commit()


def _synced_position_management_scope(db: Session, ticker: str) -> str:
    """Resolve the management_scope to stamp on a broker-synced open position.

    momentum-orphan adopt-on-cancel (2026-06-17): when a momentum_neural session
    lost track of a filled entry order (CRVO/FTHM: cancel raced the fill -> the
    broker holds the position but the session went terminal), the broker-sync
    backstop would mint/adopt the Trade as ``broker_sync`` and the legacy
    reconciler would mint a SECOND manager (stop + Trade) -> double-sell risk.
    The single-writer baton: if the symbol had a recent LIVE momentum session,
    stamp ``momentum_neural`` so the reconciler's scope-skip yields management to
    the momentum lane (which now adopts on cancel). FAIL-SAFE: any error -> the
    legacy ``broker_sync`` default (today's behavior, never worse).

    Kill-switch: when ``chili_momentum_adopt_on_cancel_fill_enabled`` is False this
    returns ``broker_sync`` unconditionally — byte-identical to pre-fix behavior.
    """
    from .trading.management_scope import (
        MANAGEMENT_SCOPE_BROKER_SYNC,
        MANAGEMENT_SCOPE_MOMENTUM_NEURAL,
    )

    if not bool(getattr(settings, "chili_momentum_adopt_on_cancel_fill_enabled", True)):
        return MANAGEMENT_SCOPE_BROKER_SYNC
    try:
        from .trading.bracket_reconciliation_service import (
            _symbol_had_recent_momentum_live_session,
        )

        if _symbol_had_recent_momentum_live_session(db, ticker):
            return MANAGEMENT_SCOPE_MOMENTUM_NEURAL
    except Exception:
        logger.debug(
            "[broker_sync] momentum-scope probe failed for %s; defaulting broker_sync",
            ticker, exc_info=True,
        )
    return MANAGEMENT_SCOPE_BROKER_SYNC


def sync_positions_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Sync Robinhood positions into local Trade model."""
    from sqlalchemy import text
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

    created = updated = closed = reopened = 0

    rh_tickers = set()
    for pos in all_positions:
        ticker = pos["ticker"]
        rh_tickers.add(ticker)
        qty = pos.get("quantity", 0)
        avg_price = pos.get("average_buy_price", 0)

        if not qty or qty <= 0:
            continue

        # FIX 47 (2026-04-29): skip positions where the broker returned a
        # zero/missing average price. Some legacy Robinhood crypto holdings
        # (observed with ETH-USD) come back with qty > 0 but
        # average_buy_price == 0 — likely because the position predates the
        # API field or sits on a deprecated nummus shard. The Trade model's
        # @validates("entry_price") raises ValueError for entry_price <= 0,
        # which crashed the WHOLE broker_sync run mid-iteration. Skipping the
        # single bad ticker (with a warning log) lets the rest of the sync
        # complete for healthy positions.
        try:
            _ap = float(avg_price or 0)
        except (TypeError, ValueError):
            _ap = 0.0
        if _ap <= 0:
            logger.warning(
                "[broker_sync] skipping %s: broker returned qty=%s but avg_price=%r "
                "(likely legacy/incomplete position data); not writing Trade row.",
                ticker, qty, avg_price,
            )
            continue
        avg_price = _ap

        # position-identity-phase-1 (2026-05-04): shadow-mode write to
        # the position layer. Additive; NEVER raises to this loop;
        # NO READERS depend on these tables for decisions in Phase 1.
        # Per docs/DESIGN/POSITION_IDENTITY.md § 8.1.
        try:
            _phase1_record_position_observation(
                db,
                user_id=user_id,
                broker_source="robinhood",
                account_type=_resolve_account_type_for_position("robinhood", ticker),
                ticker=ticker,
                direction=_resolve_direction_for_position(pos),
                asset_kind=_infer_asset_kind_for_position(ticker),
                broker_qty=float(qty),
                broker_avg=float(avg_price) if avg_price else None,
                broker_payload=pos,
            )
        except Exception:
            logger.warning(
                "[phase1_position_event] write failed for %s; shadow-mode continues",
                ticker, exc_info=True,
            )

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
            # Only stamp a scope when blank — never downgrade an already-explicit
            # scope (e.g. auto_trader_v1). The momentum-orphan baton overrides the
            # broker_sync default to momentum_neural when this symbol had a recent
            # live momentum session (see _synced_position_management_scope).
            if not existing.management_scope:
                existing.management_scope = _synced_position_management_scope(db, ticker)
            if not existing.indicator_snapshot:
                existing.indicator_snapshot = _compute_trade_snapshot(ticker, avg_price)
            updated += 1
        else:
            # broker-truth-self-heal (2026-05-04): inverse-reconcile.
            # Broker reports this position is alive. If the most recent
            # local Trade row for (user, robinhood, ticker) is closed AND
            # has zero execution-event history (i.e. no broker activity
            # was ever recorded against it), the close was bookkeeping-
            # only -- one of the automated close paths (the now-retired
            # phantom_after_terminal_reject, the freeze-replaced
            # emergency_price_monitor_guardrail, an old
            # broker_reconcile_position_gone before R32) flipped the
            # status without an actual broker exit. Re-open the existing
            # row instead of creating a fresh one (preserves entry_reason,
            # pattern, scan_pattern_id, and the bracket_intent FK chain).
            #
            # Cross-checks (ALL must hold):
            #   * status='closed' on the most-recent row
            #   * zero rows in trading_execution_events for that trade_id
            #     (any execution event signals real broker activity --
            #     stricter than "no SELL fill" because there is no SELL
            #     discriminator on the events table; safer in the
            #     contradiction direction)
            #   * exact qty match (1e-9 tolerance)
            #   * entry_price vs broker avg_price match. Crypto rows may use
            #     rounded entry prices while Robinhood reports 8-decimal cost
            #     basis, so tolerate a small relative delta there.
            #
            # If all hold: re-open + re-arm bracket_intent + audit log.
            # If execution_events count > 0 OR qty/price mismatch:
            # contradiction or different-position -- fall through to GGG
            # revive / C2 phantom guard, which handle their own cases.
            most_recent = (
                db.query(Trade)
                .filter(
                    Trade.user_id == user_id,
                    Trade.ticker == ticker,
                    Trade.broker_source == "robinhood",
                )
                .order_by(Trade.entry_date.desc())
                .first()
            )
            if most_recent is not None and most_recent.status == "closed":
                event_count = db.execute(
                    text(
                        "SELECT COUNT(*) FROM trading_execution_events "
                        "WHERE trade_id = :tid"
                    ),
                    {"tid": int(most_recent.id)},
                ).scalar() or 0
                qty_match = abs(
                    float(most_recent.quantity or 0) - float(qty or 0)
                ) < 1e-9
                price_match = False
                price_delta_bps: float | None = None
                if most_recent.entry_price is not None and avg_price is not None:
                    try:
                        entry_f = float(most_recent.entry_price)
                        avg_f = float(avg_price)
                        price_delta = abs(entry_f - avg_f)
                        price_ref = max(abs(entry_f), abs(avg_f), 1e-9)
                        price_delta_bps = (price_delta / price_ref) * 10000.0
                        asset_kind = (
                            getattr(most_recent, "asset_kind", None)
                            or _infer_asset_kind_for_position(ticker)
                        )
                        rel_tol = 0.005 if asset_kind == "crypto" else 0.0
                        price_match = (
                            price_delta < 1e-9
                            or (rel_tol > 0 and (price_delta / price_ref) <= rel_tol)
                        )
                    except Exception:
                        price_match = False
                # f-position-identity-phase-4 (2026-05-18): replace the
                # conservative event_count==0 check with a precise position-
                # level "has the broker ever recorded a SELL fill?" check.
                #
                # Phase 1's per-trade_id event_count was conservative: any
                # event (status pings, stop-place attempts, etc.) on the
                # current trade_id would block re-open, even though the
                # actual fills attached to the dead prior trade_id were
                # the load-bearing signal.
                #
                # Phase 4's check consults position_id across ALL trade
                # generations and only returns True when there's a real
                # SELL fill (status='filled' AND payload.side='sell').
                # Flag-gated by chili_position_identity_phase4_authority_enabled
                # (default False). When off, the legacy event_count==0
                # path remains authoritative.
                _phase4_enabled = False
                _phase4_has_sell = False
                try:
                    from .trading.position_resolver import (
                        position_has_recorded_sell as _phase4_check,
                        resolve_position_id as _phase4_resolve_position_id,
                    )
                    from ..config import settings as _phase4_settings
                    _phase4_enabled = bool(getattr(
                        _phase4_settings,
                        "chili_position_identity_phase4_authority_enabled",
                        False,
                    ))
                    if _phase4_enabled:
                        _pid = getattr(most_recent, "position_id", None)
                        if _pid is None:
                            _pid = _phase4_resolve_position_id(
                                db,
                                trade=most_recent,
                                user_id=user_id,
                                ticker=ticker,
                                broker_source="robinhood",
                            )
                        if _pid is None:
                            # Phase 4 is only safe when the position can be
                            # resolved. Unknown position identity must retain
                            # the older conservative event-count guard.
                            _phase4_enabled = False
                        else:
                            _phase4_has_sell = _phase4_check(db, _pid)
                except Exception:
                    # Belt-and-suspenders: if any import / lookup fails,
                    # fall back to the legacy event_count==0 path. NEVER
                    # let Phase 4 plumbing break broker_sync.
                    _phase4_enabled = False
                    _phase4_has_sell = False

                if _phase4_enabled:
                    _close_was_bookkeeping = not _phase4_has_sell
                else:
                    _close_was_bookkeeping = int(event_count) == 0

                if _close_was_bookkeeping and qty_match and price_match:
                    prior_exit_reason = most_recent.exit_reason or "<unset>"
                    most_recent.status = "open"
                    most_recent.exit_date = None
                    most_recent.exit_price = None
                    most_recent.exit_reason = None
                    most_recent.quantity = qty
                    if avg_price is not None:
                        most_recent.entry_price = avg_price
                    if not getattr(most_recent, "asset_kind", None):
                        most_recent.asset_kind = _infer_asset_kind_for_position(ticker)
                    if hasattr(most_recent, "pnl"):
                        most_recent.pnl = None
                    if hasattr(most_recent, "pnl_pct"):
                        most_recent.pnl_pct = None
                    most_recent.last_broker_sync = datetime.utcnow()
                    db.execute(
                        text(
                            "UPDATE trading_bracket_intents "
                            "SET intent_state='intent', "
                            "    last_diff_reason='inverse_reconcile_reopen', "
                            "    updated_at=NOW() "
                            "WHERE trade_id=:tid "
                            "  AND intent_state IN ('closed','reconciled','terminal_reject')"
                        ),
                        {"tid": int(most_recent.id)},
                    )
                    _phase4_tag = "phase4_no_sell" if _phase4_enabled else "phase1_event_count_0"
                    logger.warning(
                        "[broker_sync] INVERSE RECONCILE [%s]: re-opened "
                        "trade_id=%d position_id=%s ticker=%s qty=%s avg=%s "
                        "(prior exit_reason=%s, broker qty/price match, "
                        "price_delta_bps=%s)",
                        _phase4_tag,
                        most_recent.id,
                        getattr(most_recent, "position_id", None),
                        ticker, qty, avg_price,
                        prior_exit_reason,
                        price_delta_bps,
                    )
                    reopened += 1
                    continue
                # Contradiction branch -- under Phase 4 it fires when a sell
                # IS on record but broker still shows the position; under
                # legacy it fires when any event is on the dead trade_id.
                _is_contradiction = (
                    _phase4_has_sell if _phase4_enabled
                    else int(event_count) > 0
                )
                if _is_contradiction:
                    _ctag = "phase4_has_sell" if _phase4_enabled else "phase1_events_exist"
                    logger.error(
                        "[broker_sync] CONTRADICTION [%s]: trade_id=%d "
                        "position_id=%s ticker=%s status=closed has recorded "
                        "activity (event_count=%d, has_sell=%s) yet broker "
                        "still reports position qty=%s avg=%s. NOT "
                        "auto-reconciling. Operator review required.",
                        _ctag, most_recent.id,
                        getattr(most_recent, "position_id", None),
                        ticker, int(event_count), bool(_phase4_has_sell),
                        qty, avg_price,
                    )
                    continue
                # event_count == 0 but qty/price mismatch -- the broker
                # position differs from the closed Trade's recorded
                # values. Likely a different position (re-bought after
                # exit, partial fill, etc.); fall through to GGG/C2.
            # GGG -- broker_sync crypto dedup. Before creating a brand-new
            # broker_sync row, look for a recent (last 24h) trade on the
            # SAME ticker+broker that may have been stamped "rejected"
            # incorrectly while the broker actually holds the position.
            # The RAY-USD failure mode: trade#384 placed by autotrader_v1
            # at 10:18 -- broker filled but post-place verification timed
            # out and stamped the row rejected. Without this revive,
            # broker_sync then created phantom open rows on every cycle.
            from datetime import timedelta as _td
            _revive_cutoff = datetime.utcnow() - _td(hours=24)
            revive = (
                db.query(Trade)
                .filter(
                    Trade.user_id == user_id,
                    Trade.ticker == ticker,
                    Trade.broker_source == "robinhood",
                    Trade.status.in_(("rejected", "cancelled", "failed", "unknown")),
                    Trade.entry_date >= _revive_cutoff,
                )
                .order_by(Trade.id.desc())
                .first()
            )
            if revive is not None:
                _rev_entry = float(revive.entry_price or 0)
                _broker_avg = float(avg_price or 0)
                price_match = (
                    _broker_avg > 0 and _rev_entry > 0
                    and abs(_rev_entry - _broker_avg) / _broker_avg < 0.05
                )
                qty_match = abs(float(revive.quantity or 0) - float(qty or 0)) < 1e-6
                if price_match and qty_match:
                    # FIX C2b (2026-04-29 third-pass audit follow-up): the
                    # GGG revive path was reopening cancelled phantom trades
                    # without an order_id, undoing mig 205's cleanup. Resolve
                    # the order_id from recent history before reviving; if no
                    # order can be found, skip the revive and let the next
                    # reconcile pass retry once history is available.
                    if revive.broker_order_id:
                        _rev_resolved_id = revive.broker_order_id
                    else:
                        _rev_resolved_id = None
                        try:
                            _recent = get_recent_orders(limit=50)
                            for _o in _recent or []:
                                if (
                                    (_o.get("ticker") or "").upper() == ticker.upper()
                                    and (_o.get("side") or "").lower() == "buy"
                                    and (_o.get("state") or "").lower() in ("filled", "partially_filled")
                                    and _o.get("id")
                                ):
                                    _rev_resolved_id = str(_o["id"])
                                    break
                        except Exception:
                            logger.debug(
                                "[broker_sync] FIX C2b revive order-lookup failed for %s",
                                ticker, exc_info=True,
                            )
                            _rev_resolved_id = None
                    if not _rev_resolved_id:
                        logger.warning(
                            "[broker_sync] FIX C2b: REFUSING to revive cancelled "
                            "trade#%s for %s qty=%s -- no matching filled buy order "
                            "in recent history; will retry next pass.",
                            revive.id, ticker, qty,
                        )
                        continue
                    revive.status = "open"
                    revive.broker_status = "filled"
                    revive.broker_order_id = _rev_resolved_id
                    revive.filled_quantity = qty
                    revive.last_broker_sync = datetime.utcnow()
                    logger.warning(
                        "[broker_sync] GGG revived trade#%s ticker=%s qty=%s price=%s order_id=%s",
                        revive.id, ticker, qty, avg_price, _rev_resolved_id,
                    )
                    updated += 1
                    continue
            # FIX C2 (2026-04-29 third-pass audit): refuse to create a phantom
            # Trade row with NULL broker_order_id. The Robinhood positions API
            # does not surface the originating order_id; previously this writer
            # inserted a Trade with broker_order_id=None.
            resolved_order_id = None
            try:
                _recent = get_recent_orders(limit=50)
                for _o in _recent or []:
                    if (
                        (_o.get("ticker") or "").upper() == ticker.upper()
                        and (_o.get("side") or "").lower() == "buy"
                        and (_o.get("state") or "").lower() in ("filled", "partially_filled")
                        and _o.get("id")
                    ):
                        resolved_order_id = str(_o["id"])
                        break
            except Exception:
                logger.debug(
                    "[broker_sync] FIX C2 order-lookup failed for %s; will retry next pass",
                    ticker, exc_info=True,
                )
                resolved_order_id = None

            if not resolved_order_id:
                logger.warning(
                    "[broker_sync] FIX C2: position %s qty=%s avg=%s present in broker "
                    "but no matching filled buy order found in recent history; "
                    "REFUSING to create phantom Trade row. Next reconcile pass will retry.",
                    ticker, qty, avg_price,
                )
                continue

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
                management_scope=_synced_position_management_scope(db, ticker),
                indicator_snapshot=snapshot,
                last_broker_sync=datetime.utcnow(),
                stop_model="atr_crypto_breakout" if is_crypto else "atr_swing",
                broker_order_id=resolved_order_id,
                notes=f"Auto-synced from Robinhood on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} (order_id resolved from recent history)",
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

    # R32 (2026-04-30): guard against the auth-lapse / api-failure case.
    # When ``rh_tickers`` is empty -- because get_positions() returned []
    # while broker auth was failing, the network was flaky, or any other
    # transient -- the original ``Trade.ticker.notin_(rh_tickers) if
    # rh_tickers else True`` short-circuited to True, so EVERY open
    # local trade joined the stale list and got auto-closed via
    # ``broker_reconcile_position_gone``. That manufactured phantom
    # losses (R31's consecutive-loss breaker fix closes that loop, but
    # the underlying wipeout is THIS bug). Real incident: 2026-04-30
    # 15:56:02-15:56:03 UTC, 3 crypto positions closed within 1 second.
    #
    # We CANNOT distinguish "broker auth is flapping" from "account
    # legitimately has 0 positions" looking only at this snapshot.
    # Default to safety: refuse to mass-close. If the operator really
    # zeroed their account, repeated warnings will tell them to manually
    # reconcile the stale local rows.
    #
    # Phase B (f-equity-broker-reconcile-wipeout-protection, 2026-05-08):
    # the operator audit on the equity book found 14 phantom rows that
    # accreted PRE-R32. R32's empty-list guard has held since deploy;
    # this brief layered three additional defences:
    #   * ``_record_reconcile_close_burst`` (module-level above) trips
    #     the drawdown breaker on a 3-in-5s row-burst pattern even if
    #     R32's empty-list condition isn't met -- a wipeout signature
    #     by cardinality, distinct from R31's consecutive-loss check.
    #   * Per-close ``[broker_sync] RECONCILE_CLOSE`` warning replaces
    #     the prior ``logger.debug`` so ops can grep close events from
    #     the broker-sync worker logs in real time.
    #   * ``test_r32_empty_broker_positions_guard`` pins the
    #     ``skipped_reason='empty_broker_positions_with_open_local_trades'``
    #     return shape so a regression flips visibly red.
    # Phase A (f-pdt-count-broker-confirmed-only, commit 60c26f8) is
    # the symptom-side fix that filters the phantom rows out of the
    # PDT count; this guard + Phase B's defences together prevent new
    # phantoms from accreting.
    if not rh_tickers:
        open_local_count = (
            db.query(Trade)
            .filter(
                Trade.user_id == user_id,
                Trade.broker_source == "robinhood",
                Trade.status == "open",
            )
            .count()
        )
        if open_local_count > 0:
            logger.warning(
                "[broker_sync] R32 GUARD: get_positions() returned 0 positions "
                "but %d local trade(s) are open. Likely broker auth issue or "
                "transient API failure. REFUSING to mass-close (would "
                "manufacture phantom broker_reconcile_position_gone losses). "
                "Will retry next cycle.",
                open_local_count,
            )
            return {
                "created": created,
                "updated": updated,
                "closed": 0,
                "reopened": reopened,
                "skipped_reason": "empty_broker_positions_with_open_local_trades",
            }

    # f-equity-reconcile-partial-list-guard (2026-05-08): per-trade
    # consecutive-cycle counter. Increment for trades whose ticker is
    # NOT in this cycle's ``rh_tickers``; reset for those that ARE
    # present. Two bulk UPDATE statements per cycle -- the increment
    # query is gated on a non-empty ``rh_tickers`` because R32 already
    # short-circuits the empty-list case above and unconditional
    # increment-when-empty would defeat that guard. Streak is consumed
    # by the stale-close gate further down.
    if rh_tickers:
        try:
            db.query(Trade).filter(
                Trade.user_id == user_id,
                Trade.broker_source == "robinhood",
                Trade.status == "open",
                Trade.ticker.notin_(rh_tickers),
            ).update(
                {Trade.broker_sync_missing_streak:
                    Trade.broker_sync_missing_streak + 1},
                synchronize_session=False,
            )
            db.query(Trade).filter(
                Trade.user_id == user_id,
                Trade.broker_source == "robinhood",
                Trade.status == "open",
                Trade.ticker.in_(rh_tickers),
            ).update(
                {Trade.broker_sync_missing_streak: 0},
                synchronize_session=False,
            )
            logger.debug(
                "[broker_sync] partial-list streak updated; "
                "rh_tickers_size=%d threshold=%d",
                len(rh_tickers), _RECONCILE_PARTIAL_LIST_STREAK_MIN,
            )
        except Exception:
            logger.warning(
                "[broker_sync] partial-list streak bulk UPDATE failed; "
                "stale-close gate will fall back to time-window only",
                exc_info=True,
            )

    # position-identity-phase-1 (2026-05-04): close-detection in shadow
    # mode. For each trading_positions row currently state='open' for
    # this (user, broker) where the ticker is NOT in this cycle's
    # broker response, write a 'closed' event + flip state. Mirrors
    # the existing stale-trade close path below; runs additively in
    # shadow mode. Never raises; failures log + continue.
    try:
        _phase1_close_dropped_positions(
            db,
            user_id=user_id,
            broker_source="robinhood",
            observed_tickers=rh_tickers,
        )
    except Exception:
        logger.warning(
            "[phase1_position_event] close-detection failed; shadow-mode continues",
            exc_info=True,
        )

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

    # MMM -- skip option trades from the stale auto-close path.
    # broker_sync is option-blind: get_positions and get_crypto_positions
    # do not return option contracts, so an open option trade always
    # looks "stale" by ticker. The stale path closes it using
    # _get_exit_price(ticker) which fetches the UNDERLYING spot price
    # (e.g. SPY $715), turning a $4 option into a phantom $711 gain.
    # Phase 5 options exit monitor handles option exits separately.
    try:
        from .trading.autopilot_scope import is_option_trade
        before_n = len(stale)
        stale = [t for t in stale if not is_option_trade(t)]
        if len(stale) < before_n:
            logger.info(
                "[broker_sync] MMM: skipped %d option trade(s) from stale "
                "auto-close (broker_sync is option-blind; Phase 5 monitor handles)",
                before_n - len(stale),
            )
    except Exception:
        logger.debug("[broker_sync] MMM filter failed", exc_info=True)

    for trade in stale:
        try:
            from .trading.broker_position_truth import (
                broker_stale_open_trade_snapshot,
                reconcile_stale_robinhood_open_trade,
            )

            identity_snap = broker_stale_open_trade_snapshot(db, trade)
            if identity_snap and identity_snap.get("reason") in {
                "position_identity_closed",
                "position_identity_zero_qty",
            }:
                reconciled = reconcile_stale_robinhood_open_trade(
                    db,
                    trade,
                    snapshot=identity_snap,
                    source="sync_positions_to_db",
                    exit_price_resolver=lambda tr: _resolve_close_exit_price(tr.ticker),
                )
                if reconciled:
                    logger.warning(
                        "[broker_sync] RECONCILE_CLOSE: ticker=%s trade_id=%s "
                        "exit_reason=%s rh_tickers_size=%d broker_truth_reason=%s",
                        trade.ticker,
                        trade.id,
                        trade.exit_reason,
                        len(rh_tickers),
                        identity_snap.get("reason"),
                    )
                    if trade.exit_reason == "broker_reconcile_position_gone":
                        _record_reconcile_close_burst(trade.ticker, trade.id)
                    closed += 1
                    continue
        except Exception:
            logger.debug(
                "[broker_sync] position-identity stale reconcile skipped for trade#%s",
                getattr(trade, "id", None),
                exc_info=True,
            )

        # f-equity-reconcile-partial-list-guard (2026-05-08): require
        # the per-trade consecutive-missing streak to have reached
        # ``_RECONCILE_PARTIAL_LIST_STREAK_MIN`` before allowing the
        # stale-close path. With the default N=2, a single truncated
        # broker response increments the streak to 1 but defers the
        # close; only after a SECOND consecutive cycle missing is the
        # close authorized. Setting the threshold to 0 (env override)
        # disables this guard cleanly. The streak counter itself is
        # maintained by the bulk UPDATE above this loop.
        streak = getattr(trade, "broker_sync_missing_streak", 0) or 0
        if streak < _RECONCILE_PARTIAL_LIST_STREAK_MIN:
            logger.debug(
                "[broker_sync] %s missing from RH but streak=%d < threshold=%d "
                "(partial-list guard) -- deferring close",
                trade.ticker, streak, _RECONCILE_PARTIAL_LIST_STREAK_MIN,
            )
            continue

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

        try:
            from .trading.broker_position_truth import (
                pending_exit_thesis_reason as _pending_exit_thesis_reason,
                reconcile_exit_reason_preserving_pending_thesis as _reconcile_reason,
            )

            _pending_exit_reason = _pending_exit_thesis_reason(trade)
            _resolved_reconcile_reason = _reconcile_reason(
                trade,
                fallback="broker_reconcile_position_gone",
            )
        except Exception:
            _pending_exit_reason = None
            _resolved_reconcile_reason = "broker_reconcile_position_gone"

        trade.status = "closed"
        trade.exit_date = datetime.utcnow()
        trade.pending_exit_order_id = None
        trade.pending_exit_status = None
        trade.pending_exit_requested_at = None
        trade.pending_exit_reason = None
        trade.pending_exit_limit_price = None
        entry = trade.entry_price or 0.0
        qty = trade.quantity or 0.0
        # FIX (2026-04-30): try to resolve the REAL exit price from broker
        # order history before falling through to a market quote. If neither
        # is available, store pnl=NULL with exit_reason indicating unknown
        # exit price -- never fake PnL=$0 by synthesizing exit=entry, which
        # corrupts the brain's learning signal.
        resolved_exit = _resolve_close_exit_price(trade.ticker)
        if resolved_exit is not None and resolved_exit > 0:
            trade.exit_price = float(resolved_exit)
            if trade.direction == "short":
                trade.pnl = round((entry - resolved_exit) * qty, 2)
            else:
                trade.pnl = round((resolved_exit - entry) * qty, 2)
            if not trade.exit_reason:
                trade.exit_reason = _resolved_reconcile_reason
            trade.notes = (
                (trade.notes or "")
                + f"\nAuto-closed: position no longer on Robinhood "
                f"({datetime.utcnow().strftime('%Y-%m-%d %H:%M')}). "
                f"Exit ${resolved_exit:.4f} (resolved from order history or quote)."
            )
        else:
            # Honest unknown: do NOT fake PnL=0. Operator can later look up
            # the broker's actual fill and reconcile manually.
            trade.exit_price = None
            trade.pnl = None
            if not trade.exit_reason:
                trade.exit_reason = "broker_reconcile_no_exit_price"
            trade.notes = (
                (trade.notes or "")
                + f"\nAuto-closed: position no longer on Robinhood "
                f"({datetime.utcnow().strftime('%Y-%m-%d %H:%M')}). "
                f"Exit price UNKNOWN (no recent sell in order history, no live quote). "
                f"PnL left NULL; reconcile manually if material."
            )
            logger.warning(
                "[broker] %s closed without recoverable exit price; "
                "trade#%s left with pnl=NULL (no fake PnL=0).",
                trade.ticker, trade.id,
            )
        # R28 (2026-04-30): TCA call removed from this synthetic-close path.
        # The original code did `tca_reference_exit_price = exit_price` then
        # called apply_tca_on_trade_close, but `exit_price` was undefined in
        # this scope (latent NameError silently swallowed by the bare
        # except). Even if the var were valid, setting ref = fill produces
        # slippage = 0 by construction -- a corrupt zero rather than a
        # genuine measurement. Leaving tca_exit_slippage_bps NULL is the
        # honest state for an externally-driven close where CHILI made no
        # decision and has no reference price.
        try:
            from .trading.brain_work.execution_hooks import on_broker_reconciled_close

            on_broker_reconciled_close(db, trade, source="sync_positions_to_db")
        except Exception:
            pass

        # f-bracket-fired-stop-recording (2026-05-19): write a sell-side
        # execution_events row for this Robinhood stale-close. This is the
        # landing path for broker-fired bracket stops on equity: the
        # broker fires the resting stop autonomously, the position
        # vanishes, sync_positions sees the stale-open Trade and lands
        # here. Without this writer, Phase 4's position_has_recorded_sell
        # helper would not see these closures. Mirrors the Coinbase
        # version in coinbase_service.sync_positions_to_db.
        # Wrapped in a savepoint + try/except: observability-only, never
        # blocks close.
        _event_trade_id = int(getattr(trade, "id", 0) or 0)
        try:
            from .trading.execution_audit import record_execution_event

            _payload = {
                "side": "sell",
                "source": "broker_reconcile_position_gone",
                "trade_id": _event_trade_id,
                "exit_reason": trade.exit_reason,
                "pending_exit_reason": _pending_exit_reason,
                "broker_reconcile_exit_reason": (
                    "broker_reconcile_position_gone"
                    if resolved_exit is not None
                    else "broker_reconcile_no_exit_price"
                ),
                "synthetic": True,
            }
            with db.begin_nested():
                record_execution_event(
                    db,
                    user_id=trade.user_id,
                    ticker=trade.ticker,
                    trade=trade,
                    scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                    broker_source="robinhood",
                    event_type="broker_reconcile_gone_close",
                    status="filled",
                    average_fill_price=trade.exit_price,
                    cumulative_filled_quantity=float(trade.quantity or 0.0),
                    payload_json=_payload,
                )
        except Exception:
            logger.debug(
                "[broker_sync] sell-side execution_event write failed for "
                "trade#%s (non-fatal -- Phase 4 visibility only)",
                _event_trade_id, exc_info=True,
            )

        try:
            from .trading.bracket_intent_writer import mark_closed

            _intent_ids = db.execute(
                text(
                    "SELECT id FROM trading_bracket_intents "
                    "WHERE trade_id = :tid AND intent_state <> 'closed'"
                ),
                {"tid": _event_trade_id},
            ).scalars().all()
            for _intent_id in _intent_ids:
                mark_closed(
                    db,
                    int(_intent_id),
                    reason=str(trade.exit_reason or "broker_reconcile_close")[:128],
                )
        except Exception:
            logger.debug(
                "[broker_sync] bracket intent close failed for trade#%s "
                "(non-fatal)",
                _event_trade_id, exc_info=True,
            )

        if not trade.management_scope:
            # Same momentum-orphan baton as the open-position sync sites: only
            # stamp when blank, and prefer momentum_neural when the symbol had a
            # recent live momentum session (never downgrade an explicit scope).
            trade.management_scope = _synced_position_management_scope(
                db, getattr(trade, "ticker", "") or ""
            )
        # f-equity-broker-reconcile-wipeout-protection (2026-05-08):
        # structured close-event observability + wipeout-burst record.
        # Observability fires for BOTH exit-reason branches
        # (broker_reconcile_position_gone and
        # broker_reconcile_no_exit_price) so ops can grep the full
        # close stream. Burst-record fires only on the
        # position_gone reason since that is the wipeout signature
        # the breaker trips on.
        logger.warning(
            "[broker_sync] RECONCILE_CLOSE: ticker=%s trade_id=%s "
            "exit_reason=%s rh_tickers_size=%d",
            trade.ticker, trade.id, trade.exit_reason, len(rh_tickers),
        )
        if trade.exit_reason == "broker_reconcile_position_gone":
            _record_reconcile_close_burst(trade.ticker, trade.id)
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
        "reopened": reopened,
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
    skipped_options = 0
    for trade in manual_open:
        if trade.ticker not in live_tickers:
            if _is_option_trade_for_order_sync(trade):
                skipped_options += 1
                logger.info(
                    "[broker] Manual cleanup skipped option trade#%s ticker=%s; "
                    "stock/crypto position lists are option-blind",
                    getattr(trade, "id", None),
                    trade.ticker,
                )
                continue
            trade.status = "closed"
            trade.exit_date = datetime.utcnow()
            if not trade.exit_reason:
                trade.exit_reason = "broker_reconcile_manual_not_live"
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
            # R28 (2026-04-30): TCA call removed from this synthetic-close
            # path. The original code set tca_reference_exit_price = the
            # SAME market quote it had just written to trade.exit_price,
            # then called apply_tca_on_trade_close -- producing slippage =
            # (ref - fill) / ref = 0 every time. Corrupt zeros that
            # masquerade as real measurements. Leaving the column NULL
            # is the honest state when CHILI synthesized the close from a
            # market quote rather than a decision-time reference.
            try:
                from .trading.brain_work.execution_hooks import on_broker_reconciled_close

                on_broker_reconciled_close(db, trade, source="cleanup_manual_trades")
            except Exception:
                pass
            closed_manual += 1

    if closed_manual:
        db.commit()
        logger.info(f"[broker] Manual cleanup: {closed_manual} manual trade(s) auto-closed")

    return {"closed_manual": closed_manual, "skipped_options": skipped_options}


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
        is_option = _is_option_trade_for_order_sync(trade)
        if is_option and not trade.exit_price:
            logger.info(
                "[broker] Backfill skipped option trade#%s ticker=%s with no "
                "premium exit_price; refusing underlying quote fallback",
                getattr(trade, "id", None),
                trade.ticker,
            )
            continue
        exit_price = trade.exit_price or _get_exit_price(trade.ticker, entry)
        trade.exit_price = exit_price
        multiplier = 100.0 if is_option else 1.0
        if trade.direction == "short":
            trade.pnl = round((entry - exit_price) * qty * multiplier, 2)
        else:
            trade.pnl = round((exit_price - entry) * qty * multiplier, 2)
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
    time_in_force: str | None = None,
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
            # ORDER-TRUTH (2026-06-11): callers may force the TIF — day-trade
            # ENTRY limits must be 'gfd', never GTC: a dead momentum session's
            # resting GTC buy (KMRK) filled hours later into a -21.9% dump.
            tif = time_in_force or ("gtc" if order_type == "limit" else "gfd")
            return rh.orders.order(
                symbol=ticker,
                quantity=quantity,
                side="buy",
                # Phase 1 (2026-05-01): venue-aware tick alignment.
                # Replaces destructive round(*, 2) which truncated sub-dollar
                # equity prices (NMS Rule 612 sub-dollar tick is $0.0001).
                limitPrice=(
                    normalize_price(limit_price, ticker, asset_class="equity")
                    if order_type == "limit" and limit_price else None
                ),
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
                # Phase 1 (2026-05-01): venue-aware tick alignment.
                limitPrice=(
                    normalize_price(limit_price, ticker, asset_class="equity")
                    if order_type == "limit" and limit_price else None
                ),
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


def place_sell_stop_loss_order(
    ticker: str,
    quantity: float,
    *,
    trigger_price: float,
    market_hours_override: str | None = None,
    extended_hours_override: bool | None = None,
) -> dict[str, Any]:
    """Place a server-side STOP-LOSS SELL order via Robinhood.

    The order rests at the broker and triggers (becomes a market order)
    when the last trade prints at or below ``trigger_price``. This is
    the protective primitive used by the Phase G.2 bracket writer to
    repair ``missing_stop`` reconciliation findings on open long
    positions.

    Why a stop-loss MARKET (not stop-limit): a stop-limit can fail to
    fill on a fast gap-down because the limit price becomes non-marketable
    once the bid drops below it. A stop-loss converts to a market order
    on trigger, accepting slippage in exchange for guaranteed exit. For
    a defensive risk-protection primitive, guaranteed exit is the right
    trade-off.

    Returns the same envelope as ``place_sell_order``:
        {"ok": True,  "order_id": "...", "state": "...", "raw": {...}}
        {"ok": False, "error": "..."}

    Notes:
        * Robinhood requires ``timeInForce='gtc'`` for stop orders;
          'gfd' is rejected by the API.
        * ``trigger_price`` must be strictly below the current bid for
          a SELL stop-loss; the broker rejects "stop trigger above
          current price" as ``invalid_stop_price``. The caller (the
          bracket writer) is responsible for validating this before
          submission â we don't fetch a quote here so the call stays
          idempotent and free of side-effects on a probe.
    """
    if not _rh_available:
        return {"ok": False, "error": "robin_stocks not installed"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Robinhood"}
    if trigger_price is None or float(trigger_price) <= 0:
        return {"ok": False, "error": f"invalid trigger_price: {trigger_price!r}"}
    if quantity is None or float(quantity) <= 0:
        return {"ok": False, "error": f"invalid quantity: {quantity!r}"}

    # f-prefilter-bypass-and-cooldown-investigation (2026-05-08):
    # defence-in-depth backstop for the ADA/SOL crash loop. The
    # bracket_writer_g2 prefilter at place_missing_stop already
    # refuses crypto, but a stale-container deploy (or any other
    # bypass path that lands here) was still hitting the IndexError
    # inside rh.orders.order -> get_instruments_by_symbols('ADA')[0].
    # Refusing here too means the equity primitive cannot reach the
    # SDK for ANY crypto base regardless of upstream gating.
    #
    # Detection: if the (already-stripped) ticker matches a known
    # Robinhood crypto base, refuse. Robinhood's equity instruments
    # endpoint returns [] for crypto bases, so the [0] indexing inside
    # the SDK crashes; refusing here is the surgical alternative to
    # patching third-party code.
    base_for_check = (ticker or "").strip().upper()
    if (
        base_for_check
        and base_for_check in ROBINHOOD_SUPPORTED_CRYPTO_BASES
    ):
        logger.warning(
            "[broker] SELL_STOP refused: ticker=%s is a Robinhood crypto "
            "base; the equity rh.orders.order primitive crashes inside "
            "get_instruments_by_symbols('%s')[0]. Use a crypto-native "
            "stop primitive when one is wired.",
            ticker, base_for_check,
        )
        return {
            "ok": False,
            "error": "crypto_ticker_unsupported_via_equity_primitive",
        }

    try:
        import robin_stocks.robinhood as rh
        session_kwargs = _rh_order_session_kwargs(
            market_hours_override=market_hours_override,
            extended_hours_override=extended_hours_override,
        )

        # f-bracket-writer-stop-construction-fix (2026-05-06): normalize
        # the stop_price to the venue's tick size BEFORE submission. Today's
        # PED rejection cascade (45+ retries / hour, all "Limit order
        # requested, but no price provided.") was traced back to either
        # (a) the broker rejecting an invalid 4-decimal stop_price with a
        # misleading limit-order error, or (b) something in the body
        # construction. Step 1 of the fix surfaces the actual on-wire body
        # via INFO-level pre-submit + WARNING-level full-diagnostic on
        # rejection. tick_normalizer.normalize_price already aligns to NMS
        # Rule 612 (equity >= $1 → 2 decimals), so the PED 13.6275 → 13.63
        # rounding lands here. Computing once outside the retry closure so
        # the same value gets logged + sent.
        normalized_stop = normalize_price(
            trigger_price, ticker, asset_class="equity",
        )
        if normalized_stop != trigger_price:
            logger.info(
                "[broker] stop_price rounded to broker tick: "
                "ticker=%s %s -> %s",
                ticker, trigger_price, normalized_stop,
            )

        def _do_stop_sell():
            # robin_stocks rh.orders.order(...) DERIVES order type internally
            # from which prices are set:
            #   stopPrice + no limitPrice  -> stop-loss MARKET (the trigger
            #     converts to a market order on breach)
            #   stopPrice + limitPrice     -> stop-LIMIT
            #   limitPrice only            -> LIMIT
            #   neither                    -> MARKET
            # Older code here passed trigger='stop'/orderType='market' as
            # explicit kwargs, but rh.orders.order rejects those with
            # TypeError. Pass just stopPrice + side='sell' to land a real
            # stop-loss-market order.
            logger.info(
                "[broker] SELL_STOP submitting: ticker=%s qty=%s "
                "stopPrice=%s tif=gtc extended=%s market_hours=%s",
                ticker, quantity, normalized_stop,
                session_kwargs["extendedHours"],
                session_kwargs["market_hours"],
            )
            return rh.orders.order(
                symbol=ticker,
                quantity=quantity,
                side="sell",
                # Phase 1 (2026-05-01): the destructive round(*, 2) here was
                # the smoking-gun for the 2026-05-01 incident. CCCC 2.5898 →
                # 2.59 was getting flagged invalid by Robinhood post-acceptance.
                # tick_normalizer aligns to the venue's actual rule.
                stopPrice=normalized_stop,
                timeInForce="gtc",
                extendedHours=session_kwargs["extendedHours"],
                market_hours=session_kwargs["market_hours"],
                jsonify=True,
            )

        result = _retry_api_call(_do_stop_sell, label=f"SELL_STOP {ticker}")

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            if not order_id:
                error_msg = (
                    result.get("detail")
                    or result.get("error")
                    or result.get("message")
                    or "Robinhood returned no order_id"
                )
                logger.error(
                    f"[broker] SELL_STOP rejected (no order_id): {ticker} "
                    f"x{quantity} trigger={trigger_price} response={result}"
                )
                # f-bracket-writer-stop-construction-fix: full-diagnostic
                # log so the operator can see the on-wire body when RH
                # returns a misleading error. Includes the normalized
                # stop_price (the value actually sent), all session flags,
                # and the full response body (truncated to 1000 chars).
                logger.warning(
                    "[broker] SELL_STOP rejected (full diagnostic): "
                    "ticker=%s qty=%s trigger_in=%s normalized_stop=%s "
                    "tif=gtc extended=%s market_hours=%s response=%s",
                    ticker, quantity, trigger_price, normalized_stop,
                    session_kwargs["extendedHours"],
                    session_kwargs["market_hours"],
                    str(result)[:1000],
                )
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
            logger.info(
                f"[broker] SELL_STOP order placed: {ticker} x{quantity} "
                f"trigger={trigger_price} -> {state}"
            )
            # Stop orders rest at the broker until triggered; do NOT poll
            # to terminal here â that's a different lifecycle than a
            # market order. The reconciliation sweep tracks broker state.
            _cache.pop("recent_orders", None)
            return {"ok": True, "order_id": order_id, "state": state, "raw": result}
        else:
            error_msg = str(result) if result else "Empty response from Robinhood"
            logger.error(
                f"[broker] SELL_STOP order failed for {ticker}: {error_msg}"
            )
            return {"ok": False, "error": error_msg}

    except Exception as e:
        logger.error(
            f"[broker] SELL_STOP exception for {ticker}: {e}", exc_info=True
        )
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


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out > 0 else None


def _decimal_plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    units = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return units * increment


def _round_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    units = (value / increment).to_integral_value(rounding=ROUND_HALF_UP)
    return units * increment


def _get_robinhood_crypto_pair_info(base: str) -> dict[str, Any] | None:
    """Return Robinhood's pair metadata, cached briefly with other broker data."""
    symbol = _to_crypto_base(base)
    if not symbol:
        return None
    key = f"crypto_pair_info:{symbol}"
    cached = _cache.get(key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        data = cached[1]
        return data if isinstance(data, dict) else None
    try:
        import robin_stocks.robinhood as rh

        data = rh.crypto.get_crypto_info(symbol)
        if isinstance(data, dict) and data:
            _cache[key] = (time.time(), data)
            return data
    except Exception:
        logger.debug(
            "[broker] could not load crypto pair info for %s",
            symbol,
            exc_info=True,
        )
    return None


def _normalize_robinhood_crypto_quantity(
    base: str,
    quantity: float | int | Decimal | str,
) -> tuple[str | None, dict[str, Any]]:
    """Align crypto quantity to Robinhood's per-pair order increment.

    Robinhood advertises pair-specific ``min_order_quantity_increment``.
    Some pairs, for example COMP, reject otherwise valid 8-decimal crypto
    quantities. Floor the size so we never accidentally submit a larger
    buy than intended or a sell above available inventory.
    """
    symbol = _to_crypto_base(base)
    ticker = f"{symbol}-USD" if symbol else str(base or "")
    raw = _decimal_or_none(quantity)
    if raw is None:
        return None, {"error": f"invalid quantity: {quantity!r}"}

    info = _get_robinhood_crypto_pair_info(symbol) or {}
    increment = (
        _decimal_or_none(info.get("min_order_quantity_increment"))
        or Decimal("0.00000001")
    )
    min_size = _decimal_or_none(info.get("min_order_size"))
    max_size = _decimal_or_none(info.get("max_order_size"))

    if not info:
        # Preserve the legacy 8-decimal fallback when metadata is unavailable.
        raw = _decimal_or_none(normalize_quantity(raw, ticker)) or raw

    aligned = _floor_to_increment(raw, increment)
    if aligned <= 0:
        return None, {
            "error": (
                f"quantity_below_increment:{symbol}:"
                f"qty={_decimal_plain(raw)} increment={_decimal_plain(increment)}"
            )
        }
    if min_size is not None and aligned < min_size:
        return None, {
            "error": (
                f"quantity_below_min_size:{symbol}:"
                f"qty={_decimal_plain(aligned)} min={_decimal_plain(min_size)}"
            )
        }
    if max_size is not None and aligned > max_size:
        return None, {
            "error": (
                f"quantity_above_max_size:{symbol}:"
                f"qty={_decimal_plain(aligned)} max={_decimal_plain(max_size)}"
            )
        }

    meta = {
        "symbol": symbol,
        "raw_quantity": _decimal_plain(raw),
        "quantity": _decimal_plain(aligned),
        "min_order_quantity_increment": _decimal_plain(increment),
        "min_order_size": _decimal_plain(min_size) if min_size is not None else None,
        "max_order_size": _decimal_plain(max_size) if max_size is not None else None,
        "adjusted": aligned != raw,
    }
    return meta["quantity"], meta


def _normalize_robinhood_crypto_limit_price(
    base: str,
    limit_price: float | int | Decimal | str,
) -> float:
    symbol = _to_crypto_base(base)
    info = _get_robinhood_crypto_pair_info(symbol) or {}
    raw = _decimal_or_none(limit_price)
    increment = _decimal_or_none(info.get("min_order_price_increment"))
    if raw is None or increment is None:
        return normalize_price(limit_price, f"{symbol}-USD", asset_class="crypto")
    return float(_round_to_increment(raw, increment))


def _extract_robinhood_error_message(result: Any, fallback: str) -> str:
    if not isinstance(result, dict):
        return str(result) if result else fallback
    for key in ("detail", "error", "message"):
        value = result.get(key)
        if value:
            return str(value)
    for key, value in result.items():
        if isinstance(value, list) and value:
            return f"{key}: {'; '.join(str(v) for v in value)}"
        if isinstance(value, dict) and value:
            return f"{key}: {value}"
    return fallback


def _robinhood_crypto_order_payload_quantity(quantity: str) -> float:
    """Use a numeric payload while preserving prior Decimal tick alignment."""
    return float(Decimal(str(quantity)))


def _coerce_robinhood_crypto_order_response(
    result: Any,
    *,
    fallback: str,
) -> tuple[dict[str, Any] | None, str | None, Any | None]:
    """Normalize robin_stocks crypto order responses.

    ``robin_stocks`` treats HTTP 422 as an exception and, when asked for JSON,
    returns ``None`` after printing the status. Calling it with
    ``jsonify=False`` returns the underlying response object, so parse it here
    and keep the broker's rejection body for logs and alert decisions.
    """
    if hasattr(result, "status_code"):
        status = int(getattr(result, "status_code", 0) or 0)
        try:
            body = result.json()
        except Exception:
            body = (getattr(result, "text", "") or "").strip()
        if 200 <= status < 300:
            if isinstance(body, dict):
                return body, None, body
            return None, f"Robinhood crypto HTTP {status}: non-JSON response", body
        if isinstance(body, dict):
            msg = _extract_robinhood_error_message(body, fallback)
        else:
            msg = str(body or fallback)
        return None, f"Robinhood crypto HTTP {status}: {msg}", body

    if isinstance(result, dict):
        return result, None, result
    return None, str(result) if result else fallback, result


# audit-unsupported-crypto-prefilter (2026-05-04) — static whitelist of
# crypto bases Robinhood actually trades. The intent is a cheap, offline,
# deterministic prefilter that runs BEFORE any broker call, complementing
# the older ``_is_crypto_supported_on_robinhood`` quote-probe (which still
# runs as defense-in-depth and self-heals on Robinhood adding/removing
# pairs without a list update).
#
# Failure mode is "false unsupported" — if Robinhood adds a pair we don't
# list, we'll false-reject until the list updates. That direction is safer
# than false-accept, which produces broker tracebacks (the exact problem
# this prefilter exists to stop).
#
# Source of truth: this module. Touch carefully — adding a base routes it
# through Robinhood's crypto API; removing one bypasses it.
ROBINHOOD_SUPPORTED_CRYPTO_BASES: frozenset[str] = frozenset({
    # Fast-path canonical pairs (CHILI_FAST_PATH_PAIRS in docker-compose).
    # MUST stay in sync with the fast-path baseline; test asserts
    # this subset is present.
    "BTC", "ETH", "SOL", "AVAX", "DOGE",
    # Other Robinhood-listed crypto bases (verified against rh.crypto
    # holdings as of 2026-05-04). Subset of the broader Coinbase / brain
    # universe; the brain scans more, only these execute on Robinhood.
    "ADA", "BCH", "ETC", "LTC", "SHIB", "UNI", "XLM", "XTZ", "AAVE",
    "COMP", "LINK", "USDC",
})


def is_robinhood_supported_crypto(base: str) -> bool:
    """Return True iff ``base`` (the bare crypto symbol, e.g. ``BTC``)
    is on the static Robinhood-supported whitelist.

    Cheap, offline, no broker round-trip. Use as a pre-broker filter
    before any code path that would invoke Robinhood's crypto endpoints
    (autotrader, bracket writer, etc.). Falls back to the slower
    quote-probe (``_is_crypto_supported_on_robinhood``) only when the
    static list misses — defense-in-depth for new pairs the list
    hasn't caught up with.
    """
    if not base:
        return False
    return str(base).strip().upper() in ROBINHOOD_SUPPORTED_CRYPTO_BASES


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

    order_quantity, qty_meta = _normalize_robinhood_crypto_quantity(base, quantity)
    if order_quantity is None:
        return {"ok": False, "error": qty_meta.get("error", "invalid crypto quantity")}
    if qty_meta.get("adjusted"):
        logger.info(
            "[broker] BUY-CRYPTO quantity aligned: %s raw=%s qty=%s increment=%s",
            base,
            qty_meta.get("raw_quantity"),
            qty_meta.get("quantity"),
            qty_meta.get("min_order_quantity_increment"),
        )
    order_quantity_payload = _robinhood_crypto_order_payload_quantity(order_quantity)

    try:
        import robin_stocks.robinhood as rh

        def _do_buy():
            if order_type == "limit":
                if not limit_price or limit_price <= 0:
                    raise ValueError("limit_price required for crypto limit order")
                return rh.orders.order_buy_crypto_limit(
                    symbol=base,
                    quantity=order_quantity_payload,
                    # Phase 1 (2026-05-01): crypto needs 8-decimal precision.
                    # The previous round(*, 2) silently destroyed sub-penny
                    # crypto prices (DOGE-USD 0.10984 → 0.11 = 1.4% slippage).
                    # ``ticker`` (full -USD form) drives venue detection.
                    limitPrice=_normalize_robinhood_crypto_limit_price(base, limit_price),
                    timeInForce="gtc",
                    jsonify=False,
                )
            return rh.orders.order_buy_crypto_by_quantity(
                symbol=base,
                quantity=order_quantity_payload,
                jsonify=False,
            )

        result = _retry_api_call(_do_buy, label=f"BUY-CRYPTO {base}")
        result, transport_error, raw_result = _coerce_robinhood_crypto_order_response(
            result,
            fallback="Empty response from Robinhood crypto",
        )
        if transport_error:
            logger.error("[broker] BUY-CRYPTO order failed for %s: %s", base, transport_error)
            return {"ok": False, "error": transport_error, "raw": raw_result}

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            if not order_id:
                error_msg = _extract_robinhood_error_message(
                    result,
                    "Robinhood crypto endpoint returned no order_id",
                )
                logger.error(
                    "[broker] BUY-CRYPTO rejected (no order_id): %s x%s response=%s",
                    base, order_quantity, result,
                )
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
            logger.info(
                "[broker] BUY-CRYPTO order placed: %s x%s (%s) -> %s",
                base, order_quantity, order_type, state,
            )
            _cache.pop("crypto_positions", None)
            _cache.pop("portfolio", None)
            return {"ok": True, "order_id": order_id, "state": state, "raw": result}

        error_msg = "Empty response from Robinhood crypto"
        logger.error("[broker] BUY-CRYPTO order failed for %s: %s", base, error_msg)
        return {"ok": False, "error": error_msg, "raw": raw_result}

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

    order_quantity, qty_meta = _normalize_robinhood_crypto_quantity(base, quantity)
    if order_quantity is None:
        return {"ok": False, "error": qty_meta.get("error", "invalid crypto quantity")}
    if qty_meta.get("adjusted"):
        logger.info(
            "[broker] SELL-CRYPTO quantity aligned: %s raw=%s qty=%s increment=%s",
            base,
            qty_meta.get("raw_quantity"),
            qty_meta.get("quantity"),
            qty_meta.get("min_order_quantity_increment"),
        )
    order_quantity_payload = _robinhood_crypto_order_payload_quantity(order_quantity)

    try:
        import robin_stocks.robinhood as rh

        def _do_sell():
            if order_type == "limit":
                if not limit_price or limit_price <= 0:
                    raise ValueError("limit_price required for crypto limit order")
                return rh.orders.order_sell_crypto_limit(
                    symbol=base,
                    quantity=order_quantity_payload,
                    # Phase 1 (2026-05-01): see place_crypto_buy_order for context.
                    limitPrice=_normalize_robinhood_crypto_limit_price(base, limit_price),
                    timeInForce="gtc",
                    jsonify=False,
                )
            return rh.orders.order_sell_crypto_by_quantity(
                symbol=base,
                quantity=order_quantity_payload,
                jsonify=False,
            )

        result = _retry_api_call(_do_sell, label=f"SELL-CRYPTO {base}")
        result, transport_error, raw_result = _coerce_robinhood_crypto_order_response(
            result,
            fallback="Empty response from Robinhood crypto",
        )
        if transport_error:
            logger.error("[broker] SELL-CRYPTO order failed for %s: %s", base, transport_error)
            return {"ok": False, "error": transport_error, "raw": raw_result}

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            if not order_id:
                error_msg = _extract_robinhood_error_message(
                    result,
                    "Robinhood crypto endpoint returned no order_id",
                )
                logger.error(
                    "[broker] SELL-CRYPTO rejected (no order_id): %s x%s response=%s",
                    base, order_quantity, result,
                )
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
            logger.info(
                "[broker] SELL-CRYPTO order placed: %s x%s (%s) -> %s",
                base, order_quantity, order_type, state,
            )
            _cache.pop("crypto_positions", None)
            _cache.pop("portfolio", None)
            return {"ok": True, "order_id": order_id, "state": state, "raw": result}

        error_msg = "Empty response from Robinhood crypto"
        logger.error("[broker] SELL-CRYPTO order failed for %s: %s", base, error_msg)
        return {"ok": False, "error": error_msg, "raw": raw_result}

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


# ── Phase 3.2 (2026-05-01): single-importer wrappers ───────────────────
#
# Every robin_stocks call lives in this file and ``trading/venue/*``.
# These wrappers exist so caller modules don't need to ``import
# robin_stocks`` directly — the CI guard in
# ``tests/test_no_raw_broker_sdk_imports.py`` enforces this. Future
# venues (different broker SDK, unit-test mocking, etc.) just need to
# replace the body of these functions.


def get_open_stock_orders() -> list[dict[str, Any]]:
    """All currently-open (non-terminal) equity orders for this account.

    Returns a list of raw Robinhood order dicts; callers should not
    assume schema beyond what they need (they typically dereference
    ``id``, ``state``, ``side``, ``type``, ``trigger``, ``stop_price``,
    ``price``, ``quantity``, ``instrument``).
    """
    if not _rh_available or not is_connected():
        return []
    try:
        import robin_stocks.robinhood as rh
        return rh.orders.get_all_open_stock_orders() or []
    except Exception as e:
        logger.warning("[broker] get_open_stock_orders failed: %s", e)
        return []


def get_instrument_by_url(instrument_url: str) -> dict[str, Any] | None:
    """Look up the instrument record (symbol, simple_name, etc.) for a
    Robinhood instrument URL. Used by reconciler / exit code that has
    only the URL and needs the ticker symbol."""
    if not _rh_available or not is_connected() or not instrument_url:
        return None
    try:
        import robin_stocks.robinhood as rh
        return rh.stocks.get_instrument_by_url(instrument_url) or None
    except Exception as e:
        logger.warning("[broker] get_instrument_by_url failed: %s", e)
        return None


def get_symbol_by_url(instrument_url: str) -> str | None:
    """Same as ``get_instrument_by_url`` but extracts just the symbol."""
    if not _rh_available or not is_connected() or not instrument_url:
        return None
    try:
        import robin_stocks.robinhood as rh
        return rh.stocks.get_symbol_by_url(instrument_url) or None
    except Exception as e:
        logger.warning("[broker] get_symbol_by_url failed: %s", e)
        return None


def get_market_hours(mic: str, date_iso: str) -> dict[str, Any] | None:
    """Robinhood market-hours lookup by MIC and ISO date (YYYY-MM-DD)."""
    if not _rh_available or not is_connected():
        return None
    try:
        import robin_stocks.robinhood as rh
        return rh.markets.get_market_hours(mic, date_iso) or None
    except Exception as e:
        logger.warning("[broker] get_market_hours(%s, %s) failed: %s", mic, date_iso, e)
        return None


def get_option_chains(symbol: str) -> dict[str, Any] | None:
    """Fetch the full option-chains record for *symbol*. Used by options
    synthesis. Returns the raw dict (callers handle missing keys)."""
    if not _rh_available or not is_connected() or not symbol:
        return None
    try:
        import robin_stocks.robinhood as rh
        return rh.options.get_chains(symbol.strip().upper())
    except Exception as e:
        logger.warning("[broker] get_option_chains(%s) failed: %s", symbol, e)
        return None


def get_stock_order_info(order_id: str) -> dict[str, Any] | None:
    """Fetch the current state record for a single stock order.

    Phase 4 (2026-05-01): introduced as the basis for post-placement
    verification — the previous "we placed it, must be working" assumption
    was wrong (Robinhood accepts API calls and cancels seconds later for
    some instruments). Callers poll this to detect post-acceptance
    rejection within a short window.
    """
    if not _rh_available or not is_connected() or not order_id:
        return None
    try:
        import robin_stocks.robinhood as rh
        return rh.orders.get_stock_order_info(order_id) or None
    except Exception as e:
        logger.warning("[broker] get_stock_order_info(%s) failed: %s", order_id, e)
        return None


def verify_order_landed(
    order_id: str,
    *,
    max_wait_s: float = 3.0,
    poll_interval_s: float = 0.5,
) -> tuple[str, str | None]:
    """Poll the broker until the order reaches a terminal-or-resting state.

    Returns ``(verdict, observed_state)`` where ``verdict`` is one of:

    * ``"resting"``   — broker confirmed (state in confirmed/queued/partially_filled/filled).
                        The order is live at the broker; safe to treat as success.
    * ``"rejected"``  — broker rejected/cancelled within the verify window.
                        Caller should NOT treat this as success regardless of
                        what the original place_*_order API call returned.
    * ``"unknown"``   — verify window expired before the state moved away from
                        ``unconfirmed`` / ``new`` / etc. Conservative callers
                        treat unknown as not-yet-success.

    Phase 4 (2026-05-01): the gap this closes is exactly the ELTX bug —
    place_stop_loss_sell_order returned an order_id with state=unconfirmed,
    we logged "successful", and the user saw a rejection in the Robinhood
    app because the broker cancelled within 250ms. ``verify_order_landed``
    sees that cancellation in time to surface it.
    """
    import time

    if not order_id:
        return ("unknown", None)
    deadline = time.time() + float(max_wait_s)
    observed = None
    while time.time() < deadline:
        info = get_stock_order_info(order_id) or {}
        observed = (info.get("state") or "").strip().lower() or None
        if observed in ("rejected", "cancelled", "failed"):
            return ("rejected", observed)
        if observed in ("confirmed", "queued", "partially_filled", "filled"):
            return ("resting", observed)
        # state likely "unconfirmed" or "new" — keep polling
        time.sleep(float(poll_interval_s))
    return ("unknown", observed)


_OPTION_ORDER_REJECTED_STATES = {"rejected", "cancelled", "canceled", "failed", "expired"}
_OPTION_ORDER_RESTING_STATES = {"confirmed", "queued", "partially_filled", "filled"}
_OPTION_ORDER_VERIFY_STATES = {"", "unknown", "unconfirmed", "new", "submitted", "pending"}
_OPTION_ORDER_FILL_KEYS = (
    "cumulative_quantity",
    "cumulative_filled_quantity",
    "filled_quantity",
    "processed_quantity",
    "quantity_filled",
    "filled_size",
)


def _option_order_filled_quantity(order: dict[str, Any]) -> float | None:
    for key in _OPTION_ORDER_FILL_KEYS:
        value = order.get(key)
        if value in (None, ""):
            continue
        try:
            qty = float(value)
        except (TypeError, ValueError):
            continue
        return max(0.0, qty)
    return None


def _verify_option_order_landed_detail(
    order_id: str,
    *,
    max_wait_s: float = 3.0,
    poll_interval_s: float = 0.5,
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Option-order equivalent of verify_order_landed.

    Robinhood option orders live behind get_option_order_info, not
    get_stock_order_info. Poll the option endpoint until the order is either
    resting/filled, explicitly rejected, terminal-with-fill evidence, or still
    ambiguous at timeout.
    """
    import time

    if not order_id:
        return ("unknown", None, None)
    deadline = time.time() + float(max_wait_s)
    observed = None
    observed_order = None
    while time.time() < deadline:
        info = get_option_order_by_id(order_id) or {}
        observed_order = info
        observed = (info.get("state") or info.get("status") or "").strip().lower() or None
        if observed in _OPTION_ORDER_REJECTED_STATES:
            filled_qty = _option_order_filled_quantity(info)
            if filled_qty is not None and filled_qty > 0:
                return ("executed", observed, info)
            return ("rejected", observed, info)
        if observed in _OPTION_ORDER_RESTING_STATES:
            return ("resting", observed, info)
        time.sleep(float(poll_interval_s))
    return ("unknown", observed, observed_order)


def verify_option_order_landed(
    order_id: str,
    *,
    max_wait_s: float = 3.0,
    poll_interval_s: float = 0.5,
) -> tuple[str, str | None]:
    verdict, observed, _observed_order = _verify_option_order_landed_detail(
        order_id,
        max_wait_s=max_wait_s,
        poll_interval_s=poll_interval_s,
    )
    return verdict, observed


def _verify_submitted_option_order(
    result: dict[str, Any],
    *,
    order_id: str,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state = str(result.get("state") or result.get("status") or "").strip().lower()
    if state in _OPTION_ORDER_REJECTED_STATES:
        filled_qty = _option_order_filled_quantity(result)
        if filled_qty is not None and filled_qty > 0:
            return result, None
        verdict, observed, observed_order = _verify_option_order_landed_detail(order_id)
        if verdict == "executed" and observed:
            updated = dict(result)
            if isinstance(observed_order, dict):
                updated.update({k: v for k, v in observed_order.items() if v is not None})
            updated["state"] = observed
            return updated, None
        return result, {
            "ok": False,
            "error": f"option_order_{state}",
            "order_id": order_id,
            "state": state,
            "raw": result,
        }
    if state not in _OPTION_ORDER_VERIFY_STATES:
        return result, None

    verdict, observed, observed_order = _verify_option_order_landed_detail(order_id)
    if verdict == "rejected":
        logger.error(
            "[broker] %s post-submit rejected order_id=%s observed_state=%s",
            label, order_id, observed,
        )
        return result, {
            "ok": False,
            "error": f"option_order_{observed or 'rejected'}",
            "order_id": order_id,
            "state": observed or state or "rejected",
            "raw": result,
        }
    if verdict in ("resting", "executed") and observed:
        updated = dict(result)
        if isinstance(observed_order, dict):
            updated.update({k: v for k, v in observed_order.items() if v is not None})
        updated["state"] = observed
        return updated, None
    return result, None


# ── Options order placement (Task MM) ──────────────────────────────────
#
# Robinhood options live at api.robinhood.com/options/ — same equity-scope
# OAuth token as stocks (NOT a separate scope like crypto's nummus). If the
# RH session is connected for stocks, options endpoints work too, modulo
# account-level options approval (Level 2 buy / Level 3 spreads / etc).
# Approval is operator-side; we surface broker rejections cleanly via the
# existing audit row plumbing, same as KK did for crypto-not-supported.
#
# robin_stocks options API used here. NOTE: order placement lives in
# rh.orders (same as equity + crypto orders), while discovery and quotes
# live in rh.options. Don't confuse them — discovered the hard way during
# Phase 1 paper smoke when the first attempt looked for order placement
# in rh.options and got AttributeError.
#   rh.orders.order_buy_option_limit(...)    — single-leg long buy
#   rh.orders.order_sell_option_limit(...)   — single-leg long sell
#   rh.orders.cancel_option_order(order_id)  — kill an open option order
#   rh.options.find_options_by_expiration_and_strike(...) — locate contract
#   rh.options.get_option_market_data_by_id(option_id) — quote + IV + greeks
#   rh.options.get_open_option_positions(...) — held legs
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


def _normalize_option_order_envelope(
    result: dict[str, Any],
    *,
    action: str,
    underlying: str,
    expiration: str,
    strike: float,
    option_type: str,
    quantity: int,
    limit_price: float,
    position_effect: str,
) -> dict[str, Any]:
    state = str(result.get("state") or result.get("status") or "unknown").strip().lower()
    out: dict[str, Any] = {
        "ok": True,
        "order_id": result.get("id") or "",
        "state": state,
        "status": state,
        "raw": result,
        "side": action,
        "position_effect": position_effect,
        "underlying": underlying,
        "expiration": expiration,
        "strike": float(strike),
        "option_type": option_type,
        "quantity": int(quantity),
        "base_size": int(quantity),
        "limit_price": float(limit_price),
    }
    for key in ("average_price", "avg_price", "average_fill_price", "price"):
        if result.get(key) is not None:
            out[key] = result.get(key)
    return out


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
    submitted_limit = normalize_price(limit_price, sym, asset_class="option")

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
            return rh.orders.order_buy_option_limit(
                positionEffect="open",
                creditOrDebit="debit",  # buying-to-open is always a debit
                # Phase 1 (2026-05-01): OPRA tier alignment.
                # Premium ≥ $3 → penny tick; < $3 → nickel ($0.05) tick.
                price=submitted_limit,
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
            result, post_submit_reject = _verify_submitted_option_order(
                result,
                order_id=str(order_id),
                label=f"BUY-OPT {sym} {expiration} {strike}{side}",
            )
            if post_submit_reject is not None:
                return post_submit_reject
            state = result.get("state", state)
            logger.info(
                "[broker] BUY-OPT order placed: %s %s %s%s qty=%d limit=%.2f -> %s",
                sym, expiration, strike, side, quantity, submitted_limit, state,
            )
            return _normalize_option_order_envelope(
                result,
                action="buy",
                underlying=sym,
                expiration=expiration,
                strike=float(strike),
                option_type=side,
                quantity=int(quantity),
                limit_price=float(submitted_limit),
                position_effect="open",
            )

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
    submitted_limit = normalize_price(limit_price, sym, asset_class="option")

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
            return rh.orders.order_sell_option_limit(
                positionEffect=pe,
                creditOrDebit=cod,
                # Phase 1 (2026-05-01): OPRA tier alignment.
                price=submitted_limit,
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
            result, post_submit_reject = _verify_submitted_option_order(
                result,
                order_id=str(order_id),
                label=f"SELL-OPT {sym} {expiration} {strike}{side}",
            )
            if post_submit_reject is not None:
                return post_submit_reject
            state = result.get("state", state)
            logger.info(
                "[broker] SELL-OPT order placed: %s %s %s%s qty=%d limit=%.2f effect=%s -> %s",
                sym, expiration, strike, side, quantity, submitted_limit, pe, state,
            )
            return _normalize_option_order_envelope(
                result,
                action="sell",
                underlying=sym,
                expiration=expiration,
                strike=float(strike),
                option_type=side,
                quantity=int(quantity),
                limit_price=float(submitted_limit),
                position_effect=pe,
            )

        error_msg = str(result) if result else "Empty response from Robinhood options"
        logger.error("[broker] SELL-OPT order failed for %s: %s", sym, error_msg)
        return {"ok": False, "error": error_msg}

    except Exception as e:
        logger.error("[broker] SELL-OPT exception for %s: %s", sym, e, exc_info=True)
        return {"ok": False, "error": str(e)}


def place_option_spread(
    legs: list[dict[str, Any]],
    underlying: str,
    quantity: int,
    limit_price: float,
    *,
    direction: str = "debit",
    time_in_force: str = "gtc",
) -> dict[str, Any]:
    """Place a multi-leg option spread via Robinhood (Phase 4).

    Wraps ``rh.orders.order_option_debit_spread`` / ``order_option_credit_spread``
    so the autotrader can submit verticals, iron condors, etc. as a
    single atomic order rather than orchestrating individual legs.

    Each ``legs`` entry is a dict with keys:
      expiration:   ISO YYYY-MM-DD
      strike:       float
      option_type:  'call' or 'put'
      action:       'buy' or 'sell'
      effect:       'open' or 'close'  (defaults to 'open' if missing)

    ``direction`` is 'debit' (you pay net) or 'credit' (you receive net).
    The autotrader's strategies map vertical bull-call / iron-condor /
    etc. into the right combination of legs + direction at the strategy
    layer; the adapter just submits whatever the caller assembled.

    Returns the same envelope shape as ``place_option_buy_order``.
    """
    if not _rh_available:
        return {"ok": False, "error": "robin_stocks not installed"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Robinhood"}

    sym = (underlying or "").strip().upper()
    if not sym or quantity <= 0 or limit_price <= 0 or not legs:
        return {"ok": False, "error": f"bad inputs sym={underlying!r} qty={quantity} px={limit_price} legs={len(legs or [])}"}
    direction_l = (direction or "debit").strip().lower()
    if direction_l not in ("debit", "credit"):
        return {"ok": False, "error": f"bad direction: {direction!r}"}

    # Translate legs into RH's spread payload format.
    spread: list[dict[str, Any]] = []
    for leg in legs:
        try:
            spread.append({
                "expirationDate": str(leg["expiration"]),
                "strike": float(leg["strike"]),
                "optionType": str(leg["option_type"]).lower(),
                "effect": str(leg.get("effect", "open")).lower(),
                "ratio_quantity": int(leg.get("ratio_quantity", 1)),
                "side": "buy" if str(leg["action"]).lower() == "buy" else "sell",
            })
        except (KeyError, ValueError, TypeError) as e:
            return {"ok": False, "error": f"bad leg {e}: {leg!r}"}

    try:
        import robin_stocks.robinhood as rh

        def _do_spread():
            # Phase 1 (2026-05-01): spread net premium follows OPRA tick.
            # If individual legs are sub-$3, the spread net is too —
            # nickel tick. tick_normalizer handles the dispatch.
            spread_price = normalize_price(limit_price, sym, asset_class="option")
            if direction_l == "debit":
                return rh.orders.order_option_debit_spread(
                    price=spread_price,
                    symbol=sym,
                    quantity=int(quantity),
                    spread=spread,
                    timeInForce=time_in_force,
                    jsonify=True,
                )
            return rh.orders.order_option_credit_spread(
                price=spread_price,
                symbol=sym,
                quantity=int(quantity),
                spread=spread,
                timeInForce=time_in_force,
                jsonify=True,
            )

        label = f"{direction_l.upper()}-SPREAD {sym} legs={len(spread)}"
        result = _retry_api_call(_do_spread, label=label)

        if result and isinstance(result, dict):
            order_id = result.get("id") or ""
            state = result.get("state", "unknown")
            if not order_id:
                error_msg = (
                    result.get("detail")
                    or result.get("error")
                    or result.get("message")
                    or "Robinhood spread endpoint returned no order_id"
                )
                logger.error("[broker] %s rejected (no order_id): response=%s", label, result)
                return {"ok": False, "error": str(error_msg)[:500], "raw": result}
            result, post_submit_reject = _verify_submitted_option_order(
                result,
                order_id=str(order_id),
                label=label,
            )
            if post_submit_reject is not None:
                return post_submit_reject
            state = result.get("state", state)
            logger.info("[broker] %s placed: qty=%d limit=%.2f -> %s", label, quantity, limit_price, state)
            return {"ok": True, "order_id": order_id, "state": state, "raw": result}

        error_msg = str(result) if result else "Empty response from Robinhood spread"
        logger.error("[broker] %s failed: %s", label, error_msg)
        return {"ok": False, "error": error_msg}

    except Exception as e:
        logger.error("[broker] place_option_spread exception for %s: %s", sym, e, exc_info=True)
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
        result = rh.orders.cancel_option_order(order_id)
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


def get_option_order_by_id(order_id: str) -> dict[str, Any] | None:
    """Fetch a single option order from Robinhood by ID."""
    if not is_connected() or not order_id:
        return None
    try:
        import robin_stocks.robinhood as rh

        data = rh.orders.get_option_order_info(order_id)
        if data and isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug(f"[broker] get_option_order_by_id({order_id}) failed: {e}")
    return None


def _is_option_trade_for_order_sync(trade: Any) -> bool:
    try:
        from .trading.autopilot_scope import is_option_trade

        return bool(is_option_trade(trade))
    except Exception:
        return False


def _robinhood_order_lookup_for_trade(
    trade: Any,
    order_id: str | None,
) -> dict[str, Any] | None:
    if _is_option_trade_for_order_sync(trade):
        return get_option_order_by_id(str(order_id or ""))
    return get_order_by_id(str(order_id or ""))


def _is_robinhood_entry_order_sync_candidate(trade: Any) -> bool:
    source = str(getattr(trade, "broker_source", "") or "").strip().lower()
    return source in {"", "robinhood"}


def _first_present_float(mapping: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _robinhood_order_event_payload_for_trade(
    trade: Any,
    order: dict[str, Any],
    order_id: str | None,
) -> dict[str, Any]:
    """Normalize stock/option order fields for execution-event ingestion."""
    payload = dict(order or {})
    payload.setdefault("id", order_id)
    if not _is_option_trade_for_order_sync(trade):
        return payload

    state = str(payload.get("state") or payload.get("status") or "").strip().lower()
    requested = _first_present_float(payload, ("quantity", "requested_quantity"))
    if requested is None:
        requested = _safe_float(getattr(trade, "quantity", None))
    if requested > 0 and payload.get("quantity") is None:
        payload["quantity"] = requested

    cumulative = _first_present_float(
        payload,
        (
            "cumulative_quantity",
            "cumulative_filled_quantity",
            "filled_quantity",
            "processed_quantity",
            "quantity_filled",
            "filled_size",
        ),
    )
    if cumulative is None:
        cumulative = requested if state == "filled" and requested > 0 else 0.0
    payload["cumulative_quantity"] = cumulative

    average = _first_present_float(
        payload,
        ("average_price", "avg_price", "average_fill_price", "price", "limit_price"),
    )
    if average is None:
        average = _safe_float(getattr(trade, "entry_price", None)) or None
    if average is not None and payload.get("average_price") is None:
        payload["average_price"] = average
    return payload


def _preserve_option_partial_entry_fill(
    trade: Any,
    order_payload: dict[str, Any],
    cumulative: float,
) -> None:
    if not _is_option_trade_for_order_sync(trade) or cumulative <= 0:
        return
    requested = _first_present_float(order_payload, ("quantity", "requested_quantity"))
    if requested is None:
        requested = _safe_float(getattr(trade, "quantity", None)) or cumulative
    average = _first_present_float(
        order_payload,
        ("average_price", "avg_price", "average_fill_price", "price", "limit_price"),
    )
    if average is not None and average > 0:
        trade.avg_fill_price = average
        trade.entry_price = average
    trade.filled_quantity = cumulative
    if requested > cumulative + 1e-9:
        trade.quantity = cumulative
        trade.remaining_quantity = 0.0
        trade.broker_status = "partially_filled_cancelled"
        try:
            snap = dict(trade.indicator_snapshot) if isinstance(trade.indicator_snapshot, dict) else {}
            entry = dict(snap.get("entry_execution") or {})
            entry.update({
                "option_position_partial": True,
                "option_position_requested_quantity": requested,
                "option_position_quantity": cumulative,
                "option_position_remaining_quantity": 0.0,
                "option_position_residual_cancelled": True,
                "option_entry_cancel_reason": "partial_entry_cancelled_by_broker",
            })
            snap["entry_execution"] = entry
            trade.indicator_snapshot = snap
        except Exception:
            pass
    else:
        trade.remaining_quantity = 0.0


def _pending_exit_audit_prefix(pending_exit_reason: str | None) -> str:
    reason = str(pending_exit_reason or "").strip().lower()
    if reason == "desk_close_now":
        return "desk_close"
    if reason.startswith("emergency_"):
        return "emergency_exit"
    return "monitor_exit"


def sync_orders_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Reconcile local trades (with broker_order_id) against Robinhood.

    For each local trade that has a broker_order_id and is still in a
    non-terminal state, we look up the order on Robinhood and update
    local status, fill price, and timestamps accordingly.
    """
    from ..models.trading import Trade, StrategyProposal
    from .trading.decision_ledger import mark_linked_trade_packets_executed, mark_linked_trade_packets_terminal
    from .trading.execution_audit import normalize_robinhood_order_event, record_execution_event
    from .trading.robinhood_exit_execution import (
        reconcile_pending_exit_liveness,
        sync_pending_exit_order,
    )

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
            or_(Trade.broker_source == "robinhood", Trade.broker_source.is_(None)),
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
            rh_order = _robinhood_order_lookup_for_trade(trade, trade.broker_order_id)
            if not rh_order:
                errors += 1
                continue

            order_payload = _robinhood_order_event_payload_for_trade(
                trade,
                rh_order,
                trade.broker_order_id,
            )
            rh_state = (
                order_payload.get("state") or order_payload.get("status") or ""
            ).lower()
            now = datetime.utcnow()
            normalized = normalize_robinhood_order_event(
                order=order_payload,
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
                mark_linked_trade_packets_executed(
                    db,
                    trade_id=int(trade.id),
                    source="robinhood_order_sync",
                    broker_order_id=trade.broker_order_id,
                )
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
                    cum = float(order_payload.get("cumulative_quantity") or 0)
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
                    _preserve_option_partial_entry_fill(trade, order_payload, cum)
                    mark_linked_trade_packets_executed(
                        db,
                        trade_id=int(trade.id),
                        source="robinhood_order_sync_state_anomaly",
                        broker_order_id=trade.broker_order_id,
                    )
                    filled += 1
                else:
                    trade.status = "cancelled"
                    if not trade.exit_reason:
                        trade.exit_reason = f"broker_order_cancelled:{rh_state}"[:50]
                    mark_linked_trade_packets_terminal(
                        db,
                        trade_id=int(trade.id),
                        outcome_status="cancelled" if rh_state in ("cancelled", "canceled") else "rejected",
                        source="robinhood_order_sync",
                        reason_code=f"robinhood_order_{rh_state}",
                        reason_text=f"Robinhood order ended {rh_state} with no cumulative fill",
                        broker_order_id=trade.broker_order_id,
                    )
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
            audit_prefix = _pending_exit_audit_prefix(trade.pending_exit_reason)
            rh_order = _robinhood_order_lookup_for_trade(
                trade,
                trade.pending_exit_order_id,
            )
            if not rh_order:
                # The broker can't surface this exit order. Don't leak it as
                # 'queued' forever — the monitor skips has_active_pending_exit
                # rows, so a stranded mirror leaves the stop unmanaged.
                # Reconcile against position truth: close the envelope only if
                # the position is also gone; a live position self-heals next
                # tick (a transient lookup blip stays an 'error').
                live_out = reconcile_pending_exit_liveness(
                    db, trade, broker_order=None, audit_decision_prefix=audit_prefix,
                )
                if live_out.get("action") == "closed":
                    cancelled += 1
                    synced += 1
                else:
                    errors += 1
                continue
            sync_out = sync_pending_exit_order(
                db,
                trade,
                order={**rh_order, "id": trade.pending_exit_order_id},
                audit_decision_prefix=audit_prefix,
            )
            st = str(sync_out.get("state") or "").lower()
            if st == "filled":
                filled += 1
            elif st in ("cancelled", "canceled", "rejected", "failed", "expired"):
                cancelled += 1
            else:
                # Still resting (queued/confirmed/...). If it has leaked past a
                # regular-session open without routing, escalate: cancel + re-
                # submit under price protection so the stop stays live. No-op
                # for fresh, non-urgent, or off-hours resting orders.
                reconcile_pending_exit_liveness(
                    db,
                    trade,
                    broker_order={**rh_order, "id": trade.pending_exit_order_id},
                    audit_decision_prefix=audit_prefix,
                )
            synced += 1
        except Exception as e:
            logger.warning(f"[broker] Pending exit sync failed for {trade.ticker}: {e}")
            errors += 1

    # Reconcile "open" trades that have broker_order_id: if RH says cancelled, fix local
    for trade in open_with_order_id:
        try:
            if _is_option_trade_for_order_sync(trade):
                continue
            if not _is_robinhood_entry_order_sync_candidate(trade):
                continue
            rh_order = get_order_by_id(trade.broker_order_id)
            if not rh_order:
                continue
            rh_state = (rh_order.get("state") or "").lower()
            if rh_state == "filled" and (trade.broker_status or "").lower() == "filled":
                # The entry order is already reflected locally. Re-polling it
                # should not refresh ``last_broker_sync`` forever; position
                # sync owns the open-position liveness clock after fill.
                synced += 1
                continue
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
                    mark_linked_trade_packets_terminal(
                        db,
                        trade_id=int(trade.id),
                        outcome_status="cancelled" if rh_state in ("cancelled", "canceled") else "rejected",
                        source="robinhood_open_order_reconcile",
                        reason_code=f"robinhood_order_{rh_state}",
                        reason_text=f"Robinhood order reconcile saw {rh_state} with no cumulative fill",
                        broker_order_id=trade.broker_order_id,
                    )
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
