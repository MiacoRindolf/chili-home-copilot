"""Tests for the resilient multi-provider search backend + SSRF-safe fetcher.

Covers the salvaged-from-odysseus behavior contract:
  - provider cascade order honors config and always backstops to DuckDuckGo
  - keyed providers self-skip when unconfigured (zero behavior change by default)
  - first non-empty provider wins; failures fall through, never raise
  - SSRF guard blocks private/internal/non-http URLs and re-checks redirects
  - content fetcher extracts text and respects the bounded TTL cache
"""
import ipaddress
from unittest.mock import MagicMock, patch

import pytest

from app import search_providers as sp


# ---------------------------------------------------------------------------
# Provider cascade
# ---------------------------------------------------------------------------

class TestProviderOrder:
    def test_default_order_ends_with_ddg(self):
        with patch.object(sp.settings, "search_provider_order",
                          "searxng,brave,tavily,serper,google_pse,duckduckgo", create=True):
            order = sp.provider_order()
        assert order[-1] == "duckduckgo"
        assert order[0] == "searxng"

    def test_ddg_always_appended_if_missing(self):
        with patch.object(sp.settings, "search_provider_order", "brave", create=True):
            order = sp.provider_order()
        assert "duckduckgo" in order
        assert order[-1] == "duckduckgo"

    def test_unknown_providers_dropped(self):
        with patch.object(sp.settings, "search_provider_order", "bogus,brave", create=True):
            order = sp.provider_order()
        assert "bogus" not in order
        assert "brave" in order


class TestKeyedProvidersSelfSkip:
    """With no keys configured, every keyed provider returns [] without a call."""

    def test_brave_skips_without_key(self):
        with patch.object(sp.settings, "brave_api_key", "", create=True), \
             patch.dict("os.environ", {}, clear=False), \
             patch("app.search_providers.httpx.get") as mock_get:
            mock_get.side_effect = AssertionError("must not hit network without key")
            assert sp._brave_search("q", 5, None, "general") == []

    def test_tavily_skips_without_key(self):
        with patch.object(sp.settings, "tavily_api_key", "", create=True), \
             patch("app.search_providers.httpx.post") as mock_post:
            mock_post.side_effect = AssertionError("must not hit network without key")
            assert sp._tavily_search("q", 5, None, "general") == []

    def test_serper_skips_without_key(self):
        with patch.object(sp.settings, "serper_api_key", "", create=True), \
             patch("app.search_providers.httpx.post") as mock_post:
            mock_post.side_effect = AssertionError("must not hit network without key")
            assert sp._serper_search("q", 5, None, "general") == []

    def test_searxng_skips_without_url(self):
        with patch.object(sp.settings, "searxng_url", "", create=True), \
             patch("app.search_providers.httpx.get") as mock_get:
            mock_get.side_effect = AssertionError("must not hit network without url")
            assert sp._searxng_search("q", 5, None, "general") == []

    def test_google_pse_skips_without_key_or_cx(self):
        with patch.object(sp.settings, "google_pse_key", "", create=True), \
             patch.object(sp.settings, "google_pse_cx", "", create=True):
            assert sp._google_pse_search("q", 5, None, "general") == []


class TestResilientCascade:
    def test_first_nonempty_wins(self):
        good = [{"title": "t", "url": "https://e.com", "snippet": "s"}]
        with patch.object(sp, "provider_order", return_value=["brave", "duckduckgo"]), \
             patch.object(sp, "_brave_search", return_value=good) as mb, \
             patch.object(sp, "_duckduckgo_search") as md:
            out = sp.resilient_search("q", count=5)
        assert out == good
        mb.assert_called_once()
        md.assert_not_called()  # DDG never reached once brave produced results

    def test_falls_through_empty_to_next(self):
        good = [{"title": "t", "url": "https://e.com", "snippet": "s"}]
        with patch.object(sp, "provider_order", return_value=["brave", "duckduckgo"]), \
             patch.object(sp, "_brave_search", return_value=[]), \
             patch.object(sp, "_duckduckgo_search", return_value=good):
            out = sp.resilient_search("q", count=5)
        assert out == good

    def test_provider_exception_does_not_propagate(self):
        good = [{"title": "t", "url": "https://e.com", "snippet": "s"}]
        with patch.object(sp, "provider_order", return_value=["brave", "duckduckgo"]), \
             patch.object(sp, "_brave_search", side_effect=Exception("boom")), \
             patch.object(sp, "_duckduckgo_search", return_value=good):
            out = sp.resilient_search("q", count=5)
        assert out == good

    def test_all_empty_returns_empty(self):
        with patch.object(sp, "provider_order", return_value=["duckduckgo"]), \
             patch.object(sp, "_duckduckgo_search", return_value=[]):
            assert sp.resilient_search("q") == []

    def test_blank_query_short_circuits(self):
        assert sp.resilient_search("   ") == []

    def test_count_caps_results(self):
        many = [{"title": str(i), "url": f"https://e.com/{i}", "snippet": ""} for i in range(10)]
        with patch.object(sp, "provider_order", return_value=["duckduckgo"]), \
             patch.object(sp, "_duckduckgo_search", return_value=many):
            out = sp.resilient_search("q", count=3)
        assert len(out) == 3


class TestDuckDuckGoProvider:
    def test_normalizes_text_results(self):
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = [
            {"title": "T1", "href": "https://a.com", "body": "B1"},
        ]
        with patch("ddgs.DDGS", return_value=mock_ddgs):
            out = sp._duckduckgo_search("q", 5, None, "general")
        assert out[0]["title"] == "T1"
        assert out[0]["url"] == "https://a.com"
        assert out[0]["snippet"] == "B1"

    def test_error_returns_empty(self):
        with patch("ddgs.DDGS", side_effect=Exception("rate limited")):
            assert sp._duckduckgo_search("q", 5, None, "general") == []


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

class TestSSRFGuard:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/admin",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "https://foo.internal/secret",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "http://metadata.google.internal/",
    ])
    def test_blocks_unsafe(self, url):
        assert sp._public_http_url(url) is False

    def test_allows_public_literal_ip(self):
        assert sp._public_http_url("http://8.8.8.8/") is True

    def test_allows_public_hostname(self):
        with patch.object(sp, "_resolve_hostname_ips",
                          return_value=[ipaddress.ip_address("93.184.216.34")]):
            assert sp._public_http_url("https://example.com/page") is True

    def test_blocks_hostname_resolving_private(self):
        with patch.object(sp, "_resolve_hostname_ips",
                          return_value=[ipaddress.ip_address("10.1.2.3")]):
            assert sp._public_http_url("https://evil.example/") is False

    def test_redirect_to_private_is_blocked(self):
        # First hop is public and 302s to a private host; the fetcher must refuse.
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.headers = {"location": "http://169.254.169.254/"}
        redirect.url = "https://public.example/start"
        with patch.object(sp, "_public_http_url", side_effect=[True, False]), \
             patch("app.search_providers.httpx.get", return_value=redirect):
            with pytest.raises(Exception):
                sp._get_public_url("https://public.example/start", {}, 5)


# ---------------------------------------------------------------------------
# Content fetcher + cache
# ---------------------------------------------------------------------------

class TestFetchWebpageContent:
    def setup_method(self):
        sp._CONTENT_CACHE.clear()

    def _resp(self, text, ctype="text/html"):
        r = MagicMock()
        r.status_code = 200
        r.headers = {"Content-Type": ctype}
        r.text = text
        r.raise_for_status = MagicMock()
        return r

    def test_extracts_title_and_text(self):
        html = "<html><head><title>Hello</title></head><body><article>World body text</article></body></html>"
        with patch.object(sp, "_get_public_url", return_value=self._resp(html)):
            out = sp.fetch_webpage_content("https://example.com/x")
        assert out["success"] is True
        assert "Hello" in out["title"]
        assert "World body text" in out["content"]

    def test_blocked_url_returns_failure(self):
        out = sp.fetch_webpage_content("http://127.0.0.1/secret")
        assert out["success"] is False
        assert out["content"] == ""

    def test_network_error_returns_failure(self):
        with patch.object(sp, "_get_public_url", side_effect=Exception("dns fail")):
            out = sp.fetch_webpage_content("https://example.com/x")
        assert out["success"] is False
        assert "dns fail" in out["error"]

    def test_cache_hit_avoids_second_fetch(self):
        html = "<html><head><title>Cached</title></head><body><p>cached content here</p></body></html>"
        with patch.object(sp, "_get_public_url", return_value=self._resp(html)) as mock_fetch:
            first = sp.fetch_webpage_content("https://example.com/cached")
            second = sp.fetch_webpage_content("https://example.com/cached")
        assert first["title"] == second["title"] == "Cached"
        mock_fetch.assert_called_once()  # second call served from cache

    def test_cache_respects_max_size(self):
        sp._CONTENT_CACHE.clear()
        # Fill beyond max; oldest should be evicted, size never exceeds max.
        for i in range(sp._CONTENT_CACHE_MAX + 10):
            sp._cache_put(f"https://e.com/{i}", {"url": f"https://e.com/{i}", "success": True})
        assert len(sp._CONTENT_CACHE) <= sp._CONTENT_CACHE_MAX

    def test_max_chars_truncation(self):
        big = "x" * 50000
        html = f"<html><body><article>{big}</article></body></html>"
        with patch.object(sp, "_get_public_url", return_value=self._resp(html)):
            out = sp.fetch_webpage_content("https://example.com/big", max_chars=1000)
        assert len(out["content"]) <= 1000
