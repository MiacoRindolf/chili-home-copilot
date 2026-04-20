"""Single entry point for resolving a :class:`VenueAdapter` by broker_source.

Before this module, three different places instantiated venue adapters
inline (``auto_trader._execute_*``, ``bracket_writer_g2._default_adapter_factory``,
``stuck_order_watchdog._get_adapter``). Each inline site had a slightly
different ``broker_source`` normalization, a different error surface,
and different assumptions about which venues were supported. Adding a
new venue meant touching every caller — and forgetting one meant silent
mis-routing.

Rules:

* Unknown broker_source → ``None``. Callers decide whether that's an
  error or a skip. This matches the existing stuck_order_watchdog
  behavior, which logs a warning and returns early.
* Imports are local so a broken adapter module (e.g. Coinbase SDK
  incompatibility) can't break the factory import itself.
* The factory returns a fresh adapter instance each call. Adapters are
  stateless wrappers around ``broker_service`` / ``coinbase_service``;
  caching is the broker module's job.

Register a new venue by adding one line to :data:`_BUILDERS`.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .protocol import VenueAdapter

logger = logging.getLogger(__name__)


def _build_robinhood() -> VenueAdapter:
    from .robinhood_spot import RobinhoodSpotAdapter
    return RobinhoodSpotAdapter()


def _build_coinbase() -> VenueAdapter:
    from .coinbase_spot import CoinbaseSpotAdapter
    return CoinbaseSpotAdapter()


_BUILDERS: dict[str, Callable[[], VenueAdapter]] = {
    "robinhood": _build_robinhood,
    "coinbase": _build_coinbase,
    # Aliases — keeping them centralized means the normalization rule
    # is one lookup, not a scatter of ``.lower().strip()`` calls.
    "coinbase_spot": _build_coinbase,
}


SUPPORTED_BROKER_SOURCES: frozenset[str] = frozenset({"robinhood", "coinbase"})


def is_supported(broker_source: str | None) -> bool:
    """True when :func:`get_adapter` would succeed for this source."""
    if not broker_source:
        return False
    return broker_source.strip().lower() in _BUILDERS


def get_adapter(broker_source: str | None) -> Optional[VenueAdapter]:
    """Return a :class:`VenueAdapter` instance for ``broker_source``, or ``None``.

    Never raises — an unknown or unimportable venue returns ``None`` with
    a warning log so callers can decide how to degrade. This matches the
    safety posture of the reconciler and watchdog (skip rather than
    crash the loop).
    """
    if not broker_source:
        return None
    src = broker_source.strip().lower()
    builder = _BUILDERS.get(src)
    if builder is None:
        return None
    try:
        return builder()
    except Exception:
        logger.warning(
            "[venue_factory] adapter build failed for broker_source=%s",
            src, exc_info=True,
        )
        return None


__all__ = [
    "SUPPORTED_BROKER_SOURCES",
    "get_adapter",
    "is_supported",
]
