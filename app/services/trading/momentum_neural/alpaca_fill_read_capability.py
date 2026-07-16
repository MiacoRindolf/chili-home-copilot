"""Process-private authority for one exact Alpaca PAPER fill read.

The capability in this module is deliberately not an evidence document.  It is
an in-process handoff token which proves that the registered, exact
``AlpacaSpotAdapter`` reader produced a particular public receipt.  Durable
evidence is the content-addressed public receipt; the capability merely stops a
caller from manufacturing that receipt and re-hashing it before publication.

The HMAC key, construction marker, and registered code object live only inside
one closure.  The token has no pickle/reduce representation.  Registration is
one-shot and issuance additionally checks the immediate caller's code object,
so an instance monkeypatch or a duck-typed adapter cannot mint authority.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
import sys
from typing import Any, Callable, Mapping


UTC = timezone.utc


class AlpacaFillReadCapabilityError(RuntimeError):
    """The private read authority is absent, forged, stale, or mis-bound."""


def _canonical_payload(payload: Mapping[str, Any]) -> bytes:
    if not isinstance(payload, Mapping):
        raise AlpacaFillReadCapabilityError("fill read capability payload is malformed")
    try:
        return json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AlpacaFillReadCapabilityError(
            "fill read capability payload is not canonical JSON"
        ) from exc


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AlpacaFillReadCapabilityError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _build_authority():
    secret = secrets.token_bytes(32)
    construction_marker = object()
    issuer_code = None

    class _AlpacaFillReadCapability:
        __slots__ = ("__payload", "__nonce", "__mac")

        def __init__(
            self,
            marker: object,
            payload_bytes: bytes,
            nonce: bytes,
            mac: bytes,
        ) -> None:
            if marker is not construction_marker:
                raise TypeError("Alpaca fill read capabilities are not constructible")
            self.__payload = payload_bytes
            self.__nonce = nonce
            self.__mac = mac

        def __repr__(self) -> str:
            return "<AlpacaFillReadCapability process-private>"

        def __reduce__(self):
            raise TypeError("Alpaca fill read capabilities are not serializable")

        def __reduce_ex__(self, _protocol: int):
            raise TypeError("Alpaca fill read capabilities are not serializable")

    def register(reader: Callable[..., Any]) -> None:
        nonlocal issuer_code
        code = getattr(reader, "__code__", None)
        if (
            code is None
            or getattr(reader, "__module__", "")
            != "app.services.trading.venue.alpaca_spot"
            or getattr(reader, "__qualname__", "")
            != "AlpacaSpotAdapter.get_paper_fill_activity_batch"
        ):
            raise AlpacaFillReadCapabilityError(
                "unexpected Alpaca fill reader registration"
            )
        if issuer_code is not None and issuer_code is not code:
            raise AlpacaFillReadCapabilityError(
                "Alpaca fill reader authority is already registered"
            )
        issuer_code = code

    def issue(payload: Mapping[str, Any]):
        if issuer_code is None or sys._getframe(1).f_code is not issuer_code:
            raise AlpacaFillReadCapabilityError(
                "only the exact registered Alpaca reader may issue authority"
            )
        payload_bytes = _canonical_payload(payload)
        nonce = secrets.token_bytes(16)
        mac = hmac.new(secret, nonce + payload_bytes, hashlib.sha256).digest()
        return _AlpacaFillReadCapability(
            construction_marker,
            payload_bytes,
            nonce,
            mac,
        )

    def verify(
        capability: object,
        *,
        expected_payload: Mapping[str, Any],
        verified_at: datetime,
    ) -> None:
        if type(capability) is not _AlpacaFillReadCapability:
            raise AlpacaFillReadCapabilityError(
                "exact Alpaca fill read capability is missing"
            )
        expected = _canonical_payload(expected_payload)
        payload_bytes = capability._AlpacaFillReadCapability__payload
        nonce = capability._AlpacaFillReadCapability__nonce
        supplied_mac = capability._AlpacaFillReadCapability__mac
        expected_mac = hmac.new(secret, nonce + payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_mac, expected_mac):
            raise AlpacaFillReadCapabilityError(
                "Alpaca fill read capability HMAC is invalid"
            )
        if not hmac.compare_digest(payload_bytes, expected):
            raise AlpacaFillReadCapabilityError(
                "Alpaca fill read capability payload changed"
            )
        try:
            decoded = json.loads(payload_bytes.decode("utf-8"))
            available_at = datetime.fromisoformat(
                str(decoded["available_at"]).replace("Z", "+00:00")
            )
            expires_at = datetime.fromisoformat(
                str(decoded["expires_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise AlpacaFillReadCapabilityError(
                "Alpaca fill read capability expiry is malformed"
            ) from exc
        verified = _utc(verified_at, "verified_at")
        available = _utc(available_at, "available_at")
        expires = _utc(expires_at, "expires_at")
        if available > verified + timedelta(seconds=1):
            raise AlpacaFillReadCapabilityError(
                "Alpaca fill read capability is future-dated"
            )
        if expires <= available:
            raise AlpacaFillReadCapabilityError(
                "Alpaca fill read capability expiry is not causal"
            )
        if expires - available > timedelta(minutes=5):
            raise AlpacaFillReadCapabilityError(
                "Alpaca fill read capability expiry exceeds the bounded handoff"
            )
        if verified > expires:
            raise AlpacaFillReadCapabilityError(
                "Alpaca fill read capability expired before publication"
            )

    return _AlpacaFillReadCapability, register, issue, verify


(
    AlpacaFillReadCapability,
    register_exact_alpaca_fill_reader,
    issue_alpaca_fill_read_capability,
    verify_alpaca_fill_read_capability,
) = _build_authority()


__all__ = (
    "AlpacaFillReadCapability",
    "AlpacaFillReadCapabilityError",
    "issue_alpaca_fill_read_capability",
    "register_exact_alpaca_fill_reader",
    "verify_alpaca_fill_read_capability",
)
