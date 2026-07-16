"""News-catalyst selection boost (Ross sympathy/theme ignition).

Ross's biggest plays (the +1000-3500% movers, e.g. a low-float small-cap that 10x'd
on a 'SpaceX synergies' headline) are ignited by a FRESH news HEADLINE, not just
scheduled earnings. The momentum lane's catalyst tilt was earnings-only; this adds a
fresh general-news source and unions it into the catalyst set so the viability tilt
prefers explosive movers that ALSO just printed news.
"""

from __future__ import annotations

import app.services.trading.momentum_neural.catalyst as cat
from app.config import settings
from app.services.trading.momentum_neural.risk_policy import catalyst_conviction_size_multiplier


def test_news_catalyst_normalizes_and_dedupes(monkeypatch):
    # equities are bare tickers; a -USD pair (crypto, no news catalyst) normalizes to
    # its base. lower-case + whitespace are normalized.
    monkeypatch.setattr(
        "app.services.massive_client.get_recent_news_tickers",
        lambda **_: ["mrvl", "ASTC ", "RVSN", "rvsn"],
        raising=True,
    )
    syms = cat.news_catalyst_symbols()
    assert syms == {"MRVL", "ASTC", "RVSN"}


def test_news_catalyst_fail_open_on_error(monkeypatch):
    def _boom(**_):
        raise RuntimeError("news feed down")

    monkeypatch.setattr("app.services.massive_client.get_recent_news_tickers", _boom, raising=True)
    assert cat.news_catalyst_symbols() == set()  # no boost, no crash


def test_all_catalyst_is_union_of_earnings_and_news(monkeypatch):
    monkeypatch.setattr(cat, "earnings_catalyst_symbols", lambda: {"AAPL", "MU"})
    monkeypatch.setattr(cat, "news_catalyst_symbols", lambda: {"MRVL", "MU"})  # MU overlaps
    assert cat.all_catalyst_symbols() == {"AAPL", "MU", "MRVL"}


def test_all_catalyst_survives_one_feed_failing(monkeypatch):
    # earnings raises internally -> earnings_catalyst_symbols returns set() (fail-open);
    # news still contributes. The union must not be empty just because one feed died.
    monkeypatch.setattr(cat, "earnings_catalyst_symbols", lambda: set())
    monkeypatch.setattr(cat, "news_catalyst_symbols", lambda: {"RVSN", "ASTC"})
    assert cat.all_catalyst_symbols() == {"RVSN", "ASTC"}


def test_news_catalyst_max_age_default():
    # Preserve the pre-registry contract: adding a first-class Settings row must
    # not silently shrink the catalyst window.
    assert cat._news_catalyst_max_age_min() == cat.NEWS_CATALYST_MAX_AGE_MIN == 120
    assert settings.chili_momentum_news_catalyst_max_age_min == 120.0


def test_news_catalyst_name_gets_viability_boost():
    # a name in the catalyst set gets a positive viability delta; an unrelated name 0;
    # crypto (-USD) is always neutral (no news catalyst). This is the tilt the lane uses.
    cat_syms = {"MRVL"}
    assert cat.catalyst_viability_delta("MRVL", cat_syms) > 0
    assert cat.catalyst_viability_delta("ZZZZ", cat_syms) == 0.0
    assert cat.catalyst_viability_delta("BTC-USD", cat_syms) == 0.0


def test_catalyst_grade_rank_strong_weak_fake_dominance(monkeypatch):
    assert cat.catalyst_grade_rank("RVSN", strong_symbols={"RVSN"}) == 3
    assert cat.catalyst_grade_rank("RVSN", strong_symbols={"RVSN"}, weak_symbols={"RVSN"}) == 0
    assert cat.catalyst_grade_rank("RVSN", strong_symbols={"RVSN"}, fake_symbols={"RVSN"}) == 0
    monkeypatch.setattr(cat, "all_catalyst_symbols", lambda: {"ASTC"})
    assert cat.catalyst_grade_rank("ASTC") == 0
    assert cat.catalyst_grade_rank("BTC-USD", strong_symbols={"BTC"}) == 0


def test_catalyst_conviction_size_multiplier_uses_grade_rank_without_hard_block(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_step", 0.1)
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_max_multiplier", 1.5)

    strong_mult, strong_dbg = catalyst_conviction_size_multiplier("RVSN", strong_symbols={"RVSN"})
    weak_mult, weak_dbg = catalyst_conviction_size_multiplier("RVSN", strong_symbols={"RVSN"}, weak_symbols={"RVSN"})

    assert strong_mult == 1.3
    assert strong_dbg["grade_rank"] == 3
    assert weak_mult == 1.0
    assert weak_dbg["grade_rank"] == 0
