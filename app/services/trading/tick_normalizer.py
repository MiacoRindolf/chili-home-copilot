"""Venue-aware precision normalizer for prices and quantities.

This module is the SINGLE place in the codebase that decides how many
decimal places a price gets before it crosses the broker boundary. Every
price in every order placement must go through ``normalize_price()`` —
no ``round(price, 2)`` allowed anywhere else (CI-enforced).

History
-------
2026-05-01: created in response to a production incident where
``broker_service.py`` rounded every submission to 2 decimals regardless
of venue. Crypto stops at $0.10984 became $0.11 (1.4% destructive shift).
Equity stops the brain produced at 4 decimals (CCCC 2.5898, CRDL 1.3326)
got rounded to 2 then flagged as invalid by Robinhood's downstream
validator. See ``docs/AUDITS/2026-05-01-trading-system-audit.md``.

Rules
-----

The decimal-precision rules below come from venue rulebooks, not policy
choices. They are correctness rules (the broker rejects orders that
violate them), not tunable knobs.

* **Equity, price ≥ $1** — 2 decimals (NMS Rule 612 sub-penny rule).
* **Equity, price < $1** — 4 decimals (NMS Rule 612 carve-out for
  sub-dollar prices). Robinhood enforces this on penny-stock submissions.
* **Crypto** — 8 decimals. Coinbase / Robinhood both accept up to 8;
  internally most pairs trade in much finer increments but 8 covers
  every realistic case.
* **Options, premium ≥ $3** — 2 decimals.
* **Options, premium < $3** — 1 decimal alignment to $0.05 grid (the
  "nickel" tier). OPRA tick rules — see Options Listing Procedures Plan.

If you need a non-standard tick (e.g. specific futures contract), add
the case here, not at the call site.

Quantities follow a parallel rule but are simpler: equity = whole shares
(or fractional with 6 decimals), crypto = 8 decimals.

API
---

The public surface is intentionally narrow:

* ``normalize_price(price, ticker, *, kind="price")`` — returns the
  tick-aligned price as a float. ``kind`` distinguishes "price" (round
  half-even, the typical case) from "stop" (round-down for buy-stops /
  round-up for sell-stops, conservative). For now both behave the same
  but the parameter is reserved for Phase 3 when stop-direction matters.
* ``normalize_quantity(qty, ticker)`` — returns the tick-aligned qty as
  a float.
* ``tick_size(ticker, price=None)`` — exposes the tick size for callers
  that want to compute their own bands (e.g., spread checks).

The functions never raise on bad input; they return the input unchanged
and log a warning. That's deliberate — the broker call layer must keep
flowing, and a precision warning is loud enough to surface as an
operational issue without breaking submission logic mid-incident.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation

logger = logging.getLogger(__name__)


# ── Venue detection ───────────────────────────────────────────────────


def _is_crypto(ticker: str) -> bool:
    """A ticker is treated as crypto if it ends in '-USD' and the
    pre-suffix part is not all digits.

    Pattern matches CHILI's existing convention (broker_manager.py:55).
    Examples: BTC-USD, DOGE-USD, AVAX-USD → True. SPY → False.
    A future-USD numeric ticker like '500-USD' would be False — extend if
    that ever becomes a real case.
    """
    t = (ticker or "").upper().strip()
    if not t.endswith("-USD"):
        return False
    base = t.replace("-USD", "")
    return bool(base) and not base.isdigit()


def _is_option_symbol(ticker: str) -> bool:
    """OCC option-symbol detection.

    OCC standard: 'AAPL241220C00150000' — root + YYMMDD + C/P + strike*1000
    padded to 8 digits. So an option symbol ends with C or P followed by
    8 digits. CHILI persists option symbols in this format when it touches
    them at all; the equity codepath does not mutate them.
    """
    t = (ticker or "").upper().strip()
    if len(t) < 16:
        return False
    return (t[-9] in "CP") and t[-8:].isdigit()


# ── Tick rules ────────────────────────────────────────────────────────


def tick_size(
    ticker: str,
    price: float | None = None,
    *,
    asset_class: str | None = None,
) -> Decimal:
    """Return the venue's minimum tick size as a Decimal.

    Most callers should leave ``asset_class`` at its default and let the
    helper infer venue from the ticker. Option call sites typically pass
    an underlying like 'AAPL' rather than an OCC symbol, so they should
    set ``asset_class='option'`` explicitly.

    For equities the tick depends on price (NMS Rule 612). Pass ``price``
    when you have it; if you pass None the function returns the larger
    tick ($0.01) which is safe for normalization.
    """
    cls = (asset_class or "").lower().strip()
    if cls == "crypto" or (not cls and _is_crypto(ticker)):
        return Decimal("0.00000001")  # 8 decimals
    if cls == "option" or (not cls and _is_option_symbol(ticker)):
        # OPRA tier rule. If we don't know the premium, use the wider tick.
        if price is not None and float(price) >= 3.0:
            return Decimal("0.01")
        return Decimal("0.05")  # nickel tier
    # Equity
    if price is not None and float(price) < 1.0:
        return Decimal("0.0001")  # NMS sub-dollar carve-out
    return Decimal("0.01")


def _quantize(value: Decimal, tick: Decimal, rounding=ROUND_HALF_EVEN) -> Decimal:
    """Quantize ``value`` to the nearest multiple of ``tick``.

    ``ROUND_HALF_EVEN`` (banker's rounding) is the default to avoid the
    statistical bias of always-round-up.  Callers that want a directional
    rounding (round-down for buy-stops to be more conservative) can pass
    ``ROUND_HALF_UP`` etc.
    """
    if tick == 0:
        return value
    quotient = (value / tick).quantize(Decimal("1"), rounding=rounding)
    return quotient * tick


# ── Public API ────────────────────────────────────────────────────────


def normalize_price(
    price: float | int | Decimal | str,
    ticker: str,
    *,
    kind: str = "price",
    asset_class: str | None = None,
) -> float:
    """Normalize ``price`` to the venue's tick size.

    Returns a Python float because the broker SDKs (robin_stocks,
    coinbase) take floats. Internally uses Decimal to avoid the
    Float-of-Doom problems that motivated this module.

    Parameters
    ----------
    price : float | int | Decimal | str
        The price to normalize. Strings are parsed (lets callers pass
        broker-returned strings without an extra cast).
    ticker : str
        The ticker — drives venue detection unless asset_class overrides.
    kind : str, optional
        Reserved for future use. Currently 'price', 'stop', 'limit'
        all behave identically. Phase 3 will introduce directional
        rounding for stops.
    asset_class : str, optional
        Explicit override: 'equity', 'crypto', or 'option'. Use when the
        ticker alone is ambiguous (option call sites typically pass the
        underlying like 'AAPL' rather than an OCC symbol).

    Returns
    -------
    float
        Tick-aligned price.
    """
    if price is None:
        return None  # type: ignore[return-value]
    try:
        d = Decimal(str(price))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning(
            "[tick_normalizer] could not parse price=%r ticker=%s — passing through",
            price, ticker,
        )
        try:
            return float(price)
        except Exception:
            return 0.0  # last-resort, caller will reject downstream

    tick = tick_size(ticker, float(d), asset_class=asset_class)
    aligned = _quantize(d, tick, ROUND_HALF_EVEN)
    return float(aligned)


def normalize_quantity(
    quantity: float | int | Decimal | str,
    ticker: str,
) -> float:
    """Normalize ``quantity`` to the venue's quantity tick.

    Equity supports whole-share or fractional (6-decimal) qty. Crypto
    supports 8 decimals. We default to 6 for equity (Robinhood's
    fractional-share precision) and 8 for crypto.
    """
    if quantity is None:
        return None  # type: ignore[return-value]
    try:
        d = Decimal(str(quantity))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning(
            "[tick_normalizer] could not parse qty=%r ticker=%s — passing through",
            quantity, ticker,
        )
        try:
            return float(quantity)
        except Exception:
            return 0.0

    if _is_crypto(ticker):
        tick = Decimal("0.00000001")
    elif _is_option_symbol(ticker):
        tick = Decimal("1")  # contracts
    else:
        # Equity — fractional precision
        tick = Decimal("0.000001")

    aligned = _quantize(d, tick, ROUND_HALF_UP)
    return float(aligned)


__all__ = [
    "normalize_price",
    "normalize_quantity",
    "tick_size",
]
