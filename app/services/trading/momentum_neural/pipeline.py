"""Activation hook: refresh momentum intelligence into BrainNodeState."""

from __future__ import annotations

import logging
import math
from types import SimpleNamespace
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import BrainActivationEvent, MomentumSymbolViability
from ..brain_neural_mesh.repository import get_or_create_state
from ..brain_neural_mesh.schema import mesh_enabled

from .context import build_momentum_regime_context
from .evolution import record_evolution_trace
from .features import ExecutionReadinessFeatures
from .telemetry import log_tick
from .variants import iter_momentum_families
from .viability import score_viability
from .viability_scope import VIABILITY_SCOPE_AGGREGATE, VIABILITY_SCOPE_SYMBOL

HUB_NODE_ID = "nm_momentum_crypto_intel"
VIABILITY_NODE_ID = "nm_momentum_viability_pool"

_log = logging.getLogger(__name__)


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _settings_float(name: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _settings_int(name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _depth_levels(levels: Any, n: int = 5) -> list[tuple[float, float]]:
    if not isinstance(levels, list) or not levels:
        return []
    out: list[tuple[float, float]] = []
    for raw in levels[:n]:
        if isinstance(raw, dict):
            price = _finite_float(raw.get("price") or raw.get("px") or raw.get("p"))
            size = _finite_float(raw.get("size") or raw.get("qty") or raw.get("q"))
        else:
            try:
                price = _finite_float(raw[0])
                size = _finite_float(raw[1])
            except Exception:
                price = size = None
        if price is None or size is None or size <= 0:
            continue
        out.append((price, size))
    return out


def _depth_levels_total(levels: Any, n: int = 5) -> tuple[float | None, float | None]:
    parsed = _depth_levels(levels, n=n)
    if not parsed:
        return None, None
    return parsed[0][0], sum(size for _, size in parsed)


def _size_near_level(levels: list[tuple[float, float]], target: float | None, band_abs: float) -> float | None:
    if target is None or target <= 0 or not levels:
        return None
    distances = [(abs(price - target), size) for price, size in levels]
    best_dist = min(dist for dist, _size in distances)
    if best_dist > band_abs:
        return None
    matched = [size for dist, size in distances if abs(dist - best_dist) <= 1e-9]
    if not matched:
        return None
    return sum(matched)


def _pctile_low(values: list[float], latest: float) -> float | None:
    clean = [float(v) for v in values if math.isfinite(float(v))]
    if not clean:
        return None
    below_or_equal = sum(1 for value in clean if value <= latest)
    return max(0.0, min(1.0, below_or_equal / float(len(clean))))


def _target_level_band_abs(*, target_level: float | None, bid: float | None, ask: float | None) -> float:
    if target_level is None or target_level <= 0:
        return 0.0
    spread_abs = 0.0
    if bid is not None and ask is not None and bid > 0 and ask > bid:
        spread_abs = ask - bid
    try:
        level_mult = float(getattr(settings, "chili_momentum_l2_level_match_spread_mult", 2.0) or 2.0)
    except (TypeError, ValueError):
        level_mult = 2.0
    level_band_abs = spread_abs * max(0.0, level_mult)
    if level_band_abs <= 0:
        level_band_abs = target_level * 0.0001
    return level_band_abs


def _rank_pctile(values: list[float], latest: float) -> float | None:
    clean = [float(v) for v in values if math.isfinite(float(v))]
    if not clean:
        return None
    below_or_equal = sum(1 for value in clean if value <= latest)
    return max(0.0, min(1.0, below_or_equal / float(len(clean))))


def _ladder_from_rows(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    max_age_s: float,
    min_snaps: int,
    target_level: float | None,
) -> Any | None:
    if not rows:
        return None
    newest = rows[0]
    observed = newest.get("observed_at") or newest.get("snapshot_at")
    if observed is None:
        return None
    if getattr(observed, "tzinfo", None) is not None:
        observed = observed.replace(tzinfo=None)
    age_s = max(0.0, (now - observed).total_seconds())
    if age_s > max_age_s:
        return None

    enriched: list[dict[str, float]] = []
    for row in rows:
        bid = _finite_float(row.get("bid_top"))
        ask = _finite_float(row.get("ask_top"))
        bid5 = _finite_float(row.get("bid5_size"))
        ask5 = _finite_float(row.get("ask5_size"))
        bid_levels = _depth_levels(row.get("bid_levels") or row.get("bids_json"))
        ask_levels = _depth_levels(row.get("ask_levels") or row.get("asks_json"))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            bid, bid5 = _depth_levels_total(row.get("bid_levels") or row.get("bids_json"))
            ask, ask5 = _depth_levels_total(row.get("ask_levels") or row.get("asks_json"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or bid >= ask:
            continue
        bid5 = bid5 if bid5 is not None and bid5 >= 0 else 0.0
        ask5 = ask5 if ask5 is not None and ask5 >= 0 else 0.0
        total = bid5 + ask5
        imbalance = _finite_float(row.get("imbalance5"))
        if imbalance is None and total > 0:
            imbalance = (bid5 - ask5) / total
        if imbalance is None:
            continue
        mid = 0.5 * (bid + ask)
        level_band_abs = _target_level_band_abs(target_level=target_level, bid=bid, ask=ask)
        bid_level_size = _size_near_level(bid_levels, target_level, level_band_abs)
        ask_level_size = _size_near_level(ask_levels, target_level, level_band_abs)
        micro_edge = None
        if total > 0 and mid > 0:
            micro = (ask * bid5 + bid * ask5) / total
            micro_edge = ((micro - mid) / mid) * 10_000.0
        row_out = {
            "bid": bid,
            "ask": ask,
            "bid5": bid5,
            "ask5": ask5,
            "imbalance": imbalance,
            "spread_bps": ((ask - bid) / mid) * 10_000.0 if mid > 0 else 0.0,
            "micro_edge": micro_edge if micro_edge is not None else 0.0,
        }
        if bid_level_size is not None:
            row_out["bid_level_size"] = bid_level_size
        if ask_level_size is not None:
            row_out["ask_level_size"] = ask_level_size
        enriched.append(row_out)
    if len(enriched) < min_snaps:
        return None
    latest = enriched[0]
    oldest = enriched[-1]
    old_ask = max(1.0, float(oldest["ask5"]))
    old_bid = max(1.0, float(oldest["bid5"]))
    ask_level_sizes = [
        float(row["ask_level_size"])
        for row in enriched
        if row.get("ask_level_size") is not None
    ]
    ask_eaten_frac = None
    ask_eaten_pctile = None
    ask_eaten_confirmed = False
    bid_refill_frac = None
    bid_refill_pctile = None
    bid_refill_confirmed = False
    if len(ask_level_sizes) >= min_snaps:
        latest_level_size = ask_level_sizes[0]
        oldest_level_size = max(1.0, ask_level_sizes[-1])
        ask_eaten_frac = (oldest_level_size - latest_level_size) / oldest_level_size
        ask_eaten_pctile = _pctile_low(ask_level_sizes, latest_level_size)
        try:
            pctile_ceiling = float(getattr(settings, "chili_momentum_l2_ask_eaten_pctile_ceiling", 0.50) or 0.50)
        except (TypeError, ValueError):
            pctile_ceiling = 0.50
        ask_eaten_confirmed = bool(
            ask_eaten_frac > 0.0
            and ask_eaten_pctile is not None
            and ask_eaten_pctile <= max(0.0, min(1.0, pctile_ceiling))
        )
    bid_level_sizes = [
        float(row["bid_level_size"])
        for row in enriched
        if row.get("bid_level_size") is not None
    ]
    if len(bid_level_sizes) >= min_snaps:
        latest_bid_level_size = bid_level_sizes[0]
        oldest_bid_level_size = max(1.0, bid_level_sizes[-1])
        bid_refill_frac = (latest_bid_level_size - oldest_bid_level_size) / oldest_bid_level_size
        bid_refill_pctile = _rank_pctile(bid_level_sizes, latest_bid_level_size)
        try:
            pctile_floor = float(getattr(settings, "chili_momentum_l2_bid_refill_pctile_floor", 0.50) or 0.50)
        except (TypeError, ValueError):
            pctile_floor = 0.50
        bid_refill_confirmed = bool(
            bid_refill_frac > 0.0
            and bid_refill_pctile is not None
            and bid_refill_pctile >= max(0.0, min(1.0, pctile_floor))
        )
    return SimpleNamespace(
        n_snaps=len(enriched),
        snapshot_age_s=age_s,
        ofi=float(latest["imbalance"] - oldest["imbalance"]),
        micro_edge=float(latest["micro_edge"]),
        depth_imbal_pctile=_rank_pctile([r["imbalance"] for r in enriched], float(latest["imbalance"])),
        ask_build=(float(latest["ask5"]) - float(oldest["ask5"])) / old_ask,
        bid_refill=(float(latest["bid5"]) - float(oldest["bid5"])) / old_bid,
        spread_bps=float(latest["spread_bps"]),
        ask_eaten_frac=ask_eaten_frac,
        ask_eaten_pctile=ask_eaten_pctile,
        ask_eaten_confirmed=ask_eaten_confirmed,
        bid_refill_frac=bid_refill_frac,
        bid_refill_pctile=bid_refill_pctile,
        bid_refill_confirmed=bid_refill_confirmed,
    )


def read_ladder_distribution(
    symbol: str,
    *,
    db: Session,
    as_of: Any = None,
    target_level: float | None = None,
) -> Any | None:
    """Read a fresh, self-normalized L2 ladder window for entry gates.

    Equity reads `iqfeed_depth_snapshots`; crypto reads `fast_orderbook`. Missing,
    stale, crossed, or too-thin data returns `None` so entry-only L2 triggers fail
    closed while legacy vetoes continue to fail open at their call sites.
    """
    try:
        sym = (symbol or "").strip().upper()
        if not sym or db is None:
            return None
        max_age_s = max(0.25, float(getattr(settings, "chili_momentum_l2_snapshot_max_age_seconds", 5.0) or 5.0))
        min_snaps = max(1, int(getattr(settings, "chili_momentum_l2_distribution_min_snaps", 3) or 3))
        limit = max(
            min_snaps,
            int(getattr(settings, "chili_momentum_l2_distribution_window_snaps", 8) or 8),
        )
        now = as_of if isinstance(as_of, datetime) else datetime.utcnow()
        if getattr(now, "tzinfo", None) is not None:
            now = now.replace(tzinfo=None)
        if sym.endswith("-USD"):
            rows = (
                db.execute(
                    text(
                        """
                        SELECT snapshot_at, bid_levels, ask_levels, imbalance
                        FROM fast_orderbook
                        WHERE ticker=:symbol AND snapshot_at <= :as_of
                        ORDER BY snapshot_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"symbol": sym, "as_of": now, "limit": limit},
                )
                .mappings()
                .all()
            )
        else:
            rows = (
                db.execute(
                    text(
                        """
                        SELECT observed_at, bid_top, ask_top, bid_top_size, ask_top_size,
                               bid5_size, ask5_size, imbalance5, bids_json, asks_json
                        FROM iqfeed_depth_snapshots
                        WHERE symbol=:symbol AND observed_at <= :as_of
                        ORDER BY observed_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"symbol": sym, "as_of": now, "limit": limit},
                )
                .mappings()
                .all()
            )
        return _ladder_from_rows(
            [dict(row) for row in rows],
            now=now,
            max_age_s=max_age_s,
            min_snaps=min_snaps,
            target_level=_finite_float(target_level),
        )
    except Exception:
        return None


def read_target_level_trade_prints(
    symbol: str,
    *,
    db: Session,
    target_level: float,
    as_of: Any = None,
    window_s: float | None = None,
) -> Any | None:
    """Attribute a target-level ask-eaten quote event to actual trade prints.

    Returns telemetry only. Missing/stale/noisy trade data returns ``None`` so callers
    can keep their existing gate behavior without turning print availability into a
    hidden blocker.
    """
    try:
        sym = (symbol or "").strip().upper()
        level = _finite_float(target_level)
        if not sym or sym.endswith("-USD") or db is None or level is None or level <= 0:
            return None
        try:
            w = float(window_s) if window_s is not None else float(
                getattr(settings, "chili_momentum_l2_target_print_window_seconds", 15.0) or 15.0
            )
        except (TypeError, ValueError):
            w = 15.0
        w = max(0.25, w)
        now = as_of if isinstance(as_of, datetime) else datetime.utcnow()
        if getattr(now, "tzinfo", None) is not None:
            now = now.replace(tzinfo=None)
        rows = (
            db.execute(
                text(
                    """
                    SELECT observed_at, price, size, bid, ask
                    FROM iqfeed_trade_ticks
                    WHERE symbol=:symbol
                      AND observed_at > :as_of - make_interval(secs => :window_s)
                      AND observed_at <= :as_of
                    ORDER BY observed_at ASC
                    """
                ),
                {"symbol": sym, "as_of": now, "window_s": w},
            )
            .mappings()
            .all()
        )
        clean: list[dict[str, float]] = []
        newest_at = None
        for raw in rows:
            row = dict(raw)
            price = _finite_float(row.get("price"))
            size = _finite_float(row.get("size"))
            if price is None or size is None or price <= 0 or size <= 0:
                continue
            bid = _finite_float(row.get("bid"))
            ask = _finite_float(row.get("ask"))
            ask_ref = ask if ask is not None and ask > 0 else level
            lifted = price >= min(level, ask_ref)
            clean.append(
                {
                    "price": price,
                    "size": size,
                    "lifted": 1.0 if lifted else 0.0,
                    "at_target": 1.0 if price >= level else 0.0,
                }
            )
            observed = row.get("observed_at")
            if observed is not None:
                newest_at = observed
        if not clean:
            return None
        total_vol = sum(row["size"] for row in clean)
        if total_vol <= 0:
            return None
        lift_vol = sum(row["size"] for row in clean if row["lifted"] > 0)
        target_vol = sum(row["size"] for row in clean if row["at_target"] > 0)
        age_s = None
        if newest_at is not None:
            if getattr(newest_at, "tzinfo", None) is not None:
                newest_at = newest_at.replace(tzinfo=None)
            age_s = max(0.0, (now - newest_at).total_seconds())
        return SimpleNamespace(
            n_prints=len(clean),
            print_window_s=w,
            target_print_volume=float(target_vol),
            ask_lift_volume=float(lift_vol),
            total_print_volume=float(total_vol),
            ask_lift_ratio=float(lift_vol / total_vol),
            target_print_ratio=float(target_vol / total_vol),
            latest_print_age_s=age_s,
            ask_lift_confirmed=lift_vol > 0,
        )
    except Exception:
        return None


def _live_ofi_microprice(
    symbol: str,
    *,
    db: Session,
    as_of: Any = None,
    window_s: float | None = None,
) -> tuple[float | None, float | None]:
    """Fresh L2 OFI + microprice edge reader used by live entry/add/exit paths."""
    _ = window_s
    ladder = read_ladder_distribution(symbol, db=db, as_of=as_of)
    if ladder is None:
        return None, None
    return _finite_float(getattr(ladder, "ofi", None)), _finite_float(getattr(ladder, "micro_edge", None))


def _live_book_imbalance(
    symbol: str,
    *,
    db: Session,
    as_of: Any = None,
) -> float | None:
    ladder = read_ladder_distribution(symbol, db=db, as_of=as_of)
    if ladder is None:
        return None
    pctile = _finite_float(getattr(ladder, "depth_imbal_pctile", None))
    if pctile is not None:
        return pctile
    return _finite_float(getattr(ladder, "ofi", None))


def _trade_tick_rows(
    symbol: str,
    *,
    db: Session,
    as_of: Any = None,
    window_s: float | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    try:
        sym = (symbol or "").strip().upper()
        if not sym or sym.endswith("-USD") or db is None:
            return []
        w = (
            max(0.25, float(window_s))
            if window_s is not None
            else _settings_float("chili_momentum_trade_flow_window_seconds", 15.0, lo=0.25)
        )
        now = as_of if isinstance(as_of, datetime) else datetime.utcnow()
        if getattr(now, "tzinfo", None) is not None:
            now = now.replace(tzinfo=None)
        tick_limit = (
            max(1, int(limit))
            if limit is not None
            else _settings_int("chili_momentum_trade_flow_tick_limit", 200, lo=1)
        )
        rows = (
            db.execute(
                text(
                    """
                    SELECT observed_at, price, size, bid, ask
                    FROM iqfeed_trade_ticks
                    WHERE symbol=:symbol
                      AND observed_at > :as_of - make_interval(secs => :window_s)
                      AND observed_at <= :as_of
                    ORDER BY observed_at ASC
                    LIMIT :limit
                    """
                ),
                {"symbol": sym, "as_of": now, "window_s": w, "limit": tick_limit},
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]
    except Exception:
        return []


def _live_trade_flow(
    symbol: str,
    *,
    db: Session,
    as_of: Any = None,
    window_s: float | None = None,
) -> float | None:
    """Signed trade-flow ratio in [-1, 1] from fresh IQFeed prints."""
    rows = _trade_tick_rows(symbol, db=db, as_of=as_of, window_s=window_s)
    signed = 0.0
    total = 0.0
    for row in rows:
        price = _finite_float(row.get("price"))
        size = _finite_float(row.get("size"))
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        bid = _finite_float(row.get("bid"))
        ask = _finite_float(row.get("ask"))
        direction = 0.0
        if ask is not None and ask > 0 and price >= ask:
            direction = 1.0
        elif bid is not None and bid > 0 and price <= bid:
            direction = -1.0
        elif bid is not None and ask is not None and ask > bid:
            direction = 1.0 if price >= (bid + ask) * 0.5 else -1.0
        signed += direction * size
        total += size
    if total <= 0:
        return None
    return max(-1.0, min(1.0, signed / total))


def _live_flow_slope(
    symbol: str,
    *,
    db: Session,
    as_of: Any = None,
    window_s: float | None = None,
) -> dict[str, float | None] | None:
    """Return latest L2 imbalance and slope from recent IQFeed depth snapshots."""
    try:
        sym = (symbol or "").strip().upper()
        if not sym or sym.endswith("-USD") or db is None:
            return None
        w = (
            max(0.25, float(window_s))
            if window_s is not None
            else _settings_float("chili_momentum_flow_slope_window_seconds", 15.0, lo=0.25)
        )
        snapshot_limit = _settings_int("chili_momentum_flow_slope_snapshot_limit", 64, lo=2)
        now = as_of if isinstance(as_of, datetime) else datetime.utcnow()
        if getattr(now, "tzinfo", None) is not None:
            now = now.replace(tzinfo=None)
        rows = (
            db.execute(
                text(
                    """
                    SELECT observed_at, bid5_size, ask5_size, imbalance5
                    FROM iqfeed_depth_snapshots
                    WHERE symbol=:symbol
                      AND observed_at > :as_of - make_interval(secs => :window_s)
                      AND observed_at <= :as_of
                    ORDER BY observed_at ASC
                    LIMIT :limit
                    """
                ),
                {"symbol": sym, "as_of": now, "window_s": max(0.25, w), "limit": snapshot_limit},
            )
            .mappings()
            .all()
        )
        vals: list[float] = []
        for raw in rows:
            row = dict(raw)
            imb = _finite_float(row.get("imbalance5"))
            if imb is None:
                bid5 = _finite_float(row.get("bid5_size"))
                ask5 = _finite_float(row.get("ask5_size"))
                if bid5 is not None and ask5 is not None and bid5 + ask5 > 0:
                    imb = (bid5 - ask5) / (bid5 + ask5)
            if imb is not None:
                vals.append(float(imb))
        if not vals:
            return None
        return {"ofi_level": vals[-1], "ofi_slope": vals[-1] - vals[0]}
    except Exception:
        return None


def _live_realized_vol(
    symbol: str,
    *,
    db: Session,
    as_of: Any = None,
    window_s: float | None = None,
) -> float | None:
    vol_window_s = (
        max(0.25, float(window_s))
        if window_s is not None
        else _settings_float("chili_momentum_realized_vol_window_seconds", 60.0, lo=0.25)
    )
    tick_limit = _settings_int("chili_momentum_realized_vol_tick_limit", 300, lo=3)
    min_ticks = _settings_int("chili_momentum_realized_vol_min_ticks", 3, lo=3)
    rows = _trade_tick_rows(symbol, db=db, as_of=as_of, window_s=vol_window_s, limit=tick_limit)
    prices = [_finite_float(row.get("price")) for row in rows]
    clean = [float(px) for px in prices if px is not None and px > 0]
    if len(clean) < min_ticks:
        return None
    rets = [
        math.log(clean[i] / clean[i - 1])
        for i in range(1, len(clean))
        if clean[i - 1] > 0 and clean[i] > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((ret - mean) ** 2 for ret in rets) / max(1, len(rets) - 1)
    return math.sqrt(max(0.0, var))


def _hydrate_recent_ross_evidence(
    db: Session,
    *,
    symbols: list[str],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Carry recent Ross/pillar evidence through symbol-focused refreshes.

    A manual/operator refresh often only contains ``{"tickers": [...]}``. Without
    hydration that bare refresh overwrites rich scanner evidence with a generic
    row, making an equity look live-ready for the wrong reason or losing its
    priority. Explicit incoming evidence always wins; this only fills missing
    symbols from fresh persisted viability rows.
    """
    explicit_signals = meta.get("ross_signals") if isinstance(meta.get("ross_signals"), dict) else {}
    explicit_scores = meta.get("ross_scores") if isinstance(meta.get("ross_scores"), dict) else {}
    if not symbols:
        return meta
    missing = [s for s in symbols if s not in explicit_signals and s not in explicit_scores]
    if not missing:
        return meta

    try:
        max_age = max(
            120.0,
            float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0),
        )
    except (TypeError, ValueError):
        max_age = 600.0
    cutoff = datetime.utcnow() - timedelta(seconds=max_age)

    hydrated_signals: dict[str, dict[str, Any]] = dict(explicit_signals)
    hydrated_scores: dict[str, Any] = dict(explicit_scores)
    hydrated_symbols: list[str] = []

    for sym in missing:
        row = (
            db.query(MomentumSymbolViability)
            .filter(
                MomentumSymbolViability.symbol == sym,
                MomentumSymbolViability.freshness_ts >= cutoff,
            )
            .order_by(MomentumSymbolViability.freshness_ts.desc(), MomentumSymbolViability.id.desc())
            .first()
        )
        if row is None:
            continue
        ex = row.execution_readiness_json if isinstance(row.execution_readiness_json, dict) else {}
        extra = ex.get("extra") if isinstance(ex.get("extra"), dict) else ex
        signals = extra.get("ross_signals") if isinstance(extra, dict) else None
        scores = extra.get("ross_scores") if isinstance(extra, dict) else None
        if isinstance(signals, dict) and isinstance(signals.get(sym), dict):
            hydrated_signals[sym] = dict(signals[sym])
            hydrated_symbols.append(sym)
            continue
        if isinstance(scores, dict) and sym in scores:
            hydrated_scores[sym] = scores[sym]
            hydrated_symbols.append(sym)

    if not hydrated_symbols:
        return meta

    out = dict(meta)
    if hydrated_signals:
        out["ross_signals"] = hydrated_signals
    if hydrated_scores and not hydrated_signals:
        out["ross_scores"] = hydrated_scores
    out["ross_evidence_hydrated_from_recent_viability"] = sorted(set(hydrated_symbols))
    return out


def maybe_run_momentum_neural_tick(
    db: Session,
    ev: BrainActivationEvent,
    *,
    graph_version: int = 1,
) -> None:
    """Run tick when activation event is a momentum context refresh."""
    if not settings.chili_momentum_neural_enabled:
        return
    if not mesh_enabled():
        return
    pl = ev.payload if isinstance(ev.payload, dict) else {}
    if ev.cause != "momentum_context_refresh" and pl.get("signal_type") != "momentum_context_refresh":
        return
    meta = pl.get("meta") if isinstance(pl.get("meta"), dict) else {}
    run_momentum_neural_tick(
        db,
        meta=meta,
        correlation_id=ev.correlation_id,
        graph_version=graph_version,
    )


def run_momentum_neural_tick(
    db: Session,
    *,
    meta: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    graph_version: int = 1,
) -> dict[str, Any]:
    """Compute regime + family viability; persist on hub and viability pool nodes."""
    _ = graph_version
    meta = dict(meta or {})
    tickers = meta.get("tickers")
    if isinstance(tickers, list) and tickers:
        symbols = [str(t).strip().upper() for t in tickers if t][:32]
        scope = VIABILITY_SCOPE_SYMBOL
    else:
        symbols = ["__aggregate__"]
        scope = VIABILITY_SCOPE_AGGREGATE

    if scope == VIABILITY_SCOPE_SYMBOL:
        meta = _hydrate_recent_ross_evidence(db, symbols=symbols, meta=meta)

    # Phase 6c: optional Hurst proxy from first symbol's recent closes (feeds regime context).
    if symbols and symbols[0].upper() != "__AGGREGATE__":
        try:
            from ..market_data import fetch_ohlcv_df

            from .entry_gates import hurst_proxy_from_closes

            df_h = fetch_ohlcv_df(symbols[0], interval="15m", period="5d")
            if df_h is not None and not df_h.empty and "Close" in df_h.columns:
                meta["hurst_proxy"] = hurst_proxy_from_closes(df_h["Close"])
        except Exception:
            pass

    # Ross momentum-quality (M2): the scanner bridge forwards the RVOL/gap/
    # daily-change/float signals it computed as meta["ross_signals"] instead of
    # discarding them. Rank the batch once here and pass each symbol's [0,1]
    # quality through ctx_meta below so score_viability prefers EXPLOSIVE
    # instruments. Strict no-op when absent.
    _ross_signals = meta.get("ross_signals")
    if isinstance(_ross_signals, dict) and _ross_signals:
        try:
            from .ross_momentum import score_universe as _ross_score_universe

            meta["ross_scores"] = {
                s: rs.score for s, rs in _ross_score_universe(_ross_signals).items()
            }
        except Exception:
            pass

    # E5: news-catalyst set (EARNINGS + fresh general NEWS headlines) for the catalyst
    # viability tilt. The fresh-news union is what catches Ross's explosive sympathy/
    # theme movers (a low-float small-cap that just printed a hot headline), not just
    # scheduled earnings. Best-effort + cached; empty -> no-op (degrades gracefully
    # without the news/Benzinga feed). (catalyst.py)
    try:
        from .catalyst import all_catalyst_symbols

        _cat = all_catalyst_symbols()
        if _cat:
            # MUST be a list, not a set: meta flows into the brain_node_states
            # local_state JSONB and a set is not JSON-serializable ("Object of type
            # set is not JSON serializable"), which would fail the ENTIRE viability
            # write and leave every symbol stale. (regression guard for #528)
            meta["catalyst_symbols"] = sorted(_cat)
    except Exception:
        pass

    ctx_meta = {
        k: meta[k]
        for k in (
            "spread_regime",
            "fee_burden_regime",
            "liquidity_regime",
            "exhaustion_cooldown",
            "rolling_range_state",
            "breakout_continuity",
            "realized_vol_rank",
            "atr_pct",
            "hurst_proxy",
            "adx",
            "adx_14",
            "ross_scores",
            "catalyst_symbols",
        )
        if k in meta
    }
    ctx = build_momentum_regime_context(
        realized_vol_rank=meta.get("realized_vol_rank"),
        atr_pct=meta.get("atr_pct"),
        meta=ctx_meta,
    )
    feats = ExecutionReadinessFeatures.from_meta(meta)

    rows: list[dict[str, Any]] = []
    for sym in symbols:
        for family in iter_momentum_families():
            vr = score_viability(sym, family, ctx, feats, db=db)
            d = vr.to_public_dict()
            d["scope"] = scope
            d["label"] = family.label
            d["entry_style"] = family.entry_style
            d["default_stop_logic"] = family.default_stop_logic
            d["default_exit_logic"] = family.default_exit_logic
            rows.append(d)

    rows.sort(key=lambda r: r["viability"], reverse=True)
    top = rows[0] if rows else {}

    now = datetime.utcnow().isoformat()
    hub_payload = {
        "momentum_neural_version": 1,
        "last_tick_utc": now,
        "correlation_id": correlation_id,
        "regime": ctx.to_public_dict(),
        "symbols_evaluated": symbols,
        "top_preview": rows[:8],
    }
    viability_payload = {
        "momentum_neural_version": 1,
        "last_tick_utc": now,
        "viability_rows": rows[:64],
        "correlation_id": correlation_id,
    }

    hub = get_or_create_state(db, HUB_NODE_ID)
    hub.local_state = hub_payload
    hub.last_activated_at = datetime.utcnow()
    hub.updated_at = datetime.utcnow()

    pool = get_or_create_state(db, VIABILITY_NODE_ID)
    pool.local_state = viability_payload
    pool.last_activated_at = datetime.utcnow()
    pool.updated_at = datetime.utcnow()

    record_evolution_trace(
        db,
        snapshot={
            "top_family_id": top.get("family_id"),
            "top_viability": top.get("viability"),
            "session_label": ctx.session_label,
        },
    )

    persistence_ok = True
    try:
        from .persistence import persist_neural_momentum_tick

        n = persist_neural_momentum_tick(
            db,
            row_dicts=rows,
            regime_snapshot=ctx.to_public_dict(),
            features=feats,
            correlation_id=correlation_id,
            source_node_id=HUB_NODE_ID,
        )
        if n:
            log_tick("persisted viability rows=%s", n)
    except Exception as e:
        _log.warning("[momentum_neural] viability persistence failed: %s", e)
        persistence_ok = False
        try:
            db.rollback()
        except Exception:
            _log.debug(
                "[momentum_neural] rollback after viability persistence failure failed",
                exc_info=True,
            )

    log_tick(
        "tick symbols=%s families=%s top=%s corr=%s",
        len(symbols),
        len(rows) // max(len(symbols), 1),
        top.get("family_id"),
        correlation_id,
    )
    return {"ok": True, "rows": len(rows), "top_family": top.get("family_id"), "persistence_ok": persistence_ok}
