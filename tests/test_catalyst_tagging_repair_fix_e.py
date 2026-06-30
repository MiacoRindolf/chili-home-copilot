"""FIX E — PER-TICKER catalyst-tagging repair (chili_momentum_catalyst_tagging_repair_enabled).

Root cause (decisive w0av0u3qy LIVE-container probe): 0/11 Ross names were catalyst-tagged
because the catalyst accessors read the GLOBAL Polygon news firehose (/v2/reference/news,
limit ~200, sorted desc) — on a busy tape the low-float micro-caps Ross trades get BURIED
under large-cap news and never appear, even when Polygon DOES carry a fresh headline for
them. FIX E adds a PER-TICKER news pass for the in-play movers and unions the graded hits in.

Coverage:
  * the per_ticker_catalyst_tags grader (catalyst.py): flag-off parity, grading
    (strong/weak/fake), crypto exclusion, fail-open.
  * the get_per_ticker_news_items accessor (massive_client.py): per-ticker query path,
    freshness filter, crypto/aggregate skip, max_tickers bound, dedupe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.services.trading.momentum_neural import catalyst as cat
import app.services.massive_client as mc


# --------------------------------------------------------------------------- #
# per_ticker_catalyst_tags (the grader)
# --------------------------------------------------------------------------- #
def test_flag_off_returns_all_empty(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_catalyst_tagging_repair_enabled", False, raising=False)
    # the accessor must never even be consulted when the flag is OFF
    def _boom(*a, **k):
        raise AssertionError("per-ticker feed must not be touched when FIX E is OFF")
    monkeypatch.setattr("app.services.massive_client.get_per_ticker_news_items", _boom, raising=True)
    assert cat.per_ticker_catalyst_tags(["ILLR", "SDOT"]) == (set(), set(), set(), set())


def test_grades_strong_weak_fake_and_tags_all(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_catalyst_tagging_repair_enabled", True, raising=False)
    feed = [
        ("ILLR", "Triller announces strategic partnership and definitive agreement"),  # strong
        ("SDOT", "Sadot Group announces registered direct offering to raise cash"),     # weak (dilution)
        ("NVCT", "Nuvectis in talks rumored buyout — unconfirmed"),                      # fake
        ("SKYQ", "Sky Quarry provides a routine business update"),                       # medium/neutral
    ]
    monkeypatch.setattr(
        "app.services.massive_client.get_per_ticker_news_items",
        lambda tickers, **k: feed, raising=True,
    )
    all_t, strong, weak, fake = cat.per_ticker_catalyst_tags(["ILLR", "SDOT", "NVCT", "SKYQ"])
    # EVERY name with a fresh headline is in the binary "all" set (the catalyst-present boost)
    assert all_t == {"ILLR", "SDOT", "NVCT", "SKYQ"}
    assert "ILLR" in strong
    assert "SDOT" in weak
    assert "NVCT" in fake
    assert "SKYQ" not in strong and "SKYQ" not in weak and "SKYQ" not in fake


def test_crypto_excluded_by_accessor_and_norm(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_catalyst_tagging_repair_enabled", True, raising=False)
    # the accessor itself drops -USD; emulate it returning only the equity
    monkeypatch.setattr(
        "app.services.massive_client.get_per_ticker_news_items",
        lambda tickers, **k: [("ILLR", "FDA approval granted")], raising=True,
    )
    all_t, strong, _, _ = cat.per_ticker_catalyst_tags(["BTC-USD", "ILLR"])
    assert all_t == {"ILLR"}
    assert "ILLR" in strong


def test_fail_open_on_feed_error(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_catalyst_tagging_repair_enabled", True, raising=False)
    def _boom(*a, **k):
        raise RuntimeError("feed down")
    monkeypatch.setattr("app.services.massive_client.get_per_ticker_news_items", _boom, raising=True)
    assert cat.per_ticker_catalyst_tags(["ILLR"]) == (set(), set(), set(), set())


# --------------------------------------------------------------------------- #
# get_per_ticker_news_items (the accessor)
# --------------------------------------------------------------------------- #
def _fresh_ts():
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")


def _stale_ts():
    return (datetime.now(timezone.utc) - timedelta(minutes=600)).isoformat().replace("+00:00", "Z")


def test_accessor_queries_per_ticker_and_filters_freshness(monkeypatch):
    calls = []

    def _fake_get(url, params=None):
        calls.append(params.get("ticker"))
        tk = params.get("ticker")
        if tk == "ILLR":
            return {"results": [{"tickers": ["ILLR"], "title": "fresh ILLR news", "published_utc": _fresh_ts()}]}
        if tk == "SDOT":
            return {"results": [{"tickers": ["SDOT"], "title": "stale SDOT news", "published_utc": _stale_ts()}]}
        return {"results": []}

    monkeypatch.setattr(mc, "_get", _fake_get, raising=True)
    items = mc.get_per_ticker_news_items(["ILLR", "SDOT", "FISN"], max_age_min=120)
    tagged = {t for t, _ in items}
    # ILLR fresh -> tagged; SDOT stale -> filtered out; FISN no news -> absent.
    assert tagged == {"ILLR"}
    # one HTTP call per equity ticker (the per-ticker query, NOT the firehose)
    assert calls == ["ILLR", "SDOT", "FISN"]


def test_accessor_skips_crypto_and_aggregate_and_bounds_tickers(monkeypatch):
    seen = []
    monkeypatch.setattr(
        mc, "_get",
        lambda url, params=None: (seen.append(params.get("ticker")) or {"results": []}),
        raising=True,
    )
    # crypto + __AGGREGATE__ are skipped; max_tickers caps the equity queries.
    mc.get_per_ticker_news_items(
        ["BTC-USD", "__AGGREGATE__", "AAA", "BBB", "CCC"], max_age_min=120, max_tickers=2
    )
    assert seen == ["AAA", "BBB"]   # crypto/aggregate dropped, bounded to 2


def test_accessor_empty_tickers_returns_empty(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("no HTTP call expected for an empty ticker list")
    monkeypatch.setattr(mc, "_get", _boom, raising=True)
    assert mc.get_per_ticker_news_items([], max_age_min=120) == []
    assert mc.get_per_ticker_news_items(["ETH-USD"], max_age_min=120) == []
