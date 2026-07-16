"""Fail-closed account-generation identity shared by live execution callers.

The persisted value is deliberately adapter-produced and non-secret.  A legacy
session without it is never taught the identity during cleanup: doing that would
give today's credentials authority over an older, unknown broker generation.
"""

from __future__ import annotations

from typing import Any

from ..execution_family_registry import normalize_execution_family


NON_ALPACA_ACCOUNT_IDENTITY_KEY = "non_alpaca_account_identity"
_ALPACA_FAMILIES = frozenset({"alpaca_spot", "alpaca_short"})


def _identity_from_truth(truth: Any) -> str | None:
    if not isinstance(truth, dict) or truth.get("readable") is not True:
        return None
    identity = truth.get("identity")
    if not isinstance(identity, str):
        return None
    identity = identity.strip()
    return identity or None


def read_current_non_alpaca_account_identity(
    execution_family: str | None,
    *,
    adapter: Any | None = None,
) -> dict[str, Any]:
    """Read the current exact account generation without mutating broker state."""
    family = normalize_execution_family(execution_family)
    if family in _ALPACA_FAMILIES:
        return {"ok": True, "applicable": False, "identity": None, "reason": None}
    if adapter is None:
        try:
            from .factory import get_adapter

            adapter = get_adapter(execution_family)
        except Exception:
            adapter = None
    getter = getattr(adapter, "get_account_identity_truth", None)
    if not callable(getter):
        return {
            "ok": False,
            "applicable": True,
            "identity": None,
            "reason": "non_alpaca_account_identity_adapter_missing",
        }
    try:
        identity = _identity_from_truth(getter())
    except Exception:
        identity = None
    if identity is None:
        return {
            "ok": False,
            "applicable": True,
            "identity": None,
            "reason": "non_alpaca_account_identity_unknown",
        }
    return {
        "ok": True,
        "applicable": True,
        "identity": identity,
        "reason": None,
    }


def frozen_non_alpaca_account_identity(session: Any) -> str | None:
    """Return only a pre-existing frozen identity; never infer or backfill one."""
    family = normalize_execution_family(getattr(session, "execution_family", None))
    if family in _ALPACA_FAMILIES:
        return None
    snapshot = getattr(session, "risk_snapshot_json", None)
    if not isinstance(snapshot, dict):
        return None
    identity = snapshot.get(NON_ALPACA_ACCOUNT_IDENTITY_KEY)
    if not isinstance(identity, str):
        return None
    identity = identity.strip()
    return identity or None


def verify_frozen_non_alpaca_account_identity(
    session: Any,
    *,
    adapter: Any | None = None,
) -> dict[str, Any]:
    """Recheck one session's frozen account identity, with no broker mutation.

    This is the reusable runner/terminalizer fence.  Call it at tick start and
    again immediately before each broker POST/cancel because an earlier read does
    not grant authority after an account/configuration rotation.
    """
    family = normalize_execution_family(getattr(session, "execution_family", None))
    if family in _ALPACA_FAMILIES:
        return {
            "ok": True,
            "applicable": False,
            "frozen_identity": None,
            "current_identity": None,
            "reason": None,
        }
    frozen = frozen_non_alpaca_account_identity(session)
    if frozen is None:
        # Important: no adapter call.  Legacy identity loss cannot acquire new
        # cleanup/trading authority from whichever account is configured today.
        return {
            "ok": False,
            "applicable": True,
            "frozen_identity": None,
            "current_identity": None,
            "reason": "non_alpaca_account_identity_unfrozen",
        }
    current = read_current_non_alpaca_account_identity(
        family,
        adapter=adapter,
    )
    if current.get("ok") is not True:
        return {
            "ok": False,
            "applicable": True,
            "frozen_identity": frozen,
            "current_identity": None,
            "reason": str(
                current.get("reason") or "non_alpaca_account_identity_unknown"
            ),
        }
    current_identity = str(current.get("identity") or "").strip()
    if current_identity != frozen:
        return {
            "ok": False,
            "applicable": True,
            "frozen_identity": frozen,
            "current_identity": current_identity or None,
            "reason": "non_alpaca_account_identity_mismatch",
        }
    return {
        "ok": True,
        "applicable": True,
        "frozen_identity": frozen,
        "current_identity": current_identity,
        "reason": None,
    }
