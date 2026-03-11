"""Web search via DuckDuckGo — free, no API key required.

Provides search() for fetching results and format_results() for
producing a context block the LLM can use to compose an answer.
"""
import re
from ddgs import DDGS

from .logger import log_info

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
    """Run a DuckDuckGo text search.

    Returns a list of dicts: [{"title", "href", "body"}, ...]
    Returns empty list on any failure (network, rate-limit, etc.).
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        log_info(trace_id, f"web_search query={query!r} results={len(results)}")
        return results
    except Exception as e:
        log_info(trace_id, f"web_search_error query={query!r} error={e}")
        return []


def news_search(query: str, max_results: int = 5, trace_id: str = "web") -> list[dict]:
    """Run a DuckDuckGo news search.

    Returns a list of dicts: [{"title", "url", "publisher", "date"}, ...]
    for use by ticker news fallback. Returns empty list on any failure.
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=max_results))
        log_info(trace_id, f"news_search query={query!r} results={len(results)}")
        out = []
        for r in results:
            out.append({
                "title": (r.get("title") or "")[:100],
                "url": r.get("url") or r.get("href") or "",
                "publisher": r.get("publisher") or r.get("source") or "",
                "date": r.get("date") or "",
            })
        return out
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
