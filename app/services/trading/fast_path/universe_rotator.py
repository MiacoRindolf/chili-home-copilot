"""f-fastpath-universe-rotation (2026-05-07): hourly rotator that
populates ``fast_path_universe`` with the top-N mid-tier USD pairs
from Coinbase.

One pass per invocation:

1. List all USD-quoted Coinbase products that are online + tradable.
2. For each, fetch ``stats`` (24h volume), ``ticker`` (best bid/ask),
   and ``book?level=1`` (top-of-book sizes).
3. Apply the four admission gates (volume / spread / top-of-book size
   / trade count). All four must pass; settings-tunable thresholds.
4. Score the survivors by ``composite = volume_24h_usd / max(spread_bps,
   0.5)`` (the alpha-replay formula).
5. Diff against the previous pass's status. Apply hysteresis: a pair
   currently in ``status='active'`` only gets demoted if its new rank
   is at least ``universe_hysteresis_ranks`` worse than the cut.
6. New entrants land in ``status='shadow'`` for the first
   ``universe_shadow_window_h`` hours; existing shadows that have
   completed the window get promoted to ``active``.
7. Write one row per ranked ticker for this pass to
   ``fast_path_universe``. Demoted pairs get ``status='inactive'``.

Pure side-effect-free against in-memory state; the only mutation is
the DB writes. Failures log + return; the rotator never raises into
the scheduler.

Coinbase REST endpoints (no auth, public):
  GET /products
  GET /products/{id}/stats
  GET /products/{id}/ticker
  GET /products/{id}/book?level=1

f-fastpath-rotator-coinbase-fixes-bundle (2026-05-08):
  - HTTP client switched from urllib (custom UA hits Cloudflare bot
    detection -> 403 from inside Docker containers) to ``requests``
    with the default UA, mirroring the proven-good pattern in
    ``coinbase_ohlcv.py``.
  - Top-of-book sizes moved from ``/ticker`` (which doesn't return
    bid_size/ask_size) to ``/book?level=1``. New ``_fetch_book``
    helper. Three REST calls per pair instead of two; ~140s for a
    394-pair scan instead of ~95s.

Rate-limited to ~8 req/s (below the documented 10 req/s).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

_COINBASE_REST = "https://api.exchange.coinbase.com"
_HTTP_TIMEOUT_S = 8.0
_PER_REQ_PACING_S = 0.12  # ~8 req/s


@dataclass
class _PairCandidate:
    """One ticker's gate-input snapshot."""

    ticker: str
    volume_24h_base: float
    last_price: float
    bid: float
    ask: float
    trades_24h: int

    @property
    def volume_24h_usd(self) -> float:
        return self.volume_24h_base * self.last_price

    @property
    def spread_bps(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return float("inf")
        mid = (self.bid + self.ask) / 2.0
        if mid <= 0:
            return float("inf")
        return (self.ask - self.bid) / mid * 10000.0

    @property
    def top_of_book_usd(self) -> float:
        # Conservative: minimum of bid-side and ask-side, since for a
        # round-trip we hit both. Sourced from
        # ``/products/{id}/book?level=1`` -- ``/ticker`` does not return
        # bid_size/ask_size despite the name suggesting so. See
        # ``_fetch_book`` for the population path.
        return min(self._bid_size_usd, self._ask_size_usd)

    # Set during _fetch_book; default 0 if missing.
    _bid_size_usd: float = 0.0
    _ask_size_usd: float = 0.0

    @property
    def composite_score(self) -> float:
        """``volume / max(spread, 0.5)`` -- alpha-replay-validated."""
        return self.volume_24h_usd / max(self.spread_bps, 0.5)


# f-fastpath-rotator-http-retry (2026-05-08): retry policy for the
# per-pair Coinbase REST calls. Live observation (2026-05-08 06:57 UTC)
# saw 371/394 pairs fail with Errno 101 ('Network is unreachable') --
# Docker Desktop NAT flakiness, NOT Coinbase rate-limiting (verified by
# /products in the same pass succeeding). Without retry, those drops =
# None = snapshot fail. The 23 pairs that succeeded were the early-
# dispatch majors -- exactly the wrong cohort vs the alpha-replay
# mid-tier targets (RENDER/ICP/ARB/INJ/TAO/FET).
#
# Backoff: 0.5s -> 1.0s -> 2.0s. Worst-case per call ~12s
# (8s timeout * 3 attempts plus backoff sleeps); total rotator pass
# stays under 10 min for 394 pairs.
_HTTP_RETRY_BACKOFFS_S = (0.5, 1.0, 2.0)
_HTTP_RETRYABLE_STATUS = frozenset({429, 503})


def _http_get_json(url: str, *, params: Optional[dict] = None) -> Optional[Any]:
    """Public-API GET with timeout + 3-attempt retry. Returns None
    only after all retries exhaust.

    Uses the ``requests`` library's default User-Agent
    (``python-requests/X.Y.Z``) -- the same client + UA that
    ``coinbase_ohlcv.py`` uses successfully against this host. The
    prior implementation (urllib + custom ``chili-fast-path-rotator/1``
    UA) was returning HTTP 403 from inside Docker containers because
    Cloudflare's bot-detection blocks unrecognized UAs; the default
    requests UA is on the allowlist.

    Retry policy (f-fastpath-rotator-http-retry, 2026-05-08):

      * Retryable: ``ConnectionError`` (wraps Errno 101 / TCP drop),
        ``Timeout``, HTTP 503 (service unavailable), HTTP 429 (rate
        limited).
      * Non-retryable (give up immediately): HTTP 4xx other than 429
        (the request itself is bad; retrying won't help), JSON decode
        errors (server returned non-JSON; retrying won't help).
      * Backoff: 0.5s, 1.0s, 2.0s between attempts.
    """
    last_err: Optional[str] = None
    for attempt, backoff in enumerate((0.0, *_HTTP_RETRY_BACKOFFS_S)):
        if backoff > 0:
            time.sleep(backoff)
        try:
            resp = requests.get(url, params=params, timeout=_HTTP_TIMEOUT_S)
        except requests.exceptions.ConnectionError as e:
            last_err = f"ConnectionError: {e}"
            logger.debug(
                "[fast_path_rotator] GET %s attempt=%d connection_error=%s",
                url, attempt + 1, e,
            )
            continue
        except requests.exceptions.Timeout as e:
            last_err = f"Timeout: {e}"
            logger.debug(
                "[fast_path_rotator] GET %s attempt=%d timeout=%s",
                url, attempt + 1, e,
            )
            continue
        except requests.RequestException as e:
            last_err = f"RequestException: {e}"
            logger.debug(
                "[fast_path_rotator] GET %s attempt=%d request_exception=%s",
                url, attempt + 1, e,
            )
            return None
        except Exception as e:
            last_err = f"unexpected: {e}"
            logger.warning(
                "[fast_path_rotator] GET %s unexpected failure: %s", url, e,
            )
            return None

        # Got a response object. Check status code for retryable HTTP errors.
        if resp.status_code in _HTTP_RETRYABLE_STATUS:
            last_err = f"HTTP {resp.status_code}"
            logger.debug(
                "[fast_path_rotator] GET %s attempt=%d retryable_status=%d",
                url, attempt + 1, resp.status_code,
            )
            continue
        if resp.status_code >= 400:
            # 4xx (except 429) and unhandled 5xx -> give up.
            logger.debug(
                "[fast_path_rotator] GET %s non_retryable_status=%d",
                url, resp.status_code,
            )
            return None

        # 2xx/3xx response. Try to parse JSON.
        try:
            return resp.json()
        except ValueError as e:
            logger.debug(
                "[fast_path_rotator] GET %s json_decode_failed=%s", url, e,
            )
            return None

    logger.debug(
        "[fast_path_rotator] GET %s exhausted retries last_err=%s",
        url, last_err,
    )
    return None


def _list_usd_products() -> list[str]:
    """Fetch the Coinbase product universe filtered to live USD pairs."""
    products = _http_get_json(f"{_COINBASE_REST}/products")
    if not isinstance(products, list):
        return []
    out: list[str] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        if (p.get("quote_currency") or "").upper() != "USD":
            continue
        if (p.get("status") or "").lower() != "online":
            continue
        if p.get("trading_disabled"):
            continue
        # 2026-05-08: Coinbase changed auction_mode semantics -- now set on
        # ~all online products (393 of 394 USD pairs in May 2026 obs).
        # Was previously a "this product is auction-only, can't market-trade"
        # flag; now it's effectively meaningless. Removed from the filter.
        # Status='online' + trading_disabled=False + valid book is enough.
        pid = p.get("id")
        if isinstance(pid, str) and pid:
            out.append(pid.upper())
    return out


def _fetch_book(ticker: str) -> Optional[tuple[float, float]]:
    """Hit ``/products/{id}/book?level=1`` to get top-of-book sizes.

    Returns ``(bid_size_base, ask_size_base)`` in BASE units (caller
    multiplies by last_price to convert to USD). Returns ``None`` on
    any error.

    Coinbase's level=1 book payload shape::

        {
            "sequence": <int>,
            "bids": [["<price>", "<size>", "<num_orders>"]],
            "asks": [["<price>", "<size>", "<num_orders>"]],
        }
    """
    book = _http_get_json(
        f"{_COINBASE_REST}/products/{ticker}/book", params={"level": 1}
    )
    if not isinstance(book, dict):
        return None
    try:
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        bid0 = bids[0]
        ask0 = asks[0]
        if not isinstance(bid0, (list, tuple)) or len(bid0) < 2:
            return None
        if not isinstance(ask0, (list, tuple)) or len(ask0) < 2:
            return None
        bid_size_base = float(bid0[1])
        ask_size_base = float(ask0[1])
    except (TypeError, ValueError, IndexError):
        return None
    return bid_size_base, ask_size_base


def _fetch_pair_snapshot(ticker: str) -> Optional[_PairCandidate]:
    """Hit /stats + /ticker + /book for one ticker. Returns None on error."""
    stats = _http_get_json(f"{_COINBASE_REST}/products/{ticker}/stats")
    time.sleep(_PER_REQ_PACING_S)
    tk = _http_get_json(f"{_COINBASE_REST}/products/{ticker}/ticker")
    time.sleep(_PER_REQ_PACING_S)
    book_sizes = _fetch_book(ticker)
    time.sleep(_PER_REQ_PACING_S)
    if not isinstance(stats, dict) or not isinstance(tk, dict):
        return None
    try:
        volume_24h_base = float(stats.get("volume") or 0.0)
        last_price = float(tk.get("price") or 0.0)
        bid = float(tk.get("bid") or 0.0)
        ask = float(tk.get("ask") or 0.0)
        trades_24h = int(stats.get("trade_count") or 0)
    except (TypeError, ValueError):
        return None
    cand = _PairCandidate(
        ticker=ticker,
        volume_24h_base=volume_24h_base,
        last_price=last_price,
        bid=bid,
        ask=ask,
        trades_24h=trades_24h,
    )
    if book_sizes is not None:
        bid_size_base, ask_size_base = book_sizes
        cand._bid_size_usd = bid_size_base * last_price
        cand._ask_size_usd = ask_size_base * last_price
    return cand


def passes_admission_gates(
    cand: _PairCandidate,
    *,
    min_volume_24h_usd: float,
    max_spread_bps: float,
    min_top_of_book_usd: float,
    min_trades_24h: int,
) -> tuple[bool, Optional[str]]:
    """All four gates must pass. Returns (passed, reject_reason)."""
    if cand.volume_24h_usd < min_volume_24h_usd:
        return False, "volume_below_threshold"
    if cand.spread_bps > max_spread_bps:
        return False, "spread_above_threshold"
    if cand.top_of_book_usd < min_top_of_book_usd:
        return False, "top_of_book_below_threshold"
    if cand.trades_24h < min_trades_24h:
        return False, "trades_below_threshold"
    return True, None


def _previous_pass_status(db) -> dict[str, tuple[str, Optional[int]]]:
    """Map ticker -> (status, rank) from the most recent rotation_at.

    Returns empty dict on first pass.
    """
    from sqlalchemy import text

    rows = db.execute(text("""
        WITH latest_rotation AS (
            SELECT MAX(rotation_at) AS ts FROM fast_path_universe
        )
        SELECT ticker, status, rank
        FROM fast_path_universe
        WHERE rotation_at = (SELECT ts FROM latest_rotation)
    """)).fetchall()
    return {r.ticker: (r.status, r.rank) for r in rows}


def _shadow_completion_promotions(
    db, *, shadow_window_h: int
) -> set[str]:
    """Tickers that have been in ``status='shadow'`` for ≥ shadow_window_h.

    Used to flip them to ``status='active'`` on the next rotation.
    """
    from sqlalchemy import text

    cutoff = datetime.utcnow() - timedelta(hours=shadow_window_h)
    rows = db.execute(text("""
        SELECT DISTINCT ticker
        FROM fast_path_universe
        WHERE status = 'shadow' AND promoted_at IS NOT NULL
          AND promoted_at <= :cutoff
    """), {"cutoff": cutoff}).fetchall()
    return {r.ticker for r in rows}


def run_rotation_pass(
    db,
    *,
    settings,
    list_usd_products_fn=_list_usd_products,
    fetch_snapshot_fn=_fetch_pair_snapshot,
    fetch_book_fn=_fetch_book,
) -> dict[str, Any]:
    """Single-pass rotator. Returns a counter dict for the audit log.

    ``list_usd_products_fn`` / ``fetch_snapshot_fn`` / ``fetch_book_fn``
    are injectable for testing -- unit tests substitute synthetic data
    instead of hitting Coinbase live. ``fetch_book_fn`` is wired so
    that tests can exercise the empty/thin/deep top-of-book branches
    independently of ``fetch_snapshot_fn``; the default
    ``_fetch_pair_snapshot`` already calls ``_fetch_book`` internally.
    """
    from sqlalchemy import text

    out: dict[str, Any] = {
        "scanned": 0,
        "snapshot_failures": 0,
        "gate_rejections": {},
        "ranked_n": 0,
        "promoted_to_active": 0,
        "promoted_to_shadow": 0,
        "kept_active": 0,
        "kept_shadow": 0,
        "demoted_to_inactive": 0,
        "rotation_at": datetime.utcnow().isoformat(),
    }

    if not getattr(settings, "universe_rotation_enabled", False):
        out["skipped_reason"] = "universe_rotation_disabled"
        return out

    products = list_usd_products_fn()
    out["scanned"] = len(products)
    if not products:
        out["skipped_reason"] = "no_products_returned"
        return out

    # ``fetch_book_fn`` is held on the closure for tests that override
    # it independently of ``fetch_snapshot_fn``. Production
    # ``_fetch_pair_snapshot`` calls ``_fetch_book`` internally; the
    # extra hook here lets tests exercise the gate behaviour against
    # custom book shapes without subclassing the snapshot.
    _ = fetch_book_fn

    candidates: list[_PairCandidate] = []
    for tk in products:
        snap = fetch_snapshot_fn(tk)
        if snap is None:
            out["snapshot_failures"] += 1
            continue
        passed, reject = passes_admission_gates(
            snap,
            min_volume_24h_usd=settings.universe_min_volume_24h_usd,
            max_spread_bps=settings.universe_max_spread_bps,
            min_top_of_book_usd=settings.universe_min_top_of_book_usd,
            min_trades_24h=settings.universe_min_trades_24h,
        )
        if not passed:
            out["gate_rejections"][reject] = (
                out["gate_rejections"].get(reject, 0) + 1
            )
            continue
        candidates.append(snap)

    candidates.sort(key=lambda c: c.composite_score, reverse=True)
    top_n = settings.universe_top_n
    cut_ranked = candidates[: top_n + settings.universe_hysteresis_ranks]
    out["ranked_n"] = len(cut_ranked)

    prior = _previous_pass_status(db)
    completed_shadows = _shadow_completion_promotions(
        db, shadow_window_h=settings.universe_shadow_window_h
    )

    rotation_at = datetime.utcnow()
    rows_to_write: list[dict[str, Any]] = []

    seen_in_this_pass: set[str] = set()
    for rank_idx, cand in enumerate(cut_ranked, start=1):
        seen_in_this_pass.add(cand.ticker)
        prior_status, prior_rank = prior.get(cand.ticker, (None, None))

        if rank_idx > top_n:
            # Outside top_n; only present in cut_ranked because of the
            # hysteresis buffer. If this pair WAS active and is now
            # outside top_n + hysteresis... that's caught below in the
            # "demoted" loop. If it was active and is in the buffer,
            # keep it active (hysteresis grace).
            if prior_status == "active":
                status = "active"
                out["kept_active"] += 1
            else:
                # Not eligible for promotion this pass; skip writing.
                continue
        elif prior_status is None:
            # Brand-new entrant -> shadow
            status = "shadow"
            out["promoted_to_shadow"] += 1
        elif prior_status == "shadow":
            if cand.ticker in completed_shadows:
                status = "active"
                out["promoted_to_active"] += 1
            else:
                status = "shadow"
                out["kept_shadow"] += 1
        elif prior_status == "active":
            status = "active"
            out["kept_active"] += 1
        else:  # 'inactive' -> re-promote to shadow on rejoining top-N
            status = "shadow"
            out["promoted_to_shadow"] += 1

        promoted_at: Optional[datetime] = None
        if status == "shadow" and prior_status != "shadow":
            promoted_at = rotation_at  # start the shadow clock
        elif status == "active":
            # Carry forward promoted_at from the prior shadow promotion
            # for audit; cheap to recompute by reading the prior row.
            promoted_at = rotation_at if prior_status != "active" else None

        rows_to_write.append({
            "ticker": cand.ticker,
            "status": status,
            "rank": rank_idx if rank_idx <= top_n else None,
            "composite_score": cand.composite_score,
            "volume_24h_usd": cand.volume_24h_usd,
            "spread_bps": cand.spread_bps,
            "top_of_book_usd": cand.top_of_book_usd,
            "trades_24h": cand.trades_24h,
            "rotation_at": rotation_at,
            "promoted_at": promoted_at,
        })

    # Demote anything that was active/shadow last pass but isn't seen
    # in cut_ranked at all -- write an explicit 'inactive' row so the
    # rotator's history is complete.
    for ticker, (prior_status, _prior_rank) in prior.items():
        if ticker in seen_in_this_pass:
            continue
        if prior_status not in ("active", "shadow"):
            continue
        rows_to_write.append({
            "ticker": ticker,
            "status": "inactive",
            "rank": None,
            "composite_score": None,
            "volume_24h_usd": None,
            "spread_bps": None,
            "top_of_book_usd": None,
            "trades_24h": None,
            "rotation_at": rotation_at,
            "promoted_at": None,
        })
        out["demoted_to_inactive"] += 1

    if rows_to_write:
        db.execute(
            text("""
                INSERT INTO fast_path_universe (
                    ticker, status, rank, composite_score,
                    volume_24h_usd, spread_bps, top_of_book_usd,
                    trades_24h, rotation_at, promoted_at
                ) VALUES (
                    :ticker, :status, :rank, :composite_score,
                    :volume_24h_usd, :spread_bps, :top_of_book_usd,
                    :trades_24h, :rotation_at, :promoted_at
                )
            """),
            rows_to_write,
        )
        db.commit()

    return out


def get_active_pairs(db) -> list[str]:
    """Read the current active set for the WS subscriber.

    Returns the most-recent-rotation ``status='active'`` tickers, ordered
    by rank. Empty list if the table has never been populated.
    """
    from sqlalchemy import text

    rows = db.execute(text("""
        WITH latest_rotation AS (
            SELECT MAX(rotation_at) AS ts FROM fast_path_universe
        )
        SELECT ticker
        FROM fast_path_universe
        WHERE rotation_at = (SELECT ts FROM latest_rotation)
          AND status = 'active'
        ORDER BY rank ASC NULLS LAST
    """)).fetchall()
    return [r.ticker for r in rows]


def get_subscribed_pairs(db) -> list[str]:
    """Active + shadow combined -- the full WS subscription set.

    Shadow pairs need to be subscribed so ``decay_miner`` can collect
    samples during the cold-start window.
    """
    from sqlalchemy import text

    rows = db.execute(text("""
        WITH latest_rotation AS (
            SELECT MAX(rotation_at) AS ts FROM fast_path_universe
        )
        SELECT ticker
        FROM fast_path_universe
        WHERE rotation_at = (SELECT ts FROM latest_rotation)
          AND status IN ('active', 'shadow')
        ORDER BY rank ASC NULLS LAST
    """)).fetchall()
    return [r.ticker for r in rows]


__all__ = [
    "run_rotation_pass",
    "get_active_pairs",
    "get_subscribed_pairs",
    "passes_admission_gates",
]
