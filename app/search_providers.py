"""Resilient multi-provider web/news search + SSRF-safe page-content fetching.

This module gives CHILI's research paths a provider cascade instead of a single
DuckDuckGo backend, plus the ability to read full page content (not just search
snippets). It is **additive and safe by default**: every paid/keyed provider
self-skips when its key (or SearXNG URL) is unset, so with no configuration the
effective provider is DuckDuckGo — byte-for-byte today's behavior. The moment an
operator sets a key in config/env, that provider lights up ahead of DDG and
spares the DDG rate limit.

Salvaged and adapted (MIT) from the `odysseus` project
(https://github.com/pewdiepie-archdaemon/odysseus), `services/search/`. odysseus
in turn adapts patterns from Tongyi DeepResearch (Apache-2.0). odysseus-specific
coupling (its settings/cache/analytics/constants modules) has been removed and
replaced with CHILI's `config.settings`, `logging`, and a bounded in-process TTL
cache per CLAUDE.md ("caches must have hard max size + TTL").

Public surface:
    resilient_search(query, count, time_filter, categories) -> list[dict]
        Normalized results: [{"title", "url", "snippet", "age"}], first non-empty
        provider in the configured order wins; DuckDuckGo is the final backstop.
    fetch_webpage_content(url, timeout) -> dict
        SSRF-hardened fetch + HTML/PDF text extraction with TTL caching.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# BeautifulSoup is installed in chili-env and declared in requirements, but guard
# the import so a stripped environment degrades to regex extraction instead of
# crashing the research path.
try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAS_BS4 = True
except Exception:  # pragma: no cover - defensive
    BeautifulSoup = None  # type: ignore
    _HAS_BS4 = False

# PDF extraction is an optional dependency (pdfminer.six is not in requirements).
try:
    from pdfminer.high_level import extract_text as _pdf_extract_text  # type: ignore
except Exception:
    _pdf_extract_text = None  # type: ignore


class RateLimitError(Exception):
    """Raised internally when a provider/page returns HTTP 429."""


# Provider registry — value -> (label, needs_key, needs_url)
PROVIDER_INFO = {
    "searxng": ("SearXNG", False, True),
    "brave": ("Brave Search", True, False),
    "duckduckgo": ("DuckDuckGo", False, False),
    "google_pse": ("Google PSE", True, False),
    "tavily": ("Tavily", True, False),
    "serper": ("Serper", True, False),
}

_DEFAULT_ORDER = "searxng,brave,tavily,serper,google_pse,duckduckgo"
_REQUEST_TIMEOUT = 20
_NEWS_HINTS = ("news", "headlines", "breaking", "latest", "today")


# ---------------------------------------------------------------------------
# Config helpers (all read live so env/admin changes take effect without reload)
# ---------------------------------------------------------------------------

def _cfg(name: str, default=""):
    return getattr(settings, name, default)


def _request_timeout() -> int:
    try:
        return int(_cfg("search_request_timeout", _REQUEST_TIMEOUT) or _REQUEST_TIMEOUT)
    except (TypeError, ValueError):
        return _REQUEST_TIMEOUT


def provider_order() -> List[str]:
    """Configured provider cascade. Unknown names are dropped; DDG always last."""
    raw = (_cfg("search_provider_order", _DEFAULT_ORDER) or _DEFAULT_ORDER).strip()
    order = [p.strip().lower() for p in raw.split(",") if p.strip()]
    order = [p for p in order if p in PROVIDER_INFO]
    if "duckduckgo" not in order:
        order.append("duckduckgo")  # always keep the free backstop
    return order


def build_enhanced_query(query: str, time_filter: Optional[str]) -> str:
    """Lightly bias the query toward recency for providers without a freshness param."""
    if time_filter in ("day", "week"):
        return f"{query} latest"
    return query


# ---------------------------------------------------------------------------
# Providers — each returns [{title, url, snippet, age?}] or [] (never raises)
# ---------------------------------------------------------------------------

def _searxng_search(query: str, count: int, time_filter: Optional[str], categories: str) -> List[dict]:
    instance = (_cfg("searxng_url", "") or "").strip().rstrip("/")
    if not instance:
        return []  # no instance configured -> skip silently
    params = {"q": query, "format": "json", "language": "en"}
    q_lc = query.lower()
    is_news = time_filter is not None or categories == "news" or any(h in q_lc for h in _NEWS_HINTS)
    if is_news:
        params["categories"] = "news"
        if time_filter in ("day", "week", "month", "year"):
            params["time_range"] = "week" if time_filter in ("day", "week") else time_filter
    else:
        params["categories"] = "general"
    try:
        resp = httpx.get(f"{instance}/search", params=params,
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        out = [
            {"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("content", ""), "age": r.get("publishedDate", "")}
            for r in data.get("results", [])[:count] if r.get("url")
        ]
        logger.info("[search_providers] searxng returned %d for %r", len(out), query)
        return out
    except Exception as e:
        logger.warning("[search_providers] searxng failed: %s", e)
        return []


def _brave_search(query: str, count: int, time_filter: Optional[str], categories: str) -> List[dict]:
    api_key = (_cfg("brave_api_key", "") or os.environ.get("DATA_BRAVE_API_KEY", "")).strip()
    if not api_key:
        return []
    params = {"q": build_enhanced_query(query, time_filter), "count": count}
    if time_filter in ("day", "week", "month", "year"):
        params["freshness"] = time_filter
    try:
        resp = httpx.get("https://api.search.brave.com/res/v1/web/search",
                         headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
                         params=params, timeout=_request_timeout())
        if resp.status_code == 429:
            raise RateLimitError("brave 429")
        resp.raise_for_status()
        data = resp.json()
        out = []
        for item in data.get("web", {}).get("results", [])[:count]:
            if not item.get("url"):
                continue
            out.append({"title": item.get("title", ""), "url": item["url"],
                        "snippet": item.get("description", "") or item.get("content", ""),
                        "age": item.get("age", "") or item.get("date", "")})
        logger.info("[search_providers] brave returned %d for %r", len(out), query)
        return out
    except Exception as e:
        logger.warning("[search_providers] brave failed: %s", e)
        return []


def _tavily_search(query: str, count: int, time_filter: Optional[str], categories: str) -> List[dict]:
    api_key = (_cfg("tavily_api_key", "") or os.environ.get("TAVILY_API_KEY", "")).strip()
    if not api_key:
        return []
    payload = {"query": query, "max_results": count, "include_answer": False}
    if time_filter in ("day", "week", "month", "year"):
        payload["days"] = {"day": 1, "week": 7, "month": 30, "year": 365}[time_filter]
    try:
        resp = httpx.post("https://api.tavily.com/search", json=payload,
                          headers={"Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"}, timeout=_request_timeout())
        if resp.status_code == 429:
            raise RateLimitError("tavily 429")
        resp.raise_for_status()
        data = resp.json()
        out = [
            {"title": i.get("title", ""), "url": i.get("url", ""),
             "snippet": i.get("content", ""), "age": i.get("published_date", "")}
            for i in data.get("results", [])[:count] if i.get("url")
        ]
        logger.info("[search_providers] tavily returned %d for %r", len(out), query)
        return out
    except Exception as e:
        logger.warning("[search_providers] tavily failed: %s", e)
        return []


def _serper_search(query: str, count: int, time_filter: Optional[str], categories: str) -> List[dict]:
    api_key = (_cfg("serper_api_key", "") or os.environ.get("SERPER_API_KEY", "")).strip()
    if not api_key:
        return []
    payload = {"q": query, "num": count}
    if time_filter in ("day", "week", "month", "year"):
        payload["tbs"] = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}[time_filter]
    endpoint = "https://google.serper.dev/news" if categories == "news" else "https://google.serper.dev/search"
    try:
        resp = httpx.post(endpoint, json=payload,
                          headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                          timeout=_request_timeout())
        if resp.status_code == 429:
            raise RateLimitError("serper 429")
        resp.raise_for_status()
        data = resp.json()
        items = data.get("news") if categories == "news" else data.get("organic")
        out = []
        for item in (items or [])[:count]:
            url = item.get("link", "")
            if not url:
                continue
            out.append({"title": item.get("title", ""), "url": url,
                        "snippet": item.get("snippet", ""), "age": item.get("date", "")})
        logger.info("[search_providers] serper returned %d for %r", len(out), query)
        return out
    except Exception as e:
        logger.warning("[search_providers] serper failed: %s", e)
        return []


def _google_pse_search(query: str, count: int, time_filter: Optional[str], categories: str) -> List[dict]:
    api_key = (_cfg("google_pse_key", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
    cx = (_cfg("google_pse_cx", "") or os.environ.get("GOOGLE_PSE_CX", "")).strip()
    if not api_key or not cx:
        return []
    params = {"key": api_key, "cx": cx, "q": query, "num": min(count, 10)}
    if time_filter in ("day", "week", "month", "year"):
        params["dateRestrict"] = {"day": "d1", "week": "w1", "month": "m1", "year": "y1"}[time_filter]
    try:
        resp = httpx.get("https://www.googleapis.com/customsearch/v1", params=params,
                         timeout=_request_timeout())
        if resp.status_code == 429:
            raise RateLimitError("google_pse 429")
        resp.raise_for_status()
        data = resp.json()
        out = [
            {"title": i.get("title", ""), "url": i.get("link", ""),
             "snippet": i.get("snippet", ""), "age": ""}
            for i in data.get("items", [])[:count] if i.get("link")
        ]
        logger.info("[search_providers] google_pse returned %d for %r", len(out), query)
        return out
    except Exception as e:
        logger.warning("[search_providers] google_pse failed: %s", e)
        return []


def _duckduckgo_search(query: str, count: int, time_filter: Optional[str], categories: str) -> List[dict]:
    """Free backstop using CHILI's existing `ddgs` dependency. No API key."""
    timelimit = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(time_filter or "")
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            if categories == "news":
                raw = list(ddgs.news(query, max_results=count))
            else:
                raw = list(ddgs.text(query, max_results=count, timelimit=timelimit) if timelimit
                           else ddgs.text(query, max_results=count))
        out = []
        for item in raw:
            url = item.get("href") or item.get("url") or ""
            if not url:
                continue
            out.append({
                "title": item.get("title", ""),
                "url": url,
                "snippet": item.get("body") or item.get("excerpt") or item.get("snippet") or "",
                "age": item.get("date", ""),
                "publisher": item.get("source") or item.get("publisher") or "",
            })
        logger.info("[search_providers] duckduckgo returned %d for %r", len(out), query)
        return out
    except Exception as e:
        logger.warning("[search_providers] duckduckgo failed: %s", e)
        return []


# Map provider name -> function attribute name. Resolved dynamically in
# resilient_search() via globals() (not frozen references) so individual
# providers stay monkeypatchable in tests and at runtime.
_PROVIDER_FUNCS = {
    "searxng": "_searxng_search",
    "brave": "_brave_search",
    "tavily": "_tavily_search",
    "serper": "_serper_search",
    "google_pse": "_google_pse_search",
    "duckduckgo": "_duckduckgo_search",
}


def _normalize_url(url: str) -> str:
    """Normalize a URL for de-duplication.

    Strips scheme, a leading "www." on the host, a trailing slash on the path,
    and lowercases the host. Path/query/fragment casing is preserved (only the
    host is case-insensitive per RFC 3986). Returns the raw input lowercased if
    it cannot be parsed, so dedupe degrades gracefully rather than raising.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            # No recognizable host (e.g. a bare path) — fall back to a lowered,
            # de-slashed form of the whole string.
            return raw.lower().rstrip("/")
        netloc = host
        if parsed.port:
            netloc = f"{host}:{parsed.port}"
        path = parsed.path.rstrip("/")
        rest = path
        if parsed.query:
            rest += f"?{parsed.query}"
        if parsed.fragment:
            rest += f"#{parsed.fragment}"
        return f"{netloc}{rest}"
    except Exception:  # pragma: no cover - defensive
        return raw.lower().rstrip("/")


def _dedupe_results(results: List[dict]) -> List[dict]:
    """Remove duplicate results by normalized URL, keeping first occurrence.

    Preserves input order. Results without a usable URL are passed through
    unchanged (they cannot be deduped against).
    """
    seen: set = set()
    out: List[dict] = []
    for r in results:
        key = _normalize_url(r.get("url", "") if isinstance(r, dict) else "")
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


def resilient_search(query: str, count: int = 5, time_filter: Optional[str] = None,
                     categories: str = "general") -> List[dict]:
    """Try configured providers in order; return the first non-empty result set.

    Normalized result dicts: {"title", "url", "snippet", "age", "publisher"?}.
    Never raises — returns [] if every provider fails or is unconfigured.
    """
    query = (query or "").strip()
    if not query:
        return []
    for name in provider_order():
        func = globals().get(_PROVIDER_FUNCS.get(name, ""))
        if not func:
            continue
        try:
            results = func(query, count, time_filter, categories)
        except Exception as e:  # pragma: no cover - providers already guard, belt-and-suspenders
            logger.warning("[search_providers] provider %s raised: %s", name, e)
            results = []
        if results:
            # Dedupe by normalized URL BEFORE capping so duplicates don't eat
            # into the count budget.
            return _dedupe_results(results)[:count]
    return []


# ---------------------------------------------------------------------------
# SSRF-safe page-content fetching (salvaged from odysseus services/search/content)
# ---------------------------------------------------------------------------

_PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_private_address(addr: ipaddress._BaseAddress) -> bool:
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or any(addr in net for net in _PRIVATE_NETWORKS))


def _resolve_hostname_ips(hostname: str) -> List[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return []
    out: List[ipaddress._BaseAddress] = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except Exception:
            continue
    return out


def _public_http_url(url: str) -> bool:
    """True only if url is http(s) and resolves to a public IP (SSRF guard)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").strip()
        if not host:
            return False
        lower = host.lower()
        if lower in ("localhost", "metadata", "metadata.google.internal"):
            return False
        if lower.endswith((".local", ".localhost", ".internal", ".lan", ".intranet")):
            return False
        try:
            return not _is_private_address(ipaddress.ip_address(host))
        except ValueError:
            pass
        addrs = _resolve_hostname_ips(host)
        return bool(addrs) and not any(_is_private_address(a) for a in addrs)
    except Exception:
        return False


def _get_public_url(url: str, headers: dict, timeout: int, max_redirects: int = 5) -> httpx.Response:
    """GET following redirects manually, re-validating SSRF safety on every hop."""
    current = url
    for _ in range(max_redirects + 1):
        if not _public_http_url(current):
            raise httpx.RequestError("Blocked private/internal URL",
                                     request=httpx.Request("GET", current))
        response = httpx.get(current, headers=headers, timeout=timeout, follow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current = urljoin(str(response.url), location)
    raise httpx.RequestError("Too many redirects", request=httpx.Request("GET", current))


# Bounded in-process TTL cache (CLAUDE.md: hard max size + TTL).
_CONTENT_CACHE: "dict[str, tuple[float, dict]]" = {}
_CONTENT_CACHE_LOCK = threading.Lock()
_CONTENT_CACHE_MAX = 256


def _cache_ttl() -> int:
    try:
        return int(_cfg("search_content_cache_ttl_sec", 1800) or 1800)
    except (TypeError, ValueError):
        return 1800


def _cache_get(url: str) -> Optional[dict]:
    with _CONTENT_CACHE_LOCK:
        entry = _CONTENT_CACHE.get(url)
        if not entry:
            return None
        ts, data = entry
        if time.time() - ts > _cache_ttl():
            _CONTENT_CACHE.pop(url, None)
            return None
        return data


def _cache_put(url: str, data: dict) -> None:
    with _CONTENT_CACHE_LOCK:
        if len(_CONTENT_CACHE) >= _CONTENT_CACHE_MAX:
            # Evict the oldest entry.
            oldest = min(_CONTENT_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CONTENT_CACHE.pop(oldest, None)
        _CONTENT_CACHE[url] = (time.time(), data)


def _empty_content(url: str, error: str = "") -> dict:
    return {"url": url, "title": "", "content": "", "meta_description": "",
            "success": False, "error": error}


def _extract_text_regex(html: str) -> tuple[str, str]:
    """Fallback HTML→(title, text) extraction when bs4 is unavailable."""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return title, body


def fetch_webpage_content(url: str, timeout: int = 6, max_chars: int = 8000) -> dict:
    """Fetch a public URL and extract readable text. SSRF-hardened, TTL-cached.

    Returns {"url", "title", "content", "meta_description", "success", "error"}.
    Never raises; on any failure returns a dict with success=False.
    """
    cached = _cache_get(url)
    if cached is not None:
        return cached

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        response = _get_public_url(url, headers=headers, timeout=timeout)
        if response.status_code == 429:
            raise RateLimitError(f"429 for {url}")
        response.raise_for_status()
    except RateLimitError as e:
        return _empty_content(url, str(e))
    except Exception as e:
        logger.warning("[search_providers] fetch failed %s: %s", url, e)
        return _empty_content(url, f"{type(e).__name__}: {e}")

    content_type = response.headers.get("Content-Type", "").lower()

    # PDF path (optional dependency).
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        text = ""
        if _pdf_extract_text is not None:
            try:
                import io
                text = _pdf_extract_text(io.BytesIO(response.content)) or ""
            except Exception as e:
                logger.warning("[search_providers] pdf extract failed %s: %s", url, e)
        result = {"url": url, "title": os.path.basename(urlparse(url).path),
                  "content": re.sub(r"\s+", " ", text).strip()[:max_chars],
                  "meta_description": "", "success": bool(text),
                  "error": "" if text else "pdf extraction unavailable"}
        _cache_put(url, result)
        return result

    # HTML path.
    html = response.text
    meta_desc = ""
    if _HAS_BS4:
        try:
            soup = BeautifulSoup(html, "html.parser")
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""
            desc_tag = soup.find("meta", attrs={"name": re.compile("description", re.I)})
            if desc_tag and desc_tag.get("content"):
                meta_desc = desc_tag["content"].strip()
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            areas = soup.find_all(["main", "article", "section", "div"],
                                  class_=re.compile("content|main|body|article|post|entry|text", re.I))
            main = " ".join(a.get_text(separator=" ", strip=True) for a in areas[:3])
            if not main:
                body = soup.find("body")
                main = body.get_text(separator=" ", strip=True) if body else ""
            content = re.sub(r"\s+", " ", main).strip()
        except Exception as e:
            logger.warning("[search_providers] bs4 parse failed %s: %s", url, e)
            title, content = _extract_text_regex(html)
    else:
        title, content = _extract_text_regex(html)

    result = {"url": url, "title": title, "content": content[:max_chars],
              "meta_description": meta_desc, "success": bool(content), "error": ""}
    _cache_put(url, result)
    return result


def fetch_many(urls: List[str], max_workers: int = 4, max_chars: int = 8000) -> List[dict]:
    """Fetch multiple URLs concurrently, reusing the SSRF-safe single fetcher.

    De-duplicates the input URLs (same normalization as `_dedupe_results`),
    fetches each remaining URL via `fetch_webpage_content` in a bounded thread
    pool, and returns one result dict per de-duped URL in first-seen order.
    Never raises — a failed fetch yields the standard failure dict.
    """
    # De-dupe while preserving first-seen order.
    seen: set = set()
    unique: List[str] = []
    for u in urls or []:
        key = _normalize_url(u)
        if key:
            if key in seen:
                continue
            seen.add(key)
        unique.append(u)

    if not unique:
        return []

    workers = min(max_workers, len(unique))
    workers = max(1, min(workers, 8))  # never exceed 8, never below 1

    def _one(u: str) -> dict:
        try:
            return fetch_webpage_content(u, max_chars=max_chars)
        except Exception as e:  # pragma: no cover - fetch_webpage_content already guards
            logger.warning("[search_providers] fetch_many failed %s: %s", u, e)
            return _empty_content(u, f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # executor.map preserves input order in its output.
        results = list(pool.map(_one, unique))
    logger.info("[search_providers] fetch_many fetched %d urls (%d ok)",
                len(results), sum(1 for r in results if r.get("success")))
    return results
