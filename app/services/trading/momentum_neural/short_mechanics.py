"""Ortex short-mechanics fetch for the SQUEEZE-FUEL selection edge (Ross SS101 #2).

Ross's SS101 course names short-squeeze fuel as a HIGH-value precondition for the
100-1000% low-float verticals: a name with a heavily-shorted, hard/expensive-to-borrow
float has trapped sellers who must cover INTO the pop — the rocket fuel that turns a
+30% gap into a +300% halt-ladder. Conversely a name with FREE shares (very low
cost-to-borrow / easy-to-borrow) lets shorts press the pop with impunity, so the same
breakout fades. This module fetches the two core squeeze signals (short-interest %% of
free float + cost-to-borrow) from Ortex so the selection layer can SOFT-tilt toward
squeeze-prone names and slightly DE-RATE free-share names.

``get_short_mechanics(symbol)`` returns a dict (or ``None``) — it is a pure data
fetcher; the tilt math lives in ``ross_momentum.py`` / the pipeline bridge.

CREDIT FRUGALITY (operator constraint — Trader plan: 1,000 credits/mo, 1 req/s,
single-stock only):
  * **Cached per-symbol with a long TTL** (~12h default) + a hard max cache size.
    Short-interest / CTB rows are daily and barely move intraday, so a long TTL costs
    nothing in signal freshness and everything in credits.
  * **Top-N gated by the CALLER.** This module does NOT fetch the universe — the
    pipeline bridge only calls it for the top-N explosive low-float candidates that
    already pass the Ross explosive screen (``chili_momentum_squeeze_fuel_top_n``).
  * **1 req/s rate-limit-safe.** A process-wide serialised lock + a small inter-request
    sleep so two endpoints for one symbol (SI + CTB) never burst the 1 req/s ceiling.
  * **Equity-only.** Crypto (``-USD``) has no borrow data — the caller skips it; this
    module also guards.

FAIL-OPEN by construction: no key / non-200 / timeout / parse error / unknown exchange
⇒ ``None`` ⇒ NO tilt ⇒ byte-identical selection. A squeeze signal can only ADD
preference; its ABSENCE never benches a name.

Endpoints (VERIFIED live with the TEST key, 2026-06-24):
  GET /stock/{exchange}/{ticker}/short_interest -> rows[].shortInterestPcFreeFloat (fraction)
  GET /stock/{exchange}/{ticker}/ctb/all        -> rows[].costToBorrowAll (annual %)
Utilization has no confirmed single-stock path in docs/llms.txt and no easy-to-borrow
field is documented, so we SHIP with SI%% + CTB (the core squeeze signals) and derive
``is_easy_to_borrow`` from a near-zero CTB. ``utilization`` is left ``None`` (optional).
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
import json

from ....config import settings

logger = logging.getLogger(__name__)

# Ortex API base + auth header (operator-verified working).
_ORTEX_BASE = "https://api.ortex.com/api/v1"
_ORTEX_KEY_HEADER = "Ortex-Api-Key"

# Cache: per-symbol short mechanics with a long TTL (daily data, barely moves intraday)
# + a hard max size (CHILI convention — caches must have hard max + TTL). The cache stores
# the RESULT (incl. None misses, with a shorter negative TTL) so a fail-open symbol does
# not re-burn a request every tick.
_CACHE_TTL_SECONDS = 12 * 3600.0       # 12h: daily short data barely moves intraday
_NEG_CACHE_TTL_SECONDS = 30 * 60.0     # 30m: don't hammer a failing/no-data symbol
_MAX_CACHE = 512                       # hard cap; LRU-ish prune of expired then oldest
_cache: dict[str, tuple[float, dict | None]] = {}
_cache_lock = threading.Lock()

# Rate-limit guard: serialise ALL Ortex requests process-wide and sleep between them so we
# never exceed the Trader plan's 1 req/s ceiling (two endpoints per symbol must not burst).
_REQ_INTERVAL_SECONDS = 1.05
_req_lock = threading.Lock()
_last_req_ts = 0.0

# Network timeout per request (seconds) — short so a hung Ortex never stalls the
# viability-refresh pass; on timeout we fail-open to None.
_REQ_TIMEOUT_SECONDS = 8.0

# Easy-to-borrow reference: a cost-to-borrow at/below this annual %% means shares are
# essentially FREE to borrow ⇒ shorts can press the pop ⇒ small de-rate. The ONE documented
# base (operator: irreducible base = one documented setting); the actual ranking is the
# within-batch percentile of the RAW CTB / SI%% in ross_momentum, so this only flags the
# easy-to-borrow boolean, it is not a ranking cutoff.
EASY_TO_BORROW_CTB_PCT = 1.0


def _cache_get(symbol: str):
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is None:
            return None
        ts, val = entry
        ttl = _CACHE_TTL_SECONDS if val is not None else _NEG_CACHE_TTL_SECONDS
        if (time.time() - ts) > ttl:
            del _cache[symbol]
            return None
        # sentinel-free: return a 1-tuple wrapper so a cached None ("miss") is
        # distinguishable from "not cached" (None from .get).
        return (val,)


def _cache_set(symbol: str, val: dict | None) -> None:
    with _cache_lock:
        if len(_cache) >= _MAX_CACHE:
            now = time.time()
            # prune expired first (use the appropriate TTL per entry), then oldest.
            expired = [
                k for k, (t, v) in _cache.items()
                if (now - t) > (_CACHE_TTL_SECONDS if v is not None else _NEG_CACHE_TTL_SECONDS)
            ]
            for k in expired:
                del _cache[k]
            if len(_cache) >= _MAX_CACHE:
                oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[: max(1, _MAX_CACHE // 8)]
                for k, _ in oldest:
                    _cache.pop(k, None)
        _cache[symbol] = (time.time(), val)


def _exchange_for(symbol: str) -> str | None:
    """Best-effort exchange segment for the Ortex path. Ortex wants ``nasdaq`` or
    ``nyse`` (lower-case). We don't carry a per-symbol listing map here, so we try
    ``nasdaq`` first (the vast majority of the low-float momentum universe lists on
    Nasdaq) and let the caller's NYSE fallback (or a future listing lookup) extend it.
    Returns None for obviously non-equity tickers."""
    s = (symbol or "").strip().upper()
    if not s or "-" in s or "/" in s:  # crypto pairs / FX have no borrow data
        return None
    return "nasdaq"


def _rate_limited_get_json(path: str, key: str) -> dict | None:
    """Serialised, rate-limited GET that returns parsed JSON or None (fail-open)."""
    global _last_req_ts
    url = _ORTEX_BASE + path
    req = urllib.request.Request(url, headers={_ORTEX_KEY_HEADER: key})
    with _req_lock:
        # honour the 1 req/s ceiling across the whole process
        wait = _REQ_INTERVAL_SECONDS - (time.time() - _last_req_ts)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=_REQ_TIMEOUT_SECONDS) as resp:
                if getattr(resp, "status", 200) != 200:
                    return None
                raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
            return None
        except Exception:  # never let a fetch bug propagate into selection
            return None
        finally:
            _last_req_ts = time.time()


def _latest_row(payload: dict | None) -> dict | None:
    """Ortex rows are slow-moving daily rows; take the LATEST (last in the list)."""
    if not isinstance(payload, dict):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    last = rows[-1]
    return last if isinstance(last, dict) else None


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def get_short_mechanics(symbol: str) -> dict | None:
    """Fetch the squeeze-fuel short mechanics for one EQUITY ``symbol`` from Ortex.

    Returns ``{short_interest_pct, cost_to_borrow, utilization, is_easy_to_borrow}`` or
    ``None`` (fail-open — no key / non-equity / fetch error / no data). Cached per-symbol
    with a long TTL; the CALLER is responsible for the top-N gate (this fetches unconditionally
    when called, but the cache + the bridge's top-N gate keep credit use bounded).

      * ``short_interest_pct`` — short interest as a fraction of free float
        (Ortex ``shortInterestPcFreeFloat``; e.g. 0.18 = 18%%). HIGHER ⇒ more trapped shorts.
      * ``cost_to_borrow``     — annual borrow cost %% (Ortex ``costToBorrowAll``). HIGHER ⇒
        hard/expensive to short ⇒ squeeze-prone. VERY LOW ⇒ free shares ⇒ de-rate.
      * ``utilization``        — short utilization %% if resolvable, else ``None`` (no
        confirmed single-stock path; shipped optional).
      * ``is_easy_to_borrow``  — True when CTB is at/below ``EASY_TO_BORROW_CTB_PCT`` (free
        shares — the de-rate flag), else False; ``None`` when CTB is unknown.

    FAIL-OPEN on EVERY error path — a squeeze signal only ADDS preference, never benches a name.
    """
    try:
        if not bool(getattr(settings, "chili_momentum_squeeze_fuel_tilt_enabled", True)):
            return None
        key = str(getattr(settings, "chili_ortex_api_key", "") or "").strip()
        if not key:
            return None  # no key ⇒ no fetch ⇒ no tilt (byte-identical)
        sym = (symbol or "").strip().upper()
        if not sym:
            return None
        exch = _exchange_for(sym)
        if exch is None:
            return None  # crypto / non-equity ⇒ no borrow data

        cached = _cache_get(sym)
        if cached is not None:
            return cached[0]

        # SHORT INTEREST (%% free float) + COST-TO-BORROW (annual %%) — the two core squeeze
        # signals. Two serialised, rate-limited requests; either failing ⇒ that field is None.
        si_payload = _rate_limited_get_json(f"/stock/{exch}/{sym}/short_interest", key)
        ctb_payload = _rate_limited_get_json(f"/stock/{exch}/{sym}/ctb/all", key)

        si_row = _latest_row(si_payload)
        ctb_row = _latest_row(ctb_payload)

        short_interest_pct = _to_float(si_row.get("shortInterestPcFreeFloat")) if si_row else None
        cost_to_borrow = _to_float(ctb_row.get("costToBorrowAll")) if ctb_row else None

        # Both signals absent ⇒ nothing to tilt on ⇒ cache a (short-TTL) miss, return None.
        if short_interest_pct is None and cost_to_borrow is None:
            _cache_set(sym, None)
            return None

        is_easy_to_borrow = None
        if cost_to_borrow is not None:
            is_easy_to_borrow = bool(cost_to_borrow <= EASY_TO_BORROW_CTB_PCT)

        result = {
            "short_interest_pct": short_interest_pct,
            "cost_to_borrow": cost_to_borrow,
            "utilization": None,  # no confirmed single-stock path; optional, shipped None
            "is_easy_to_borrow": is_easy_to_borrow,
        }
        _cache_set(sym, result)
        return result
    except Exception:
        return None  # fail-open: any bug ⇒ no tilt
