"""Alpaca equities VenueAdapter — the DMA-style limit-posting upgrade over Robinhood.

Robinhood routes via PFOF with no direct market access, so CHILI is forced to CROSS the
3.6%-median spreads of Ross low-float names (0 clean fills ever — project_momentum_zero_
fills_root_cause). Alpaca is API-first (built for bots), commission-free, has a FREE paper
sandbox, and its LIMIT orders route to the market and can REST on the book (the post-inside-
the-spread capability RH lacks). This adapter implements the venue ``VenueAdapter`` Protocol
so the momentum FSM (limit-entry #553, software stop, liquidity-bias #552, auto-arm) runs
through Alpaca unchanged — only the venue changes. (docs/DESIGN/ALPACA_LANE.md)

Paper-only: this adapter is disabled unless ``CHILI_ALPACA_PAPER`` is true. This prevents an
old paper-session identifier or cached client from ever being reused against the live account.
``alpaca-py`` is imported LAZILY so this module loads even before the SDK is installed
(``is_enabled`` returns False, every call returns a safe error envelope).
"""
from __future__ import annotations

import logging
import hashlib
import math
import re
import threading
import uuid
from dataclasses import replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime, timezone
from typing import Any, Optional

from ....config import settings
from .protocol import (
    FreshnessMeta,
    NormalizedFill,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
)

logger = logging.getLogger(__name__)

_VENUE = "alpaca"
_IQFEED_AUTHORITY_BASIS = "iqfeed_q_receive_trade_reference_fenced"
_IQFEED_AUTHORITY_MAX_AGE_S = 2.0
_IQFEED_FUTURE_TOLERANCE_S = 1.0
_IQFEED_BUILD_RE = re.compile(
    r"^iqfeed-l1-quote-provenance-v2\+sha256:[0-9a-f]{16}$"
)
# Alpaca order statuses -> the lowercase vocabulary the runner's _order_done_for_entry /
# _order_open helpers understand (#550/#551). Working states map to "open" so the fill
# poll keeps going; terminal states map to their canonical terminal words.
_STATUS_MAP = {
    "filled": "filled",
    "partially_filled": "open",          # still working toward full fill
    "new": "open",
    "accepted": "open",
    "pending_new": "open",
    "accepted_for_bidding": "open",
    # None of these rare states proves an executable resting order. ``held``
    # still awaits a condition, ``calculated`` is completed for the day while
    # settlement is pending, and ``suspended`` is explicitly ineligible for
    # trading.  Preserve the exact Alpaca value in ``raw.alpaca_status`` and
    # keep the normalized state unresolved so recovery must inspect broker fill,
    # successor, and position truth instead of certifying phantom protection.
    "held": "pending",
    "calculated": "pending",
    "stopped": "open",
    "suspended": "pending",
    "pending_cancel": "pending",
    "pending_replace": "pending",
    # ``replaced`` has a successor order and is not terminal exposure proof.
    "replaced": "pending",
    "canceled": "canceled",
    "cancelled": "canceled",
    "expired": "expired",
    # Dormant for the session, but Alpaca may update/reactivate it next day.
    "done_for_day": "pending",
    "rejected": "rejected",
}


def quantize_alpaca_equity_sell_stop_price(price: Any) -> str:
    """Return the exact Alpaca-valid tick for a protective equity sell stop.

    Alpaca accepts cents at/above $1 and four decimals below $1.  A protective
    sell stop is rounded *up* to the next valid tick: rounding down would loosen
    the frozen disaster floor and leave more loss than the risk decision allowed.
    The string return value is intentional so the durable owner request and the
    SDK request carry the same decimal generation without a binary-float rewrite.
    """
    try:
        value = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid Alpaca equity sell-stop price") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("invalid Alpaca equity sell-stop price")
    tick = Decimal("0.01") if value >= Decimal("1") else Decimal("0.0001")
    quantized = value.quantize(tick, rounding=ROUND_CEILING)
    if not quantized.is_finite() or quantized <= 0:
        raise ValueError("invalid quantized Alpaca equity sell-stop price")
    # A sub-dollar value may round exactly to $1.00.  Canonicalize that boundary
    # to the >=$1 two-decimal rule while retaining four decimals below it.
    if quantized >= Decimal("1"):
        quantized = quantized.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        return format(quantized, ".2f")
    return format(quantized, ".4f")


def quantize_alpaca_equity_limit_price(price: Any, side: Any) -> str:
    """Return the canonical marketable Alpaca equity limit-price string.

    Alpaca permits cents at/above $1 and four decimals below $1.  BUY limits
    round up and SELL limits round down so the adapter never makes an order less
    marketable while normalizing it.  A string is returned deliberately: the
    durable risk request and the adapter input must preserve one exact decimal
    generation instead of independently rewriting a binary float.
    """
    try:
        value = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid Alpaca equity limit price") from exc
    side_key = str(getattr(side, "value", side) or "").strip().lower()
    if side_key not in {"buy", "sell"}:
        raise ValueError("invalid Alpaca equity limit side")
    if not value.is_finite() or value <= 0:
        raise ValueError("invalid Alpaca equity limit price")
    tick = Decimal("0.01") if value >= Decimal("1") else Decimal("0.0001")
    rounding = ROUND_CEILING if side_key == "buy" else ROUND_FLOOR
    quantized = value.quantize(tick, rounding=rounding)
    if not quantized.is_finite() or quantized <= 0:
        raise ValueError("invalid quantized Alpaca equity limit price")
    # A sub-dollar BUY may round exactly to $1.00.  Once it crosses that
    # boundary, freeze the >=$1 canonical representation as two decimals.
    if quantized >= Decimal("1"):
        quantized = quantized.quantize(Decimal("0.01"), rounding=rounding)
        return format(quantized, ".2f")
    return format(quantized, ".4f")


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _opt_bool(v: Any) -> Optional[bool]:
    """None-preserving bool coercion. Returns None when the field is absent so a
    missing short signal fails CLOSED at the gate (not silently treated as False)."""
    if v is None:
        return None
    return bool(v)


def _norm_status(raw: Any) -> str:
    s = getattr(raw, "value", raw)
    s = str(s or "").strip().lower()
    return _STATUS_MAP.get(s, s or "unknown")


def _submit_failure_metadata(exc: Exception) -> dict[str, Any]:
    """Classify a failed Alpaca submit without guessing that no order exists.

    A transport exception can happen *after* Alpaca accepted the deterministic
    ``client_order_id``.  Only an explicit 4xx broker response (other than request
    timeout) proves a rejection.  Everything else is indeterminate and must be
    reconciled by client id before the runner may terminalize or submit again.
    """

    def _status_from(value: Any) -> int | None:
        try:
            status = int(value)
        except (TypeError, ValueError):
            return None
        return status if 100 <= status <= 599 else None

    status: int | None = None
    candidates = [
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
    ]
    response = getattr(exc, "response", None)
    if response is not None:
        candidates.extend(
            [
                getattr(response, "status_code", None),
                getattr(response, "status", None),
            ]
        )
    http_error = getattr(exc, "_http_error", None)
    http_response = getattr(http_error, "response", None)
    if http_response is not None:
        candidates.extend(
            [
                getattr(http_response, "status_code", None),
                getattr(http_response, "status", None),
            ]
        )
    for candidate in candidates:
        status = _status_from(candidate)
        if status is not None:
            break

    # 408 is explicitly ambiguous.  5xx and response-less SDK/transport failures
    # are also indeterminate: the server may have committed the order before the
    # response path failed.  A non-timeout 4xx is an explicit broker refusal.
    message = str(exc or "").lower()
    duplicate_client_id = bool(
        "client_order_id" in message
        and ("unique" in message or "duplicate" in message or "already" in message)
    )
    definitive_reject = bool(
        status is not None
        and 400 <= status < 500
        and status != 408
        and not duplicate_client_id
    )
    return {
        "submit_outcome": (
            "broker_rejected" if definitive_reject else "indeterminate"
        ),
        "error_type": type(exc).__name__,
        "http_status": status,
    }


def _is_crypto_pid(product_id: str) -> bool:
    """Reject dash- and slash-form crypto at the equity-only order seam."""
    pid = str(product_id or "").strip().upper()
    return "/" in pid or pid.endswith("-USD")


def _is_crypto_asset_class(asset_class: Any) -> bool:
    raw = getattr(asset_class, "value", asset_class)
    return "crypto" in str(raw or "").strip().lower()


def _to_symbol(product_id: str) -> str:
    """Equity product_id is the bare ticker (AAPL); Alpaca uses the same. The
    lane's crypto pairs are dash-form (BTC-USD) — Alpaca's crypto API wants the
    slash form (BTC/USD)."""
    pid = str(product_id or "").strip().upper()
    if pid.endswith("-USD"):
        return pid[:-4] + "/USD"
    return pid


def _from_alpaca_symbol(sym: str) -> str:
    """Normalize an Alpaca order/asset symbol back to the lane's product_id:
    crypto BTC/USD -> BTC-USD; equities unchanged."""
    s2 = str(sym or "").strip().upper()
    return s2.replace("/", "-") if "/" in s2 else s2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fresh(seconds: float | None = None) -> FreshnessMeta:
    max_age = float(seconds if seconds is not None
                    else getattr(settings, "chili_alpaca_quote_max_age_seconds", 60.0) or 60.0)
    return FreshnessMeta(retrieved_at_utc=_now(), max_age_seconds=max_age)


# ── lazy SDK clients (cached) ─────────────────────────────────────────────────
_clients: dict[str, Any] = {}
_clients_lock = threading.RLock()


def _keys() -> tuple[str, str]:
    """Return paper credentials; live posture deliberately has no usable keys."""
    if not _paper():
        # This adapter is deliberately paper-only.  Returning no credentials is
        # the final boundary for any caller that missed the higher-level frozen-
        # account-scope quarantine.
        return ("", "")
    return (
        str(getattr(settings, "chili_alpaca_api_key", "") or ""),
        str(getattr(settings, "chili_alpaca_api_secret", "") or ""),
    )


def _paper() -> bool:
    return bool(getattr(settings, "chili_alpaca_paper", True))


def _require_paper_posture() -> None:
    if not _paper():
        raise RuntimeError("Alpaca adapter is paper-only; live posture is quarantined")


def _data_feed():
    """The DataFeed enum value (iex free / sip paid). Falls back to a plain string."""
    want = str(getattr(settings, "chili_alpaca_data_feed", "iex") or "iex").strip().lower()
    try:
        from alpaca.data.enums import DataFeed
        return DataFeed.SIP if want == "sip" else DataFeed.IEX
    except Exception:
        return want


def _expected_account_id() -> str:
    return str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip()


def _raw_trading_client():
    _require_paper_posture()
    key, secret = _keys()
    fingerprint = hashlib.sha256(
        f"paper\0{key}\0{secret}".encode("utf-8")
    ).hexdigest()
    with _clients_lock:
        if (
            "trading:paper" not in _clients
            or _clients.get("trading:fingerprint") != fingerprint
        ):
            from alpaca.trading.client import TradingClient
            client = TradingClient(key, secret, paper=True)
            account = client.get_account()
            observed = str(getattr(account, "id", "") or "").strip()
            if not observed:
                raise RuntimeError("Alpaca account identity is unavailable")
            _clients["trading:paper"] = client
            _clients["trading:fingerprint"] = fingerprint
            _clients["trading:observed_account_id"] = observed
        return _clients["trading:paper"]


def _trading_client():
    """Return a client only after a fresh stable account-UUID pin match.

    Every order/position/account/clock operation routes through this seam. Thus a
    credential swap cannot turn a wrong-account 404/flat read into lifecycle proof,
    even in legacy reapers that do not yet carry session metadata explicitly.
    """
    # Preserve the strongest posture boundary even when the account pin is also
    # absent.  A live-posture process must fail specifically at the paper-only
    # guard before consulting credentials, cache generations, or account identity.
    _require_paper_posture()
    expected = _expected_account_id()
    if not expected:
        raise RuntimeError("CHILI_ALPACA_EXPECTED_ACCOUNT_ID is required")
    with _clients_lock:
        client = _raw_trading_client()
        observed = str(_clients.get("trading:observed_account_id") or "").strip()
        if not observed or observed != expected:
            raise RuntimeError("Alpaca account identity does not match configured pin")
        return client


def _data_client():
    _require_paper_posture()
    if "data:paper" not in _clients:
        from alpaca.data.historical import StockHistoricalDataClient
        key, secret = _keys()
        _clients["data:paper"] = StockHistoricalDataClient(key, secret)
    return _clients["data:paper"]


def _crypto_data_client():
    _require_paper_posture()
    if "crypto_data:paper" not in _clients:
        from alpaca.data.historical import CryptoHistoricalDataClient
        key, secret = _keys()
        _clients["crypto_data:paper"] = CryptoHistoricalDataClient(key, secret)
    return _clients["crypto_data:paper"]


def reset_clients_for_tests() -> None:
    with _clients_lock:
        _clients.clear()
        _LISTED_CACHE.clear()


# Per-process listing cache (listings change rarely; a probe is one HTTP call).
_LISTED_CACHE: dict[str, bool] = {}


def alpaca_lists_symbol(product_id: str) -> bool:
    """True when Alpaca has a TRADABLE asset for this lane symbol (equity ticker or
    crypto BASE-USD -> BASE/USD). Cached per process. FAIL-CLOSED (False) on any probe
    error — callers route the symbol to its default venue instead. Used by the
    crypto->alpaca-paper router: only Alpaca-LISTED majors go to the paper account;
    unlisted low-cap alts stay on their default (and the arm-side guard skips them
    while the paper posture is on)."""
    sym = str(product_id or "").strip().upper()
    if not sym:
        return False
    if sym in _LISTED_CACHE:
        return _LISTED_CACHE[sym]
    listed = False
    try:
        prod, _ = AlpacaSpotAdapter().get_product(sym)
        listed = prod is not None and not bool(getattr(prod, "trading_disabled", True))
    except Exception:
        listed = False
    _LISTED_CACHE[sym] = listed
    return listed


class AlpacaSpotAdapter:
    """Paper-only VenueAdapter for Alpaca US equities."""

    def __init__(self) -> None:
        self._bound_account_id: str | None = None

    def bind_account_id(self, account_id: str) -> bool:
        """Freeze this adapter instance to one session/account generation."""
        frozen = str(account_id or "").strip()
        if not frozen or frozen != _expected_account_id():
            return False
        if self._bound_account_id not in (None, frozen):
            return False
        self._bound_account_id = frozen
        return True

    def _account_client(self):
        """Return a client only when its credential generation matches the session."""
        client = _trading_client()
        if self._bound_account_id is None:
            return client
        with _clients_lock:
            observed = (
                str(_clients.get("trading:observed_account_id") or "").strip()
                if _clients.get("trading:paper") is client
                else ""
            )
        if observed != self._bound_account_id:
            raise RuntimeError("Alpaca adapter account generation changed")
        return client

    # ── availability ─────────────────────────────────────────────────────────
    def is_enabled(self) -> bool:
        if not bool(getattr(settings, "chili_alpaca_enabled", False)):
            return False
        if not _paper():
            logger.error("[alpaca_spot] live posture quarantined; adapter is paper-only")
            return False
        key, secret = _keys()
        if not key or not secret or not _expected_account_id():
            return False
        try:
            import alpaca  # noqa: F401
            return True
        except Exception:
            logger.warning("[alpaca_spot] alpaca-py not installed — adapter disabled")
            return False

    # ── market data ──────────────────────────────────────────────────────────
    def _iqfeed_l1_quote(self, sym: str, *, max_age_seconds: float | None = None):
        """Return one exact-build v2 IQFeed BBO or fail into direct Alpaca.

        Most-Recent-Trade-Time is only a causal containment reference, never a
        quote-event timestamp. Both that reference and the local receive clock must
        independently be fresh and chronologically possible. Legacy v1 rows, replayed
        receive-time rows, and unpinned bridge builds are non-authoritative.
        """
        try:
            from ....db import SessionLocal
            from sqlalchemy import text

            expected_build = str(
                getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "")
                or ""
            ).strip()
            if _IQFEED_BUILD_RE.fullmatch(expected_build) is None:
                return None
            requested_max_age = float(
                max_age_seconds
                if max_age_seconds is not None
                else (getattr(settings, "chili_alpaca_quote_max_age_seconds", 60.0) or 60.0)
            )
            if requested_max_age <= 0:
                return None
            max_age = min(requested_max_age, _IQFEED_AUTHORITY_MAX_AGE_S)
            with SessionLocal() as _db:
                row = _db.execute(text(
                    "SELECT id, bid, ask, mid, spread_bps, observed_at, source, "
                    "provider_event_at, received_at, timestamp_basis, bridge_version, "
                    "provider_trade_reference_at, message_type, bridge_run_id, "
                    "connection_generation "
                    "FROM momentum_nbbo_spread_tape "
                    "WHERE symbol = :s AND source = 'iqfeed_l1' AND mid > 0 "
                    "AND received_at IS NOT NULL "
                    # observed_at is the provider trade reference for v2 rows. Keep
                    # the read on the existing large-tape index; migration 317 adds
                    # only nullable metadata and never indexes/backfills 54M rows.
                    "ORDER BY observed_at DESC, id DESC LIMIT 1"
                ), {"s": str(sym or "").upper()}).fetchone()
            if row is None:
                return None
            tape_row_id = int(row[0])
            bid = _f(row[1]); ask = _f(row[2]); mid = _f(row[3])
            if (
                bid is None
                or ask is None
                or mid is None
                or not all(math.isfinite(value) for value in (bid, ask, mid))
                or bid <= 0
                or ask <= 0
                or mid <= 0
                or ask < bid
            ):
                return None
            provider_at = row[7]
            received_at = row[8]
            timestamp_basis = str(row[9] or "")
            bridge_version = str(row[10] or "")
            provider_trade_reference_at = row[11]
            message_type = str(row[12] or "")
            bridge_run_id = str(row[13] or "")
            connection_generation = row[14]

            def _aware_utc(value, *, allow_naive: bool = False):
                if not isinstance(value, datetime):
                    return None
                if value.tzinfo is None:
                    if not allow_naive:
                        return None
                    value = value.replace(tzinfo=timezone.utc)
                offset = value.utcoffset()
                if offset is None or offset != timezone.utc.utcoffset(value):
                    return None
                return value.astimezone(timezone.utc)

            # Exact v2 provenance tuple. provider_event_at must remain NULL: the
            # default IQFeed frame has no quote-event clock.
            if provider_at is not None:
                return None
            _received_at = _aware_utc(received_at)
            _reference_at = _aware_utc(provider_trade_reference_at)
            _observed_at = _aware_utc(row[5], allow_naive=True)
            if _received_at is None or _reference_at is None or _observed_at is None:
                return None
            if str(row[6] or "") != "iqfeed_l1":
                return None
            if timestamp_basis != _IQFEED_AUTHORITY_BASIS:
                return None
            if bridge_version != expected_build or message_type != "Q":
                return None
            try:
                if str(uuid.UUID(bridge_run_id)) != bridge_run_id:
                    return None
            except (ValueError, AttributeError):
                return None
            if (
                isinstance(connection_generation, bool)
                or not isinstance(connection_generation, int)
                or connection_generation <= 0
            ):
                return None
            if abs((_observed_at - _reference_at).total_seconds()) > 0.001:
                return None
            receive_reference_delta = (
                _received_at - _reference_at
            ).total_seconds()
            if not (
                -_IQFEED_FUTURE_TOLERANCE_S
                <= receive_reference_delta
                <= _IQFEED_AUTHORITY_MAX_AGE_S
            ):
                return None
            now_utc = _now()
            received_age = (now_utc - _received_at).total_seconds()
            reference_age = (now_utc - _reference_at).total_seconds()
            if (
                received_age < -_IQFEED_FUTURE_TOLERANCE_S
                or reference_age < -_IQFEED_FUTURE_TOLERANCE_S
                or received_age > max_age
                or reference_age > max_age
            ):
                return None
            spread_bps = _f(row[4])
            if spread_bps is None and ask >= bid:
                spread_bps = (ask - bid) / mid * 10_000.0
            meta = FreshnessMeta(
                retrieved_at_utc=_received_at,
                # The trade reference is not a provider quote timestamp.
                provider_time_utc=None,
                max_age_seconds=max_age,
            )
            return NormalizedTicker(
                product_id=sym, bid=bid, ask=ask, mid=mid, spread_bps=spread_bps,
                bid_size=None,
                ask_size=None,
                freshness=meta,
                raw={
                    "feed": str(row[6] or "iqfeed_l1"),
                    "tape_row_id": tape_row_id,
                    "legacy_observed_at_utc": (
                        _observed_at.isoformat()
                    ),
                    "received_at_utc": _received_at.isoformat(),
                    "provider_event_at_utc": None,
                    "provider_trade_reference_at_utc": _reference_at.isoformat(),
                    "timestamp_basis": timestamp_basis,
                    "bridge_version": bridge_version,
                    "message_type": message_type,
                    "bridge_run_id": bridge_run_id,
                    "connection_generation": connection_generation,
                },
            ), meta
        except Exception as exc:
            logger.debug("[alpaca_spot] _iqfeed_l1_quote(%s) failed: %s", sym, exc)
            return None

    def _alpaca_latest_quote(self, product_id: str):
        """Direct Alpaca quote with the provider timestamp preserved."""
        sym = _to_symbol(product_id)
        try:
            if _is_crypto_pid(product_id):
                from alpaca.data.requests import CryptoLatestQuoteRequest

                req = CryptoLatestQuoteRequest(symbol_or_symbols=sym)
                q = _crypto_data_client().get_crypto_latest_quote(req).get(sym)
            else:
                from alpaca.data.requests import StockLatestQuoteRequest

                req = StockLatestQuoteRequest(symbol_or_symbols=sym, feed=_data_feed())
                q = _data_client().get_stock_latest_quote(req).get(sym)
            if q is None:
                return None, _fresh()
            bid = _f(getattr(q, "bid_price", None))
            ask = _f(getattr(q, "ask_price", None))
            if not (bid and ask and bid > 0 and ask >= bid):
                return None, _fresh()
            mid = (bid + ask) / 2.0
            spread_bps = (ask - bid) / mid * 10_000.0
            ts = getattr(q, "timestamp", None)
            retrieved = _now()
            provider_ts = ts if isinstance(ts, datetime) else None
            meta = FreshnessMeta(
                retrieved_at_utc=retrieved,
                provider_time_utc=provider_ts,
                max_age_seconds=float(
                    getattr(settings, "chili_alpaca_quote_max_age_seconds", 60.0) or 60.0
                ),
            )
            return NormalizedTicker(
                product_id=sym,
                bid=bid,
                ask=ask,
                mid=mid,
                spread_bps=spread_bps,
                bid_size=_f(getattr(q, "bid_size", None)),
                ask_size=_f(getattr(q, "ask_size", None)),
                freshness=meta,
                raw={
                    "feed": str(_data_feed()),
                    "provider_event_at_utc": (
                        provider_ts.isoformat() if provider_ts is not None else None
                    ),
                    "received_at_utc": retrieved.isoformat(),
                    "timestamp_basis": (
                        "provider_event_at" if provider_ts is not None else "request_received_at"
                    ),
                },
            ), meta
        except Exception as exc:
            logger.debug("[alpaca_spot] direct latest quote(%s) failed: %s", sym, exc)
            return None, _fresh()

    def get_execution_bbo(self, product_id: str, *, max_age_seconds: float = 2.0):
        """Authoritative pre-submit BBO.

        IQFeed is accepted only when its persisted row is explicitly sourced and
        inside the execution-age bound. Otherwise make a fresh Alpaca data request;
        never reuse the lane's older decision quote at the broker boundary.
        """
        sym = _to_symbol(product_id)
        if not _is_crypto_pid(product_id) and bool(
            getattr(settings, "chili_alpaca_quotes_via_iqfeed", True)
        ):
            iq = self._iqfeed_l1_quote(sym, max_age_seconds=max_age_seconds)
            if iq is not None:
                return iq
        direct = self._alpaca_latest_quote(product_id)
        if not isinstance(direct, tuple) or len(direct) != 2:
            return None, _fresh(max_age_seconds)
        tick, meta = direct
        if tick is None or not isinstance(meta, FreshnessMeta):
            return None, meta
        provider_at = meta.provider_time_utc
        if not isinstance(provider_at, datetime):
            # Request completion only proves when we received the response, not
            # when Alpaca's cached quote was generated.
            return None, meta
        if provider_at.tzinfo is None:
            provider_at = provider_at.replace(tzinfo=timezone.utc)
        else:
            provider_at = provider_at.astimezone(timezone.utc)
        now_utc = _now()
        provider_age = (now_utc - provider_at).total_seconds()
        if provider_age < -1.0 or provider_age > float(max_age_seconds):
            return None, meta
        execution_meta = FreshnessMeta(
            retrieved_at_utc=meta.retrieved_at_utc,
            provider_time_utc=provider_at,
            max_age_seconds=float(max_age_seconds),
        )
        return replace(tick, freshness=execution_meta), execution_meta

    def get_best_bid_ask(self, product_id: str):
        sym = _to_symbol(product_id)
        # DATA/EXECUTION DECOUPLING (2026-07-07): Alpaca is EXECUTION-only. Alpaca-IEX quotes have
        # thin small-cap coverage — the dormancy root cause (stale_bbo/no_bbo on Ross low-float
        # names since 06-18). Prefer IQFeed L1 (momentum_nbbo_spread_tape, same feed the live lane
        # uses, ~0.26s fresh); fall back to Alpaca-IEX only on a miss. Kill-switch
        # chili_alpaca_quotes_via_iqfeed (default True). Equities only. See ALPACA_PAPER_ENABLE_PLAN.md.
        if not _is_crypto_pid(product_id) and bool(
            getattr(settings, "chili_alpaca_quotes_via_iqfeed", True)
        ):
            _iq = self._iqfeed_l1_quote(sym)
            if _iq is not None:
                return _iq
        return self._alpaca_latest_quote(product_id)

    def get_ticker(self, product_id: str):
        return self.get_best_bid_ask(product_id)

    def get_recent_trades(self, product_id: str, *, limit: int = 50):
        sym = _to_symbol(product_id)
        try:
            if _is_crypto_pid(product_id):
                from alpaca.data.requests import CryptoLatestTradeRequest
                t = _crypto_data_client().get_crypto_latest_trade(
                    CryptoLatestTradeRequest(symbol_or_symbols=sym)
                ).get(sym)
            else:
                from alpaca.data.requests import StockLatestTradeRequest
                t = _data_client().get_stock_latest_trade(
                    StockLatestTradeRequest(symbol_or_symbols=sym, feed=_data_feed())
                ).get(sym)
            if t is None:
                return [], _fresh()
            return [{"price": _f(getattr(t, "price", None)), "size": _f(getattr(t, "size", None)),
                     "time": str(getattr(t, "timestamp", ""))}], _fresh()
        except Exception as exc:
            logger.debug("[alpaca_spot] get_recent_trades(%s) failed: %s", sym, exc)
            return [], _fresh()

    # ── products / assets ────────────────────────────────────────────────────
    def get_product(self, product_id: str):
        sym = _to_symbol(product_id)
        try:
            a = self._account_client().get_asset(sym)
            tradable = bool(getattr(a, "tradable", False))
            status = str(getattr(getattr(a, "status", None), "value", getattr(a, "status", "")) or "").lower()
            fractionable = bool(getattr(a, "fractionable", False))
            # This governed lane requires a full-position GTC disaster stop.
            # Alpaca fractional stops are DAY-only, so strategy sizing is whole-
            # share even when the underlying account supports fractions.
            base_inc = 1.0
            min_sz = 1.0
            price_inc = _f(getattr(a, "price_increment", None)) or 0.01
            prod = NormalizedProduct(
                product_id=sym, base_currency=sym, quote_currency="USD",
                status=status or ("active" if tradable else "inactive"),
                trading_disabled=not tradable, cancel_only=False, limit_only=False,
                post_only=False, auction_mode=False,
                base_min_size=min_sz, base_increment=base_inc, price_increment=price_inc,
                product_type="crypto" if _is_crypto_pid(product_id) else "equity",
                raw={
                    "fractionable": fractionable,
                    "exchange": str(getattr(a, "exchange", "")),
                    # Short-lane locate-feasibility surfacing (SHORT_SIDE_LANE.md P0).
                    # Asset-level borrow signals so the short-entry gate can fail-closed
                    # on a not-shortable / hard-to-borrow name. (None when the SDK/asset
                    # doesn't expose them — fail-closed at the gate, not here.)
                    "shortable": _opt_bool(getattr(a, "shortable", None)),
                    "easy_to_borrow": _opt_bool(getattr(a, "easy_to_borrow", None)),
                },
            )
            return prod, _fresh(3600.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] get_product(%s) failed: %s", sym, exc)
            return None, _fresh(3600.0)

    def get_products(self):
        try:
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass, AssetStatus
            assets = self._account_client().get_all_assets(
                GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
            )
            out = []
            for a in assets or []:
                if not bool(getattr(a, "tradable", False)):
                    continue
                sym = _from_alpaca_symbol(getattr(a, "symbol", ""))
                if not sym:
                    continue
                out.append(NormalizedProduct(
                    product_id=sym, base_currency=sym, quote_currency="USD", status="active",
                    trading_disabled=False, cancel_only=False, limit_only=False, post_only=False,
                    auction_mode=False, base_min_size=1.0, base_increment=1.0,
                    price_increment=0.01, product_type="equity", raw={},
                ))
            return out, _fresh(3600.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] get_products failed: %s", exc)
            return [], _fresh(3600.0)

    # ── orders ───────────────────────────────────────────────────────────────
    def _normalize_order(self, o: Any) -> NormalizedOrder:
        # Preserve broker chronology verbatim enough to round-trip the exact
        # instant. Orphan accounting refuses to invent a fill timestamp when
        # Alpaca does not return one.
        filled_at = getattr(o, "filled_at", None)
        submitted_at = getattr(o, "submitted_at", None)
        time_in_force = getattr(o, "time_in_force", None)
        extended_hours = getattr(o, "extended_hours", None)
        position_intent = getattr(o, "position_intent", None)
        return NormalizedOrder(
            order_id=str(getattr(o, "id", "") or ""),
            client_order_id=getattr(o, "client_order_id", None),
            product_id=_from_alpaca_symbol(getattr(o, "symbol", "")),
            side=str(getattr(getattr(o, "side", None), "value", getattr(o, "side", "")) or "").lower(),
            status=_norm_status(getattr(o, "status", None)),
            order_type=str(getattr(getattr(o, "order_type", None), "value",
                                   getattr(o, "type", "") or getattr(o, "order_type", "")) or "").lower(),
            filled_size=_f(getattr(o, "filled_qty", None)) or 0.0,
            average_filled_price=_f(getattr(o, "filled_avg_price", None)),
            created_time=str(getattr(o, "created_at", "") or ""),
            raw={
                "alpaca_status": str(
                    getattr(getattr(o, "status", None), "value", getattr(o, "status", ""))
                ),
                "filled_at": str(filled_at) if filled_at is not None else None,
                "submitted_at": str(submitted_at) if submitted_at is not None else None,
                "qty": _f(getattr(o, "qty", None)),
                "notional": _f(getattr(o, "notional", None)),
                "limit_price": _f(getattr(o, "limit_price", None)),
                # Replacement recovery must compare the broker successor's
                # exact protective trigger with the immutable dead-man request.
                # Keep this as broker truth; omitting it would allow a linked
                # stop at a different price to be mistaken for our protection.
                "stop_price": _f(getattr(o, "stop_price", None)),
                "time_in_force": (
                    str(getattr(time_in_force, "value", time_in_force) or "").lower()
                    or None
                ),
                "extended_hours": (
                    bool(extended_hours) if extended_hours is not None else None
                ),
                "position_intent": (
                    str(getattr(position_intent, "value", position_intent) or "").lower()
                    or None
                ),
                "replaced_by": str(getattr(o, "replaced_by", "") or "") or None,
                "replaces": str(getattr(o, "replaces", "") or "") or None,
            },
        )

    def get_order(self, order_id: str):
        try:
            o = self._account_client().get_order_by_id(str(order_id))
            return self._normalize_order(o), _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] get_order(%s) failed: %s", order_id, exc)
            return None, _fresh(5.0)

    def get_order_truth(self, order_id: str) -> dict[str, Any]:
        """Strict broker-id lookup: explicit 404 is absence; all else is unknown."""
        try:
            order = self._account_client().get_order_by_id(str(order_id))
            return {
                "readable": True,
                "found": True,
                "order": self._normalize_order(order),
            }
        except Exception as exc:
            failure = _submit_failure_metadata(exc)
            if failure.get("http_status") == 404:
                return {"readable": True, "found": False, "order": None}
            logger.debug("[alpaca_spot] strict order-id lookup failed oid=%s: %s", order_id, exc)
            return {
                "readable": False,
                "found": False,
                "order": None,
                "error": failure,
            }

    def get_order_by_client_order_id(self, client_order_id: str):
        """Resolve Alpaca's broker order from our deterministic client id.

        A timed-out first submit may have reached Alpaca even when the caller never
        received its broker order id.  Alpaca then rejects an idempotent retry with
        ``40010001 client_order_id must be unique``.  The client id is the only safe
        recovery key in that case; treating the duplicate reject as a failed place
        can leave a real fill unmanaged.
        """
        try:
            o = self._account_client().get_order_by_client_id(str(client_order_id))
            return self._normalize_order(o), _fresh(5.0)
        except Exception as exc:
            logger.debug(
                "[alpaca_spot] get_order_by_client_order_id(%s) failed: %s",
                client_order_id,
                exc,
            )
            return None, _fresh(5.0)

    def get_order_by_client_order_id_truth(self, client_order_id: str) -> dict[str, Any]:
        """Strict CID lookup that separates explicit absence from read failure.

        Retry protocols must not infer "no order" from the legacy adapter method's
        ``None`` because it intentionally folds every SDK exception into that value.
        Alpaca's explicit HTTP 404 is the only negative proof accepted here.
        """
        try:
            order = self._account_client().get_order_by_client_id(str(client_order_id))
            return {
                "readable": True,
                "found": True,
                "order": self._normalize_order(order),
            }
        except Exception as exc:
            failure = _submit_failure_metadata(exc)
            if failure.get("http_status") == 404:
                return {"readable": True, "found": False, "order": None}
            logger.debug(
                "[alpaca_spot] strict client-id lookup failed cid=%s: %s",
                client_order_id,
                exc,
            )
            return {
                "readable": False,
                "found": False,
                "order": None,
                "error": failure,
            }

    def list_open_orders(self, *, product_id: Optional[str] = None, limit: int = 50,
                         strict: bool = False):
        """Open orders. strict=True returns (None, meta) on a READ FAILURE so safety-
        critical callers (the orphan reconciler's in-flight guard) can distinguish
        'no open orders' from 'unreadable' and fail safely with no mutation; default keeps the legacy
        ([], meta)-on-error contract for existing callers."""
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=int(limit),
                                   symbols=[_to_symbol(product_id)] if product_id else None)
            orders = self._account_client().get_orders(filter=req)
            return [self._normalize_order(o) for o in (orders or [])], _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] list_open_orders failed: %s", exc)
            return (None if strict else []), _fresh(5.0)

    def get_fills(self, *, product_id: Optional[str] = None, order_id: Optional[str] = None, limit: int = 50):
        # Alpaca exposes fills via account activities; the runner reads avg_fill_price off the
        # order itself, so a thin best-effort implementation is sufficient for v1.
        try:
            o = self._account_client().get_order_by_id(str(order_id)) if order_id else None
            if o is None:
                return [], _fresh(5.0)
            fp = _f(getattr(o, "filled_avg_price", None)); fq = _f(getattr(o, "filled_qty", None))
            if not fp or not fq:
                return [], _fresh(5.0)
            return [NormalizedFill(
                fill_id=None, order_id=str(getattr(o, "id", "")), product_id=_from_alpaca_symbol(getattr(o, "symbol", "")),
                side=str(getattr(getattr(o, "side", None), "value", "")).lower(), size=fq, price=fp,
                trade_time=str(getattr(o, "filled_at", "") or ""),
            )], _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] get_fills failed: %s", exc)
            return [], _fresh(5.0)

    def list_positions(self):
        """ALL account positions, normalized to plain dicts (read-only; feeds the orphan
        reconciler + ops views). Returns (list, meta) — or (None, meta) when the account
        read FAILED, so callers can distinguish 'flat' ([]) from 'unreadable' (None) and
        fail safely (take no action) on the latter."""
        try:
            rows = self._account_client().get_all_positions() or []
            out = []
            for p in rows:
                out.append({
                    "product_id": _from_alpaca_symbol(str(getattr(p, "symbol", "") or "")),
                    "raw_symbol": str(getattr(p, "symbol", "") or ""),
                    "qty": _f(getattr(p, "qty", None)) or 0.0,
                    "avg_entry_price": _f(getattr(p, "avg_entry_price", None)),
                    "market_value": _f(getattr(p, "market_value", None)),
                    "unrealized_pl": _f(getattr(p, "unrealized_pl", None)),
                    "asset_class": str(
                        getattr(getattr(p, "asset_class", None), "value", "")
                        or getattr(p, "asset_class", "")
                        or ""
                    ).lower(),
                })
            return out, _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] list_positions failed: %s", exc)
            return None, _fresh(5.0)

    def get_position_quantity(self, product_id: str) -> Optional[float]:
        """Exact broker quantity for one symbol (``None`` means unreadable).

        Alpaca uses HTTP 404 for a genuinely absent position.  Only that explicit
        response is treated as flat; transport/SDK failures remain unknown so an
        emergency path cannot mistake a data outage for a successful exit.
        """
        symbol = _to_symbol(product_id)
        if not symbol:
            return None
        try:
            pos = self._account_client().get_open_position(symbol)
            qty = _f(getattr(pos, "qty", None))
            return qty if qty is not None else None
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            if status is None and response is not None:
                status = getattr(response, "status_code", None) or getattr(response, "status", None)
            try:
                if int(status) == 404:
                    return 0.0
            except (TypeError, ValueError):
                pass
            logger.debug("[alpaca_spot] get_position_quantity(%s) failed: %s", symbol, exc)
            return None

    def place_market_order(self, *, product_id: str, side: str, base_size: str,
                           client_order_id: Optional[str] = None,
                           position_intent: Optional[str] = None,
                           time_in_force: Optional[str] = None,
                           asset_class: Any = None, **_ignored) -> dict[str, Any]:
        return self._submit(product_id, side, base_size, client_order_id, limit_price=None,
                            position_intent=position_intent, time_in_force=time_in_force,
                            asset_class=asset_class)

    def place_limit_order_gtc(self, *, product_id: str, side: str, base_size: str,
                              limit_price: str, client_order_id: Optional[str] = None,
                              extended_hours: bool = False,
                              position_intent: Optional[str] = None,
                              time_in_force: Optional[str] = None,
                              asset_class: Any = None, **_ignored) -> dict[str, Any]:
        return self._submit(product_id, side, base_size, client_order_id,
                            limit_price=limit_price, extended_hours=bool(extended_hours),
                            position_intent=position_intent, time_in_force=time_in_force,
                            asset_class=asset_class)

    def place_deadman_stop(self, *, product_id: str, base_size: str, stop_price: float,
                           client_order_id: Optional[str] = None,
                           asset_class: Any = None) -> dict[str, Any]:
        """DEAD-MAN protective stop (2026-07-10, the GMM -$16k orphan incident): a
        RESTING GTC STOP order at the BROKER itself, placed BELOW the software stop —
        not the primary exit (the FSM manages the position), but the FLOOR when the
        whole machine dies or loses network while holding (the exact incident: TCP
        ephemeral-port exhaustion -> the worker was alive but could not reach Alpaca
        -> GMM collapsed unprotected). ``sell_to_close`` means even a double-fire
        alongside a software exit can never flip the position short. Equity-only
        (Alpaca equities support stop orders; the crypto lane is separate)."""
        quantized_stop: str | None = None
        try:
            qty = float(base_size)
            stop = float(stop_price)
            quantized_stop = quantize_alpaca_equity_sell_stop_price(stop_price)
        except (TypeError, ValueError):
            qty = stop = float("nan")
        fractional_qty = bool(
            math.isfinite(qty) and abs(qty - round(qty)) > 1e-9
        )
        if (
            _is_crypto_pid(product_id)
            or _is_crypto_asset_class(asset_class)
            or not _to_symbol(product_id)
            or not str(client_order_id or "").strip()
            or not math.isfinite(qty)
            or qty <= 0.0
            or fractional_qty
            or not math.isfinite(stop)
            or stop <= 0.0
        ):
            return {
                "ok": False,
                "error": (
                    "alpaca_fractional_deadman_not_certified"
                    if fractional_qty
                    else "alpaca_deadman_instruction_not_certified"
                ),
                "client_order_id": client_order_id,
                "pre_submit_blocked": True,
            }
        try:
            from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
            from alpaca.trading.requests import StopOrderRequest

            req = StopOrderRequest(
                symbol=_to_symbol(product_id),
                qty=base_size,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=quantized_stop,
                client_order_id=client_order_id,
                position_intent=PositionIntent.SELL_TO_CLOSE,
            )
            o = self._account_client().submit_order(req)
            return {"ok": True, "order_id": str(getattr(o, "id", "") or ""),
                    "status": str(getattr(getattr(o, "status", None), "value", "") or ""),
                    "client_order_id": client_order_id,
                    "stop_price": quantized_stop,
                    "order_request": {
                        "product_id": _to_symbol(product_id),
                        "base_size": str(base_size),
                        "side": "sell",
                        "position_intent": "sell_to_close",
                        "order_type": "stop",
                        "time_in_force": "gtc",
                        "stop_price": quantized_stop,
                        "client_order_id": client_order_id,
                    }}
        except Exception as exc:
            logger.warning("[alpaca_spot] deadman stop place failed for %s: %s", product_id, exc)
            return {
                "ok": False,
                "error": str(exc),
                "client_order_id": client_order_id,
                "stop_price": quantized_stop,
                **_submit_failure_metadata(exc),
            }

    def cancel_order_by_id(self, order_id: str) -> bool:
        """Cancel one resting order by broker id (the dead-man release path).
        True = cancelled or already gone; False = a real cancel failure."""
        try:
            self._account_client().cancel_order_by_id(order_id)
            return True
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "unable to be cancel" in msg or "filled" in msg:
                return True  # already gone / already terminal — released either way
            logger.warning("[alpaca_spot] cancel_order_by_id(%s) failed: %s", order_id, exc)
            return False

    def _resolve_position_intent(self, position_intent):
        """Map the lane's intent string to the alpaca-py ``PositionIntent`` enum.

        The intent DISAMBIGUATES an otherwise-ambiguous ``SELL`` (open-short vs
        close-long) — the #1 short-lane adapter change (SHORT_SIDE_LANE.md P0):

          - short ENTRY  → ``OrderSide.SELL`` + ``SELL_TO_OPEN``
          - short COVER  → ``OrderSide.BUY``  + ``BUY_TO_CLOSE``
          - long open/close keep ``BUY_TO_OPEN`` / ``SELL_TO_CLOSE``.

        ``None`` (the long-lane default) returns ``None`` so the request is built
        WITHOUT the field — byte-identical to today. Accepts either the enum name
        (``"sell_to_open"``) or the raw enum.
        """
        if position_intent is None:
            return None
        try:
            from alpaca.trading.enums import PositionIntent
        except Exception:
            return None
        if isinstance(position_intent, PositionIntent):
            return position_intent
        key = str(position_intent).strip().lower()
        _MAP = {
            "buy_to_open": PositionIntent.BUY_TO_OPEN,
            "buy_to_close": PositionIntent.BUY_TO_CLOSE,
            "sell_to_open": PositionIntent.SELL_TO_OPEN,
            "sell_to_close": PositionIntent.SELL_TO_CLOSE,
        }
        return _MAP.get(key)

    @staticmethod
    def _equity_limit_price(price, side) -> float:
        """Alpaca EQUITY sub-penny rule (reject 42210000 'sub-penny increment does not
        fulfill minimum pricing criteria'): >= $1.00 -> $0.01 increments, < $1.00 ->
        $0.0001. The lane's trail/target math emits raw floats (1.5345426..., 5.544) —
        Alpaca REJECTED every such EXIT for 2 days (2026-07-07/08: ~38 failed exit
        submissions across every symbol; VTAK bled -40%/-$3,390 while its stop,
        scale-out AND trail submissions all bounced; even winners' scale-outs failed).
        Entries passed only because 2-decimal quotes fed them. Round TOWARD
        MARKETABILITY (SELL -> floor, BUY -> ceiling) so a protective exit is never
        stranded over a fraction of a cent. Decimal-quantized (no float artifacts).
        Equities only — crypto increments differ and that path is untouched."""
        return float(quantize_alpaca_equity_limit_price(price, side))

    def _submit(self, product_id, side, base_size, client_order_id, *, limit_price,
                extended_hours: bool = False, position_intent=None,
                time_in_force: str | None = None,
                asset_class: Any = None) -> dict[str, Any]:
        sym = _to_symbol(product_id)
        side_key = str(getattr(side, "value", side) or "").strip().lower()
        intent_key = str(
            getattr(position_intent, "value", position_intent) or ""
        ).strip().lower()
        requested_tif = str(time_in_force or "").strip().lower()
        try:
            qty_value = float(base_size)
            limit_value = None if limit_price is None else float(limit_price)
        except (TypeError, ValueError):
            qty_value = float("nan")
            limit_value = float("nan")
        invalid_common = bool(
            side_key not in {"buy", "sell"}
            or not sym
            or not str(client_order_id or "").strip()
            or not math.isfinite(qty_value)
            or qty_value <= 0.0
            or (
                limit_price is not None
                and (
                    limit_value is None
                    or not math.isfinite(limit_value)
                    or limit_value <= 0.0
                )
            )
        )
        exact_pairs = {
            ("buy", "buy_to_open"),
            ("sell", "sell_to_close"),
        }
        instruction_ok = bool(
            not _is_crypto_pid(product_id)
            and not _is_crypto_asset_class(asset_class)
            and not invalid_common
            and (side_key, intent_key) in exact_pairs
        )
        if intent_key == "buy_to_open":
            instruction_ok = bool(
                instruction_ok
                and requested_tif in {"day", "gfd"}
                # This recertification lane is RTH-only.  A stale premarket
                # decision must not carry extended-hours eligibility across the
                # 09:30 boundary and later fill after the broker stop is dormant.
                and extended_hours is False
                and abs(qty_value - round(qty_value)) <= 1e-9
            )
        fractional_entry = bool(
            intent_key == "buy_to_open"
            and math.isfinite(qty_value)
            and abs(qty_value - round(qty_value)) > 1e-9
        )
        extended_entry = bool(
            intent_key == "buy_to_open" and extended_hours is not False
        )
        if not instruction_ok:
            return {
                "ok": False,
                "error": (
                    "alpaca_fractional_entry_not_certified"
                    if fractional_entry
                    else (
                        "alpaca_extended_hours_entry_not_certified"
                        if extended_entry
                        else "alpaca_instruction_side_intent_not_certified"
                    )
                ),
                "client_order_id": client_order_id,
                "submit_outcome": "pre_transport_blocked",
                "pre_submit_blocked": True,
            }
        canonical_limit: str | None = None
        if limit_price is not None:
            try:
                canonical_limit = quantize_alpaca_equity_limit_price(
                    limit_price,
                    side_key,
                )
            except ValueError:
                return {
                    "ok": False,
                    "error": "alpaca_equity_limit_price_invalid",
                    "client_order_id": client_order_id,
                    "submit_outcome": "pre_transport_blocked",
                    "pre_submit_blocked": True,
                }
            # Every risk-increasing entry must arrive in the exact decimal form
            # frozen by the runner.  Silently re-quantizing here would make the
            # broker order economically different from its durable risk permit.
            if (
                intent_key == "buy_to_open"
                and str(limit_price).strip() != canonical_limit
            ):
                return {
                    "ok": False,
                    "error": "alpaca_entry_limit_not_canonical",
                    "client_order_id": client_order_id,
                    "canonical_limit_price": canonical_limit,
                    "submit_outcome": "pre_transport_blocked",
                    "pre_submit_blocked": True,
                }
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
            _side = OrderSide.BUY if side_key == "buy" else OrderSide.SELL
            qty = qty_value
            # Optional position-intent (short lane). None ⇒ omit the field entirely
            # so the long-path request is byte-identical to today.
            _intent = self._resolve_position_intent(position_intent)
            if _intent is None:
                return {
                    "ok": False,
                    "error": "alpaca_position_intent_resolution_failed",
                    "client_order_id": client_order_id,
                    "submit_outcome": "pre_transport_blocked",
                    "pre_submit_blocked": True,
                }
            _intent_kw = {"position_intent": _intent}
            if limit_price is not None:
                # Sub-penny normalization (see _equity_limit_price) — MUST precede the
                # request build; every raw-float exit limit was rejected 42210000.
                _lp = canonical_limit
                # Marketable/posting limit. Alpaca rejects extended_hours unless the order
                # is a LIMIT with DAY tif — so for pre-/after-market (Ross's gap-and-go) we
                # send DAY + extended_hours=True; the RTH default stays a plain GTC.
                if extended_hours:
                    req = LimitOrderRequest(symbol=sym, qty=qty, side=_side, time_in_force=TimeInForce.DAY,
                                            limit_price=_lp, client_order_id=client_order_id,
                                            extended_hours=True, **_intent_kw)
                else:
                    # Fractional-qty orders REQUIRE DAY tif on Alpaca (GTC is
                    # rejected) — 25% of twin entries died on this (2026-06-12
                    # quant pass v2 A6). Whole-share orders keep GTC.
                    _tif = TimeInForce.GTC
                    if requested_tif in {"day", "gfd"}:
                        _tif = TimeInForce.DAY
                    elif requested_tif == "gtc":
                        _tif = TimeInForce.GTC
                    try:
                        if (
                            not requested_tif
                            and abs(float(qty) - round(float(qty))) > 1e-9
                        ):
                            _tif = TimeInForce.DAY
                    except (TypeError, ValueError):
                        pass
                    req = LimitOrderRequest(symbol=sym, qty=qty, side=_side, time_in_force=_tif,
                                            limit_price=_lp, client_order_id=client_order_id,
                                            **_intent_kw)
            else:
                req = MarketOrderRequest(symbol=sym, qty=qty, side=_side, time_in_force=TimeInForce.DAY,
                                         client_order_id=client_order_id, **_intent_kw)
            if intent_key == "buy_to_open":
                try:
                    sdk_limit = Decimal(str(getattr(req, "limit_price", None)))
                    frozen_limit = Decimal(str(canonical_limit))
                except (InvalidOperation, TypeError, ValueError):
                    sdk_limit = frozen_limit = Decimal("NaN")
                if (
                    not sdk_limit.is_finite()
                    or not frozen_limit.is_finite()
                    or sdk_limit != frozen_limit
                ):
                    return {
                        "ok": False,
                        "error": "alpaca_entry_limit_transport_mismatch",
                        "client_order_id": client_order_id,
                        "canonical_limit_price": canonical_limit,
                        "submit_outcome": "pre_transport_blocked",
                        "pre_submit_blocked": True,
                    }
            o = self._account_client().submit_order(order_data=req)
            res = {"ok": True, "order_id": str(getattr(o, "id", "") or ""),
                   "client_order_id": getattr(o, "client_order_id", None) or client_order_id,
                   "status": _norm_status(getattr(o, "status", None))}
            # Surface the resolved short intent + the broker's signed position-intent
            # echo so the runner can confirm a short opened/covered as expected.
            if _intent is not None:
                res["position_intent"] = str(getattr(_intent, "value", _intent))
                pi_echo = getattr(o, "position_intent", None)
                if pi_echo is not None:
                    res["position_intent_echo"] = str(getattr(pi_echo, "value", pi_echo))
            return res
        except Exception as exc:
            msg = str(exc)
            failure_meta = _submit_failure_metadata(exc)
            # Distinctly surface SSR / borrow-locate rejections so the runner can DEFER
            # (post an up-bid limit / skip) rather than blind-retry into a venue wall.
            low = msg.lower()
            reject_kind = None
            if ("short" in low and ("restrict" in low or "ssr" in low or "uptick" in low)) or "regulation sho" in low:
                reject_kind = "ssr"
            elif "borrow" in low or "locate" in low or "not shortable" in low or "htb" in low:
                reject_kind = "borrow"
            logger.warning("[alpaca_spot] submit order failed sym=%s side=%s limit=%s intent=%s reject=%s: %s",
                           sym, side, limit_price, position_intent, reject_kind, exc)
            out = {
                "ok": False,
                "error": msg[:200],
                "client_order_id": client_order_id,
                **failure_meta,
            }
            if reject_kind:
                out["reject_kind"] = reject_kind
            return out

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        try:
            self._account_client().cancel_order_by_id(str(order_id))
            return {"ok": True, "order_id": str(order_id)}
        except Exception as exc:
            logger.debug("[alpaca_spot] cancel_order(%s) failed: %s", order_id, exc)
            return {"ok": False, "error": str(exc)[:200], "order_id": str(order_id)}

    def preview_market_order(self, *, product_id: str, side: str, base_size: str, **_ignored) -> dict[str, Any]:
        # Alpaca has no order-preview endpoint; estimate locally from the latest quote.
        tick, _ = self.get_best_bid_ask(product_id)
        px = None
        if tick is not None:
            px = tick.ask if str(side).lower() == "buy" else tick.bid
        return {"ok": True, "estimated_price": px, "base_size": base_size, "note": "local estimate (no preview API)"}

    # ── account ──────────────────────────────────────────────────────────────
    def get_account_snapshot(self) -> dict[str, Any]:
        try:
            a = self._account_client().get_account()
            return {"ok": True,
                    # Stable Alpaca UUID.  This is non-secret and is the durable
                    # execution-generation identity; account_number is intentionally
                    # not persisted or surfaced.
                    "account_id": str(getattr(a, "id", "") or ""),
                    "equity": _f(getattr(a, "equity", None)),
                    "last_equity": _f(getattr(a, "last_equity", None)),
                    "buying_power": _f(getattr(a, "buying_power", None)),
                    "cash": _f(getattr(a, "cash", None)),
                    "status": str(getattr(getattr(a, "status", None), "value", getattr(a, "status", "")) or ""),
                    # Short-lane capability surfacing (SHORT_SIDE_LANE.md P0): the lane must
                    # never arm a short on a cash / no-margin account. multiplier>1 ⇒ margin;
                    # shorting_enabled is the explicit account capability flag.
                    "shorting_enabled": _opt_bool(getattr(a, "shorting_enabled", None)),
                    "multiplier": _f(getattr(a, "multiplier", None)),
                    "paper": True}
        except Exception as exc:
            logger.debug("[alpaca_spot] get_account_snapshot failed: %s", exc)
            return {"ok": False, "error": str(exc)[:200]}

    def get_market_clock_snapshot(self) -> dict[str, Any]:
        """Fresh Alpaca exchange clock for fail-closed RTH entry admission."""
        try:
            clock = self._account_client().get_clock()
            return {
                "ok": True,
                "is_open": bool(getattr(clock, "is_open", False)),
                "timestamp": str(getattr(clock, "timestamp", "") or ""),
                "next_open": str(getattr(clock, "next_open", "") or ""),
                "next_close": str(getattr(clock, "next_close", "") or ""),
                "paper": True,
            }
        except Exception as exc:
            logger.debug("[alpaca_spot] get_market_clock_snapshot failed: %s", exc)
            return {"ok": False, "error": str(exc)[:200]}
