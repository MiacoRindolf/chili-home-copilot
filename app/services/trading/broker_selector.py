"""f-coinbase-autotrader-enablement-phase-3-broker-selector (2026-05-09).

Pure-function broker selector for the autotrader entry path.

Returns a :class:`VenueDecision` with one of three venues:

* ``'rh'`` — route through the existing Robinhood adapter. The
  autotrader's RH path is BYTE-IDENTICAL post-Phase-3; this venue
  string is what tells the autotrader to fall through to the
  pre-existing code path.
* ``'coinbase'`` — route through the Coinbase adapter. Gated by
  ``CHILI_COINBASE_AUTOTRADER_LIVE`` at the autotrader call site:
  when False (default), the autotrader writes a shadow-log row to
  ``trading_venue_routing_log`` and skips the broker call. When True,
  the autotrader places via the Coinbase adapter.
* ``'skip'`` — refuse entry. Reason carries the cause for audit.

Five-branch decision tree (executed in order):

  1. **Global kill switch** (``CHILI_AUTOTRADER_KILL_SWITCH=1`` OR
     in-process ``governance.is_kill_switch_active()``) → skip.
  2. **Fast-path overlap** (the ticker is currently held by the
     fast-path subsystem) → skip; the fast-path owns it.
  3. **RH whitelist match** — for equities, any non-``-USD`` ticker
     routes RH. For crypto bases, RH whitelist
     (:data:`ROBINHOOD_SUPPORTED_CRYPTO_BASES`) wins on cost
     (RH crypto = fee-free; Coinbase crypto = 60bps taker).
  4. **Coinbase whitelist match** — long-tail crypto bases that
     RH doesn't list but Coinbase does.
  5. **No match** → skip with reason ``no_venue_supports``.

Operator-locked design constraints (binding from Phase 1):

* Cross-venue position cap: SEPARATE per-venue caps (no aggregation).
  This selector does NOT enforce caps; it picks a venue. Cap
  enforcement stays in the autotrader's existing position-count
  checks per venue.
* Kill switch: GLOBAL (one lever stops both venues).
* Selector preference for both-listed: RH-first.
* Fast-path overlap: skip-on-fast-path-active.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Branch-name constants. Used in audit reason strings + tests so a
# typo flips visibly red.
REASON_KILL_SWITCH_GLOBAL = "kill_switch_global"
REASON_KILL_SWITCH_GOVERNANCE = "kill_switch_governance"
REASON_FAST_PATH_ACTIVE = "fast_path_active"
REASON_RH_WHITELIST = "rh_whitelist_match"
REASON_COINBASE_WHITELIST = "coinbase_whitelist_match"
REASON_NO_VENUE = "no_venue_supports"


@dataclass(frozen=True)
class VenueDecision:
    """Selector output. ``venue`` is one of ``'rh' | 'coinbase' | 'skip'``."""

    venue: str
    reason: str
    extra: Optional[dict[str, Any]] = None


# ── Helpers ──────────────────────────────────────────────────────────


def _is_crypto_ticker(ticker: str) -> bool:
    """Crypto convention: tickers ending in ``-USD``."""
    return bool(ticker) and ticker.upper().endswith("-USD")


def resolve_rh_whitelist(ticker: str) -> bool:
    """True iff Robinhood supports trading this ticker.

    Equities (no ``-USD`` suffix): always True. The autotrader's
    existing RH-side gates (instrument lookup, market hours, etc.)
    handle finer eligibility.

    Crypto (``-USD`` suffix): True iff the bare base symbol is in
    ``ROBINHOOD_SUPPORTED_CRYPTO_BASES`` (the static whitelist
    maintained in :mod:`broker_service`).
    """
    if not ticker:
        return False
    t = ticker.strip().upper()
    if not t.endswith("-USD"):
        # Equities — RH supports.
        return True
    base = t[:-4]
    try:
        from ..broker_service import ROBINHOOD_SUPPORTED_CRYPTO_BASES
    except Exception:
        logger.warning(
            "[broker_selector] failed to import "
            "ROBINHOOD_SUPPORTED_CRYPTO_BASES; defaulting to NOT supported",
            exc_info=True,
        )
        return False
    return base in ROBINHOOD_SUPPORTED_CRYPTO_BASES


def resolve_coinbase_whitelist(ticker: str) -> bool:
    """True iff Coinbase trades this ticker as a USD-quoted spot pair.

    Equities (no ``-USD`` suffix): always False. Coinbase doesn't
    trade equities.

    Crypto (``-USD`` suffix): True. The selector hands the routing to
    the autotrader, which queries the live Coinbase product list at
    placement time. False positives (a base Coinbase doesn't actually
    list) are caught by the broker's pre-trade risk check —
    ``coinbase_service.place_buy_order`` will return
    ``{"ok": False, "error": "..."}`` and the autotrader records the
    rejection.

    A future enhancement (Phase 5+) can cache the Coinbase USD-spot
    universe so the selector pre-filters; the brief explicitly leaves
    that out of scope here.
    """
    if not ticker:
        return False
    t = ticker.strip().upper()
    if not t.endswith("-USD"):
        return False
    return True


def _is_fast_path_active(ticker: str) -> bool:
    """Per the operator's locked constraint: skip-on-fast-path-active.

    The fast-path subsystem owns its own placement decisions for
    pairs in ``fast_path_universe`` with ``status IN ('active',
    'shadow')``. When the autotrader sees one of those tickers, it
    must NOT route a duplicate Coinbase entry; the fast-path is
    authoritative.

    Helper-level testable: tests inject the active set via the
    selector's ``fast_path_active_tickers`` kwarg. Production callers
    leave it None and the resolver queries the DB.

    Returns False on any DB failure to err on the side of letting
    the selector continue. The fast-path skip is observability /
    coordination, not a safety belt.
    """
    if not ticker:
        return False
    t = ticker.strip().upper()
    try:
        from sqlalchemy import text
        from ...db import SessionLocal
        sess = SessionLocal()
        try:
            row = sess.execute(text("""
                SELECT 1 FROM fast_path_universe
                 WHERE UPPER(ticker) = :t
                   AND status IN ('active', 'shadow')
                 LIMIT 1
            """), {"t": t}).fetchone()
            return row is not None
        finally:
            try:
                sess.rollback()
            except Exception:
                pass
            try:
                sess.close()
            except Exception:
                pass
    except Exception:
        logger.debug(
            "[broker_selector] fast_path_active query failed; "
            "defaulting to NOT-active",
            exc_info=True,
        )
        return False


def _kill_switch_env_active(settings_=None) -> bool:
    """Read the env-driven kill switch from settings."""
    try:
        if settings_ is None:
            from ...config import settings as _s
            settings_ = _s
        return bool(getattr(settings_, "chili_autotrader_kill_switch", False))
    except Exception:
        return False


def _kill_switch_governance_active() -> bool:
    """Read the in-process governance kill switch."""
    try:
        from .governance import is_kill_switch_active
        return bool(is_kill_switch_active())
    except Exception:
        return False


# ── Selector ─────────────────────────────────────────────────────────


def select_venue(
    *,
    ticker: str,
    settings_=None,
    fast_path_active: Optional[bool] = None,
) -> VenueDecision:
    """Five-branch decision tree. See module docstring.

    ``fast_path_active`` is the test-injection seam. Production
    callers leave it None so the resolver queries the DB.
    """
    if not ticker or not ticker.strip():
        return VenueDecision(
            venue="skip", reason="empty_ticker",
        )

    # Branch 1a: env-driven kill switch.
    if _kill_switch_env_active(settings_):
        return VenueDecision(
            venue="skip", reason=REASON_KILL_SWITCH_GLOBAL,
        )

    # Branch 1b: governance in-process kill switch.
    if _kill_switch_governance_active():
        return VenueDecision(
            venue="skip", reason=REASON_KILL_SWITCH_GOVERNANCE,
        )

    # Branch 2: fast-path overlap. Only relevant for crypto tickers
    # (the fast-path universe is crypto-only today). Equities always
    # skip this check.
    is_crypto = _is_crypto_ticker(ticker)
    fp_active = (
        fast_path_active
        if fast_path_active is not None
        else (_is_fast_path_active(ticker) if is_crypto else False)
    )
    if fp_active:
        return VenueDecision(
            venue="skip", reason=REASON_FAST_PATH_ACTIVE,
        )

    # Branch 3: RH whitelist match (cost-cheaper preference).
    if resolve_rh_whitelist(ticker):
        return VenueDecision(venue="rh", reason=REASON_RH_WHITELIST)

    # Branch 4: Coinbase whitelist match (long-tail crypto).
    if resolve_coinbase_whitelist(ticker):
        return VenueDecision(
            venue="coinbase", reason=REASON_COINBASE_WHITELIST,
        )

    # Branch 5: no venue supports this ticker.
    return VenueDecision(venue="skip", reason=REASON_NO_VENUE)


__all__ = [
    "REASON_COINBASE_WHITELIST",
    "REASON_FAST_PATH_ACTIVE",
    "REASON_KILL_SWITCH_GLOBAL",
    "REASON_KILL_SWITCH_GOVERNANCE",
    "REASON_NO_VENUE",
    "REASON_RH_WHITELIST",
    "VenueDecision",
    "resolve_coinbase_whitelist",
    "resolve_rh_whitelist",
    "select_venue",
]
