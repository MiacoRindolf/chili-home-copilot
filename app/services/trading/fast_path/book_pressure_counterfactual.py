"""Counterfactual research harness for fast-path book pressure.

This module is deliberately read-only and runtime-agnostic. It replays
``fast_orderbook`` rows into rolling book-pressure windows, derives candidate
threshold variants from the observed window distribution, and evaluates each
variant with executable long returns:

    buy at the triggering window's current best ask
    sell at the first future best bid at or after the horizon

Because entry/exit spread is already included in that executable return, the
net-return math subtracts fee only. Runtime promotion still belongs to the
scanner/settings path after a candidate proves confidence-bounded net edge.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Any, Iterable

from .calibration import (
    NEGATIVE_EDGE_CONFIDENCE,
    _bounded_confidence,
    _student_t_critical,
)
from .scanner import BPS_PER_UNIT


@dataclass(frozen=True)
class BookPressureObservation:
    ticker: str
    snapshot_at: datetime
    best_bid: float
    best_ask: float
    mid: float
    imbalance: float
    spread_bps: float
    microprice_edge_bps: float
    top_bid_notional_usd: float
    top_ask_notional_usd: float
    bid_total_size: float
    ask_total_size: float


@dataclass(frozen=True)
class BookPressureWindow:
    ticker: str
    snapshot_at: datetime
    best_bid: float
    best_ask: float
    avg_imbalance: float
    avg_microprice_edge_bps: float
    current_microprice_edge_bps: float
    max_spread_bps: float
    min_touch_notional_usd: float
    mid_move_bps: float
    best_bid_move_bps: float


@dataclass(frozen=True)
class BookPressureVariant:
    name: str
    min_avg_imbalance: float | None = None
    min_avg_microprice_bps: float | None = None
    min_current_microprice_bps: float | None = None
    max_spread_bps: float | None = None
    min_touch_notional_usd: float | None = None
    min_mid_move_bps: float | None = None
    min_best_bid_move_bps: float | None = None
    max_mid_move_bps: float | None = None
    max_best_bid_move_bps: float | None = None
    cooldown_s: float = 0.0

    def passes(self, window: BookPressureWindow) -> bool:
        checks = (
            self.min_avg_imbalance is None
            or window.avg_imbalance >= self.min_avg_imbalance,
            self.min_avg_microprice_bps is None
            or window.avg_microprice_edge_bps >= self.min_avg_microprice_bps,
            self.min_current_microprice_bps is None
            or window.current_microprice_edge_bps >= self.min_current_microprice_bps,
            self.max_spread_bps is None
            or window.max_spread_bps <= self.max_spread_bps,
            self.min_touch_notional_usd is None
            or window.min_touch_notional_usd >= self.min_touch_notional_usd,
            self.min_mid_move_bps is None
            or window.mid_move_bps >= self.min_mid_move_bps,
            self.min_best_bid_move_bps is None
            or window.best_bid_move_bps >= self.min_best_bid_move_bps,
            self.max_mid_move_bps is None
            or window.mid_move_bps <= self.max_mid_move_bps,
            self.max_best_bid_move_bps is None
            or window.best_bid_move_bps <= self.max_best_bid_move_bps,
        )
        return all(checks)


@dataclass(frozen=True)
class VariantEvaluation:
    variant_name: str
    horizon_s: int
    sample_count: int
    mean_net_bps: float | None
    lower_net_bps: float | None
    upper_net_bps: float | None
    stdev_net_bps: float | None
    confidence: float
    gross_mean_bps: float | None
    fee_bps_per_side: float
    triggered_by_ticker: dict[str, int]
    verdict: str


def _first_level(levels: Any) -> tuple[float, float] | None:
    if not levels:
        return None
    first = levels[0]
    try:
        if isinstance(first, dict):
            price = float(first.get("price") or first.get("price_level"))
            size = float(first.get("size") or first.get("quantity"))
        else:
            price = float(first[0])
            size = float(first[1])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if price <= 0.0 or size <= 0.0:
        return None
    return price, size


def observation_from_book_row(row: dict[str, Any]) -> BookPressureObservation | None:
    bid_level = _first_level(row.get("bid_levels"))
    ask_level = _first_level(row.get("ask_levels"))
    if bid_level is None or ask_level is None:
        return None
    best_bid, top_bid_size = bid_level
    best_ask, top_ask_size = ask_level
    if best_ask <= best_bid:
        return None
    mid = (best_bid + best_ask) / 2.0
    spread_bps = float(row.get("spread_bps") or 0.0)
    if spread_bps <= 0.0:
        spread_bps = ((best_ask - best_bid) / mid) * BPS_PER_UNIT
    bid_total_size = float(row.get("bid_total_size") or top_bid_size)
    ask_total_size = float(row.get("ask_total_size") or top_ask_size)
    if bid_total_size <= 0.0 or ask_total_size <= 0.0:
        return None
    depth_denom = bid_total_size + ask_total_size
    microprice = (
        (best_ask * bid_total_size) + (best_bid * ask_total_size)
    ) / depth_denom
    snapshot_at = row.get("snapshot_at")
    if not isinstance(snapshot_at, datetime):
        return None
    imbalance = max(0.0, min(1.0, float(row.get("imbalance") or 0.5)))
    return BookPressureObservation(
        ticker=str(row.get("ticker") or ""),
        snapshot_at=snapshot_at,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        imbalance=imbalance,
        spread_bps=max(0.0, spread_bps),
        microprice_edge_bps=((microprice - mid) / mid) * BPS_PER_UNIT,
        top_bid_notional_usd=top_bid_size * best_bid,
        top_ask_notional_usd=top_ask_size * best_ask,
        bid_total_size=bid_total_size,
        ask_total_size=ask_total_size,
    )


def build_windows(
    observations: Iterable[BookPressureObservation],
    *,
    window_size: int,
) -> list[BookPressureWindow]:
    by_ticker: dict[str, list[BookPressureObservation]] = {}
    for obs in observations:
        if not obs.ticker:
            continue
        by_ticker.setdefault(obs.ticker, []).append(obs)

    out: list[BookPressureWindow] = []
    size = max(1, int(window_size or 1))
    for ticker, rows in by_ticker.items():
        rows = sorted(rows, key=lambda obs: obs.snapshot_at)
        window: deque[BookPressureObservation] = deque(maxlen=size)
        for obs in rows:
            window.append(obs)
            if len(window) < size:
                continue
            items = list(window)
            first = items[0]
            avg_imbalance = sum(o.imbalance for o in items) / float(size)
            avg_micro = sum(o.microprice_edge_bps for o in items) / float(size)
            mid_move_bps = (
                ((obs.mid - first.mid) / first.mid) * BPS_PER_UNIT
                if first.mid > 0.0
                else 0.0
            )
            best_bid_move_bps = (
                ((obs.best_bid - first.best_bid) / first.best_bid)
                * BPS_PER_UNIT
                if first.best_bid > 0.0
                else 0.0
            )
            out.append(BookPressureWindow(
                ticker=ticker,
                snapshot_at=obs.snapshot_at,
                best_bid=obs.best_bid,
                best_ask=obs.best_ask,
                avg_imbalance=avg_imbalance,
                avg_microprice_edge_bps=avg_micro,
                current_microprice_edge_bps=obs.microprice_edge_bps,
                max_spread_bps=max(o.spread_bps for o in items),
                min_touch_notional_usd=min(
                    min(o.top_bid_notional_usd, o.top_ask_notional_usd)
                    for o in items
                ),
                mid_move_bps=mid_move_bps,
                best_bid_move_bps=best_bid_move_bps,
            ))
    return sorted(out, key=lambda row: (row.ticker, row.snapshot_at))


def _quantile(values: Iterable[float], q: float) -> float | None:
    clean = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not clean:
        return None
    q = min(1.0, max(0.0, float(q)))
    idx = q * float(len(clean) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return clean[lo]
    weight = idx - float(lo)
    return clean[lo] * (1.0 - weight) + clean[hi] * weight


def _threshold(values: Iterable[float], q: float, *, floor: float | None = None) -> float | None:
    value = _quantile(values, q)
    if value is None:
        return None
    if floor is not None:
        value = max(float(floor), value)
    return float(value)


def evenly_spaced_quantiles(points: int) -> list[float]:
    points = max(1, int(points or 1))
    denom = float(points + 1)
    return [float(i) / denom for i in range(1, points + 1)]


def variant_from_settings(settings: Any) -> BookPressureVariant:
    return BookPressureVariant(
        name="scanner_current",
        min_avg_imbalance=float(
            getattr(settings, "scanner_book_pressure_min_avg_imbalance", 0.0)
            or 0.0
        ),
        min_avg_microprice_bps=float(
            getattr(settings, "scanner_book_pressure_min_microprice_bps", 0.0)
            or 0.0
        ),
        min_current_microprice_bps=float(
            getattr(settings, "scanner_book_pressure_min_microprice_bps", 0.0)
            or 0.0
        ),
        max_spread_bps=float(
            getattr(settings, "scanner_book_pressure_max_spread_bps", 0.0)
            or 0.0
        ),
        min_touch_notional_usd=float(
            getattr(settings, "scanner_book_pressure_min_touch_notional_usd", 0.0)
            or 0.0
        ),
        min_mid_move_bps=float(
            getattr(settings, "scanner_book_pressure_min_mid_move_bps", 0.0)
            or 0.0
        ),
        min_best_bid_move_bps=float(
            getattr(settings, "scanner_book_pressure_min_mid_move_bps", 0.0)
            or 0.0
        ),
        max_mid_move_bps=float(
            getattr(settings, "scanner_book_pressure_max_spread_bps", 0.0)
            or 0.0
        ),
        max_best_bid_move_bps=float(
            getattr(settings, "scanner_book_pressure_max_spread_bps", 0.0)
            or 0.0
        ),
        cooldown_s=float(
            getattr(settings, "scanner_book_pressure_cooldown_s", 0.0) or 0.0
        ),
    )


def derive_quantile_variants(
    windows: Iterable[BookPressureWindow],
    *,
    quantiles: Iterable[float],
    cooldown_s: float,
) -> list[BookPressureVariant]:
    rows = list(windows)
    if not rows:
        return []
    variants: list[BookPressureVariant] = []
    for q in quantiles:
        q = min(1.0, max(0.0, float(q)))
        spread_q = 1.0 - q
        name = f"derived_q{q:.4f}".replace(".", "_")
        variants.append(BookPressureVariant(
            name=name,
            min_avg_imbalance=_threshold(
                (w.avg_imbalance for w in rows), q, floor=0.5,
            ),
            min_avg_microprice_bps=_threshold(
                (w.avg_microprice_edge_bps for w in rows), q, floor=0.0,
            ),
            min_current_microprice_bps=_threshold(
                (w.current_microprice_edge_bps for w in rows), q, floor=0.0,
            ),
            max_spread_bps=_threshold(
                (w.max_spread_bps for w in rows), spread_q,
            ),
            min_touch_notional_usd=_threshold(
                (w.min_touch_notional_usd for w in rows), q, floor=0.0,
            ),
            min_mid_move_bps=_threshold(
                (w.mid_move_bps for w in rows), q, floor=0.0,
            ),
            min_best_bid_move_bps=_threshold(
                (w.best_bid_move_bps for w in rows), q, floor=0.0,
            ),
            cooldown_s=max(0.0, float(cooldown_s or 0.0)),
        ))
    return variants


def _mean(values: list[float]) -> float:
    return sum(values) / float(len(values))


def _stdev(values: list[float], mean: float) -> float:
    n = len(values)
    if n <= 1:
        return 0.0
    m2 = sum((v - mean) ** 2 for v in values)
    return math.sqrt(max(0.0, m2 / float(n - 1)))


def _net_return_summary(
    values: list[float],
    *,
    confidence: float,
    min_net_bps: float,
) -> tuple[float | None, float | None, float | None, float | None, str]:
    n = len(values)
    if n <= 1:
        return None, None, None, None, "insufficient_statistical_evidence"
    mean = _mean(values)
    stdev = _stdev(values, mean)
    if stdev <= 0.0:
        verdict = (
            "positive_edge_candidate"
            if mean >= float(min_net_bps or 0.0)
            else "below_cost"
        )
        return mean, mean, mean, 0.0, verdict
    stderr = stdev / math.sqrt(float(n))
    critical = _student_t_critical(confidence, n - 1)
    lower = mean - critical * stderr
    upper = mean + critical * stderr
    if lower >= float(min_net_bps or 0.0):
        verdict = "positive_edge_candidate"
    elif upper < float(min_net_bps or 0.0):
        verdict = "below_cost"
    else:
        verdict = "uncertain"
    return mean, lower, upper, stdev, verdict


def evaluate_variants(
    observations: Iterable[BookPressureObservation],
    windows: Iterable[BookPressureWindow],
    variants: Iterable[BookPressureVariant],
    *,
    horizons_s: Iterable[int],
    fee_bps_per_side: float,
    min_net_bps: float = 0.0,
    confidence: float = NEGATIVE_EDGE_CONFIDENCE,
) -> list[VariantEvaluation]:
    obs_by_ticker: dict[str, list[BookPressureObservation]] = {}
    for obs in observations:
        obs_by_ticker.setdefault(obs.ticker, []).append(obs)
    times_by_ticker: dict[str, list[datetime]] = {}
    for ticker, rows in obs_by_ticker.items():
        rows.sort(key=lambda row: row.snapshot_at)
        times_by_ticker[ticker] = [row.snapshot_at for row in rows]

    windows_by_ticker: dict[str, list[BookPressureWindow]] = {}
    for window in windows:
        windows_by_ticker.setdefault(window.ticker, []).append(window)
    for rows in windows_by_ticker.values():
        rows.sort(key=lambda row: row.snapshot_at)

    confidence = _bounded_confidence(confidence)
    fee_round_trip_bps = 2.0 * max(0.0, float(fee_bps_per_side or 0.0))
    results: list[VariantEvaluation] = []
    for variant in variants:
        for horizon_s in horizons_s:
            horizon = max(1, int(horizon_s or 1))
            gross_returns: list[float] = []
            net_returns: list[float] = []
            by_ticker: dict[str, int] = {}
            for ticker, ticker_windows in windows_by_ticker.items():
                obs_rows = obs_by_ticker.get(ticker) or []
                obs_times = times_by_ticker.get(ticker) or []
                last_fire: datetime | None = None
                for window in ticker_windows:
                    if not variant.passes(window):
                        continue
                    if (
                        last_fire is not None
                        and variant.cooldown_s > 0.0
                        and (
                            window.snapshot_at - last_fire
                        ).total_seconds() < variant.cooldown_s
                    ):
                        continue
                    idx = bisect_left(
                        obs_times,
                        window.snapshot_at + timedelta(seconds=horizon),
                    )
                    if idx >= len(obs_rows):
                        continue
                    future = obs_rows[idx]
                    if window.best_ask <= 0.0 or future.best_bid <= 0.0:
                        continue
                    gross_bps = (
                        (future.best_bid - window.best_ask)
                        / window.best_ask
                        * BPS_PER_UNIT
                    )
                    gross_returns.append(gross_bps)
                    net_returns.append(gross_bps - fee_round_trip_bps)
                    by_ticker[ticker] = by_ticker.get(ticker, 0) + 1
                    last_fire = window.snapshot_at

            mean, lower, upper, stdev, verdict = _net_return_summary(
                net_returns,
                confidence=confidence,
                min_net_bps=min_net_bps,
            )
            gross_mean = _mean(gross_returns) if gross_returns else None
            results.append(VariantEvaluation(
                variant_name=variant.name,
                horizon_s=horizon,
                sample_count=len(net_returns),
                mean_net_bps=mean,
                lower_net_bps=lower,
                upper_net_bps=upper,
                stdev_net_bps=stdev,
                confidence=confidence,
                gross_mean_bps=gross_mean,
                fee_bps_per_side=float(fee_bps_per_side or 0.0),
                triggered_by_ticker=dict(sorted(by_ticker.items())),
                verdict=verdict,
            ))
    return sorted(
        results,
        key=lambda row: (
            row.lower_net_bps if row.lower_net_bps is not None else -math.inf,
            row.mean_net_bps if row.mean_net_bps is not None else -math.inf,
            row.sample_count,
        ),
        reverse=True,
    )


__all__ = [
    "BookPressureObservation",
    "BookPressureVariant",
    "BookPressureWindow",
    "VariantEvaluation",
    "build_windows",
    "derive_quantile_variants",
    "evaluate_variants",
    "evenly_spaced_quantiles",
    "observation_from_book_row",
    "variant_from_settings",
]
