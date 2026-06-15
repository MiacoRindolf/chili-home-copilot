"""scan_premarket_gaps output-cap: adaptive when an already-screened universe is passed.

ROOT-CAUSE REGRESSION (2026-06-15, QUCY): the equity Ross momentum lane hands its
already-SCREENED universe (price / $-volume / change-floored, ≤max_universe) to the
premarket-gap scan, but the scan re-truncated that curated pool to a FIXED top-15 by
RAW gap magnitude. A fresh-catalyst mid-gap runner (low-float +7%-+10% name on an 8AM
catalyst — QUCY: ~$1.9, +9.8%, ~22M float) ranked below 15 already-extended +50%-+200%
gappers and was dropped → it never became a "mover" → never got a fresh
``momentum_symbol_viability`` row → the auto-arm (which selects from FRESH viability
rows) could never arm it. These are exactly the runners the lane exists to catch.

The ``max_signals`` override lets the Ross-lane caller size the output cap to the
screened pool so EVERY screened gapper reaches the downstream Ross percentile re-rank
(which makes the real selection). Default (None) keeps the historical fixed cap, so the
broad default-universe alert sweep is byte-identical. Pure: ``fetch_quote`` mocked, no
DB / no network. docs/DESIGN/MOMENTUM_LANE.md
"""

from app.services.trading import intraday_signals
from app.services.trading.intraday_signals import (
    PREMARKET_GAP_MAX_SIGNALS,
    scan_premarket_gaps,
)


def _mk_quotes():
    """One fresh mid-gapper (QUCY-like) buried under PREMARKET_GAP_MAX_SIGNALS bigger
    gappers, so the fixed top-N cap truncates the mid-gapper out by raw magnitude.
    """
    quotes: dict[str, dict] = {}
    big = []
    # PREMARKET_GAP_MAX_SIGNALS huge gappers (+50% .. +200%), all above the mid-gapper.
    for i in range(PREMARKET_GAP_MAX_SIGNALS):
        t = f"BIG{i:02d}"
        gap = 200.0 - i  # 200, 199, ... distinct, all >> the mid-gapper
        prev = 1.00
        quotes[t] = {"price": prev * (1 + gap / 100.0), "previous_close": prev}
        big.append(t)
    # The QUCY-like fresh mid-gap runner: +9.8%, well above the 3% floor but below
    # every BIG name — it is the cap victim.
    quotes["QUCY"] = {"price": 1.90, "previous_close": 1.73}  # +9.8%
    return quotes, big


def _patch_fetch_quote(monkeypatch, quotes):
    from app.services.trading import market_data

    monkeypatch.setattr(market_data, "fetch_quote", lambda t: quotes.get(t))


def test_fixed_cap_truncates_the_fresh_mid_gapper(monkeypatch):
    """Default behavior (max_signals=None): the mid-gapper is dropped by the fixed cap.

    Locks in the BUG so the fix is a real, measured delta — and so the default-universe
    sweep behavior cannot silently change.
    """
    quotes, big = _mk_quotes()
    _patch_fetch_quote(monkeypatch, quotes)
    tickers = big + ["QUCY"]

    out = scan_premarket_gaps(tickers=tickers)  # default cap (historical behavior)
    syms = {r["ticker"] for r in out}

    assert len(out) == PREMARKET_GAP_MAX_SIGNALS
    assert "QUCY" not in syms  # the fresh mid-gapper is truncated out


def test_adaptive_cap_keeps_every_screened_gapper(monkeypatch):
    """With max_signals sized to the screened pool, the mid-gapper survives."""
    quotes, big = _mk_quotes()
    _patch_fetch_quote(monkeypatch, quotes)
    tickers = big + ["QUCY"]

    out = scan_premarket_gaps(tickers=tickers, max_signals=len(tickers))
    syms = {r["ticker"] for r in out}

    assert "QUCY" in syms  # the fresh-catalyst mid-gap runner now reaches viability
    assert len(out) == len(tickers)  # every screened gapper is kept


def test_default_universe_sweep_is_byte_identical(monkeypatch):
    """max_signals=None must reproduce the exact historical fixed-cap output."""
    quotes, big = _mk_quotes()
    _patch_fetch_quote(monkeypatch, quotes)
    tickers = big + ["QUCY"]

    none_cap = scan_premarket_gaps(tickers=tickers, max_signals=None)
    legacy = scan_premarket_gaps(tickers=tickers)  # no kwarg at all

    assert none_cap == legacy
    assert len(none_cap) == PREMARKET_GAP_MAX_SIGNALS


def test_adaptive_cap_still_respects_min_gap_floor(monkeypatch):
    """A larger cap must NOT admit sub-floor noise — only true gappers survive."""
    quotes = {
        "GAP": {"price": 1.10, "previous_close": 1.00},   # +10% -> keep
        "FLAT": {"price": 1.01, "previous_close": 1.00},  # +1% below 3% floor -> drop
    }
    _patch_fetch_quote(monkeypatch, quotes)

    out = scan_premarket_gaps(tickers=["GAP", "FLAT"], max_signals=50)
    syms = {r["ticker"] for r in out}

    assert "GAP" in syms
    assert "FLAT" not in syms


def test_max_signals_zero_or_negative_is_clamped(monkeypatch):
    """Defensive: a non-positive override clamps to 1, never an empty/raising slice."""
    quotes, big = _mk_quotes()
    _patch_fetch_quote(monkeypatch, quotes)

    out = scan_premarket_gaps(tickers=big + ["QUCY"], max_signals=0)
    assert len(out) == 1  # clamped to a single (largest) gapper, not 0
