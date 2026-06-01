"""Web/news search with a resilient provider cascade.

Provides search()/news_search() for fetching results and format_results() for
producing a context block the LLM can use to compose an answer.

Backend resolution lives in `search_providers`: a configured cascade (SearXNG,
Brave, Tavily, Serper, Google PSE, DuckDuckGo) that self-skips any provider
without a key/URL and always backstops to DuckDuckGo. With no provider config
this is byte-for-byte the original DuckDuckGo-only behavior; setting a key in
config/env lights that provider up ahead of DDG and spares the DDG rate limit.

Also exposes fetch_source()/search_with_sources() so research paths can read
full page content instead of search snippets alone (SSRF-hardened).
"""
import re
from ddgs import DDGS

from .logger import log_info
from .config import settings
from . import search_providers

_MAX_RESULTS = 5

# ---------------------------------------------------------------------------
# Search intent detection
# ---------------------------------------------------------------------------

_SEARCH_PATTERNS = re.compile(
    r"(?i)"
    r"(\bsearch\s+(?:for|the\s+web|online|the\s+internet|google)?\s*.+)"
    r"|(\blook\s+up\b)"
    r"|(\bgoogle\b)"
    r"|(\bfind\s+(?:me\s+)?(?:online|on\s+the\s+web|on\s+the\s+internet)\b)"
    r"|(\bwhat(?:'s| is)\s+the\s+(?:latest|current|newest|recent)\b)"
    r"|(\b(?:latest|current|today'?s?|recent|new)\s+(?:news|price|score|weather|update|result|release|version)\b)"
    r"|(\bbrowse\s+(?:the\s+)?(?:web|internet|online)\b)"
    r"|(\bweb\s+search\b)"
    r"|(\blook\s+(?:it\s+)?up\s+(?:online|on\s+the\s+web|on\s+the\s+internet)\b)"
    r"|(\bfind\s+(?:a|an|some|the)\s+(?:job|article|recipe|tutorial|guide|link|website|page|resource|opening|listing)\b)"
    r"|(\bgive\s+me\s+(?:a\s+)?link\b)"
    r"|(\bprovide\s+(?:me\s+)?(?:a\s+)?(?:link|url)\b)"
    r"|(\bsend\s+me\s+(?:a\s+)?link\b)"
    r"|(\bshow\s+me\s+(?:results|links|websites|pages)\b)"
    r"|(\blook\s+for\s+.+\s+(?:online|on\s+the\s+web)\b)"
    r"|(\bsearch\b.{0,60}$)"
)


def detect_search_intent(text: str) -> bool:
    """Return True if the user message implies a web search request."""
    return bool(_SEARCH_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# Core search
# ---------------------------------------------------------------------------

def search(query: str, max_results: int = _MAX_RESULTS, trace_id: str = "web") -> list[dict]:
    """Run a web text search through the provider cascade.

    Returns a list of dicts: [{"title", "href", "body"}, ...] — the historical
    DuckDuckGo result shape, preserved so existing callers are unaffected.
    Returns empty list on any failure (network, rate-limit, etc.).
    """
    try:
        results = search_providers.resilient_search(query, count=max_results)
        out = [
            {"title": r.get("title", ""), "href": r.get("url", ""), "body": r.get("snippet", "")}
            for r in results
        ]
        log_info(trace_id, f"web_search query={query!r} results={len(out)}")
        return out
    except Exception as e:
        log_info(trace_id, f"web_search_error query={query!r} error={e}")
        return []


def news_search(query: str, max_results: int = 5, trace_id: str = "web") -> list[dict]:
    """Run a news search.

    Returns a list of dicts: [{"title", "url", "publisher", "date"}, ...]
    for use by ticker news fallback. Returns empty list on any failure.

    DuckDuckGo news is tried first (it carries the richest publisher/date
    fields); if it is empty/rate-limited the provider cascade's news category
    is used as a fallback so a single backend outage no longer blanks news.
    """
    def _to_news_shape(items: list[dict]) -> list[dict]:
        out = []
        for r in items:
            out.append({
                "title": (r.get("title") or "")[:100],
                "url": r.get("url") or r.get("href") or "",
                "publisher": r.get("publisher") or r.get("source") or "",
                "date": r.get("date") or r.get("age") or "",
            })
        return out

    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=max_results))
        if results:
            log_info(trace_id, f"news_search query={query!r} results={len(results)} src=ddg")
            return _to_news_shape(results)
    except Exception as e:
        log_info(trace_id, f"news_search_ddg_error query={query!r} error={e}")

    # Fallback: provider cascade (news category) for resilience.
    try:
        fallback = search_providers.resilient_search(
            query, count=max_results, time_filter="week", categories="news")
        log_info(trace_id, f"news_search query={query!r} results={len(fallback)} src=cascade")
        return _to_news_shape(fallback)
    except Exception as e:
        log_info(trace_id, f"news_search_error query={query!r} error={e}")
        return []


def format_results(results: list[dict]) -> str:
    """Format search results into a context block for the LLM."""
    if not results:
        return "No web search results found."
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        href = r.get("href", "")
        body = r.get("body", "")
        lines.append(f"{i}. **{title}**\n   {body}\n   Link: {href}")
    return "\n\n".join(lines)


def fetch_source(url: str, max_chars: int = 8000, trace_id: str = "web") -> dict:
    """Fetch readable page content for a URL (SSRF-hardened, TTL-cached).

    Returns {"url", "title", "content", "meta_description", "success", "error"}.
    Never raises. Opt-in helper for research paths that want full article text
    rather than search snippets.
    """
    result = search_providers.fetch_webpage_content(url, max_chars=max_chars)
    log_info(trace_id, f"fetch_source url={url!r} ok={result.get('success')} len={len(result.get('content',''))}")
    return result


def search_with_sources(query: str, max_results: int = _MAX_RESULTS,
                        max_fetch: int = 3, trace_id: str = "web") -> list[dict]:
    """Search, then enrich the top results with fetched page content.

    Returns the search dicts ({title, href, body}) with an added "content" key
    holding extracted article text for up to `max_fetch` results (empty string
    if a page could not be read). Opt-in; no existing caller uses this by
    default, so it adds no latency to current flows.
    """
    results = search(query, max_results=max_results, trace_id=trace_id)
    for r in results[:max_fetch]:
        href = r.get("href", "")
        if not href:
            r["content"] = ""
            continue
        fetched = fetch_source(href, trace_id=trace_id)
        r["content"] = fetched.get("content", "") if fetched.get("success") else ""
    return results


def research_search(query: str, max_results: int = _MAX_RESULTS,
                    trace_id: str = "web", content_chars: int = 2000) -> list[dict]:
    """Search for background research, optionally enriched with page content.

    Behaves like search() ({title, href, body}), but when
    settings.search_fetch_sources is True it adds a "content" key holding fetched
    article text (truncated to content_chars) for up to settings.search_max_fetch
    results. With the flag off this is exactly search() — no extra latency.
    Intended for the reasoning_brain / project_brain research paths.
    """
    results = search(query, max_results=max_results, trace_id=trace_id)
    if not getattr(settings, "search_fetch_sources", False):
        return results
    try:
        max_fetch = max(0, int(getattr(settings, "search_max_fetch", 3)))
    except (TypeError, ValueError):
        max_fetch = 3
    for r in results[:max_fetch]:
        href = r.get("href", "")
        if not href:
            continue
        fetched = fetch_source(href, max_chars=content_chars, trace_id=trace_id)
        if fetched.get("success") and fetched.get("content"):
            r["content"] = fetched["content"]
    return results


def extract_search_query(message: str) -> str:
    """Extract the intended search query from a user message.

    Strips common prefixes like "search for", "google", "look up" etc.
    """
    cleaned = message.strip()
    cleaned = re.sub(
        r"(?i)^(?:please\s+)?(?:can\s+you\s+)?(?:search\s+(?:for|the\s+web\s+for|online\s+for|google\s+for)?|"
        r"google|look\s+up|browse\s+(?:the\s+)?(?:web|internet)\s+for|"
        r"find\s+(?:me\s+)?(?:online|on\s+the\s+web|on\s+the\s+internet)?|"
        r"web\s+search\s+(?:for)?|look\s+for)\s*:?\s*",
        "",
        cleaned,
    ).strip()
    if not cleaned:
        cleaned = message.strip()
    return cleaned
