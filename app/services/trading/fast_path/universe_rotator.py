"""f-fastpath-universe-rotation (2026-05-07): hourly rotator that
populates ``fast_path_universe`` with the top-N mid-tier USD pairs
from Coinbase.

One pass per invocation:

1. List all USD-quoted Coinbase products that are online + tradable.
2. For each, fetch ``stats`` (24h volume) and ``ticker`` (best bid/ask).
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

Rate-limited to ~8 req/s (below the documented 10 req/s).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

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
        # round-trip we hit both.
        # Stats endpoint doesn't carry size; ticker endpoint carries
        # ``size`` for the last trade and ``bid_size`` / ``ask_size``
        # for top-of-book in the L2 product. Use the bid/ask sizes
        # downstream caller passes; default 0 if unknown.
        return min(self._bid_size_usd, self._ask_size_usd)

    # Set during _fetch_ticker; default 0 if missing.
    _bid_size_usd: float = 0.0
    _ask_size_usd: float = 0.0

    @property
    def composite_score(self) -> float:
        """``volume / max(spread, 0.5)`` -- alpha-replay-validated."""
        return self.volume_24h_usd / max(self.spread_bps, 0.5)


def _http_get_json(url: str) -> Optional[Any]:
    """Public-API GET with timeout. Returns None on any error."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "chili-fast-path-rotator/1"}
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.debug("[fast_path_rotator] GET %s failed: %s", url, e)
        return None
    except Exception as e:
        logger.warning(
            "[fast_path_rotator] GET %s unexpected failure: %s", url, e
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
        if p.get("auction_mode"):
            continue
        pid = p.get("id")
        if isinstance(pid, str) and pid:
            out.append(pid.upper())
    return out


def _fetch_pair_snapshot(ticker: str) -> Optional[_PairCandidate]:
    """Hit /stats + /ticker for one ticker. Returns None on error."""
    stats = _http_get_json(f"{_COINBASE_REST}/products/{ticker}/stats")
    time.sleep(_PER_REQ_PACING_S)
    tk = _http_get_json(f"{_COINBASE_REST}/products/{ticker}/ticker")
    time.sleep(_PER_REQ_PACING_S)
    if not isinstance(stats, dict) or not isinstance(tk, dict):
        return None
    try:
        volume_24h_base = float(stats.get("volume") or 0.0)
        last_price = float(tk.get("price") or 0.0)
        bid = float(tk.get("bid") or 0.0)
        ask = float(tk.get("ask") or 0.0)
        trades_24h = int(stats.get("trade_count") or 0)
        bid_size_base = float(tk.get("bid_size") or 0.0)
        ask_size_base = float(tk.get("ask_size") or 0.0)
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
) -> dict[str, Any]:
    """Single-pass rotator. Returns a counter dict for the audit log.

    ``list_usd_products_fn`` and ``fetch_snapshot_fn`` are injectable
    for testing -- unit tests substitute synthetic data instead of
    hitting Coinbase live.
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
