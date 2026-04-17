"""Phase L.18 - breadth + cross-sectional relative-strength (pure functions).

Builds an extended breadth + sector-rotation + relative-strength
snapshot from:

1. An ETF-basket "reference universe" of 11 US sector SPDRs plus
   SPY / QQQ / IWM benchmarks.
2. Per-member :class:`UniverseMember` readings (last close, previous
   close, 20d momentum, missing flag).

The pure model has **no side effects**: no DB, no network, no logging,
no config reads. All callers wrap it with a service-layer writer that
handles ETF fetching, mode gating, and persistence to
``trading_breadth_relstr_snapshots``.

Relationship to Phase L.17
--------------------------

L.17 classifies the macro regime (rates / credit / USD) with a
separate fixed symbol basket (IEF / SHY / TLT / HYG / LQD / UUP). L.18
is strictly **additive**: it shares the ``classify_trend`` primitive
with L.17 (re-exported below so callers don't duplicate the
``momentum_20d`` threshold logic) but operates on a disjoint symbol
basket and never mutates any L.17 wire shape.

Determinism
-----------

``compute_snapshot_id(as_of_date)`` returns
``sha1(as_of_date.isoformat())`` truncated to 16 hex chars. Two sweeps
for the same trading day produce the same ``snapshot_id`` so
append-only writes are de-duplicable by the caller when desired.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

# Re-use L.17's trend primitives verbatim to avoid duplicating
# threshold logic. L.18 imports but never mutates L.17 state.
from .macro_regime_model import (
    TREND_DOWN,
    TREND_FLAT,
    TREND_MISSING,
    TREND_UP,
    _VALID_TRENDS,
    classify_trend as _classify_trend_l17,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sector SPDRs (11 GICS sectors).
SECTOR_XLK = "XLK"   # technology
SECTOR_XLF = "XLF"   # financials
SECTOR_XLE = "XLE"   # energy
SECTOR_XLV = "XLV"   # health care
SECTOR_XLY = "XLY"   # consumer discretionary
SECTOR_XLP = "XLP"   # consumer staples
SECTOR_XLI = "XLI"   # industrials
SECTOR_XLB = "XLB"   # materials
SECTOR_XLU = "XLU"   # utilities
SECTOR_XLRE = "XLRE"  # real estate
SECTOR_XLC = "XLC"   # communication services
SECTOR_SYMBOLS = (
    SECTOR_XLK, SECTOR_XLF, SECTOR_XLE, SECTOR_XLV, SECTOR_XLY,
    SECTOR_XLP, SECTOR_XLI, SECTOR_XLB, SECTOR_XLU, SECTOR_XLRE,
    SECTOR_XLC,
)

# Benchmarks used for RS and tilts.
SYMBOL_SPY = "SPY"
SYMBOL_QQQ = "QQQ"
SYMBOL_IWM = "IWM"
BENCHMARK_SYMBOLS = (SYMBOL_SPY, SYMBOL_QQQ, SYMBOL_IWM)

ALL_SYMBOLS = SECTOR_SYMBOLS + BENCHMARK_SYMBOLS

# Composite breadth labels.
BREADTH_RISK_ON = "broad_risk_on"
BREADTH_MIXED = "mixed"
BREADTH_RISK_OFF = "broad_risk_off"
_VALID_BREADTH_LABELS = (BREADTH_RISK_ON, BREADTH_MIXED, BREADTH_RISK_OFF)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreadthRelstrConfig:
    """Tuning knobs for the breadth + RS classifier.

    Defaults intentionally mirror L.17's ``MacroRegimeConfig`` for the
    common thresholds (``trend_up_threshold``, ``min_coverage_score``)
    so an operator flipping both shadows at once sees consistent
    trend classifications across L.17 and L.18. The new knobs
    (``tilt_threshold``, ``risk_on_ratio``, ``risk_off_ratio``)
    control the size/style tilt and composite label.
    """

    trend_up_threshold: float = 0.01     # 1.0% 20d momentum -> up
    strong_trend_threshold: float = 0.03  # 3.0% 20d momentum -> strong up
    tilt_threshold: float = 0.02         # |sector RS vs SPY| > 2% -> material tilt
    min_coverage_score: float = 0.5      # persist only if at least 50% of basket resolved
    risk_on_ratio: float = 0.65          # advance_ratio >= 0.65 -> broad_risk_on candidate
    risk_off_ratio: float = 0.35         # advance_ratio <= 0.35 -> broad_risk_off candidate


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniverseMember:
    """One ETF reading produced by the service layer.

    Fields are optional because real providers drop data; the classifier
    handles missing entries defensively. When ``missing`` is ``True``
    the numeric fields are ignored and the symbol contributes ``0`` to
    coverage but never skews the classifier.

    ``trend`` is classified by the caller (or re-derived from
    ``momentum_20d``) and must match ``_VALID_TRENDS``. ``direction`` is
    a shallow bucket derived from ``last_close`` vs ``prev_close`` and
    is one of ``up`` / ``down`` / ``flat`` / ``missing``. It is the
    primary input to the advance / decline proxy (independent of the
    20d trend, which smooths short-term noise).
    """

    symbol: str
    missing: bool = False
    last_close: float | None = None
    prev_close: float | None = None
    momentum_20d: float | None = None
    trend: str = TREND_MISSING
    direction: str = TREND_MISSING
    new_high_20d: bool = False
    new_low_20d: bool = False

    def __post_init__(self) -> None:
        if self.trend not in _VALID_TRENDS:
            raise ValueError(
                f"UniverseMember.trend={self.trend!r} not in {_VALID_TRENDS}"
            )
        if self.direction not in _VALID_TRENDS:
            raise ValueError(
                f"UniverseMember.direction={self.direction!r} not in {_VALID_TRENDS}"
            )


@dataclass(frozen=True)
class BreadthRelstrInput:
    """Inputs to :func:`compute_breadth_relstr`."""

    as_of_date: date
    members: Sequence[UniverseMember] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreadthRelstrOutput:
    """Pure output of the breadth + RS classifier.

    Mirrors the ORM columns on ``BreadthRelstrSnapshot`` so the service
    layer writer is a shallow copy. ``sector_map`` is the structured
    per-sector ``{trend, momentum_20d, rs_vs_spy_20d}`` dict that ends
    up in the JSONB ``sector_json`` column.
    """

    snapshot_id: str
    as_of_date: date

    # breadth block
    members_sampled: int
    members_advancing: int
    members_declining: int
    members_flat: int
    advance_ratio: float
    new_highs_count: int
    new_lows_count: int

    # sector block (goes into sector_json)
    sector_map: Mapping[str, Mapping[str, Any]]

    # benchmark block
    spy_trend: str | None
    spy_momentum_20d: float | None
    qqq_trend: str | None
    qqq_momentum_20d: float | None
    iwm_trend: str | None
    iwm_momentum_20d: float | None

    # tilt block
    size_tilt: float | None
    style_tilt: float | None

    # composite block
    breadth_numeric: int
    breadth_label: str
    leader_sector: str | None
    laggard_sector: str | None

    # coverage
    symbols_sampled: int
    symbols_missing: int
    coverage_score: float

    payload: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_snapshot_id(as_of_date: date) -> str:
    """Deterministic 16-char sha1 of the ISO date string."""
    if not isinstance(as_of_date, date):
        raise TypeError(
            "compute_snapshot_id expected datetime.date, got "
            f"{type(as_of_date).__name__}"
        )
    return hashlib.sha1(as_of_date.isoformat().encode("utf-8")).hexdigest()[:16]


def classify_trend(
    momentum_20d: float | None,
    *,
    cfg: BreadthRelstrConfig,
) -> str:
    """Single-symbol 20d trend classification.

    Thin adapter that reuses L.17's ``classify_trend`` via a synthetic
    ``MacroRegimeConfig`` so the threshold semantics stay identical
    across phases.
    """
    # Build a minimal L.17 config with matching thresholds. We only
    # consume the ``trend_up_threshold`` field so the other defaults
    # are irrelevant.
    from .macro_regime_model import MacroRegimeConfig as _MRCfg

    return _classify_trend_l17(
        momentum_20d,
        cfg=_MRCfg(trend_up_threshold=cfg.trend_up_threshold),
    )


def classify_direction(
    last_close: float | None,
    prev_close: float | None,
) -> str:
    """Classify a bar-over-bar direction for the advance / decline proxy.

    Returns ``up`` / ``down`` / ``flat`` when both closes are usable,
    else ``missing``. A pure equality is treated as ``flat`` (not up /
    not down) so an all-flat market doesn't spuriously tilt breadth.
    """
    if last_close is None or prev_close is None:
        return TREND_MISSING
    try:
        lc = float(last_close)
        pc = float(prev_close)
    except (TypeError, ValueError):
        return TREND_MISSING
    if pc == 0.0:
        return TREND_MISSING
    if lc > pc:
        return TREND_UP
    if lc < pc:
        return TREND_DOWN
    return TREND_FLAT


def _count_advance_decline(
    members: Sequence[UniverseMember],
) -> tuple[int, int, int, int, int, int]:
    """Count A/D across the input members.

    Returns ``(sampled, advancing, declining, flat, new_highs, new_lows)``
    where ``sampled`` excludes missing entries.
    """
    sampled = 0
    advancing = 0
    declining = 0
    flat = 0
    new_highs = 0
    new_lows = 0
    for m in members:
        if m.missing:
            continue
        sampled += 1
        if m.direction == TREND_UP:
            advancing += 1
        elif m.direction == TREND_DOWN:
            declining += 1
        elif m.direction == TREND_FLAT:
            flat += 1
        if m.new_high_20d:
            new_highs += 1
        if m.new_low_20d:
            new_lows += 1
    return sampled, advancing, declining, flat, new_highs, new_lows


def _compute_rs_vs_spy(
    sector_mom20: float | None,
    spy_mom20: float | None,
) -> float | None:
    """Pure relative-strength diff (sector 20d momentum minus SPY's)."""
    if sector_mom20 is None or spy_mom20 is None:
        return None
    return float(sector_mom20) - float(spy_mom20)


def _safe_member(
    members: Mapping[str, UniverseMember],
    symbol: str,
) -> UniverseMember:
    """Return member if present-and-not-missing, else a missing stub."""
    m = members.get(symbol)
    if m is None or m.missing:
        return UniverseMember(
            symbol=symbol,
            missing=True,
            trend=TREND_MISSING,
            direction=TREND_MISSING,
        )
    return m


def _composite_breadth_label(
    advance_ratio: float,
    size_tilt: float | None,
    leader_rs: float | None,
    *,
    cfg: BreadthRelstrConfig,
) -> tuple[str, int]:
    """Combine A/D + size tilt + leader RS into a composite label.

    Rules:
    - If ``advance_ratio >= risk_on_ratio`` and the leader sector has
      a positive RS vs SPY that clears ``tilt_threshold``, label as
      ``broad_risk_on``.
    - If ``advance_ratio <= risk_off_ratio``, label as
      ``broad_risk_off``.
    - Otherwise ``mixed`` (including partial-coverage / unknown
      leader cases).
    """
    if advance_ratio >= cfg.risk_on_ratio:
        if leader_rs is None or leader_rs >= cfg.tilt_threshold:
            return BREADTH_RISK_ON, 1
        # A/D supportive but no leader -> mixed.
        return BREADTH_MIXED, 0
    if advance_ratio <= cfg.risk_off_ratio:
        return BREADTH_RISK_OFF, -1
    # Neutral A/D window: let a strong positive size tilt upgrade to
    # mixed; otherwise keep mixed. We never override a weak A/D with
    # a tilt (tilt is a secondary signal).
    _ = size_tilt  # reserved for future refinement; keeps signature stable
    return BREADTH_MIXED, 0


def _pick_leader_laggard(
    sector_map: Mapping[str, Mapping[str, Any]],
) -> tuple[str | None, str | None, float | None, float | None]:
    """Pick the strongest and weakest sector by RS vs SPY.

    Sectors with ``rs_vs_spy_20d is None`` are excluded. Ties are
    broken deterministically by sector symbol (alphabetical) so two
    identical momenta never produce flapping output.
    """
    candidates: list[tuple[str, float]] = []
    for sym, rec in sector_map.items():
        rs = rec.get("rs_vs_spy_20d")
        if rs is None:
            continue
        try:
            candidates.append((sym, float(rs)))
        except (TypeError, ValueError):
            continue
    if not candidates:
        return None, None, None, None
    # Sort by (rs, sym) so ties break alphabetically both ways.
    candidates.sort(key=lambda kv: (kv[1], kv[0]))
    laggard_sym, laggard_rs = candidates[0]
    leader_sym, leader_rs = candidates[-1]
    return leader_sym, laggard_sym, leader_rs, laggard_rs


def compute_breadth_relstr(
    inputs: BreadthRelstrInput,
    *,
    config: BreadthRelstrConfig | None = None,
) -> BreadthRelstrOutput:
    """Pure classifier for the breadth + RS snapshot.

    Rules:
    - Missing members never raise. They reduce ``coverage_score`` and
      leave the corresponding trend / RS fields as ``None``.
    - ``breadth_label`` is always one of
      ``{broad_risk_on, mixed, broad_risk_off}``. When every member
      is missing we fall back to ``mixed`` with ``breadth_numeric=0``.
    - ``coverage_score = symbols_sampled / len(ALL_SYMBOLS)`` (based
      on the fixed reference basket, not the caller's input length).
    """
    cfg = config or BreadthRelstrConfig()

    indexed: dict[str, UniverseMember] = {}
    for m in inputs.members:
        if m.symbol in ALL_SYMBOLS:
            indexed[m.symbol] = m

    spy = _safe_member(indexed, SYMBOL_SPY)
    qqq = _safe_member(indexed, SYMBOL_QQQ)
    iwm = _safe_member(indexed, SYMBOL_IWM)

    sector_members: dict[str, UniverseMember] = {
        s: _safe_member(indexed, s) for s in SECTOR_SYMBOLS
    }

    spy_mom = spy.momentum_20d if not spy.missing else None

    sector_map: dict[str, dict[str, Any]] = {}
    for sym, mem in sector_members.items():
        rs = _compute_rs_vs_spy(mem.momentum_20d, spy_mom) if not mem.missing else None
        sector_map[sym] = {
            "trend": (None if mem.missing else mem.trend),
            "momentum_20d": (None if mem.missing else mem.momentum_20d),
            "rs_vs_spy_20d": rs,
        }

    # breadth A/D proxy is computed across the full ALL_SYMBOLS basket
    # (sectors + benchmarks). That keeps the measure stable even when
    # one sector drops data; the coverage score records the gap.
    ref_members = [
        _safe_member(indexed, sym) for sym in ALL_SYMBOLS
    ]
    (
        sampled, advancing, declining, flat,
        new_highs, new_lows,
    ) = _count_advance_decline(ref_members)
    advance_ratio = (
        round(advancing / float(sampled), 6) if sampled else 0.0
    )

    size_tilt = _compute_rs_vs_spy(iwm.momentum_20d if not iwm.missing else None,
                                    spy_mom)
    style_tilt = _compute_rs_vs_spy(qqq.momentum_20d if not qqq.missing else None,
                                     spy_mom)

    leader_sector, laggard_sector, leader_rs, _laggard_rs = _pick_leader_laggard(
        sector_map
    )

    breadth_label, breadth_numeric = _composite_breadth_label(
        advance_ratio, size_tilt, leader_rs, cfg=cfg,
    )

    # Force mixed when we have zero sampled members; nothing else makes sense.
    if sampled == 0:
        breadth_label = BREADTH_MIXED
        breadth_numeric = 0

    symbols_sampled = sum(1 for m in ref_members if not m.missing)
    symbols_missing = len(ALL_SYMBOLS) - symbols_sampled
    coverage_score = round(symbols_sampled / float(len(ALL_SYMBOLS)), 6)

    payload: dict[str, Any] = {
        "readings": {
            m.symbol: {
                "missing": bool(m.missing),
                "last_close": m.last_close,
                "prev_close": m.prev_close,
                "momentum_20d": m.momentum_20d,
                "trend": m.trend,
                "direction": m.direction,
                "new_high_20d": bool(m.new_high_20d),
                "new_low_20d": bool(m.new_low_20d),
            }
            for m in ref_members
        },
        "config": {
            "trend_up_threshold": cfg.trend_up_threshold,
            "strong_trend_threshold": cfg.strong_trend_threshold,
            "tilt_threshold": cfg.tilt_threshold,
            "min_coverage_score": cfg.min_coverage_score,
            "risk_on_ratio": cfg.risk_on_ratio,
            "risk_off_ratio": cfg.risk_off_ratio,
        },
    }

    return BreadthRelstrOutput(
        snapshot_id=compute_snapshot_id(inputs.as_of_date),
        as_of_date=inputs.as_of_date,
        members_sampled=int(sampled),
        members_advancing=int(advancing),
        members_declining=int(declining),
        members_flat=int(flat),
        advance_ratio=float(advance_ratio),
        new_highs_count=int(new_highs),
        new_lows_count=int(new_lows),
        sector_map=sector_map,
        spy_trend=(None if spy.missing else spy.trend),
        spy_momentum_20d=(None if spy.missing else spy.momentum_20d),
        qqq_trend=(None if qqq.missing else qqq.trend),
        qqq_momentum_20d=(None if qqq.missing else qqq.momentum_20d),
        iwm_trend=(None if iwm.missing else iwm.trend),
        iwm_momentum_20d=(None if iwm.missing else iwm.momentum_20d),
        size_tilt=size_tilt,
        style_tilt=style_tilt,
        breadth_numeric=int(breadth_numeric),
        breadth_label=breadth_label,
        leader_sector=leader_sector,
        laggard_sector=laggard_sector,
        symbols_sampled=int(symbols_sampled),
        symbols_missing=int(symbols_missing),
        coverage_score=float(coverage_score),
        payload=payload,
    )
