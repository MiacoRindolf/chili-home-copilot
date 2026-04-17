"""Triple-barrier labeling (pure math, no I/O, no DB).

Given an entry close and a list of forward OHLCV bars, decide whether the
trade would have hit its take-profit barrier, its stop-loss barrier, or
neither (timeout) within ``max_bars`` bars.

References:
    Lopez de Prado, "Advances in Financial Machine Learning", ch. 3.

Tie-break rule: if a single bar breaches both TP and SL (intra-bar), we
assume **stop-loss hit first** (conservative long bias). For shorts we
assume **take-profit hit first** is unsafe — so we also default to
stop-loss. This keeps labels pessimistic, which is the right bias for
training models that must survive live costs.

Public API:
    TripleBarrierConfig
    TripleBarrierLabel
    compute_label(entry_close, future_bars, cfg) -> TripleBarrierLabel
    compute_label_atr(entry_close, entry_atr, future_bars,
                      atr_mult_tp, atr_mult_sl, max_bars, side) -> TripleBarrierLabel

All helpers are pure: same inputs → same outputs; no logging, no network,
no DB. Designed to be unit-tested exhaustively and reused by the labeler
service in ``triple_barrier_labeler.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

Side = Literal["long", "short"]
BarrierHit = Literal["tp", "sl", "timeout", "missing_data"]


@dataclass(frozen=True)
class OHLCVBar:
    """Minimal OHLCV bar view for barrier evaluation."""
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class TripleBarrierConfig:
    """Pct-based triple-barrier configuration.

    tp_pct and sl_pct are **positive fractions** (e.g. 0.015 = 1.5%).
    For ``side='long'``: TP = entry*(1+tp), SL = entry*(1-sl).
    For ``side='short'``: TP = entry*(1-tp), SL = entry*(1+sl).
    """
    tp_pct: float
    sl_pct: float
    max_bars: int
    side: Side = "long"

    def __post_init__(self) -> None:
        if self.tp_pct <= 0:
            raise ValueError(f"tp_pct must be > 0, got {self.tp_pct}")
        if self.sl_pct <= 0:
            raise ValueError(f"sl_pct must be > 0, got {self.sl_pct}")
        if self.max_bars <= 0:
            raise ValueError(f"max_bars must be > 0, got {self.max_bars}")
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {self.side!r}")


@dataclass(frozen=True)
class TripleBarrierLabel:
    """Outcome of a triple-barrier evaluation.

    label:
        +1 = take-profit barrier hit (winner)
        -1 = stop-loss barrier hit  (loser)
         0 = timeout / no barrier hit / missing data

    realized_return_pct is expressed as a fraction (not percent * 100). For
    long trades it's `(exit_close - entry_close) / entry_close`. For shorts
    it's `(entry_close - exit_close) / entry_close`.
    """
    label: int
    exit_bar_idx: int
    realized_return_pct: float
    barrier_hit: BarrierHit
    entry_close: float
    tp_price: float
    sl_price: float


def _coerce_bar(b: object) -> OHLCVBar | None:
    """Accept dict-like or dataclass-like bars; return None on bad input."""
    if b is None:
        return None
    if isinstance(b, OHLCVBar):
        return b
    try:
        if isinstance(b, dict):
            o = float(b.get("open") if b.get("open") is not None else b.get("Open"))
            h = float(b.get("high") if b.get("high") is not None else b.get("High"))
            lo = float(b.get("low") if b.get("low") is not None else b.get("Low"))
            c = float(b.get("close") if b.get("close") is not None else b.get("Close"))
            v = float(b.get("volume") if b.get("volume") is not None else (b.get("Volume") or 0.0))
        else:
            o = float(getattr(b, "open", getattr(b, "Open")))
            h = float(getattr(b, "high", getattr(b, "High")))
            lo = float(getattr(b, "low", getattr(b, "Low")))
            c = float(getattr(b, "close", getattr(b, "Close")))
            v = float(getattr(b, "volume", getattr(b, "Volume", 0.0)) or 0.0)
        return OHLCVBar(open=o, high=h, low=lo, close=c, volume=v)
    except Exception:
        return None


def compute_label(
    entry_close: float,
    future_bars: Sequence[object] | Iterable[object],
    cfg: TripleBarrierConfig,
) -> TripleBarrierLabel:
    """Evaluate the triple-barrier outcome for a trade entered at ``entry_close``.

    ``future_bars`` must be ordered chronologically and represent the bars
    that come **after** the entry (bar 0 is the first bar after entry).
    """
    if entry_close is None or entry_close <= 0:
        return TripleBarrierLabel(
            label=0,
            exit_bar_idx=-1,
            realized_return_pct=0.0,
            barrier_hit="missing_data",
            entry_close=float(entry_close or 0.0),
            tp_price=0.0,
            sl_price=0.0,
        )

    bars = [b for b in (list(future_bars)[: cfg.max_bars]) if _coerce_bar(b) is not None]
    coerced = [_coerce_bar(b) for b in bars]
    # mypy/pyright: coerced entries are not None because of the filter above
    coerced_bars: list[OHLCVBar] = [b for b in coerced if b is not None]

    if cfg.side == "long":
        tp_price = entry_close * (1.0 + cfg.tp_pct)
        sl_price = entry_close * (1.0 - cfg.sl_pct)
    else:
        tp_price = entry_close * (1.0 - cfg.tp_pct)
        sl_price = entry_close * (1.0 + cfg.sl_pct)

    if not coerced_bars:
        return TripleBarrierLabel(
            label=0,
            exit_bar_idx=-1,
            realized_return_pct=0.0,
            barrier_hit="missing_data",
            entry_close=entry_close,
            tp_price=tp_price,
            sl_price=sl_price,
        )

    for idx, bar in enumerate(coerced_bars):
        hi, lo = bar.high, bar.low
        if cfg.side == "long":
            tp_hit = hi >= tp_price
            sl_hit = lo <= sl_price
            if tp_hit and sl_hit:
                # Conservative tie-break: SL first.
                realized = (sl_price - entry_close) / entry_close
                return TripleBarrierLabel(
                    label=-1,
                    exit_bar_idx=idx,
                    realized_return_pct=realized,
                    barrier_hit="sl",
                    entry_close=entry_close,
                    tp_price=tp_price,
                    sl_price=sl_price,
                )
            if sl_hit:
                realized = (sl_price - entry_close) / entry_close
                return TripleBarrierLabel(
                    label=-1,
                    exit_bar_idx=idx,
                    realized_return_pct=realized,
                    barrier_hit="sl",
                    entry_close=entry_close,
                    tp_price=tp_price,
                    sl_price=sl_price,
                )
            if tp_hit:
                realized = (tp_price - entry_close) / entry_close
                return TripleBarrierLabel(
                    label=+1,
                    exit_bar_idx=idx,
                    realized_return_pct=realized,
                    barrier_hit="tp",
                    entry_close=entry_close,
                    tp_price=tp_price,
                    sl_price=sl_price,
                )
        else:
            tp_hit = lo <= tp_price
            sl_hit = hi >= sl_price
            if tp_hit and sl_hit:
                realized = (entry_close - sl_price) / entry_close
                return TripleBarrierLabel(
                    label=-1,
                    exit_bar_idx=idx,
                    realized_return_pct=realized,
                    barrier_hit="sl",
                    entry_close=entry_close,
                    tp_price=tp_price,
                    sl_price=sl_price,
                )
            if sl_hit:
                realized = (entry_close - sl_price) / entry_close
                return TripleBarrierLabel(
                    label=-1,
                    exit_bar_idx=idx,
                    realized_return_pct=realized,
                    barrier_hit="sl",
                    entry_close=entry_close,
                    tp_price=tp_price,
                    sl_price=sl_price,
                )
            if tp_hit:
                realized = (entry_close - tp_price) / entry_close
                return TripleBarrierLabel(
                    label=+1,
                    exit_bar_idx=idx,
                    realized_return_pct=realized,
                    barrier_hit="tp",
                    entry_close=entry_close,
                    tp_price=tp_price,
                    sl_price=sl_price,
                )

    # Timeout — use final close to compute realized return.
    last = coerced_bars[-1]
    if cfg.side == "long":
        realized = (last.close - entry_close) / entry_close
    else:
        realized = (entry_close - last.close) / entry_close
    return TripleBarrierLabel(
        label=0,
        exit_bar_idx=len(coerced_bars) - 1,
        realized_return_pct=realized,
        barrier_hit="timeout",
        entry_close=entry_close,
        tp_price=tp_price,
        sl_price=sl_price,
    )


def compute_label_atr(
    entry_close: float,
    entry_atr: float,
    future_bars: Sequence[object] | Iterable[object],
    *,
    atr_mult_tp: float = 2.0,
    atr_mult_sl: float = 1.0,
    max_bars: int = 5,
    side: Side = "long",
) -> TripleBarrierLabel:
    """ATR-scaled variant: barriers are ``atr_mult * entry_atr`` away from entry."""
    if entry_close is None or entry_close <= 0 or entry_atr is None or entry_atr <= 0:
        return TripleBarrierLabel(
            label=0,
            exit_bar_idx=-1,
            realized_return_pct=0.0,
            barrier_hit="missing_data",
            entry_close=float(entry_close or 0.0),
            tp_price=0.0,
            sl_price=0.0,
        )

    tp_pct = (atr_mult_tp * entry_atr) / entry_close
    sl_pct = (atr_mult_sl * entry_atr) / entry_close
    cfg = TripleBarrierConfig(
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        max_bars=max_bars,
        side=side,
    )
    return compute_label(entry_close, future_bars, cfg)


__all__ = [
    "OHLCVBar",
    "TripleBarrierConfig",
    "TripleBarrierLabel",
    "compute_label",
    "compute_label_atr",
]
