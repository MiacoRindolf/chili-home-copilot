"""Process-private authority for one exact Alpaca PAPER open-order census."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
import sys
from typing import Any, Callable, Mapping


UTC = timezone.utc


class AlpacaBuyingPowerCensusCapabilityError(RuntimeError):
    """The open-order census authority is absent, forged, stale, or mis-bound."""


def _canonical(payload: Mapping[str, Any]) -> bytes:
    if not isinstance(payload, Mapping):
        raise AlpacaBuyingPowerCensusCapabilityError(
            "open-order census capability payload is malformed"
        )
    try:
        return json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AlpacaBuyingPowerCensusCapabilityError(
            "open-order census capability payload is not canonical JSON"
        ) from exc


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AlpacaBuyingPowerCensusCapabilityError(
            f"{field} must be timezone-aware"
        )
    return value.astimezone(UTC)


def _build_authority():
    secret = secrets.token_bytes(32)
    marker = object()
    issuer_code = None

    class _Capability:
        __slots__ = ("__payload", "__nonce", "__mac")

        def __init__(
            self,
            construction_marker: object,
            payload: bytes,
            nonce: bytes,
            mac: bytes,
        ) -> None:
            if construction_marker is not marker:
                raise TypeError(
                    "Alpaca open-order census capabilities are not constructible"
                )
            self.__payload = payload
            self.__nonce = nonce
            self.__mac = mac

        def __repr__(self) -> str:
            return "<AlpacaBuyingPowerCensusCapability process-private>"

        def __reduce__(self):
            raise TypeError(
                "Alpaca open-order census capabilities are not serializable"
            )

        def __reduce_ex__(self, _protocol: int):
            raise TypeError(
                "Alpaca open-order census capabilities are not serializable"
            )

        # In-process copies must carry the SAME live authority object — a
        # copy never re-mints payload/nonce/mac, so authority is neither
        # duplicated nor lost. Without these, copy.deepcopy falls back to
        # __reduce_ex__ and the operator flow's recorded GET-only read
        # replay (which deep-copies census payloads to freeze them) fails
        # closed. Serialization across process/disk stays refused above.
        def __copy__(self):
            return self

        def __deepcopy__(self, memo):
            return self

    def register(reader: Callable[..., Any]) -> None:
        nonlocal issuer_code
        code = getattr(reader, "__code__", None)
        if (
            code is None
            or getattr(reader, "__module__", "")
            != "app.services.trading.venue.alpaca_spot"
            or getattr(reader, "__qualname__", "")
            != "AlpacaSpotAdapter.get_paper_open_order_census"
        ):
            raise AlpacaBuyingPowerCensusCapabilityError(
                "unexpected Alpaca open-order census reader registration"
            )
        if issuer_code is not None and issuer_code is not code:
            raise AlpacaBuyingPowerCensusCapabilityError(
                "Alpaca open-order census authority is already registered"
            )
        issuer_code = code

    def issue(payload: Mapping[str, Any]):
        if issuer_code is None or sys._getframe(1).f_code is not issuer_code:
            raise AlpacaBuyingPowerCensusCapabilityError(
                "only the exact registered open-order census reader may issue authority"
            )
        payload_bytes = _canonical(payload)
        nonce = secrets.token_bytes(16)
        mac = hmac.new(secret, nonce + payload_bytes, hashlib.sha256).digest()
        return _Capability(marker, payload_bytes, nonce, mac)

    def verify(
        capability: object,
        *,
        expected_payload: Mapping[str, Any],
        verified_at: datetime,
    ) -> None:
        if type(capability) is not _Capability:
            raise AlpacaBuyingPowerCensusCapabilityError(
                "exact Alpaca open-order census capability is missing"
            )
        expected = _canonical(expected_payload)
        payload = capability._Capability__payload
        nonce = capability._Capability__nonce
        supplied = capability._Capability__mac
        calculated = hmac.new(secret, nonce + payload, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, calculated):
            raise AlpacaBuyingPowerCensusCapabilityError(
                "Alpaca open-order census capability HMAC is invalid"
            )
        if not hmac.compare_digest(payload, expected):
            raise AlpacaBuyingPowerCensusCapabilityError(
                "Alpaca open-order census capability payload changed"
            )
        try:
            decoded = json.loads(payload.decode("utf-8"))
            available_at = datetime.fromisoformat(
                str(decoded["available_at"]).replace("Z", "+00:00")
            )
            expires_at = datetime.fromisoformat(
                str(decoded["expires_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise AlpacaBuyingPowerCensusCapabilityError(
                "Alpaca open-order census capability expiry is malformed"
            ) from exc
        verified = _utc(verified_at, "verified_at")
        available = _utc(available_at, "available_at")
        expires = _utc(expires_at, "expires_at")
        if available > verified + timedelta(seconds=1):
            raise AlpacaBuyingPowerCensusCapabilityError(
                "Alpaca open-order census capability is future-dated"
            )
        if not available < expires <= available + timedelta(minutes=5):
            raise AlpacaBuyingPowerCensusCapabilityError(
                "Alpaca open-order census capability expiry is not bounded"
            )
        if verified > expires:
            raise AlpacaBuyingPowerCensusCapabilityError(
                "Alpaca open-order census capability expired before use"
            )

    return _Capability, register, issue, verify


(
    AlpacaBuyingPowerCensusCapability,
    register_exact_alpaca_bp_census_reader,
    issue_alpaca_bp_census_capability,
    verify_alpaca_bp_census_capability,
) = _build_authority()


__all__ = (
    "AlpacaBuyingPowerCensusCapability",
    "AlpacaBuyingPowerCensusCapabilityError",
    "issue_alpaca_bp_census_capability",
    "register_exact_alpaca_bp_census_reader",
    "verify_alpaca_bp_census_capability",
)
