"""f-fastpath-universe-rotation (2026-05-07): hourly rotator that
populates ``fast_path_universe`` with the top-N mid-tier USD pairs
from Coinbase.

One pass per invocation:

1. List all USD-quoted Coinbase products that are online + tradable.
2. For each, fetch ``stats`` (24h volume), ``ticker`` (best bid/ask),
   and ``book?level=1`` (top-of-book sizes).
3. Apply admission gates (volume / spread / top-of-book size / trade
   count when Coinbase provides it). Measured thresholds must pass;
   settings-tunable thresholds.
4. Score the survivors by a data-derived opportunity score:
   ``range_24h_bps * top_of_book_usd`` with trade count included only
   when Coinbase provides it. Volume and spread stay admission/cost
   gates rather than ranking weights so quiet mega-cap pairs cannot
   dominate solely by size or near-zero spread.
5. Diff against the previous pass's status. Apply hysteresis: a pair
   currently in ``status='active'`` only gets demoted if its new rank
   is at least ``universe_hysteresis_ranks`` worse than the cut.
6. New entrants land in ``status='shadow'`` for the first
   ``universe_shadow_window_h`` hours. Existing shadows that have
   completed the window promote to ``active`` only when learned
   decay evidence clears the configured execution cost.
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

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

from .universe_status import (
    UNIVERSE_HARD_DEMOTE_REASONS,
    UNIVERSE_REJECT_RANGE_BELOW,
    UNIVERSE_REJECT_SHADOW_TOP_OF_BOOK_BELOW,
    UNIVERSE_REJECT_SPREAD_ABOVE,
    UNIVERSE_REJECT_TOP_OF_BOOK_BELOW,
    UNIVERSE_REJECT_TRADES_BELOW,
    UNIVERSE_REJECT_VOLUME_BELOW,
    UNIVERSE_STATUS_ACTIVE,
    UNIVERSE_STATUS_INACTIVE,
    UNIVERSE_STATUS_SHADOW,
    UNIVERSE_SUBSCRIBED_STATUSES,
)

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
    high_24h: float = 0.0
    low_24h: float = 0.0

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

    @property
    def range_24h_bps(self) -> float:
        """High-low range over the last 24h in bps.

        This is a coarse all-universe volatility proxy available from
        Coinbase public stats. The faster decay tables still decide
        whether an emitted signal has real edge.
        """
        if self.high_24h <= 0 or self.low_24h <= 0:
            return 0.0
        if self.high_24h < self.low_24h:
            return 0.0
        ref_price = self.last_price if self.last_price > 0 else (
            (self.high_24h + self.low_24h) / 2.0
        )
        if ref_price <= 0:
            return 0.0
        return (self.high_24h - self.low_24h) / ref_price * 10000.0

    @property
    def has_valid_opportunity_data(self) -> bool:
        """True when the public snapshot is usable for exploration ranking."""
        spread_bps = self.spread_bps
        return (
            self.volume_24h_usd > 0.0
            and spread_bps > 0.0
            and math.isfinite(spread_bps)
            and self.top_of_book_usd > 0.0
            and self.range_24h_bps > 0.0
        )

    # Set during _fetch_book; default 0 if missing.
    _bid_size_usd: float = 0.0
    _ask_size_usd: float = 0.0

    @property
    def composite_score(self) -> float:
        """Opportunity score for scalp universe ranking.

        The score intentionally avoids coin names and fixed spread
        floors. Hard liquidity thresholds decide whether a pair is
        admissible; among admissible pairs, ranking rewards volatility,
        depth, and measured trade count when available.
        """
        if not self.has_valid_opportunity_data:
            return 0.0
        trade_activity = float(self.trades_24h) if self.trades_24h > 0 else 1.0
        return (
            self.range_24h_bps
            * self.top_of_book_usd
            * trade_activity
        )


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
        trades_24h = int(stats.get("trade_count") or tk.get("trade_count") or 0)
        high_24h = float(stats.get("high") or 0.0)
        low_24h = float(stats.get("low") or 0.0)
    except (TypeError, ValueError):
        return None
    cand = _PairCandidate(
        ticker=ticker,
        volume_24h_base=volume_24h_base,
        last_price=last_price,
        bid=bid,
        ask=ask,
        trades_24h=trades_24h,
        high_24h=high_24h,
        low_24h=low_24h,
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
    min_range_24h_bps: float = 0.0,
) -> tuple[bool, Optional[str]]:
    """All configured gates must pass. Returns (passed, reject_reason)."""
    if cand.volume_24h_usd < min_volume_24h_usd:
        return False, UNIVERSE_REJECT_VOLUME_BELOW
    if cand.spread_bps > max_spread_bps:
        return False, UNIVERSE_REJECT_SPREAD_ABOVE
    if cand.top_of_book_usd < min_top_of_book_usd:
        return False, UNIVERSE_REJECT_TOP_OF_BOOK_BELOW
    if cand.range_24h_bps < min_range_24h_bps:
        return False, UNIVERSE_REJECT_RANGE_BELOW
    if cand.trades_24h > 0 and cand.trades_24h < min_trades_24h:
        return False, UNIVERSE_REJECT_TRADES_BELOW
    return True, None


def passes_shadow_exploration_gates(
    cand: _PairCandidate,
    *,
    min_volume_24h_usd: float,
    max_spread_bps: float,
    min_top_of_book_usd: float,
    min_trades_24h: int,
    min_range_24h_bps: float,
) -> bool:
    """Shadow-only exploration eligibility when depth is the shortfall.

    Active/live eligibility still requires ``passes_admission_gates``.
    This helper deliberately keeps volume, spread, trade-count,
    volatility, and a probe-sized touch-depth floor binding while
    relaxing only the much larger active top-of-book threshold. That
    lets the rotator learn on volatile-but-thinner symbols without
    wasting subscriptions on books too thin for the configured probe.
    """
    if not cand.has_valid_opportunity_data:
        return False
    if cand.volume_24h_usd < min_volume_24h_usd:
        return False
    if cand.spread_bps > max_spread_bps:
        return False
    if cand.top_of_book_usd < min_top_of_book_usd:
        return False
    if cand.range_24h_bps < min_range_24h_bps:
        return False
    if cand.trades_24h > 0 and cand.trades_24h < min_trades_24h:
        return False
    return True


def _adaptive_range_floor_bps(
    candidates: list[_PairCandidate],
    *,
    static_floor_bps: float,
    target_count: int,
    enabled: bool,
) -> tuple[float, float | None]:
    """Return (effective_floor, dynamic_component).

    The dynamic component is the 24h range of the last slot in the
    volatility cohort, where cohort size is the subscription target
    plus hysteresis. If the exchange does not have enough probe-eligible
    candidates to fill that cohort, use the candidate median instead of
    the weakest tail. That turns a poor opportunity set into fewer
    subscriptions rather than quietly backfilling low-range names.

    Volume/spread/trade gates and the shadow touch-depth floor are
    applied before this helper is called, so a book too thin for the
    configured probe cannot lift the floor.
    """
    floor = max(float(static_floor_bps or 0.0), 0.0)
    if not enabled or not candidates:
        return floor, None
    ranges = sorted(
        (float(c.range_24h_bps) for c in candidates if c.range_24h_bps > 0.0),
        reverse=True,
    )
    if not ranges:
        return floor, None
    slot = max(1, int(target_count or 1))
    if len(ranges) < slot:
        dynamic = ranges[len(ranges) // 2]
    else:
        dynamic = ranges[slot - 1]
    return max(floor, dynamic), dynamic


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


def _recent_missing_grace_count(db, ticker: str) -> int:
    """Count consecutive latest rows that were kept only for grace."""
    from sqlalchemy import text

    rows = db.execute(text("""
        SELECT status, rank, composite_score
        FROM fast_path_universe
        WHERE ticker = :ticker
        ORDER BY rotation_at DESC
        LIMIT 16
    """), {"ticker": ticker}).fetchall()
    count = 0
    for row in rows:
        status = str(getattr(row, "status", "") or "")
        rank = getattr(row, "rank", None)
        composite_score = getattr(row, "composite_score", None)
        if (
            status == UNIVERSE_STATUS_SHADOW
            and rank is None
            and composite_score is None
        ):
            count += 1
            continue
        break
    return count


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
        WHERE status = :shadow_status AND promoted_at IS NOT NULL
          AND promoted_at <= :cutoff
    """), {
        "cutoff": cutoff,
        "shadow_status": UNIVERSE_STATUS_SHADOW,
    }).fetchall()
    return {r.ticker for r in rows}


def _promotion_decay_source(settings) -> tuple[str, float]:
    """Return the decay table + per-side fee used for universe promotion."""
    exec_mode = str(getattr(settings, "execution_mode", "taker") or "taker").lower()
    from .fees import fee_bps_for_execution_mode

    fee_bps, _fee_detail = fee_bps_for_execution_mode(settings, exec_mode)
    if exec_mode in ("maker_only", "maker_first_then_taker"):
        return ("fast_signal_decay_maker_filled", fee_bps)
    return ("fast_signal_decay", fee_bps)


def _promotion_edge_evidence(db, cand: _PairCandidate, settings) -> tuple[bool, dict[str, Any]]:
    """Ticker-level learned-edge check for shadow -> active promotion.

    A pair can look volatile and liquid yet still have no tradable signal.
    Promotion therefore requires at least one calibrated
    ticker/alert/bucket/horizon row whose mean forward return clears the
    same cost model used by the executor gates.
    """
    from sqlalchemy import text

    table, fee_bps = _promotion_decay_source(settings)
    if table not in ("fast_signal_decay", "fast_signal_decay_maker_filled"):
        raise ValueError(f"unsupported promotion decay table: {table!r}")

    min_samples = int(getattr(settings, "live_alpha_min_samples", 50) or 50)
    min_net_bps = float(getattr(settings, "live_alpha_min_net_bps", 0.0) or 0.0)
    spread_bps = float(cand.spread_bps or 0.0)
    cost_bps = 2.0 * (fee_bps + spread_bps)

    row = db.execute(text(f"""
        SELECT alert_type, score_bucket, horizon_s, sample_count, mean_return,
               (mean_return * 10000.0) - :cost_bps AS net_bps
        FROM {table}
        WHERE ticker = :ticker
          AND sample_count >= :min_samples
        ORDER BY net_bps DESC, sample_count DESC
        LIMIT 1
    """), {
        "ticker": cand.ticker,
        "min_samples": min_samples,
        "cost_bps": cost_bps,
    }).mappings().one_or_none()

    base = {
        "decay_table": table,
        "min_samples": min_samples,
        "cost_bps": round(cost_bps, 4),
        "fee_bps": round(fee_bps, 4),
        "spread_bps": round(spread_bps, 4),
        "min_net_bps": round(min_net_bps, 4),
    }
    if row is None:
        return False, {**base, "verdict": "no_decay_row"}

    net_bps = float(row["net_bps"] or 0.0)
    evidence = {
        **base,
        "verdict": "cleared" if net_bps >= min_net_bps else "below_cost",
        "alert_type": row["alert_type"],
        "score_bucket": row["score_bucket"],
        "horizon_s": int(row["horizon_s"] or 0),
        "sample_count": int(row["sample_count"] or 0),
        "mean_bps": round(float(row["mean_return"] or 0.0) * 10000.0, 4),
        "net_bps": round(net_bps, 4),
    }
    return net_bps >= min_net_bps, evidence


def _shadow_edge_exhaustion_evidence(
    db,
    cand: _PairCandidate,
    settings,
) -> tuple[bool, dict[str, Any]]:
    """Return True when every learned ticker lane is no longer worth shadowing.

    Shadow subscriptions are for learning. Once a ticker has decay rows
    and every observed lane is confidently negative or cost-impossible,
    keeping that symbol subscribed only spends websocket/decay budget on
    a known bad surface. This is ticker- and alert-name agnostic: lanes
    are whatever the scanner actually emitted into the decay table.
    """
    from collections import Counter, defaultdict
    from sqlalchemy import text

    from .signal_health import (
        SIGNAL_HEALTH_LEARNABLE_VERDICTS,
        summarize_signal_group,
    )

    table, fee_bps = _promotion_decay_source(settings)
    if table not in ("fast_signal_decay", "fast_signal_decay_maker_filled"):
        raise ValueError(f"unsupported promotion decay table: {table!r}")

    rows = db.execute(text(f"""
        SELECT ticker, alert_type, score_bucket, horizon_s, sample_count,
               mean_return, m2_return
        FROM {table}
        WHERE ticker = :ticker
          AND sample_count > 0
        ORDER BY alert_type, score_bucket, horizon_s
    """), {"ticker": cand.ticker}).mappings().all()

    base = {
        "decay_table": table,
        "fee_bps": round(float(fee_bps or 0.0), 4),
        "spread_bps": round(float(cand.spread_bps or 0.0), 4),
    }
    if not rows:
        return False, {**base, "verdict": "no_decay_row"}

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        d = dict(row)
        grouped[(str(d.get("alert_type")), str(d.get("score_bucket")))].append(d)

    lane_summaries: list[dict[str, Any]] = []
    for group in grouped.values():
        lane_summaries.append(
            summarize_signal_group(
                group,
                table=table,
                scope="ticker",
                fee_bps=float(fee_bps or 0.0),
                spread_bps=float(cand.spread_bps or 0.0),
                min_net_bps=float(
                    getattr(settings, "live_alpha_min_net_bps", 0.0) or 0.0
                ),
            )
        )

    verdict_counts = Counter(str(s.get("verdict") or "unknown") for s in lane_summaries)
    has_learnable_lane = any(
        str(s.get("verdict") or "") in SIGNAL_HEALTH_LEARNABLE_VERDICTS
        for s in lane_summaries
    )
    exhausted = bool(lane_summaries) and not has_learnable_lane
    return exhausted, {
        **base,
        "verdict": "edge_exhausted" if exhausted else "still_learning",
        "lane_verdict_counts": dict(verdict_counts),
        "lanes": [
            {
                "alert_type": s.get("alert_type"),
                "score_bucket": s.get("score_bucket"),
                "verdict": s.get("verdict"),
                "action": s.get("action"),
                "total_samples": s.get("total_samples"),
            }
            for s in lane_summaries
        ],
    }


def _persist_run_diagnostics(
    db,
    *,
    rotation_at: datetime,
    out: dict[str, Any],
    rows_to_write: list[dict[str, Any]],
) -> None:
    """Persist one rotator-pass summary for postmortem/API diagnostics.

    ``fast_path_universe`` stores one row per ticker. This companion row
    stores the pass-level facts that explain why the row set looks that
    way: effective volatility floor, gate rejection counts, and learned
    edge promotion blocks.
    """
    from sqlalchemy import text

    db.execute(text("""
        CREATE TABLE IF NOT EXISTS fast_path_universe_runs (
            id BIGSERIAL PRIMARY KEY,
            rotation_at TIMESTAMP NOT NULL,
            scanned INTEGER NOT NULL DEFAULT 0,
            snapshot_failures INTEGER NOT NULL DEFAULT 0,
            ranked_n INTEGER NOT NULL DEFAULT 0,
            active_n INTEGER NOT NULL DEFAULT 0,
            shadow_n INTEGER NOT NULL DEFAULT 0,
            inactive_n INTEGER NOT NULL DEFAULT 0,
            range_floor_static_bps DOUBLE PRECISION NULL,
            range_floor_dynamic_bps DOUBLE PRECISION NULL,
            range_floor_effective_bps DOUBLE PRECISION NULL,
            gate_rejections JSONB NOT NULL DEFAULT '{}'::jsonb,
            edge_promotion_blocks JSONB NOT NULL DEFAULT '{}'::jsonb,
            promotion_decay_table VARCHAR(64) NULL,
            promotion_fee_bps DOUBLE PRECISION NULL,
            promotion_min_samples INTEGER NULL,
            promotion_min_net_bps DOUBLE PRECISION NULL,
            exploration_fallback BOOLEAN NOT NULL DEFAULT FALSE,
            counters_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_fast_path_universe_runs_rotation_at
            ON fast_path_universe_runs (rotation_at DESC)
    """))

    active_n = sum(
        1 for row in rows_to_write
        if row.get("status") == UNIVERSE_STATUS_ACTIVE
    )
    shadow_n = sum(
        1 for row in rows_to_write
        if row.get("status") == UNIVERSE_STATUS_SHADOW
    )
    inactive_n = sum(
        1 for row in rows_to_write
        if row.get("status") == UNIVERSE_STATUS_INACTIVE
    )

    db.execute(text("""
        INSERT INTO fast_path_universe_runs (
            rotation_at, scanned, snapshot_failures, ranked_n,
            active_n, shadow_n, inactive_n,
            range_floor_static_bps, range_floor_dynamic_bps,
            range_floor_effective_bps,
            gate_rejections, edge_promotion_blocks,
            promotion_decay_table, promotion_fee_bps,
            promotion_min_samples, promotion_min_net_bps,
            exploration_fallback, counters_json
        ) VALUES (
            :rotation_at, :scanned, :snapshot_failures, :ranked_n,
            :active_n, :shadow_n, :inactive_n,
            :range_floor_static_bps, :range_floor_dynamic_bps,
            :range_floor_effective_bps,
            CAST(:gate_rejections AS JSONB),
            CAST(:edge_promotion_blocks AS JSONB),
            :promotion_decay_table, :promotion_fee_bps,
            :promotion_min_samples, :promotion_min_net_bps,
            :exploration_fallback,
            CAST(:counters_json AS JSONB)
        )
    """), {
        "rotation_at": rotation_at,
        "scanned": int(out.get("scanned") or 0),
        "snapshot_failures": int(out.get("snapshot_failures") or 0),
        "ranked_n": int(out.get("ranked_n") or 0),
        "active_n": active_n,
        "shadow_n": shadow_n,
        "inactive_n": inactive_n,
        "range_floor_static_bps": out.get("range_floor_static_bps"),
        "range_floor_dynamic_bps": out.get("range_floor_dynamic_bps"),
        "range_floor_effective_bps": out.get("range_floor_effective_bps"),
        "gate_rejections": json.dumps(dict(out.get("gate_rejections") or {})),
        "edge_promotion_blocks": json.dumps(
            dict(out.get("edge_promotion_blocks") or {})
        ),
        "promotion_decay_table": out.get("promotion_decay_table"),
        "promotion_fee_bps": out.get("promotion_fee_bps"),
        "promotion_min_samples": out.get("promotion_min_samples"),
        "promotion_min_net_bps": out.get("promotion_min_net_bps"),
        "exploration_fallback": bool(out.get("exploration_fallback")),
        "counters_json": json.dumps(dict(out)),
    })


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

    rotation_at = datetime.utcnow()
    out: dict[str, Any] = {
        "scanned": 0,
        "snapshot_failures": 0,
        "gate_rejections": {},
        "ranked_n": 0,
        "hard_ranked_n": 0,
        "promoted_to_active": 0,
        "promoted_to_shadow": 0,
        "kept_active": 0,
        "kept_shadow": 0,
        "shadow_window_pending": 0,
        "demoted_to_inactive": 0,
        "demoted_to_shadow": 0,
        "kept_shadow_missing_grace": 0,
        "exploration_fallback": False,
        "shadow_exploration_shortfall": 0,
        "edge_promotion_blocks": {},
        "edge_exhaustion_blocks": {},
        "edge_exhausted_demotions": 0,
        "promotion_decay_table": None,
        "promotion_fee_bps": None,
        "promotion_min_samples": int(
            getattr(settings, "live_alpha_min_samples", 50) or 50
        ),
        "promotion_min_net_bps": float(
            getattr(settings, "live_alpha_min_net_bps", 0.0) or 0.0
        ),
        "range_floor_static_bps": float(
            getattr(settings, "universe_min_range_24h_bps", 0.0) or 0.0
        ),
        "range_floor_dynamic_bps": None,
        "range_floor_effective_bps": None,
        "shadow_min_top_of_book_usd": None,
        "rotation_at": rotation_at.isoformat(),
    }

    if not getattr(settings, "universe_rotation_enabled", False):
        out["skipped_reason"] = "universe_rotation_disabled"
        return out

    promotion_decay_table, promotion_fee_bps = _promotion_decay_source(settings)
    out["promotion_decay_table"] = promotion_decay_table
    out["promotion_fee_bps"] = promotion_fee_bps

    products = list_usd_products_fn()
    out["scanned"] = len(products)
    if not products:
        out["skipped_reason"] = "no_products_returned"
        _persist_run_diagnostics(
            db, rotation_at=rotation_at, out=out, rows_to_write=[],
        )
        db.commit()
        return out

    # ``fetch_book_fn`` is held on the closure for tests that override
    # it independently of ``fetch_snapshot_fn``. Production
    # ``_fetch_pair_snapshot`` calls ``_fetch_book`` internally; the
    # extra hook here lets tests exercise the gate behaviour against
    # custom book shapes without subclassing the snapshot.
    _ = fetch_book_fn

    snapshots: list[_PairCandidate] = []
    valid_snapshots: list[_PairCandidate] = []
    base_range_candidates: list[_PairCandidate] = []
    reject_by_ticker: dict[str, str] = {}
    active_eligible_tickers: set[str] = set()
    for tk in products:
        snap = fetch_snapshot_fn(tk)
        if snap is None:
            out["snapshot_failures"] += 1
            continue
        snapshots.append(snap)
        if snap.has_valid_opportunity_data:
            valid_snapshots.append(snap)

    shadow_min_top_of_book_usd = max(
        float(getattr(settings, "universe_shadow_min_top_of_book_usd", 0.0) or 0.0),
        0.0,
    )
    out["shadow_min_top_of_book_usd"] = shadow_min_top_of_book_usd

    for snap in valid_snapshots:
        passed = passes_shadow_exploration_gates(
            snap,
            min_volume_24h_usd=settings.universe_min_volume_24h_usd,
            max_spread_bps=settings.universe_max_spread_bps,
            min_top_of_book_usd=shadow_min_top_of_book_usd,
            min_range_24h_bps=0.0,
            min_trades_24h=settings.universe_min_trades_24h,
        )
        if passed:
            base_range_candidates.append(snap)

    top_n = settings.universe_top_n
    range_floor_bps, dynamic_range_floor_bps = _adaptive_range_floor_bps(
        base_range_candidates,
        static_floor_bps=settings.universe_min_range_24h_bps,
        target_count=top_n + settings.universe_hysteresis_ranks,
        enabled=bool(getattr(settings, "universe_adaptive_range_floor_enabled", True)),
    )
    out["range_floor_dynamic_bps"] = dynamic_range_floor_bps
    out["range_floor_effective_bps"] = range_floor_bps

    candidates: list[_PairCandidate] = []
    for snap in snapshots:
        passed, reject = passes_admission_gates(
            snap,
            min_volume_24h_usd=settings.universe_min_volume_24h_usd,
            max_spread_bps=settings.universe_max_spread_bps,
            min_top_of_book_usd=settings.universe_min_top_of_book_usd,
            min_range_24h_bps=range_floor_bps,
            min_trades_24h=settings.universe_min_trades_24h,
        )
        if not passed:
            reject_reason = str(reject or "unknown")
            if (
                reject == UNIVERSE_REJECT_TOP_OF_BOOK_BELOW
                and snap.top_of_book_usd < shadow_min_top_of_book_usd
            ):
                reject_reason = UNIVERSE_REJECT_SHADOW_TOP_OF_BOOK_BELOW
            reject_by_ticker[snap.ticker] = reject_reason
            out["gate_rejections"][reject_reason] = (
                out["gate_rejections"].get(reject_reason, 0) + 1
            )
            continue
        candidates.append(snap)
        active_eligible_tickers.add(snap.ticker)

    target_ranked = top_n + settings.universe_hysteresis_ranks
    exploration_pool = [
        snap for snap in valid_snapshots
        if passes_shadow_exploration_gates(
            snap,
            min_volume_24h_usd=settings.universe_min_volume_24h_usd,
            max_spread_bps=settings.universe_max_spread_bps,
            min_top_of_book_usd=shadow_min_top_of_book_usd,
            min_trades_24h=settings.universe_min_trades_24h,
            min_range_24h_bps=range_floor_bps,
        )
    ]
    if not candidates and exploration_pool:
        # Exploration fallback: if static admission thresholds reject the
        # whole exchange, subscribe the best data-valid opportunity set
        # as shadow-only. The volatility floor remains binding so quiet
        # products never re-enter through fallback.
        candidates = list(exploration_pool)
        out["exploration_fallback"] = True
    else:
        candidates.sort(key=lambda c: c.composite_score, reverse=True)
        out["hard_ranked_n"] = min(len(candidates), target_ranked)
        shortfall = max(target_ranked - len(candidates), 0)
        if shortfall > 0:
            active_candidate_tickers = {c.ticker for c in candidates}
            shadow_pool = [
                snap for snap in valid_snapshots
                if snap.ticker not in active_candidate_tickers
                and passes_shadow_exploration_gates(
                    snap,
                    min_volume_24h_usd=settings.universe_min_volume_24h_usd,
                    max_spread_bps=settings.universe_max_spread_bps,
                    min_top_of_book_usd=shadow_min_top_of_book_usd,
                    min_trades_24h=settings.universe_min_trades_24h,
                    min_range_24h_bps=range_floor_bps,
                )
            ]
            shadow_pool.sort(key=lambda c: c.composite_score, reverse=True)
            fill = shadow_pool[:shortfall]
            candidates.extend(fill)
            out["shadow_exploration_shortfall"] = len(fill)

    if out["exploration_fallback"]:
        candidates.sort(key=lambda c: c.composite_score, reverse=True)
    cut_ranked = candidates[:target_ranked]
    out["ranked_n"] = len(cut_ranked)

    prior = _previous_pass_status(db)
    completed_shadows = _shadow_completion_promotions(
        db, shadow_window_h=settings.universe_shadow_window_h
    )

    rows_to_write: list[dict[str, Any]] = []
    edge_evidence_cache: dict[str, tuple[bool, dict[str, Any]]] = {}
    edge_exhaustion_cache: dict[str, tuple[bool, dict[str, Any]]] = {}

    def _has_edge(cand: _PairCandidate) -> bool:
        if cand.ticker not in edge_evidence_cache:
            edge_evidence_cache[cand.ticker] = _promotion_edge_evidence(
                db, cand, settings,
            )
        ok, evidence = edge_evidence_cache[cand.ticker]
        if not ok:
            verdict = str(evidence.get("verdict") or "blocked")
            blocks = out["edge_promotion_blocks"]
            blocks[verdict] = blocks.get(verdict, 0) + 1
        return ok

    def _edge_exhausted(cand: _PairCandidate) -> bool:
        if cand.ticker not in edge_exhaustion_cache:
            edge_exhaustion_cache[cand.ticker] = _shadow_edge_exhaustion_evidence(
                db, cand, settings,
            )
        exhausted, evidence = edge_exhaustion_cache[cand.ticker]
        if exhausted:
            counts = evidence.get("lane_verdict_counts") or {}
            blocks = out["edge_exhaustion_blocks"]
            for verdict, count in dict(counts).items():
                blocks[str(verdict)] = blocks.get(str(verdict), 0) + int(count or 0)
        return exhausted

    seen_in_this_pass: set[str] = set()
    for rank_idx, cand in enumerate(cut_ranked, start=1):
        seen_in_this_pass.add(cand.ticker)
        prior_status, prior_rank = prior.get(cand.ticker, (None, None))
        active_eligible = cand.ticker in active_eligible_tickers

        if _edge_exhausted(cand):
            rows_to_write.append({
                "ticker": cand.ticker,
                "status": UNIVERSE_STATUS_INACTIVE,
                "rank": None,
                "composite_score": cand.composite_score,
                "volume_24h_usd": cand.volume_24h_usd,
                "spread_bps": cand.spread_bps,
                "top_of_book_usd": cand.top_of_book_usd,
                "trades_24h": cand.trades_24h,
                "rotation_at": rotation_at,
                "promoted_at": None,
            })
            out["edge_exhausted_demotions"] += 1
            out["demoted_to_inactive"] += 1
            continue

        if rank_idx > top_n:
            # Outside top_n; only present in cut_ranked because of the
            # hysteresis buffer. If this pair WAS active and is now
            # outside top_n + hysteresis... that's caught below in the
            # "demoted" loop. If it was active and is in the buffer,
            # keep it active (hysteresis grace).
            if (
                prior_status == UNIVERSE_STATUS_ACTIVE
                and active_eligible
                and _has_edge(cand)
            ):
                status = UNIVERSE_STATUS_ACTIVE
                out["kept_active"] += 1
            elif prior_status == UNIVERSE_STATUS_ACTIVE:
                status = UNIVERSE_STATUS_SHADOW
                out["demoted_to_shadow"] += 1
            else:
                # Not eligible for promotion this pass; skip writing.
                continue
        elif not active_eligible:
            status = UNIVERSE_STATUS_SHADOW
            if prior_status == UNIVERSE_STATUS_SHADOW:
                out["kept_shadow"] += 1
            elif prior_status == UNIVERSE_STATUS_ACTIVE:
                out["demoted_to_shadow"] += 1
            else:
                out["promoted_to_shadow"] += 1
        elif prior_status is None:
            # Brand-new entrant -> shadow
            status = UNIVERSE_STATUS_SHADOW
            out["promoted_to_shadow"] += 1
        elif prior_status == UNIVERSE_STATUS_SHADOW:
            if cand.ticker in completed_shadows and _has_edge(cand):
                status = UNIVERSE_STATUS_ACTIVE
                out["promoted_to_active"] += 1
            else:
                status = UNIVERSE_STATUS_SHADOW
                out["kept_shadow"] += 1
                if cand.ticker not in completed_shadows:
                    out["shadow_window_pending"] += 1
        elif prior_status == UNIVERSE_STATUS_ACTIVE:
            if _has_edge(cand):
                status = UNIVERSE_STATUS_ACTIVE
                out["kept_active"] += 1
            else:
                status = UNIVERSE_STATUS_SHADOW
                out["demoted_to_shadow"] += 1
        else:
            # Inactive or unknown historical status -> re-enter as shadow.
            status = UNIVERSE_STATUS_SHADOW
            out["promoted_to_shadow"] += 1

        promoted_at: Optional[datetime] = None
        if (
            status == UNIVERSE_STATUS_SHADOW
            and prior_status != UNIVERSE_STATUS_SHADOW
        ):
            promoted_at = rotation_at  # start the shadow clock
        elif status == UNIVERSE_STATUS_ACTIVE:
            # Carry forward promoted_at from the prior shadow promotion
            # for audit; cheap to recompute by reading the prior row.
            promoted_at = (
                rotation_at
                if prior_status != UNIVERSE_STATUS_ACTIVE else None
            )

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
        if prior_status not in UNIVERSE_SUBSCRIBED_STATUSES:
            continue
        reject_reason = reject_by_ticker.get(ticker, "snapshot_missing")
        if reject_reason not in UNIVERSE_HARD_DEMOTE_REASONS:
            grace_passes = max(
                int(getattr(settings, "universe_missing_grace_passes", 2) or 0),
                0,
            )
            if _recent_missing_grace_count(db, ticker) < grace_passes:
                rows_to_write.append({
                    "ticker": ticker,
                    "status": UNIVERSE_STATUS_SHADOW,
                    "rank": None,
                    "composite_score": None,
                    "volume_24h_usd": None,
                    "spread_bps": None,
                    "top_of_book_usd": None,
                    "trades_24h": None,
                    "rotation_at": rotation_at,
                    "promoted_at": None,
                })
                out["kept_shadow_missing_grace"] += 1
                continue
        rows_to_write.append({
            "ticker": ticker,
            "status": UNIVERSE_STATUS_INACTIVE,
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

    _persist_run_diagnostics(
        db, rotation_at=rotation_at, out=out, rows_to_write=rows_to_write,
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
          AND status = :active_status
        ORDER BY rank ASC NULLS LAST
    """), {"active_status": UNIVERSE_STATUS_ACTIVE}).fetchall()
    return [r.ticker for r in rows]


def get_subscribed_pairs(db) -> list[str]:
    """Active + shadow combined -- the full WS subscription set.

    Ranked shadow pairs need to be subscribed so ``decay_miner`` can
    collect samples during the cold-start window. Unranked shadow rows
    are grace/audit records for transient misses; they stay out of the
    websocket subscription because the current rotation did not prove
    they still meet the learning-universe floor.
    """
    from sqlalchemy import text

    rows = db.execute(text("""
        WITH latest_rotation AS (
            SELECT MAX(rotation_at) AS ts FROM fast_path_universe
        )
        SELECT ticker
        FROM fast_path_universe
        WHERE rotation_at = (SELECT ts FROM latest_rotation)
          AND (
              status = :active_status
              OR (status = :shadow_status AND rank IS NOT NULL)
          )
        ORDER BY rank ASC NULLS LAST
    """), {
        "active_status": UNIVERSE_STATUS_ACTIVE,
        "shadow_status": UNIVERSE_STATUS_SHADOW,
    }).fetchall()
    return [r.ticker for r in rows]


__all__ = [
    "run_rotation_pass",
    "get_active_pairs",
    "get_subscribed_pairs",
    "passes_admission_gates",
    "passes_shadow_exploration_gates",
]
