"""Activation hook: refresh momentum intelligence into BrainNodeState."""

from __future__ import annotations

import logging
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Optional, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import BrainActivationEvent
from ..brain_neural_mesh.repository import get_or_create_state
from ..brain_neural_mesh.schema import mesh_enabled

from .context import build_momentum_regime_context


from .evolution import record_evolution_trace
from .features import ExecutionReadinessFeatures
from .replay_capture_contract import CaptureMicrostructureOperation
from .replay_errors import (
    ReplayDecisionLocalMicrostructureCoverageUnavailableError,
    ReplayInputContractError,
    ReplayMicrostructureInputUnavailableError,
    ReplayPipelineInputUnavailableError,
)
from .telemetry import log_tick
from .variants import iter_momentum_families
from .viability import score_viability
from .viability_scope import VIABILITY_SCOPE_AGGREGATE, VIABILITY_SCOPE_SYMBOL

HUB_NODE_ID = "nm_momentum_crypto_intel"
VIABILITY_NODE_ID = "nm_momentum_viability_pool"

_log = logging.getLogger(__name__)


@runtime_checkable
class MicrostructureReadProvider(Protocol):
    """Exact no-fetch provider used by captured PAPER and sealed ReplayV3."""

    @property
    def network_fallback_allowed(self) -> bool: ...

    def read_microstructure(
        self,
        *,
        operation: CaptureMicrostructureOperation,
        symbol: str,
        decision_at: datetime,
        parameters: Mapping[str, Any],
    ) -> Any: ...


_MICROSTRUCTURE_READ_PROVIDER: ContextVar[
    MicrostructureReadProvider | None
] = ContextVar("chili_microstructure_read_provider", default=None)
_MICROSTRUCTURE_PROVIDER_FAILURE: ContextVar[
    ReplayInputContractError | None
] = ContextVar("chili_microstructure_provider_failure", default=None)
_MICROSTRUCTURE_PROVIDER_MISSING = object()
_MICROSTRUCTURE_COVERAGE_UNAVAILABLE = object()


@contextmanager
def microstructure_read_provider(
    provider: MicrostructureReadProvider | None,
) -> Iterator[MicrostructureReadProvider | None]:
    """Bind one decision-local raw-source provider without global cache state."""

    if provider is not None:
        if not isinstance(provider, MicrostructureReadProvider):
            raise ReplayMicrostructureInputUnavailableError(
                "microstructure read provider is malformed"
            )
        if provider.network_fallback_allowed:
            raise ReplayMicrostructureInputUnavailableError(
                "microstructure read provider permits network fallback"
            )
    token = _MICROSTRUCTURE_READ_PROVIDER.set(provider)
    failure_token = _MICROSTRUCTURE_PROVIDER_FAILURE.set(None)
    body_raised = False
    try:
        yield provider
    except BaseException:
        body_raised = True
        raise
    finally:
        unresolved_failure = _MICROSTRUCTURE_PROVIDER_FAILURE.get()
        _MICROSTRUCTURE_PROVIDER_FAILURE.reset(failure_token)
        _MICROSTRUCTURE_READ_PROVIDER.reset(token)
        # Several legacy feature/exit call sites deliberately catch broad
        # exceptions and fail open when an optional LIVE feed is absent.  A
        # bound capture/replay provider is not optional: swallowing its causal
        # contract rejection would let the FSM continue toward an order using
        # an unsealed input set.  Re-raise the first rejection at the decision
        # scope boundary even when an inner legacy caller caught it.
        if not body_raised and unresolved_failure is not None:
            raise unresolved_failure


def _microstructure_provider_read(
    *,
    operation: CaptureMicrostructureOperation,
    symbol: str,
    as_of: datetime | None,
    parameters: Mapping[str, Any],
) -> Any:
    provider = _MICROSTRUCTURE_READ_PROVIDER.get()
    if provider is None:
        return _MICROSTRUCTURE_PROVIDER_MISSING
    decision_at = _tape_asof_default(as_of)
    if decision_at.tzinfo is None:
        decision_at = decision_at.replace(tzinfo=timezone.utc)
    else:
        decision_at = decision_at.astimezone(timezone.utc)
    try:
        return provider.read_microstructure(
            operation=operation,
            symbol=symbol,
            decision_at=decision_at,
            parameters=dict(parameters),
        )
    except ReplayDecisionLocalMicrostructureCoverageUnavailableError:
        # A capture-native L2 provider has already emitted an append-only
        # COVERAGE_UNAVAILABLE gap.  Return a private sentinel so the exact
        # reader can produce its established type-safe missing value.  This is
        # deliberately distinct from PROVIDER_MISSING: callers must never fall
        # through to current DB/ring/network state while a capture provider is
        # bound.  Exact-print, identity/clock, and receipt failures still take
        # the hard branch below and reject the whole captured decision.
        return _MICROSTRUCTURE_COVERAGE_UNAVAILABLE
    except ReplayInputContractError as exc:
        if _MICROSTRUCTURE_PROVIDER_FAILURE.get() is None:
            _MICROSTRUCTURE_PROVIDER_FAILURE.set(exc)
        raise
    except Exception as exc:
        wrapped = ReplayMicrostructureInputUnavailableError(
            "microstructure provider rejected the exact decision read"
        )
        if _MICROSTRUCTURE_PROVIDER_FAILURE.get() is None:
            _MICROSTRUCTURE_PROVIDER_FAILURE.set(wrapped)
        raise wrapped from exc


def _tape_asof_default(as_of):
    """Resolve the tape-read "now" anchor through live_runner's replay-aware clock
    chokepoint when the caller didn't thread one (2026-07-09). LIVE: ``_utcnow()``
    IS naive wall-UTC, so routing through the bounded ``as_of`` SQL branch is
    byte-identical to the old wall-``now()`` branch. FSM REPLAY: ``_utcnow()`` is
    the sim clock — the old wall-now branch read an EMPTY window there (features
    silently None => gates/sizing diverged from live) or, against a shared DB with
    the bridge writing, foreign post-sim ticks (look-ahead)."""
    if as_of is not None:
        return as_of
    try:
        from .live_runner import _utcnow as _lr_utcnow

        return _lr_utcnow()
    except Exception:
        return datetime.utcnow()


def _replay_clock_is_bound() -> bool:
    """Whether this call is executing inside the historical ReplayV3 clock."""

    from .live_runner import _SIM_NOW

    return _SIM_NOW.get() is not None


def _symbol_country(symbol: str) -> str | None:
    """HQ country from the yfinance fundamentals CACHE — cache-only read (the
    viability tick must never block on network); a miss fires a background
    fetch so the NEXT tick has it. Crypto/aggregate/blank -> None."""
    s = (symbol or "").strip().upper()
    if not s or s.endswith("-USD") or s == "__AGGREGATE__":
        return None
    try:
        from ...yf_session import _cache_get as _yf_cache_get

        f = _yf_cache_get(f"fund:{s}")
        if isinstance(f, dict):
            c = f.get("country")
            return str(c) if c else None
    except Exception:
        return None
    try:
        import threading

        from ...yf_session import get_fundamentals

        threading.Thread(target=get_fundamentals, args=(s,), daemon=True).start()
    except Exception:
        pass
    return None


def _live_book_imbalance(symbol: str, db: Any = None) -> float | None:
    """Signed order-book imbalance in [-1, 1] from the LIVE venue feeds.

    Crypto: Coinbase Advanced WS ``level2`` ring buffer (true book depth — top
    bid total vs ask total). Equities, best-first: a FRESH multi-venue Level 2
    snapshot from the IQFeed depth bridge (host-side daemon writing
    ``iqfeed_depth_snapshots``; scripts/iqfeed_depth_bridge.py), else the
    Massive WS NBBO displayed sizes (the L1 depth the RH rail can see).
    Positive = bid-heavy (supportive for the long-only lane) — the sign
    convention viability's Phase 4a microstructure rules already score
    (>0.12 boost, <-0.18 penalty + warning). None when no live feed covers
    the symbol (behavior unchanged).
    """
    s = (symbol or "").strip().upper()
    if not s:
        return None
    captured = _microstructure_provider_read(
        operation=CaptureMicrostructureOperation.BOOK_IMBALANCE,
        symbol=s,
        as_of=None,
        parameters={
            "window_seconds": 15.0,
            "maximum_snapshot_age_seconds": 15.0,
        },
    )
    if captured is _MICROSTRUCTURE_COVERAGE_UNAVAILABLE:
        return None
    if captured is not _MICROSTRUCTURE_PROVIDER_MISSING:
        return captured
    try:
        if s.endswith("-USD"):
            from ..microstructure import get_features

            r = get_features(s).bid_ask_imbalance  # ratio: >1 = bid-heavy
            if r is None or float(r) <= 0:
                return None
            r = float(r)
            return round((r - 1.0) / (r + 1.0), 4)
        if db is not None:
            # True 5-level multi-venue depth (fail-open: missing table / stale
            # bridge -> fall through to the L1 sizes below)
            try:
                from sqlalchemy import text as _sql

                # Bounded as-of read via the replay-aware chokepoint (live-identical,
                # replay-honest — see _tape_asof_default).
                _ao_q = _tape_asof_default(None)
                from .optional_db_read import optional_fetchone

                row = optional_fetchone(
                    db,
                    _sql(
                        "SELECT imbalance5 FROM iqfeed_depth_snapshots "
                        "WHERE symbol = :s AND observed_at > :as_of - interval '15 seconds' "
                        "AND observed_at <= :as_of "
                        "ORDER BY observed_at DESC LIMIT 1"
                    ),
                    {"s": s, "as_of": _ao_q},
                )
                if row is not None and row[0] is not None:
                    return float(row[0])
            except Exception:
                pass
        from ...massive_client import get_ws_quote

        q = get_ws_quote(s)
        if q is None or not q.bid_size or not q.ask_size:
            return None
        tot = float(q.bid_size) + float(q.ask_size)
        if tot <= 0:
            return None
        return round((float(q.bid_size) - float(q.ask_size)) / tot, 4)
    except Exception:
        return None


def _cks_event(p1: float, q1: float, p0: float, q0: float, *, is_bid: bool) -> float:
    """Cont-Kukanov-Stoikov single-side OFI event for ONE price level between two
    consecutive book states. Bid contributes +new size if its price ROSE, -old
    size if it fell, the size delta if unchanged; the ask is the mirror (a rising
    ask = supply lifting = +demand; a falling ask = -old size). Sign convention
    matches the legacy level-1 loop exactly so level-1 is unchanged."""
    if is_bid:
        if p1 > p0:
            return q1
        if p1 < p0:
            return -q0
        return q1 - q0
    # ask side: price retreating UP (p1>p0) = buying pressure -> +new size
    if p1 > p0:
        return q1
    if p1 < p0:
        return -q0
    return q1 - q0


def _compute_ofi_micro(
    seq: list[tuple[float, float, float, float]],
    ladder_seq: "list[tuple[list, list]] | None" = None,
) -> tuple[float | None, float | None]:
    """OFI (normalized net directional flow in [-1, 1]) + micro-price edge (bps)
    from a time-ordered sequence of ``(bid_px, bid_sz, ask_px, ask_sz)`` best
    quotes (oldest first).

    OFI per Cont/Kukanov/Stoikov: at each step the bid contributes +new size if
    the bid price rose, -old size if it fell, the size delta if unchanged; the
    ask mirrors with the demand sign (ask retreating up = buying pressure).
    Normalized by gross flow so it is bounded and scale-free (no magic depth
    constant). Micro-price (Stoikov) edge from the latest quote.
    """
    if not seq:
        return None, None
    pb, qb, pa, qa = seq[-1]
    micro_edge: float | None = None
    mid = (pb + pa) / 2.0
    denom = qb + qa
    if mid > 0 and denom > 0:
        micro = (pa * qb + pb * qa) / denom
        micro_edge = round((micro - mid) / mid * 10000.0, 2)
    if len(seq) < 2:
        return None, micro_edge

    # ── MULTI-LEVEL OFI (Cont-Kukanov-Stoikov, P3) ────────────────────────────
    # When the per-price ladder is present AND the flag is on, sum the per-level
    # OFI events across the top-N levels of each side, depth-decay weighted by
    # w_m = 1/m (harmonic: level-1 dominant, deeper levels contribute less; NOT a
    # magic cutoff). gross uses the SAME weights so the result stays normalized in
    # [-1, 1] and dimensionless. Fail-OPEN: any ladder pair that is missing/empty
    # for either book state falls back to that step's level-1 events, so a
    # partially-populated ladder never zeroes the signal. Flag OFF, or ladder
    # absent -> skip this block entirely -> the level-1 path below is byte-identical.
    use_ml = bool(getattr(settings, "chili_momentum_l2_multilevel_ofi_enabled", True))
    if use_ml and ladder_seq and len(ladder_seq) == len(seq) and len(ladder_seq) >= 2:
        ofi_ml = 0.0
        gross_ml = 0.0
        ok = False
        for (b0, a0), (b1, a1) in zip(ladder_seq, ladder_seq[1:]):
            try:
                depth = min(len(b0), len(b1), len(a0), len(a1))
            except TypeError:
                depth = 0
            if depth <= 0:
                continue
            ok = True
            for m in range(depth):
                w = 1.0 / (m + 1.0)  # harmonic depth decay; level-1 weight = 1.0
                eb = _cks_event(float(b1[m][0]), float(b1[m][1]),
                                float(b0[m][0]), float(b0[m][1]), is_bid=True)
                ea = _cks_event(float(a1[m][0]), float(a1[m][1]),
                                float(a0[m][0]), float(a0[m][1]), is_bid=False)
                ofi_ml += w * (eb - ea)
                gross_ml += w * (abs(eb) + abs(ea))
        if ok:
            if gross_ml <= 0:
                return 0.0, micro_edge
            return round(max(-1.0, min(1.0, ofi_ml / gross_ml)), 4), micro_edge

    # ── LEVEL-1 OFI (legacy path; unchanged) ──────────────────────────────────
    ofi = 0.0
    gross = 0.0
    for (pb0, qb0, pa0, qa0), (pb1, qb1, pa1, qa1) in zip(seq, seq[1:]):
        eb = _cks_event(pb1, qb1, pb0, qb0, is_bid=True)
        ea = _cks_event(pa1, qa1, pa0, qa0, is_bid=False)
        ofi += eb - ea
        gross += abs(eb) + abs(ea)
    if gross <= 0:
        return 0.0, micro_edge
    return round(max(-1.0, min(1.0, ofi / gross)), 4), micro_edge


def _live_ofi_microprice(
    symbol: str, db: Any = None, as_of: "datetime | None" = None
) -> tuple[float | None, float | None]:
    """Live OFI (normalized [-1, 1]) + micro-price edge (bps) for a symbol.

    Crypto: from the in-process Coinbase full-book ring (``recent()`` window of
    top-of-book snapshots). Equity: from the IQFeed depth-bridge time series
    (``iqfeed_depth_snapshots`` best bid/ask price+size). Returns ``(None, None)``
    when no live sequence covers the symbol (then no tilt — behavior unchanged).

    OFI is the strongest L2 short-horizon predictor in the literature
    (Cont/Kukanov/Stoikov 2014; portable across cryptos), micro-price the
    confirmer (Stoikov). Wired as a SMALL agreement-guarded viability tilt and
    validated by live A/B (the literature edge is contemporaneous + may sit near
    Coinbase fees, so net incremental alpha is proven LIVE, not assumed).

    ``as_of`` (UTC-naive) reads L2 AS-OF a historical instant for the replay
    instrument — the window becomes ``(as_of - w, as_of]`` instead of trailing
    ``now()``, and the in-process ring (which has no history) is skipped so the
    durable table is the sole source. ``as_of=None`` is the LIVE default: the ring
    is preferred, and the table fallback anchors on the replay-aware clock
    chokepoint (``_tape_asof_default`` — live-identical row set, replay-honest).
    """
    s = (symbol or "").strip().upper()
    if not s:
        return None, None
    if as_of is not None and getattr(as_of, "tzinfo", None) is not None:
        as_of = as_of.replace(tzinfo=None)  # naive UTC for the naive snapshot columns
    try:
        window = float(getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0)
    except (TypeError, ValueError):
        window = 15.0
    captured = _microstructure_provider_read(
        operation=CaptureMicrostructureOperation.OFI_MICROPRICE,
        symbol=s,
        as_of=as_of,
        parameters={
            "window_seconds": window,
            "multilevel_ofi_enabled": bool(
                getattr(settings, "chili_momentum_l2_multilevel_ofi_enabled", True)
            ),
        },
    )
    if captured is _MICROSTRUCTURE_COVERAGE_UNAVAILABLE:
        return None, None
    if captured is not _MICROSTRUCTURE_PROVIDER_MISSING:
        return captured
    try:
        if s.endswith("-USD"):
            # Crypto L2: prefer the in-process Coinbase book ring (sub-second), and
            # FALL BACK to the durable, cross-process ``fast_orderbook`` table when the
            # ring is empty in THIS process. The ring is per-process: a held name not in
            # this process's subscription set has 0 ring snapshots → ofi=None → the exit
            # lock can never fire (JASMY-USD logged 89 armed ticks at None as a real
            # +2.3R winner). The table is fed by the crypto L2 drain and is always
            # populated for any subscribed name, mirroring how the equity branch reads
            # iqfeed_depth_snapshots. Crypto-GATED: the equity path NEVER reads
            # fast_orderbook (equity decisions stay isolated from crypto rows).
            seq: list[tuple[float, float, float, float]] = []
            if as_of is None:
                # in-process ring has no replay history → table-only when as_of is set
                try:
                    from ..microstructure import get_book_buffer

                    snaps = get_book_buffer().recent(s, window_secs=window)
                    seq = [
                        (snap.bids[0].price, snap.bids[0].size, snap.asks[0].price, snap.asks[0].size)
                        for snap in snaps
                        if snap.bids and snap.asks
                    ]
                except Exception:
                    seq = []
                if seq:
                    return _compute_ofi_micro(seq)
            # ring empty in this process (or replaying) → durable table fallback (top-of-book)
            if db is None:
                return None, None
            from sqlalchemy import text as _sql

            # Bounded as-of read via the replay-aware chokepoint (live-identical row
            # set; replay-honest). NOTE: the ring gate above stays keyed on the
            # CALLER's as_of — only the SQL anchor is resolved here.
            _ao_q = _tape_asof_default(as_of)
            _q = (
                "SELECT bid_levels, ask_levels FROM fast_orderbook "
                "WHERE ticker = :s AND snapshot_at > :as_of - make_interval(secs => :w) "
                "AND snapshot_at <= :as_of ORDER BY snapshot_at ASC"
            )
            _p = {"s": s, "w": window, "as_of": _ao_q}
            from .optional_db_read import optional_fetchall

            rows = optional_fetchall(db, _sql(_q), _p)
            for r in rows:
                bl, al = r[0], r[1]
                try:
                    if bl and al:
                        seq.append((float(bl[0][0]), float(bl[0][1]), float(al[0][0]), float(al[0][1])))
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
            return _compute_ofi_micro(seq)
        if db is not None:
            from sqlalchemy import text as _sql

            # Bounded as-of read via the replay-aware chokepoint (live-identical,
            # replay-honest — see _live_trade_flow).
            _ao_q = _tape_asof_default(as_of)
            _q = (
                "SELECT bid_top, bid_top_size, ask_top, ask_top_size, bids_json, asks_json "
                "FROM iqfeed_depth_snapshots "
                "WHERE symbol = :s AND observed_at > :as_of - make_interval(secs => :w) "
                "AND observed_at <= :as_of ORDER BY observed_at ASC"
            )
            _p = {"s": s, "w": window, "as_of": _ao_q}
            from .optional_db_read import optional_fetchall

            rows = optional_fetchall(db, _sql(_q), _p)
            seq = [
                (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
                for r in rows
                if None not in (r[0], r[1], r[2], r[3])
            ]
            # P3: per-price ladder aligned 1:1 with seq (only rows that survived the
            # level-1 filter contribute). A row whose ladder is NULL/empty yields
            # ([], []) -> that step fails open to level-1 inside _compute_ofi_micro.
            ladder_seq = [
                (r[4] if isinstance(r[4], list) else [],
                 r[5] if isinstance(r[5], list) else [])
                for r in rows
                if None not in (r[0], r[1], r[2], r[3])
            ]
            return _compute_ofi_micro(seq, ladder_seq=ladder_seq)
    except Exception:
        return None, None
    return None, None


def _live_trade_flow(
    symbol: str, db: Any = None, as_of: "datetime | None" = None
) -> float | None:
    """Live TRADE-FLOW: signed-volume AGGRESSOR imbalance in [-1, 1] over the short OFI window.

    The research's #2 micro signal (Ross's "ask getting eaten" = aggressive buying = real thrust),
    distinct from OFI (book flow) and book_imbalance (book state) — this is the TAPE.
      Equity: from ``iqfeed_trade_ticks`` (the IQFeed L1 trade-tape bridge). Each trade is aggressor-
        classified by the QUOTE RULE (Lee-Ready: px>=ask -> buy +1, px<=bid -> sell -1, else mid
        split) when the prevailing bid/ask is present, else the TICK RULE (px vs the prior trade;
        zero-tick carries the prior sign). imbalance = Σ(sign·size) / Σ(size).
      Crypto: from the in-process microstructure tape (``trade_aggression`` = buy_vol/total in [0,1])
        re-centered to 2·aggr-1 in [-1, 1].
    Returns ``None`` when no live tape covers the symbol (then the feature is simply absent). ``as_of``
    reads the tape AS-OF a historical instant for the replay instrument (window ``(as_of-w, as_of]``);
    ``as_of=None`` is the LIVE default (trailing ``now()``)."""
    s = (symbol or "").strip().upper()
    if not s:
        return None
    try:
        window = float(getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0)
    except (TypeError, ValueError):
        window = 15.0
    captured = _microstructure_provider_read(
        operation=CaptureMicrostructureOperation.TRADE_FLOW,
        symbol=s,
        as_of=as_of,
        parameters={"window_seconds": window},
    )
    if captured is not _MICROSTRUCTURE_PROVIDER_MISSING:
        return captured
    # crypto: in-process microstructure tape (no historical as_of -> live ring only)
    if s.endswith("-USD"):
        if as_of is not None:
            return None
        try:
            from ..microstructure import get_features

            aggr = getattr(get_features(s), "trade_aggression", None)
            if aggr is not None:
                return float(max(-1.0, min(1.0, 2.0 * float(aggr) - 1.0)))
        except Exception:
            return None
        return None
    # equity: the IQFeed L1 trade-tape table (defensive — absent table/rows -> None)
    if db is None:
        return None
    try:
        from sqlalchemy import text as _sql

        # Always the bounded as-of form, anchored through the replay-aware chokepoint
        # when the caller didn't thread as_of (live: _utcnow() == wall UTC => identical
        # row set to the old wall-now() branch; replay: the sim clock, so the read no
        # longer returns an empty window / foreign post-sim ticks).
        _ao = _tape_asof_default(as_of)
        _ao = _ao.replace(tzinfo=None) if getattr(_ao, "tzinfo", None) is not None else _ao
        q = ("SELECT price, size, bid, ask FROM iqfeed_trade_ticks WHERE symbol = :s AND "
             "observed_at > :as_of - make_interval(secs => :w) AND observed_at <= :as_of ORDER BY observed_at ASC")
        p = {"s": s, "w": window, "as_of": _ao}
        from .optional_db_read import optional_fetchall

        rows = optional_fetchall(db, _sql(q), p)
    except Exception:
        return None
    return _aggressor_imbalance(rows)


def _aggressor_imbalance(rows) -> float | None:
    """Pure: signed-volume aggressor imbalance in [-1,1] from oldest-first ``(price, size, bid, ask)``
    trades. QUOTE RULE (Lee-Ready) when bid/ask present, TICK RULE fallback (zero-tick carries prior
    sign). ``None`` on empty/zero-volume. Separated for lookahead-free unit testing (no DB)."""
    if not rows:
        return None
    signed = 0.0
    total = 0.0
    prev_px = None
    last_sign = 0
    for r in rows:
        try:
            px = float(r[0])
            sz = float(r[1])
        except (TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0:
            continue
        bid, ask = r[2], r[3]
        sign = 0
        if bid is not None and ask is not None:
            try:
                fb, fa = float(bid), float(ask)
            except (TypeError, ValueError):
                fb = fa = 0.0
            if fa > fb > 0:
                mid = (fa + fb) / 2.0
                if px >= fa:
                    sign = 1
                elif px <= fb:
                    sign = -1
                elif px > mid:
                    sign = 1
                elif px < mid:
                    sign = -1
        if sign == 0:                                   # tick-rule fallback
            if prev_px is not None and px != prev_px:
                sign = 1 if px > prev_px else -1
            else:
                sign = last_sign                        # zero-tick / first trade carries prior sign
        prev_px = px
        if sign != 0:
            last_sign = sign
        signed += sign * sz
        total += sz
    if total <= 0:
        return None
    return float(max(-1.0, min(1.0, signed / total)))


def _event_grid_log_returns(
    rows, *, grid_secs: float
) -> tuple[list[float], float, dict]:
    """Pure: sub-sample oldest-first ``(price, observed_at)`` trade ticks onto a
    ~``grid_secs`` EVENT-TIME grid, then return the grid log-returns r_i =
    ln(p_i / p_{i-1}), the observed TICK RATE (ticks/sec over the window span), and a
    debug dict. Sub-sampling to a coarse grid is the denoising step (collapses the
    bid-ask-bounce micro-noise the per-tick series carries) — one grid sample per
    bucket (the LAST price in each bucket). ``grid_secs <= 0`` ⇒ every tick is its own
    grid point (no sub-sample). Separated for lookahead-free unit testing (no DB)."""
    pts: list[tuple[float, float]] = []  # (epoch_secs, price)
    for r in rows or []:
        try:
            px = float(r[0])
        except (TypeError, ValueError):
            continue
        ts = r[1]
        try:
            epoch = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
        except (TypeError, ValueError, OverflowError):
            continue
        if px <= 0 or not math.isfinite(px) or not math.isfinite(epoch):
            continue
        pts.append((epoch, px))
    if len(pts) < 2:
        return [], 0.0, {"n_ticks": len(pts), "n_grid": 0}
    span = max(1e-9, pts[-1][0] - pts[0][0])
    tick_rate = len(pts) / span
    try:
        gs = float(grid_secs)
    except (TypeError, ValueError):
        gs = 0.0
    if gs > 0:
        grid: list[float] = []
        bucket_start = pts[0][0]
        last_px = pts[0][1]
        for epoch, px in pts:
            if epoch - bucket_start >= gs:
                grid.append(last_px)
                bucket_start = epoch
            last_px = px
        grid.append(last_px)  # close the final bucket
        prices = grid
    else:
        prices = [p for _, p in pts]
    returns: list[float] = []
    for i in range(1, len(prices)):
        p0, p1 = prices[i - 1], prices[i]
        if p0 > 0 and p1 > 0:
            r = math.log(p1 / p0)
            if math.isfinite(r):
                returns.append(r)
    return returns, float(tick_rate), {"n_ticks": len(pts), "n_grid": len(prices), "span_s": span}


def _live_realized_vol(
    symbol: str,
    db: Any = None,
    *,
    as_of: "datetime | None" = None,
    grid_secs: float | None = None,
) -> dict | None:
    """LIVE denoised realized-vol read for the vol-norm runner trail (LEVER 2A).

    Equity: sub-samples ``iqfeed_trade_ticks`` (price/observed_at) over the short OFI
    window onto a ~``grid_secs`` event grid, computes grid log-returns, and returns the
    inputs the pure trail-width math (``paper_execution.volnorm_trail_dist_pct``)
    needs::

        {"rv_step": per-grid-step stdev (EWMA),    # paper_execution.denoised_rv_ewma
         "tick_rate": ticks/sec over the window,
         "eff_spread_pct": Roll half-spread frac or None,
         "grid_secs": the grid bucket used,
         "n_ticks", "n_grid"}

    Returns ``None`` when the tape is thin (< 2 grid returns) ⇒ the caller falls back to
    the frozen expected_move width. Crypto (``-USD``) currently returns None (no equity
    tape) — the trail there stays on the existing path. Mirrors the OFI-window read
    pattern exactly (same window knob, same as_of replay semantics)."""
    from .paper_execution import denoised_rv_ewma, roll_effective_spread_pct

    s = (symbol or "").strip().upper()
    if not s or s.endswith("-USD"):
        return None
    try:
        window = float(getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0)
    except (TypeError, ValueError):
        window = 15.0
    # Vol-norm trail wants a longer realized-vol lookback than the 15s OFI window so the
    # EWMA has enough grid steps; reuse the documented trail window knob (default 90s).
    try:
        rv_window = float(getattr(settings, "chili_momentum_volnorm_trail_window_s", 90.0) or 90.0)
    except (TypeError, ValueError):
        rv_window = 90.0
    window = max(window, rv_window)
    if grid_secs is None:
        try:
            grid_secs = float(getattr(settings, "chili_momentum_volnorm_trail_grid_secs", 2.0) or 2.0)
        except (TypeError, ValueError):
            grid_secs = 2.0
    captured = _microstructure_provider_read(
        operation=CaptureMicrostructureOperation.REALIZED_VOL,
        symbol=s,
        as_of=as_of,
        parameters={
            "window_seconds": window,
            "grid_seconds": float(grid_secs),
        },
    )
    if captured is not _MICROSTRUCTURE_PROVIDER_MISSING:
        return captured
    if db is None:
        return None
    try:
        from sqlalchemy import text as _sql

        # Always the bounded as-of form via the replay-aware chokepoint (see
        # _live_trade_flow — live-identical, replay-honest).
        _ao = _tape_asof_default(as_of)
        _ao = _ao.replace(tzinfo=None) if getattr(_ao, "tzinfo", None) is not None else _ao
        q = ("SELECT price, observed_at FROM iqfeed_trade_ticks WHERE symbol = :s AND "
             "observed_at > :as_of - make_interval(secs => :w) AND observed_at <= :as_of "
             "ORDER BY observed_at ASC")
        p = {"s": s, "w": window, "as_of": _ao}
        from .optional_db_read import optional_fetchall

        rows = optional_fetchall(db, _sql(q), p)
    except Exception:
        return None
    returns, tick_rate, dbg = _event_grid_log_returns(rows, grid_secs=grid_secs)
    if len(returns) < 2:
        return None
    # EWMA half-life in GRID-STEP count: half the window's worth of grid steps so the
    # most recent ~window/2 dominates (derived from the window, not a fresh magic number).
    half_life = max(2.0, (window / max(grid_secs, 1e-9)) / 2.0)
    rv_step = denoised_rv_ewma(returns, half_life=half_life)
    if rv_step is None:
        return None
    return {
        "rv_step": float(rv_step),
        "tick_rate": float(tick_rate),
        "eff_spread_pct": roll_effective_spread_pct(returns),
        "grid_secs": float(grid_secs),
        "n_ticks": int(dbg.get("n_ticks", 0)),
        "n_grid": int(dbg.get("n_grid", 0)),
    }


def _event_grid_aggressor_flow(
    rows, *, grid_secs: float
) -> tuple[list[float], float, dict]:
    """Pure (LEVER 2B): from oldest-first ``(price, size, bid, ask, observed_at)`` trade
    ticks, classify every trade's aggressor sign with the SAME Lee-Ready quote/tick rule
    as ``_aggressor_imbalance`` (px>=ask buy / px<=bid sell / else mid-split; tick-rule
    fallback carries the prior sign on a zero-tick), bucket the SIGNED volume onto a
    ~``grid_secs`` event grid, and return the per-bucket OFI LEVEL series
    (Σ(sign·size)/Σ(size) within each bucket, in [-1,1]) + the observed tick_rate
    (ticks/sec over the span) + a debug dict. Sub-sampling to the grid is the denoising
    step (one OFI level per bucket); the EWMA SLOPE of THIS series (computed downstream by
    ``ofi_level_and_slope``) is the rollover signal. ``grid_secs <= 0`` ⇒ one bucket per
    tick. Separated for lookahead-free unit testing (no DB)."""
    parsed: list[tuple[float, int, float]] = []  # (epoch, signed_size, size)
    prev_px = None
    last_sign = 0
    for r in rows or []:
        try:
            px = float(r[0])
            sz = float(r[1])
        except (TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0 or not math.isfinite(px) or not math.isfinite(sz):
            continue
        ts = r[4] if len(r) > 4 else None
        try:
            epoch = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(epoch):
            continue
        bid, ask = (r[2] if len(r) > 2 else None), (r[3] if len(r) > 3 else None)
        sign = 0
        if bid is not None and ask is not None:
            try:
                fb, fa = float(bid), float(ask)
            except (TypeError, ValueError):
                fb = fa = 0.0
            if fa > fb > 0:
                mid = (fa + fb) / 2.0
                if px >= fa:
                    sign = 1
                elif px <= fb:
                    sign = -1
                elif px > mid:
                    sign = 1
                elif px < mid:
                    sign = -1
        if sign == 0:  # tick-rule fallback (zero-tick / first trade carries prior sign)
            if prev_px is not None and px != prev_px:
                sign = 1 if px > prev_px else -1
            else:
                sign = last_sign
        prev_px = px
        if sign != 0:
            last_sign = sign
        parsed.append((epoch, sign * sz, sz))
    if len(parsed) < 2:
        return [], 0.0, {"n_ticks": len(parsed), "n_grid": 0}
    span = max(1e-9, parsed[-1][0] - parsed[0][0])
    tick_rate = len(parsed) / span
    try:
        gs = float(grid_secs)
    except (TypeError, ValueError):
        gs = 0.0
    levels: list[float] = []
    if gs > 0:
        bucket_start = parsed[0][0]
        b_signed = 0.0
        b_total = 0.0
        for epoch, signed_sz, sz in parsed:
            if epoch - bucket_start >= gs and b_total > 0:
                levels.append(max(-1.0, min(1.0, b_signed / b_total)))
                bucket_start = epoch
                b_signed = 0.0
                b_total = 0.0
            b_signed += signed_sz
            b_total += sz
        if b_total > 0:
            levels.append(max(-1.0, min(1.0, b_signed / b_total)))
    else:
        for _, signed_sz, sz in parsed:
            levels.append(max(-1.0, min(1.0, signed_sz / sz)) if sz > 0 else 0.0)
    return levels, float(tick_rate), {"n_ticks": len(parsed), "n_grid": len(levels), "span_s": span}


def _live_flow_slope(
    symbol: str,
    db: Any = None,
    *,
    as_of: "datetime | None" = None,
    grid_secs: float | None = None,
) -> dict | None:
    """LIVE denoised order-flow read for the velocity/persistence RIDE-LOCK (LEVER 2B).

    Equity: reads ``iqfeed_trade_ticks`` (price/size/bid/ask/observed_at) over the same
    short OFI window the other exits use, buckets the Lee-Ready signed flow onto the
    ~``grid_secs`` event grid (``_event_grid_aggressor_flow``), and returns the inputs the
    pure RIDE-LOCK math (``paper_execution.ofi_level_and_slope`` +
    ``velocity_persistence_ride_lock``) needs::

        {"ofi_level": denoised EWMA OFI level (most recent),
         "ofi_slope": EWMA-OFI 1st-derivative (slope; <0 = rollover),
         "tick_rate": ticks/sec over the window,
         "last_price": last executed trade price,           # HARD sellers-through test
         "mid": last (bid+ask)/2 = the L1 fair-value ref,   # micro-price proxy (no L1 sizes)
         "grid_secs", "n_ticks", "n_grid"}

    Returns ``None`` when the tape is thin (< 2 grid buckets) OR STALE (newest print older
    than one event-grid step ``grid_secs`` — an explicit latest-tick-age gate so a silent
    tape that still has buckets inside the trailing window cannot yield a flow read) ⇒ the
    caller falls back to the 2A vol-norm trail (byte-identical). Crypto (``-USD``) returns None (no equity tape) — the
    RIDE-LOCK there stays off and the existing path is unchanged. Mirrors the OFI-window read
    pattern (same window knob, same as_of replay semantics)."""
    from .paper_execution import ofi_level_and_slope

    s = (symbol or "").strip().upper()
    if not s or s.endswith("-USD"):
        return None
    try:
        window = float(getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0)
    except (TypeError, ValueError):
        window = 15.0
    if grid_secs is None:
        try:
            grid_secs = float(getattr(settings, "chili_momentum_volnorm_trail_grid_secs", 2.0) or 2.0)
        except (TypeError, ValueError):
            grid_secs = 2.0
    try:
        half_life = float(getattr(settings, "chili_momentum_velocity_ofi_slope_half_life", 4.0) or 4.0)
    except (TypeError, ValueError):
        half_life = 4.0
    half_life = max(1.0, half_life)
    captured = _microstructure_provider_read(
        operation=CaptureMicrostructureOperation.FLOW_SLOPE,
        symbol=s,
        as_of=as_of,
        parameters={
            "window_seconds": window,
            "grid_seconds": float(grid_secs),
            "half_life_steps": half_life,
        },
    )
    if captured is not _MICROSTRUCTURE_PROVIDER_MISSING:
        return captured
    if db is None:
        return None
    try:
        from sqlalchemy import text as _sql

        # Always the bounded as-of form via the replay-aware chokepoint (see
        # _live_trade_flow — live-identical, replay-honest). The latest-tick-age
        # gate below reuses the SAME resolved anchor as its ``now`` reference.
        _ao = _tape_asof_default(as_of)
        _ao = _ao.replace(tzinfo=None) if getattr(_ao, "tzinfo", None) is not None else _ao
        q = ("SELECT price, size, bid, ask, observed_at FROM iqfeed_trade_ticks WHERE symbol = :s AND "
             "observed_at > :as_of - make_interval(secs => :w) AND observed_at <= :as_of "
             "ORDER BY observed_at ASC")
        p = {"s": s, "w": window, "as_of": _ao}
        from .optional_db_read import optional_fetchall

        rows = optional_fetchall(db, _sql(q), p)
    except Exception:
        return None
    # EXPLICIT latest-tick-age gate (fail-closed on a frozen tape). The rolling-window
    # query (observed_at > ref - window) only bounds the OLDEST tick; a tape that went
    # silent can still carry >= 2 grid buckets inside the trailing window (e.g. last print
    # ~one window old) and would yield a NON-None, possibly POSITIVE flow read — letting
    # GAP-B / GAP-A's decisive_flow_cut fire on an effectively FROZEN tape. We require the
    # NEWEST print to be no older than ONE event-grid step (``grid_secs``): no fresh
    # aggressor flow within a single grid bucket ⇒ the grid-based flow read is stale ⇒
    # return None ⇒ the caller falls back to the 2A vol-norm trail (byte-identical). The
    # bound is the SAME adaptive grid knob the flow is bucketed on (no magic number; falls
    # back to the window only if the grid is per-tick/0). Age ref matches the query's
    # ``now`` semantics: ``as_of`` for replay, else UTC now (both UTC-naive).
    try:
        if rows and rows[-1][4] is not None:
            _ref = _ao  # the same resolved anchor the query ran with (naive UTC)
            _last_at = rows[-1][4]
            if getattr(_last_at, "tzinfo", None) is not None:
                _last_at = _last_at.replace(tzinfo=None)
            _max_age = grid_secs if grid_secs and grid_secs > 0 else window
            if (_ref - _last_at).total_seconds() > _max_age:
                return None
    except (TypeError, ValueError, IndexError):
        return None
    levels, tick_rate, dbg = _event_grid_aggressor_flow(rows, grid_secs=grid_secs)
    if len(levels) < 2:
        return None
    level, slope = ofi_level_and_slope(levels, half_life=half_life)
    if level is None:
        return None
    # latest L1 + last print (for the HARD-exit "sellers lifting through the fair value"
    # test). iqfeed_trade_ticks carries no L1 SIZES, so the micro-price degenerates to the
    # mid — used as the fair-value reference; the last trade price vs the mid is the
    # sellers-through read (last print at/below the mid ⇒ sellers hitting the bid down).
    last_price = mid = None
    try:
        if rows:
            lr = rows[-1]
            last_price = float(lr[0]) if lr[0] is not None else None
            _lb = float(lr[2]) if lr[2] is not None else None
            _la = float(lr[3]) if lr[3] is not None else None
            if _lb is not None and _la is not None and _la > _lb > 0:
                mid = (_lb + _la) / 2.0
    except (TypeError, ValueError, IndexError):
        last_price = mid = None
    return {
        "ofi_level": float(level),
        "ofi_slope": (float(slope) if slope is not None else None),
        "tick_rate": float(tick_rate),
        "last_price": last_price,
        "mid": mid,
        "grid_secs": float(grid_secs),
        "n_ticks": int(dbg.get("n_ticks", 0)),
        "n_grid": int(dbg.get("n_grid", 0)),
    }


@dataclass(frozen=True)
class LadderRead:
    """Multi-level L2 distribution read for the proactive sell-into-strength exit.

    Every field fail-safe to ``None``; a ``None`` in any REQUIRED slot (depth_imbal,
    depth_imbal_pctile, ofi, micro_edge) keeps the exit state machine in HOLD — it
    never sells on missing/stale data. Crypto-only (``fast_orderbook`` is ``-USD``)."""

    depth_imbal: float | None          # (Σbid5 − Σask5)/(Σbid5 + Σask5) ∈ [-1,1], NEWEST snap
    depth_imbal_pctile: float | None   # where NEWEST imbalance sits in the K-window [0,1]
    ofi: float | None                  # order-flow imbalance (Cont), in-process/table
    micro_edge: float | None           # micro-price edge (bps, Stoikov)
    bid_refill: float | None           # Δ(best-bid size)/prior across the window
    ask_build: float | None            # Δ(Σask5)/prior across the window (wall building)
    spread_bps: float | None
    snapshot_age_s: float | None       # now − newest snapshot_at (staleness gate)
    n_snaps: int                       # rows actually parsed


def _depth_imbal5(bl: Any, al: Any) -> float | None:
    """5-level depth imbalance from [[price,size],…] tuple ladders. None if empty."""
    try:
        b = sum(float(l[1]) for l in bl[:5])
        a = sum(float(l[1]) for l in al[:5])
    except (TypeError, ValueError, IndexError):
        return None
    return (b - a) / (b + a) if (b + a) > 0 else None


def read_ladder_distribution(
    symbol: str, db: Any = None, *, k: int = 6, as_of: "datetime | None" = None
) -> LadderRead:
    """Multi-level L2 distribution read for the proactive sell-into-strength exit.
    CLASS-AWARE: crypto (``-USD``) reads the per-level ``fast_orderbook`` table; equities
    read the aggregate 5-level ``iqfeed_depth_snapshots`` (bid5_size/ask5_size/imbalance5
    — no per-level arrays, but the aggregate is enough for the distribution read). Both
    pair with the OFI/micro read. K newest snapshots in a 30s window, newest-first.
    Fail-open: any miss → ``None`` fields → the caller HOLDs (never sells on bad data).

    ``as_of`` (UTC-naive) reads AS-OF a historical instant for the replay instrument
    (window ``(as_of - 30s, as_of]``, age relative to ``as_of``); ``as_of=None`` is the
    LIVE default and emits the EXACT original SQL → byte-identical."""
    _NULL = LadderRead(None, None, None, None, None, None, None, None, 0)
    s = (symbol or "").strip().upper()
    if not s or db is None:
        return _NULL
    captured = _microstructure_provider_read(
        operation=CaptureMicrostructureOperation.LADDER_DISTRIBUTION,
        symbol=s,
        as_of=as_of,
        parameters={
            "window_seconds": 30.0,
            "snapshot_limit": int(k),
            "multilevel_ofi_enabled": bool(
                getattr(settings, "chili_momentum_l2_multilevel_ofi_enabled", True)
            ),
        },
    )
    if captured is _MICROSTRUCTURE_COVERAGE_UNAVAILABLE:
        return _NULL
    if captured is not _MICROSTRUCTURE_PROVIDER_MISSING:
        return captured
    if as_of is not None and getattr(as_of, "tzinfo", None) is not None:
        as_of = as_of.replace(tzinfo=None)
    ofi, micro = None, None
    try:
        ofi, micro = _live_ofi_microprice(s, db=db, as_of=as_of)
    except Exception:
        pass
    if s.endswith("-USD"):
        return _ladder_crypto(s, db, int(k), ofi, micro, as_of=as_of)
    return _ladder_equity(s, db, int(k), ofi, micro, as_of=as_of)


def _ladder_crypto(
    s: str, db: Any, k: int, ofi: float | None, micro: float | None,
    as_of: "datetime | None" = None,
) -> LadderRead:
    """Crypto ladder from ``fast_orderbook`` — per-level JSONB ``[[price,size],…]``."""
    try:
        from sqlalchemy import text as _sql

        if as_of is None:
            _q = (
                "SELECT snapshot_at, bid_levels, ask_levels, spread_bps "
                "FROM fast_orderbook WHERE ticker = :s AND snapshot_at > "
                "(now() at time zone 'utc') - make_interval(secs => :w) "
                "ORDER BY snapshot_at DESC LIMIT :k"
            )
            _p = {"s": s, "w": 30.0, "k": int(k)}
        else:
            _q = (
                "SELECT snapshot_at, bid_levels, ask_levels, spread_bps "
                "FROM fast_orderbook WHERE ticker = :s "
                "AND snapshot_at > :as_of - make_interval(secs => :w) "
                "AND snapshot_at <= :as_of ORDER BY snapshot_at DESC LIMIT :k"
            )
            _p = {"s": s, "w": 30.0, "k": int(k), "as_of": as_of}
        from .optional_db_read import optional_fetchall

        rows = optional_fetchall(db, _sql(_q), _p)
    except Exception:
        rows = []
    if not rows:
        return LadderRead(None, None, ofi, micro, None, None, None, None, 0)
    import json as _json

    series = []  # newest-first: (snapshot_at, bid_levels, ask_levels, spread_bps)
    for r in rows:
        try:
            bl = _json.loads(r[1]) if isinstance(r[1], str) else r[1]
            al = _json.loads(r[2]) if isinstance(r[2], str) else r[2]
            if isinstance(bl, list) and isinstance(al, list) and bl and al:
                series.append((r[0], bl, al, r[3]))
        except Exception:
            continue
    if not series:
        return LadderRead(None, None, ofi, micro, None, None, None, None, 0)
    newest, oldest = series[0], series[-1]
    imb_now = _depth_imbal5(newest[1], newest[2])
    imbs = [v for v in (_depth_imbal5(b, a) for _, b, a, _ in series) if v is not None]
    pctile = None
    if imb_now is not None and len(imbs) >= 3:
        # Low percentile ⇒ NEWEST book is ask-heavy relative to its own recent window
        # (a TREND of distribution, not an absolute threshold a single spoof can trip).
        pctile = sum(1 for v in imbs if v <= imb_now) / float(len(imbs))

    def _bb(lv: Any) -> float:
        try:
            return float(lv[0][1])
        except (TypeError, ValueError, IndexError):
            return 0.0

    def _sa5(lv: Any) -> float:
        try:
            return sum(float(x[1]) for x in lv[:5])
        except (TypeError, ValueError, IndexError):
            return 0.0

    bb_old = _bb(oldest[1])
    bid_refill = (_bb(newest[1]) - bb_old) / bb_old if bb_old > 0 else None
    a_old = _sa5(oldest[2])
    ask_build = (_sa5(newest[2]) - a_old) / a_old if a_old > 0 else None
    try:
        age = max(0.0, ((as_of or datetime.utcnow()) - newest[0]).total_seconds())
    except Exception:
        age = None
    try:
        spread = float(newest[3]) if newest[3] is not None else None
    except (TypeError, ValueError):
        spread = None
    return LadderRead(imb_now, pctile, ofi, micro, bid_refill, ask_build, spread, age, len(series))


def _eq_imbalance5(row: Any) -> float | None:
    """5-level imbalance for an iqfeed row: prefer the precomputed ``imbalance5``;
    else derive from the aggregate 5-level sizes (bid5_size − ask5_size)/(sum)."""
    try:
        if row[7] is not None:
            return float(row[7])
    except (TypeError, ValueError, IndexError):
        pass
    try:
        b = float(row[5] or 0.0)
        a = float(row[6] or 0.0)
    except (TypeError, ValueError, IndexError):
        return None
    return (b - a) / (b + a) if (b + a) > 0 else None


def _ladder_equity(
    s: str, db: Any, k: int, ofi: float | None, micro: float | None,
    as_of: "datetime | None" = None,
) -> LadderRead:
    """Equity ladder from ``iqfeed_depth_snapshots`` — aggregate 5-level (bid5_size,
    ask5_size, imbalance5) + top-of-book (bid_top/ask_top + sizes). No per-level arrays,
    but the aggregate carries the distribution signal. Populated market-hours by the
    iqfeed depth bridge → ``None`` (HOLD) overnight / pre-RTH before data flows."""
    try:
        from sqlalchemy import text as _sql

        if as_of is None:
            _q = (
                "SELECT observed_at, bid_top, ask_top, bid_top_size, ask_top_size, "
                "bid5_size, ask5_size, imbalance5 FROM iqfeed_depth_snapshots "
                "WHERE symbol = :s AND observed_at > "
                "(now() at time zone 'utc') - make_interval(secs => :w) "
                "ORDER BY observed_at DESC LIMIT :k"
            )
            _p = {"s": s, "w": 30.0, "k": int(k)}
        else:
            _q = (
                "SELECT observed_at, bid_top, ask_top, bid_top_size, ask_top_size, "
                "bid5_size, ask5_size, imbalance5 FROM iqfeed_depth_snapshots "
                "WHERE symbol = :s AND observed_at > :as_of - make_interval(secs => :w) "
                "AND observed_at <= :as_of ORDER BY observed_at DESC LIMIT :k"
            )
            _p = {"s": s, "w": 30.0, "k": int(k), "as_of": as_of}
        from .optional_db_read import optional_fetchall

        rows = optional_fetchall(db, _sql(_q), _p)
    except Exception:
        rows = []
    if not rows:
        return LadderRead(None, None, ofi, micro, None, None, None, None, 0)
    series = list(rows)  # newest-first
    newest, oldest = series[0], series[-1]
    imb_now = _eq_imbalance5(newest)
    imbs = [v for v in (_eq_imbalance5(r) for r in series) if v is not None]
    pctile = None
    if imb_now is not None and len(imbs) >= 3:
        pctile = sum(1 for v in imbs if v <= imb_now) / float(len(imbs))

    def _f(x: Any) -> float | None:
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    bbo = _f(oldest[3])  # oldest bid_top_size — best-bid refill across the window
    bbn = _f(newest[3])
    bid_refill = (bbn - bbo) / bbo if (bbo and bbo > 0 and bbn is not None) else None
    a_old = _f(oldest[6])  # oldest ask5_size — ask-side build across the window
    a_new = _f(newest[6])
    ask_build = (a_new - a_old) / a_old if (a_old and a_old > 0 and a_new is not None) else None
    bt, at = _f(newest[1]), _f(newest[2])  # top-of-book spread (no spread col in iqfeed)
    spread = (at - bt) / ((at + bt) / 2.0) * 10_000.0 if (bt and at and (bt + at) > 0) else None
    try:
        age = max(0.0, ((as_of or datetime.utcnow()) - newest[0]).total_seconds())
    except Exception:
        age = None
    return LadderRead(imb_now, pctile, ofi, micro, bid_refill, ask_build, spread, age, len(series))


# --- FLOAT BACKFILL (anti-flicker) ---------------------------------------------------
# Share FLOAT is STATIC (does not change intraday), so a known value is a CONSTANT, not
# stale dynamic data — caching/backfilling it is correct + safe. The scanner bridge
# forwards a ROTATING subset of movers per cycle; a symbol absent from THIS tick's
# ross_signals never gets float-enriched, so its persisted
# execution_readiness_json.extra.ross_signals[sym].float_shares flickers to None and the
# fail-closed A-setup quality floor wrongly rejects a genuine low-float A-setup. We
# backfill the missing float from the last-known value: a bounded in-process cache first
# (cheap, survives within the process), else the prior persisted viability row (durable,
# survives restarts). Gated by chili_momentum_float_persistence_enabled (OFF => no calls
# => byte-identical). Equities only; never overwrites a fresh real float; never fabricates
# for a never-seen symbol (stays None => fail-closed reject = correct).
_FLOAT_CACHE_MAX = 4096  # hard max size (bounded — no unbounded growth)
_FLOAT_CACHE_TTL_S = 86400.0  # 24h TTL; float is static so a generous TTL is safe
# symbol -> (float_shares, monotonic_expiry_ts)
_FLOAT_LAST_KNOWN: dict[str, tuple[float, float]] = {}


def _float_cache_get(symbol: str) -> float | None:
    """Last-known float for symbol from the bounded in-process cache (None if absent/expired)."""
    try:
        entry = _FLOAT_LAST_KNOWN.get(symbol)
        if entry is None:
            return None
        val, exp = entry
        if time.monotonic() >= exp:
            _FLOAT_LAST_KNOWN.pop(symbol, None)
            return None
        return val
    except Exception:
        return None


def _float_cache_put(symbol: str, float_shares: float) -> None:
    """Record a known float for symbol (bounded: evicts the oldest-expiring entry when full)."""
    try:
        if not float_shares or float_shares <= 0:
            return
        if (
            symbol not in _FLOAT_LAST_KNOWN
            and len(_FLOAT_LAST_KNOWN) >= _FLOAT_CACHE_MAX
        ):
            # evict the entry that expires soonest (approx-LRU; keeps the map bounded)
            try:
                oldest = min(_FLOAT_LAST_KNOWN.items(), key=lambda kv: kv[1][1])[0]
                _FLOAT_LAST_KNOWN.pop(oldest, None)
            except ValueError:
                pass
        _FLOAT_LAST_KNOWN[symbol] = (float(float_shares), time.monotonic() + _FLOAT_CACHE_TTL_S)
    except Exception:
        pass


def _last_known_float_for_symbol(db: Session, symbol: str) -> float | None:
    """Last-known float for symbol: bounded in-process cache first, else the prior persisted
    momentum_symbol_viability row's execution_readiness_json.extra.ross_signals[symbol].float_shares.

    Returns None when no prior value exists (never-seen symbol => stays None => fail-closed).
    Read-only; best-effort (any error => None, fail-open to the market_cap/price estimate)."""
    cached = _float_cache_get(symbol)
    if cached is not None:
        return cached
    try:
        from ....models.trading import MomentumSymbolViability

        rows = (
            db.query(MomentumSymbolViability.execution_readiness_json)
            .filter(MomentumSymbolViability.symbol == symbol)
            .order_by(MomentumSymbolViability.freshness_ts.desc())
            .limit(4)
            .all()
        )
        for (erj,) in rows:
            if not isinstance(erj, dict):
                continue
            extra = erj.get("extra")
            if not isinstance(extra, dict):
                continue
            rsig = extra.get("ross_signals")
            if not isinstance(rsig, dict):
                continue
            sig = rsig.get(symbol)
            if not isinstance(sig, dict):
                continue
            _f = sig.get("float_shares")
            if _f is None:
                continue
            try:
                fval = float(_f)
            except (TypeError, ValueError):
                continue
            if fval > 0:
                _float_cache_put(symbol, fval)  # warm the cache for cheap subsequent ticks
                return fval
    except Exception:
        return None
    return None


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
    decision_as_of_utc: Optional[datetime] = None,
) -> dict[str, Any]:
    """Compute regime + family viability; persist on hub and viability pool nodes."""
    _ = graph_version
    meta = dict(meta or {})
    decision_at = _tape_asof_default(decision_as_of_utc)
    if decision_at.tzinfo is not None:
        decision_at = decision_at.astimezone(timezone.utc).replace(tzinfo=None)
    if _replay_clock_is_bound():
        raise ReplayPipelineInputUnavailableError(
            "replay selection_pipeline inputs are unavailable: "
            "complete content-addressed provider bundle not bound"
        )
    tickers = meta.get("tickers")
    if isinstance(tickers, list) and tickers:
        symbols = [str(t).strip().upper() for t in tickers if t][:32]
        scope = VIABILITY_SCOPE_SYMBOL
    else:
        symbols = ["__aggregate__"]
        scope = VIABILITY_SCOPE_AGGREGATE

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
            from .ross_momentum import ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED
            from .ross_momentum import score_universe as _ross_score_universe

            # Liquidity-BIASED weights: prefer movers the lane can actually FILL
            # (dollar turnover -> tighter spread), not only the most explosive
            # names that get spread-gated and only ever watched. Validated on the
            # 11-day previous-days A/B replay: +6 fills, +$914 PnL vs baseline
            # (scripts/_sim_liquidity_selection.py, 2026-06-10).
            _weights = ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED
            # REAL-FLOAT enrichment (GAP 1, off => byte-identical): the low-float pillar
            # wants a SHARE COUNT but without a producer it falls back to market_cap ($,
            # wrong units, price-contaminated). Inject the real float (share count) from
            # the reference endpoint; for names it can't resolve, use a CONSISTENT
            # share-count estimate (market_cap / price) so the pillar never MIXES
            # share-count and $ across names (which would corrupt the -log10 ranking).
            # Cached + fail-open. Kill-switch chili_momentum_use_real_float=False.
            if bool(getattr(settings, "chili_momentum_use_real_float", True)):
                try:
                    from ...massive_client import get_ticker_float

                    def _pick_num(_d, _keys):
                        for _k in _keys:
                            _v = _d.get(_k)
                            if _v is not None:
                                try:
                                    return float(_v)
                                except (TypeError, ValueError):
                                    continue
                        return None

                    # anti-flicker backfill: when this cycle can't resolve a real float for a
                    # symbol (rotating-subset enrichment), fill from the last-known STATIC value
                    # so float_shares never flickers to None for a name we've already resolved.
                    # OFF => byte-identical (no cache writes, no last-known reads). FILLS missing
                    # only; never overwrites a fresh real float; never fabricates a never-seen one.
                    _float_backfill = bool(
                        getattr(settings, "chili_momentum_float_persistence_enabled", False)
                    )
                    for _sym, _sig in _ross_signals.items():
                        if not isinstance(_sig, dict) or "-USD" in str(_sym):
                            continue  # equities only
                        try:
                            _f = get_ticker_float(_sym)
                            if _f and _f > 0:
                                _sig["float_shares"] = float(_f)
                                if _float_backfill:
                                    _float_cache_put(_sym, float(_f))  # record the fresh real float
                                continue
                            # fresh real float unavailable this cycle: BACKFILL last-known (static)
                            # before the market_cap/price estimate, so a known float never flickers.
                            if _float_backfill:
                                _lk = _last_known_float_for_symbol(db, _sym)
                                if _lk and _lk > 0:
                                    _sig["float_shares"] = float(_lk)
                                    continue
                            _mc = _pick_num(_sig, ("market_cap", "marketcap"))
                            _px = _pick_num(_sig, ("price", "last", "close", "last_price"))
                            if _mc and _px and _px > 0:
                                _sig["float_shares"] = _mc / _px
                        except Exception:
                            continue
                except Exception:
                    pass
            # Daily-chart context TILT (off => byte-identical): enrich each candidate
            # with daily_structure_pct (break ABOVE a major daily level + room to the
            # next level + soft trend) and switch to the 5-pillar weights. The daily
            # fetch is cached (600s) and runs on the viability-refresh pass, NOT the
            # live tick path. A breaking-spike scores HIGH (the CUPR guarantee), so the
            # tilt PREFERS clean daily breakouts; it can never block a fill.
            if bool(getattr(settings, "chili_momentum_daily_context_enabled", True)):
                try:
                    from .ross_momentum import (
                        ROSS_PILLAR_WEIGHTS_DAILY_CONTEXT,
                        daily_200ema_room_subscore as _r200_sub,
                    )
                    from .daily_levels import compute_daily_context
                    from ..market_data import fetch_ohlcv_df as _fetch_daily

                    _lb = int(getattr(settings, "chili_momentum_daily_lookback_days", 20) or 20)
                    # Three SOFT daily-structure tilts fold INTO daily_structure_pct (no
                    # viability.py change). Each flag-off ⇒ that tilt is byte-identical.
                    _gap_tilt = bool(getattr(settings, "chili_momentum_gap_geometry_tilt_enabled", True))
                    _rej_derate = bool(getattr(settings, "chili_momentum_red_rejection_derate_enabled", True))
                    _blue_ipo = bool(getattr(settings, "chili_momentum_blue_sky_recent_ipo_enabled", True))
                    # 200-EMA ROOM tilt (opt-in, default OFF ⇒ dist_to_sma_200_atr stays discarded =
                    # byte-identical). Reward clear room above the daily 200MA, penalise pinned/below.
                    # Folded INTO daily_structure_pct (no viability.py change). Resolve flag + knob once.
                    _r200_on = bool(getattr(settings, "chili_momentum_daily_200ema_room_enabled", False))
                    _r200_clear = float(getattr(
                        settings, "chili_momentum_daily_200ema_clear_room_atr", 1.0))
                    # blue-sky needs a long window to know the all-time-high + IPO recency;
                    # fetch max history only when that tilt is on (else keep the 1y default).
                    _period = "max" if _blue_ipo else "1y"
                    # fresh-catalyst override set for (B): a name with FRESH news can blow
                    # through a previously-defended level, so the red-rejection de-rate is
                    # suppressed for it. Best-effort / cached; empty ⇒ no override (de-rate
                    # applies). Only resolved when the de-rate tilt is actually on.
                    _cat_set: set[str] = set()
                    if _rej_derate:
                        try:
                            from .catalyst import all_catalyst_symbols as _all_cat
                            _cat_set = {str(s).upper() for s in (_all_cat() or set())}
                        except Exception:
                            _cat_set = set()
                    _n_daily = 0
                    for _sym, _sig in _ross_signals.items():
                        if not isinstance(_sig, dict) or "-USD" in str(_sym):
                            continue  # equities only; crypto has no daily-S&R regime here
                        try:
                            _px = None
                            for _k in ("price", "last", "close", "last_price"):
                                _v = _sig.get(_k)
                                if _v is not None:
                                    _px = float(_v)
                                    break
                            # a fresh catalyst can override the red-rejection de-rate (a real
                            # news squeeze blows through a previously-defended level).
                            _fresh_cat = bool(_cat_set) and str(_sym).upper() in _cat_set
                            _ddf = _fetch_daily(_sym, interval="1d", period=_period)
                            _dctx = compute_daily_context(
                                _ddf, lookback=_lb, price=_px,
                                gap_geometry_tilt=_gap_tilt,
                                red_rejection_derate=_rej_derate,
                                blue_sky_recent_ipo=_blue_ipo,
                                fresh_catalyst=_fresh_cat,
                            )
                            if _dctx.daily_structure_pct is not None:
                                _ds_val = _dctx.daily_structure_pct
                                # 200-EMA ROOM blend (opt-in): fold the [0,1] room sub-score INTO
                                # daily_structure_pct as an equal-weight average (a MEASURED minority
                                # nudge, never a veto). OFF / unknown (< 200 bars) ⇒ unchanged.
                                if _r200_on:
                                    _room = _r200_sub(
                                        _dctx.dist_to_sma_200_atr, clear_room_atr=_r200_clear)
                                    if _room is not None:
                                        _ds_val = max(0.0, min(1.0, 0.5 * _ds_val + 0.5 * _room))
                                        _sig["dist_to_sma_200_atr"] = _dctx.dist_to_sma_200_atr
                                        _sig["daily_200ema_room_pct"] = _room
                                _sig["daily_structure_pct"] = _ds_val
                                _sig["daily_breaking_major"] = bool(_dctx.breaking_major_level)
                                _n_daily += 1
                        except Exception:
                            continue
                    if _n_daily > 0:
                        _weights = ROSS_PILLAR_WEIGHTS_DAILY_CONTEXT
                except Exception:
                    pass
            # FLOAT-ROTATION sustainability TILT (off => byte-identical): Ross SS101 —
            # cumulative session volume / shares float = how many times today's move has
            # turned over the tradeable float; the pace projected to EOD shapes a [0,1]
            # sub-score (>=~5x = ample fuel to sustain; <1x = fades). Stamp
            # float_rotation_pct onto each EQUITY signal and FOLD the float_rotation pillar
            # onto the ACTIVE weight-set (composable — score_universe renormalises over the
            # present pillars). Reuses the float_shares already enriched above + the
            # cumulative session volume the signal already carries (no new fetch) + the
            # session-fraction clock. RE-RANK only; never a veto. Equity-only (crypto float
            # = market-cap / 24h semantics differ). Flag default-ON ("no dark flags").
            if bool(getattr(settings, "chili_momentum_float_rotation_tilt_enabled", True)):
                try:
                    from .ross_momentum import (
                        ROSS_FLOAT_ROTATION_PILLAR_WEIGHT,
                        float_rotation_signal as _float_rotation_signal,
                    )
                    from .market_profile import minutes_since_regular_open as _mins_open

                    # OVER-ROTATION (exhaustion) fix — opt-in, default OFF (byte-identical).
                    # Resolve the kill-switch + its two documented knobs ONCE; when OFF we pass
                    # overrotation_threshold=None ⇒ the signal uses the legacy monotone curve.
                    _overrot_on = bool(getattr(
                        settings, "chili_momentum_float_overrotation_fix_enabled", False))
                    _overrot_thr = (
                        float(getattr(settings, "chili_momentum_float_overrotation_threshold", 3.0))
                        if _overrot_on else None
                    )
                    _overrot_min = float(getattr(
                        settings, "chili_momentum_float_overrotation_session_minute", 120.0))

                    _n_fr = 0
                    for _sym, _sig in _ross_signals.items():
                        if not isinstance(_sig, dict) or str(_sym).upper().endswith("-USD"):
                            continue  # equities only
                        try:
                            _cum_vol = None
                            for _vk in ("volume", "day_volume", "today_volume",
                                        "cumulative_volume", "session_volume"):
                                _vv = _sig.get(_vk)
                                if _vv is not None:
                                    try:
                                        _cum_vol = float(_vv)
                                        break
                                    except (TypeError, ValueError):
                                        continue
                            _flt = _sig.get("float_shares")
                            if _cum_vol is None or _flt is None:
                                continue  # fail-open: omit the pillar for this name
                            # session fraction: regular session is 390 min (09:30-16:00 ET).
                            _mo = _mins_open(_sym)
                            _sf = (max(0.0, float(_mo)) / 390.0) if _mo is not None else None
                            _fr = _float_rotation_signal(
                                _cum_vol, _flt, _sf,
                                overrotation_threshold=_overrot_thr,
                                overrotation_session_minute=_overrot_min,
                                minutes_since_open=(float(_mo) if _mo is not None else None),
                            )
                            if _fr.rotation_pct is not None:
                                _sig["float_rotation_pct"] = _fr.rotation_pct
                                _sig["float_rotation"] = _fr.float_rotation
                                _sig["projected_rotation_at_eod"] = _fr.projected_rotation_at_eod
                                _n_fr += 1
                        except Exception:
                            continue
                    if _n_fr > 0:
                        # FOLD the pillar onto the active weight-set without replacing it
                        # (score_universe self-renormalises over present pillars).
                        _weights = dict(_weights)
                        _weights["float_rotation"] = ROSS_FLOAT_ROTATION_PILLAR_WEIGHT
                except Exception:
                    pass
            # MECHANIZE THE RVOL PILLAR PREMARKET (2026-07-07): the scanner payload carries NO rvol
            # field premarket (0/182 blocked movers had any rvol OR raw-volume key), so
            # ross_tick_scalp_evidence_ok's rvol pillar is ALWAYS None premarket and genuine
            # explosive gappers (heavy float rotation but no rvol key) are wrongly blocked
            # (~146/182 recovered on merit). DERIVE a premarket-scale-correct rvol-equivalent from
            # FLOAT ROTATION = shares_traded/float (shares_traded = a raw volume key when present,
            # else dollar_volume/price -- present for ~100% of blocked movers) and STAMP it under the
            # exact key the gate already reads (intraday_cumulative_rvol, tick_scalp.py:_first_num).
            # Rotation is PARTICIPATION, not a time-deflated cumulative ratio, so it is correct
            # premarket. The ONE documented base maps rotation->rvol: rvol_equiv = MIN_RVOL *
            # rotation / rotation_base. GUARDED + MONOTONIC: stamp ONLY when it would ADMIT
            # (rvol_equiv >= MIN_RVOL) so a weak-rotation name stays None and the change-solo
            # null-pillar admission still fires -- never demotes a currently-admitted name. Never
            # clobbers a real scanner/ignition rvol (skip if any alias present; _first_num also
            # prefers them). Fail-open. Flag default-ON (no dark flags); OFF => key stays unset =>
            # byte-identical. Downstream tape-required + live-RVOL floor + structural stop +
            # max-loss circuit still gate the actual fill; this only affects WATCH admission.
            if bool(getattr(settings, "chili_momentum_premarket_rvol_pillar_enabled", True)):
                _MECH_MIN_RVOL = 5.0  # == ross_tick_scalp_evidence_ok min_rvol; the single anchor
                try:
                    _rot_base = float(getattr(
                        settings, "chili_momentum_premarket_rvol_rotation_base", 0.20) or 0.20)
                except (TypeError, ValueError):
                    _rot_base = 0.20
                _rvol_alias_keys = (
                    "rvol_pace", "rvol", "relative_volume", "relative_volume_daily_rate",
                    "daily_rate", "five_min_rvol", "5m_rvol", "volume_rate", "vol_ratio",
                    "intraday_cumulative_rvol",
                )
                for _sym, _sig in _ross_signals.items():
                    if not isinstance(_sig, dict) or str(_sym).upper().endswith("-USD"):
                        continue  # equities only
                    try:
                        if _rot_base <= 0:
                            continue
                        # never clobber a real, scanner/ignition-provided rvol
                        if any(_sig.get(_k) is not None for _k in _rvol_alias_keys):
                            continue
                        _flt = _sig.get("float_shares")
                        _flt = float(_flt) if _flt is not None else None
                        if _flt is None or _flt <= 0:
                            continue
                        # shares traded: prefer a raw volume key (RTH), else derive $-vol / price
                        _shares = None
                        for _vk in ("volume", "day_volume", "today_volume",
                                    "cumulative_volume", "session_volume"):
                            _vv = _sig.get(_vk)
                            if _vv is not None:
                                try:
                                    _shares = float(_vv)
                                    break
                                except (TypeError, ValueError):
                                    continue
                        if _shares is None:
                            _dv = _sig.get("dollar_volume")
                            _px = _sig.get("price")
                            _dv = float(_dv) if _dv is not None else None
                            _px = float(_px) if _px is not None else None
                            if _dv is not None and _px is not None and _px > 0:
                                _shares = _dv / _px
                        if _shares is None or _shares <= 0:
                            continue
                        _rotation = _shares / _flt
                        if _rotation <= 0:
                            continue
                        _rvol_equiv = _MECH_MIN_RVOL * (_rotation / _rot_base)
                        # GUARDED: stamp only when it would ADMIT (monotonic; never demotes a
                        # change-solo admit whose rotation is in [0, rotation_base)).
                        if _rvol_equiv >= _MECH_MIN_RVOL:
                            _sig["intraday_cumulative_rvol"] = round(_rvol_equiv, 4)
                            _sig["intraday_rvol_source"] = "float_rotation_premarket"
                    except Exception:
                        continue
            # SQUEEZE-FUEL TILT (off => byte-identical): Ross SS101 #2 — a heavily-shorted,
            # hard/expensive-to-borrow float = trapped sellers covering INTO the pop (the
            # rocket fuel behind the 100-1000% low-float verticals); free shares / easy-to-
            # borrow names get a small DE-RATE (shorts press the pop). CREDIT-FRUGAL: the
            # Ortex fetch is gated to the TOP-N explosive low-float candidates that already
            # pass the Ross screen (ranked by the CURRENT weight-set, NOT-below-floor), so the
            # Trader plan (1,000 credits/mo, 1 req/s) lasts; each result is cached 12h. Stamp
            # squeeze_fuel_pct onto those EQUITY signals + FOLD the squeeze_fuel pillar onto the
            # ACTIVE weight-set (composable). RE-RANK only; never a veto. Equity-only (crypto has
            # no borrow data). Flag default-ON ("no dark flags").
            if bool(getattr(settings, "chili_momentum_squeeze_fuel_tilt_enabled", True)):
                try:
                    from .ross_momentum import (
                        ROSS_SQUEEZE_FUEL_PILLAR_WEIGHT,
                        below_explosive_floor as _sf_below_floor,
                        score_universe as _sf_prelim_rank,
                        squeeze_fuel_signal as _squeeze_fuel_signal,
                    )
                    from .short_mechanics import get_short_mechanics as _get_short_mech

                    _top_n = int(getattr(settings, "chili_momentum_squeeze_fuel_top_n", 12) or 0)
                    if _top_n > 0:
                        # Preliminary rank with the CURRENT weights to pick the top-N explosive
                        # low-float EQUITY candidates that ALSO clear the explosive floor — the
                        # only names worth a credit. Crypto / below-floor names are excluded.
                        _prelim = _sf_prelim_rank(_ross_signals, weights=_weights)
                        _cands = [
                            s for s in _ross_signals
                            if isinstance(_ross_signals.get(s), dict)
                            and not str(s).upper().endswith("-USD")
                            and not _sf_below_floor(_ross_signals[s])
                        ]
                        _cands.sort(
                            key=lambda s: (_prelim[s].score if s in _prelim else 0.0),
                            reverse=True,
                        )
                        _n_sf = 0
                        for _sym in _cands[:_top_n]:
                            _sig = _ross_signals.get(_sym)
                            if not isinstance(_sig, dict):
                                continue
                            try:
                                _mech = _get_short_mech(_sym)
                                if not _mech:
                                    continue  # fail-open: no data => omit the pillar for this name
                                _sf = _squeeze_fuel_signal(
                                    _mech.get("short_interest_pct"),
                                    _mech.get("cost_to_borrow"),
                                    utilization=_mech.get("utilization"),
                                    is_easy_to_borrow=_mech.get("is_easy_to_borrow"),
                                )
                                if _sf.squeeze_pct is not None:
                                    _sig["squeeze_fuel_pct"] = _sf.squeeze_pct
                                    _sig["short_interest_pct"] = _sf.short_interest_pct
                                    _sig["cost_to_borrow"] = _sf.cost_to_borrow
                                    _sig["is_easy_to_borrow"] = _sf.is_easy_to_borrow
                                    _n_sf += 1
                            except Exception:
                                continue
                        if _n_sf > 0:
                            _weights = dict(_weights)
                            _weights["squeeze_fuel"] = ROSS_SQUEEZE_FUEL_PILLAR_WEIGHT
                            # P4 DEEPENING: stamp the WITHIN-BATCH PERCENTILE of each name's OWN
                            # raw squeeze_fuel_pct so the downstream entry size-up + exit band-widen
                            # levers (squeeze_entry_size_multiplier / squeeze_exit_band_widen) have a
                            # no-magic, batch-adaptive axis (the live bar floats with the batch). This
                            # rank rides through ross_signals -> execution_readiness_json.extra exactly
                            # like the raw sub-score. Equity-only (only -USD-excluded names were fetched).
                            try:
                                from .ross_momentum import _percentile_rank as _sf_pctl
                                _sf_vals = sorted(
                                    float(_ross_signals[_s]["squeeze_fuel_pct"])
                                    for _s in _ross_signals
                                    if isinstance(_ross_signals.get(_s), dict)
                                    and _ross_signals[_s].get("squeeze_fuel_pct") is not None
                                )
                                for _s in _ross_signals:
                                    _ss = _ross_signals.get(_s)
                                    if isinstance(_ss, dict) and _ss.get("squeeze_fuel_pct") is not None:
                                        _ss["squeeze_fuel_rank_pct"] = round(
                                            _sf_pctl(float(_ss["squeeze_fuel_pct"]), _sf_vals), 4
                                        )
                            except Exception:
                                pass
                except Exception:
                    pass
            # NEWS-CATALYST TILT (default OFF => byte-identical): the 🔥 pillar Ross weights
            # heavily on his scanner — the 4th Ross pillar that was a STUB until now. Map each
            # symbol's REAL Polygon/Benzinga catalyst GRADE (the strong/weak/fake/all sets the
            # pipeline already computes from headlines — no new fetch) to a [0,1] news_catalyst_pct
            # sub-score and FOLD the news_catalyst pillar onto the ACTIVE weight-set (composable —
            # score_universe self-renormalises over present pillars). MEASURED: 0.10 weight keeps
            # news a minority RE-RANK; float/RVOL/change stay primary. GRACEFUL: a name in NO
            # catalyst set gets news_catalyst_pct=None ⇒ the pillar is omitted for it (NEUTRAL, no
            # penalty, never rejected). Works for equities AND crypto (the catalyst sets are
            # symbol-keyed). Flag DEFAULT-OFF (operator confirms feed amplitude first); OFF ⇒ the
            # sub-score is never stamped and the pillar weight is never folded ⇒ byte-identical.
            if bool(getattr(settings, "chili_momentum_news_catalyst_weight_enabled", False)):
                try:
                    from .ross_momentum import (
                        ROSS_NEWS_CATALYST_PILLAR_WEIGHT,
                        ROSS_NEWS_GRADE_STRONG,
                        news_catalyst_signal as _news_catalyst_signal,
                    )
                    from .catalyst import (
                        all_catalyst_symbols as _nc_all,
                        strong_catalyst_symbols as _nc_strong,
                        weak_catalyst_symbols as _nc_weak,
                        fake_catalyst_symbols as _nc_fake,
                        pr_cadence_active as _pr_cadence_active,
                    )

                    # NEWS-PR CADENCE (opt-in, default OFF ⇒ no cadence boost = byte-identical):
                    # Ross watches the top/bottom of the hour PREMARKET (7:00/7:30/8:00/8:30 ET)
                    # for PR drops. When ON AND ET-now is inside a premarket PR window, a catalyst
                    # name (already stamped with a news_catalyst_pct) gets a small additional
                    # LEAN-IN toward 1.0 — a time-of-day nudge ON TOP of the grade, never a new
                    # pillar and never a penalty (outside the window the name is unchanged).
                    _pr_cadence_on = bool(getattr(
                        settings, "chili_momentum_news_pr_cadence_enabled", False))
                    _pr_window_now = _pr_cadence_on and _pr_cadence_active()

                    # Cached, best-effort grade sets (each fails-open to empty).
                    def _safe_set(fn) -> set[str]:
                        try:
                            return {str(s).upper() for s in (fn() or set())}
                        except Exception:
                            return set()

                    _set_all = _safe_set(_nc_all)
                    _set_strong = _safe_set(_nc_strong)
                    _set_weak = _safe_set(_nc_weak)
                    _set_fake = _safe_set(_nc_fake)
                    # Only do per-symbol work if SOME news data exists this pass (else graceful no-op).
                    if _set_all or _set_strong or _set_weak or _set_fake:
                        _n_nc = 0
                        for _sym, _sig in _ross_signals.items():
                            if not isinstance(_sig, dict):
                                continue
                            try:
                                _nc = _news_catalyst_signal(
                                    _sym,
                                    strong_catalyst_symbols=_set_strong,
                                    weak_catalyst_symbols=_set_weak,
                                    fake_catalyst_symbols=_set_fake,
                                    all_catalyst_symbols=_set_all,
                                )
                                if _nc.news_pct is not None:
                                    _np = _nc.news_pct
                                    # PR-CADENCE lean-in (opt-in): during a premarket PR window a
                                    # FRESH-news catalyst name is treated as AT LEAST strong-grade
                                    # (Ross leans in when the PR drops on the half/whole hour). Reuses
                                    # the documented STRONG grade reference — no new magic number — and
                                    # only ever RAISES the sub-score, never penalises. Outside the
                                    # window (or flag OFF) ⇒ _np == _nc.news_pct ⇒ byte-identical.
                                    if _pr_window_now:
                                        _np = max(_np, float(ROSS_NEWS_GRADE_STRONG))
                                    _sig["news_catalyst_pct"] = _np
                                    _sig["news_catalyst_grade"] = _nc.grade
                                    if _pr_window_now and _np > _nc.news_pct:
                                        _sig["news_pr_cadence_boost"] = True
                                    _n_nc += 1
                            except Exception:
                                continue
                        if _n_nc > 0:
                            _weights = dict(_weights)
                            _weights["news_catalyst"] = ROSS_NEWS_CATALYST_PILLAR_WEIGHT
                except Exception:
                    pass
            # CROWDED-TAPE CATALYST-SUBSTITUTE (selection-rank hold; default ON, no-op until the
            # news pillar is also on ⇒ byte-identical). Ross trades SYMPATHY movers: a crowded-tape
            # / high-RVOL name riding a THEME is a real leadership signal even WITHOUT its own
            # primary (P1) catalyst. In the news pillar above, a name in NONE of the catalyst sets
            # reads news_catalyst_pct=None (pillar omitted) — NEUTRAL in isolation, but its
            # strong-catalyst peers carry a high news percentile and out-rank it on the same
            # RVOL+momentum core, so the catalyst-LESS sympathy name is demoted purely on P1-absence.
            # THE FIX: a genuine keyword-THEME member (a sympathy peer of a real leader) that is ALSO
            # a CROWDED high-RVOL name (within-batch RVOL percentile in the top tail) gets a partial
            # news-substitute sub-score FLOORED onto news_catalyst_pct — at MOST the present/ungraded
            # reference (so a real GRADED catalyst leader still out-ranks it, and a NON-mover crowded
            # tape with low RVOL earns nothing). Adaptive (own within-batch RVOL percentile), bounded
            # (capped below strong), a FLOOR applied ONLY to no-own-catalyst names (never lowers a
            # graded name), selection-only re-rank — never a veto. Equity-only (crypto has no equity
            # news theme). Gated on BOTH the news pillar AND this flag; either OFF ⇒ byte-identical.
            if (
                bool(getattr(settings, "chili_momentum_news_catalyst_weight_enabled", False))
                and bool(getattr(settings, "chili_momentum_theme_crowded_substitute_enabled", True))
            ):
                try:
                    from ...massive_client import get_recent_news_items as _ctx_news_items
                    from .ross_momentum import (
                        ROSS_NEWS_GRADE_PRESENT as _ctx_grade_present,
                        ROSS_NEWS_CATALYST_PILLAR_WEIGHT as _ctx_nc_weight,
                        _extract_pillars as _ctx_extract,
                        _percentile_rank as _ctx_pctl,
                    )
                    from .theme_detector import (
                        crowded_tape_news_substitute as _ctx_substitute,
                        theme_sympathy_symbols as _ctx_theme_peers,
                    )

                    # within-batch RVOL percentiles + the theme-sympathy movers, from the SAME
                    # equity signals (crypto -USD has no equity news theme — excluded).
                    _ctx_rvol: dict[str, float] = {}
                    _ctx_movers: dict[str, float] = {}
                    for _csym, _csig in _ross_signals.items():
                        _csu = str(_csym).upper()
                        if _csu.endswith("-USD") or not isinstance(_csig, dict):
                            continue
                        _crv, _cmom, _, _ = _ctx_extract(_csig)
                        if _crv is not None:
                            _ctx_rvol[_csu] = float(_crv)
                        if _cmom is not None:
                            _ctx_movers[_csu] = float(_cmom)
                    if _ctx_rvol and _ctx_movers:
                        _rvol_sorted = sorted(_ctx_rvol.values())
                        _peers = _ctx_theme_peers(_ctx_movers, _ctx_news_items())
                        _peers_u = {str(p).upper() for p in (_peers or set())}
                        if _peers_u:
                            _n_sub = 0
                            for _csym, _csig in _ross_signals.items():
                                if not isinstance(_csig, dict):
                                    continue
                                _csu = str(_csym).upper()
                                if _csu.endswith("-USD") or _csu not in _peers_u:
                                    continue
                                # only NO-own-catalyst names (the demoted ones); never lower a
                                # graded name. A name the news block stamped has grade != "none".
                                _grade = _csig.get("news_catalyst_grade")
                                if _grade not in (None, "none"):
                                    continue
                                _rv = _ctx_rvol.get(_csu)
                                if _rv is None:
                                    continue
                                _rp = _ctx_pctl(_rv, _rvol_sorted)
                                _sub = _ctx_substitute(
                                    True, _rp, grade_present=float(_ctx_grade_present))
                                if _sub is None:
                                    continue
                                _prev = _csig.get("news_catalyst_pct")
                                try:
                                    _prev_f = float(_prev) if _prev is not None else None
                                except (TypeError, ValueError):
                                    _prev_f = None
                                # FLOOR: only stamp when it RAISES (or first-sets) the sub-score.
                                if _prev_f is None or _sub > _prev_f:
                                    _csig["news_catalyst_pct"] = _sub
                                    _csig["news_catalyst_grade"] = "theme_crowded"
                                    _csig["news_crowded_substitute"] = True
                                    _n_sub += 1
                            if _n_sub > 0:
                                # ensure the pillar is folded even if NO graded name was stamped
                                # above (the credit alone makes the news axis meaningful).
                                _weights = dict(_weights)
                                _weights["news_catalyst"] = _ctx_nc_weight
                except Exception:
                    pass
            # PRICE SWEET-SPOT TILT (default OFF ⇒ byte-identical): Ross trades mostly $3-10.
            # The HARD $1-20 price-band gate (auto_arm) is UNTOUCHED — this is a SOFT PREFERENCE
            # pillar that BOOSTS names in the $3-10 sweet-spot and mildly de-rates names outside
            # it (but still within the broad band). Map each EQUITY signal's current price to a
            # [0.5,1.0] price_band_pct sub-score and FOLD a SMALL 0.05-weight pillar onto the
            # active weight-set (composable — score_universe self-renormalises). MEASURED: price
            # is the weakest pillar (half the others), so it only breaks near-ties; float/RVOL/
            # change stay primary. Equity-only (crypto price semantics differ; the band gate is
            # equity-only too). Flag DEFAULT-OFF; OFF ⇒ no sub-score stamped, no weight folded ⇒
            # byte-identical.
            if bool(getattr(settings, "chili_momentum_price_sweetspot_tilt_enabled", False)):
                try:
                    from .ross_momentum import (
                        ROSS_PRICE_SWEETSPOT_PILLAR_WEIGHT,
                        price_sweetspot_subscore as _price_sweetspot_subscore,
                    )

                    _ss_min = float(getattr(settings, "chili_momentum_price_sweetspot_min", 3.0))
                    _ss_max = float(getattr(settings, "chili_momentum_price_sweetspot_max", 10.0))
                    _n_pb = 0
                    for _sym, _sig in _ross_signals.items():
                        if not isinstance(_sig, dict) or str(_sym).upper().endswith("-USD"):
                            continue  # equities only
                        try:
                            _px = None
                            for _pk in ("price", "last", "close", "last_price"):
                                _pv = _sig.get(_pk)
                                if _pv is not None:
                                    try:
                                        _px = float(_pv)
                                        break
                                    except (TypeError, ValueError):
                                        continue
                            if _px is None:
                                continue  # fail-open: omit the pillar for this name
                            _pb = _price_sweetspot_subscore(
                                _px, sweet_min=_ss_min, sweet_max=_ss_max)
                            if _pb is not None:
                                _sig["price_band_pct"] = _pb
                                _n_pb += 1
                        except Exception:
                            continue
                    if _n_pb > 0:
                        _weights = dict(_weights)
                        _weights["price_band"] = ROSS_PRICE_SWEETSPOT_PILLAR_WEIGHT
                except Exception:
                    pass
            # OVERHEAD-SUPPLY CEILING SELECTION TILT (default OFF ⇒ byte-identical): de-weight a
            # name climbing toward a prior huge-VOLUME doji / round-trip overhead level from below
            # (trapped supply ahead — Ross "don't buy into resistance"). REUSES the SAME daily
            # context the daily-structure pillar already fetched: overhead_supply_atr(ctx, entry)
            # gives the daily-ATR room UP to the nearest ceiling, which the [0,1] sub-score maps
            # (clear sky ⇒ 1.0, pinned at the level ⇒ 0.0). A composable 0.10-weight pillar folded
            # onto the active weight-set (score_universe self-renormalises). DISTINCT from the
            # entry-side overhead VETO (a hard pre-fill gate) — this only RE-RANKS selection, it can
            # never block a fill or remove a name from the pool. Equity-only (crypto has no daily
            # overhead-supply structure). Flag DEFAULT-OFF; OFF ⇒ no sub-score stamped, no weight
            # folded ⇒ byte-identical ranking.
            if bool(getattr(settings, "chili_momentum_overhead_supply_tilt_enabled", False)):
                try:
                    from .daily_levels import (
                        compute_daily_context as _ohs_compute_daily_context,
                        overhead_supply_atr as _ohs_overhead_supply_atr,
                    )
                    from ..market_data import fetch_ohlcv_df as _ohs_fetch_daily
                    from .ross_momentum import (
                        ROSS_OVERHEAD_SUPPLY_CLEAR_ROOM_ATR,
                        ROSS_OVERHEAD_SUPPLY_PILLAR_WEIGHT,
                        overhead_supply_subscore as _overhead_supply_subscore,
                    )

                    _ohs_lb = int(getattr(settings, "chili_momentum_daily_lookback_days", 20) or 20)
                    _ohs_clear = float(getattr(
                        settings, "chili_momentum_overhead_supply_clear_room_atr",
                        ROSS_OVERHEAD_SUPPLY_CLEAR_ROOM_ATR))
                    _n_oh = 0
                    for _sym, _sig in _ross_signals.items():
                        if not isinstance(_sig, dict) or "-USD" in str(_sym):
                            continue  # equities only (crypto has no daily overhead structure)
                        try:
                            _px = None
                            for _pk in ("price", "last", "close", "last_price"):
                                _pv = _sig.get(_pk)
                                if _pv is not None:
                                    try:
                                        _px = float(_pv)
                                        break
                                    except (TypeError, ValueError):
                                        continue
                            if _px is None or _px <= 0:
                                continue  # fail-open: omit the pillar for this name
                            _ddf2 = _ohs_fetch_daily(_sym, interval="1d", period="1y")
                            _dctx2 = _ohs_compute_daily_context(_ddf2, lookback=_ohs_lb, price=_px)
                            _room = _ohs_overhead_supply_atr(_dctx2, entry=_px)
                            _oh = _overhead_supply_subscore(_room, clear_room_atr=_ohs_clear)
                            if _oh is not None:
                                _sig["overhead_supply_pct"] = _oh
                                _n_oh += 1
                        except Exception:
                            continue
                    if _n_oh > 0:
                        _weights = dict(_weights)
                        _weights["overhead_supply"] = ROSS_OVERHEAD_SUPPLY_PILLAR_WEIGHT
                except Exception:
                    pass
            meta["ross_scores"] = {
                s: rs.score
                for s, rs in _ross_score_universe(_ross_signals, weights=_weights).items()
            }
        except Exception:
            pass
        # Ross gap #3: absolute RVOL/change FLOOR on top of the within-batch percentile.
        # On a dull tape the best-of-a-dull-batch still percentile-ranks #1 and would arm
        # a non-explosive name; Ross's rule is that <~5x RVOL or <~10% up is simply not a
        # setup. Mark the EQUITY symbols (crypto 24h semantics differ) that fail the floor
        # so score_viability can drop them from LIVE eligibility (pool membership +
        # paper/scoring untouched). Compact symbol list — does not bloat the persisted
        # ctx_meta the way the full signal dict would.
        try:
            from .ross_momentum import below_explosive_floor as _below_floor

            meta["ross_below_floor"] = sorted(
                s
                for s, sig in _ross_signals.items()
                if isinstance(sig, dict)
                and not str(s).upper().endswith("-USD")
                and _below_floor(sig)
            )
        except Exception:
            pass

        # Ross gap #4: sympathy/theme cluster. The day's EQUITY movers cluster by SIC
        # sector; a sector whose LEADER is a big % gainer drags its peers (the "hot potato"
        # sympathy run = the STI/ASTC-class moves). Resolve sectors for the strongest
        # in-play equity movers (cached process-lifetime; bounded so a cold start can't
        # flood the rate limiter), cluster, and forward the peer set so viability tilts the
        # sympathy longs. Equity-only (crypto has no SIC sector) -> a no-op on the
        # crypto-only lane (no equity movers -> no fetches). Fail-open.
        try:
            from ...massive_client import get_ticker_sector
            from .catalyst import sympathy_peer_symbols
            from .ross_momentum import _extract_pillars

            _eq: list[tuple[str, float]] = []
            for s, sig in _ross_signals.items():
                su = str(s).upper()
                if su.endswith("-USD") or not isinstance(sig, dict):
                    continue
                _, _mom, _, _ = _extract_pillars(sig)
                if _mom is not None:
                    _eq.append((su, float(_mom)))
            _eq.sort(key=lambda x: x[1], reverse=True)
            # Ross gap #6: market-wide leading-gainer tilt — only the top few % gainers get
            # the broker hot-lists / eyes that make a pattern resolve; a perfect pattern on
            # a non-obvious name is a tree falling in an empty forest. The top-N equity
            # movers by change get a small additive viability boost (Ross's "top 3-5"). The
            # eligibility floor (#3) gates membership; this orders WITHIN it. Equity-only.
            if _eq:
                meta["top_market_gainers"] = sorted(su for su, _ in _eq[:5])
            _eq = _eq[:40]  # only the strongest movers cluster; bounds cold-start fetches
            _movers = {su: mom for su, mom in _eq}
            _peers = sympathy_peer_symbols(_movers, {su: get_ticker_sector(su) for su in _movers})
            if _peers:
                meta["sympathy_symbols"] = sorted(_peers)
        except Exception:
            pass

        # E7: THEME / SYMPATHY detector (the 1000%-mover lever). Complements the SIC-sector
        # sympathy above with a SHARED-CATALYST-KEYWORD axis: when a LEADER squeezes on a
        # catalyst, OTHER names whose fresh headlines share the same keyword run too
        # (STI -> ASTC; a "SpaceX synergies" headline lifting every space name). Cluster the
        # batch's equity movers by a salient keyword shared across their fresh headlines; if
        # the cluster has a genuine leader + >= min_cluster members, forward the non-leader
        # peers so viability applies a small additive boost. Flag OFF / no fresh news / no
        # cluster -> no key written -> byte-identical. Equity-only; fail-open. (theme_detector.py)
        try:
            if bool(getattr(settings, "chili_momentum_theme_sympathy_enabled", True)):
                from ...massive_client import get_recent_news_items
                from .ross_momentum import _extract_pillars as _ep_theme
                from .theme_detector import theme_sympathy_symbols

                _tmovers: dict[str, float] = {}
                for s, sig in _ross_signals.items():
                    su = str(s).upper()
                    if su.endswith("-USD") or not isinstance(sig, dict):
                        continue
                    _, _mom_t, _, _ = _ep_theme(sig)
                    if _mom_t is not None:
                        _tmovers[su] = float(_mom_t)
                if _tmovers:
                    _news = get_recent_news_items()
                    _tpeers = theme_sympathy_symbols(_tmovers, _news)
                    if _tpeers:
                        meta["theme_sympathy_symbols"] = sorted(_tpeers)
        except Exception:
            pass

        # Ross gap #16: dilution-risk penalty. A recent S-1/424B* (registration / offering)
        # filing means the low-float will ISSUE SHARES and fade despite good news (CTNT vs
        # SNTI). Flag the equity movers with a recent dilution filing (SEC EDGAR, free;
        # per-ticker cached, bounded per pass) so viability PENALIZES them. Equity-only;
        # a no-op on the crypto-only lane (no equity movers -> no lookups). Fail-open.
        try:
            from .edgar import dilution_risk_symbols

            _eq_syms = [
                str(s).upper()
                for s, sig in _ross_signals.items()
                if isinstance(sig, dict) and not str(s).upper().endswith("-USD")
            ]
            _dil = dilution_risk_symbols(_eq_syms)
            if _dil:
                meta["dilution_symbols"] = sorted(_dil)
        except Exception:
            pass

        # Re-analysis survivor S1: cross-day close-strength prior. A name that closed near
        # its HOD (and green) into the power hour gap-continues the next day — get warm on
        # it BEFORE the tape forms (the proven "right name early" bottleneck). Compute the
        # prior for the equity movers (bounded daily-bar reads, cached) -> viability tilt.
        # Equity-only -> a no-op on the crypto-only lane. Fail-open.
        try:
            from .catalyst import close_strength_priors

            _csp = close_strength_priors(_eq_syms)
            if _csp:
                meta["close_strength_priors"] = _csp
        except Exception:
            pass

    # E5: news-catalyst set (EARNINGS + fresh general NEWS headlines) for the catalyst
    # viability tilt. The fresh-news union is what catches Ross's explosive sympathy/
    # theme movers (a low-float small-cap that just printed a hot headline), not just
    # scheduled earnings. Best-effort + cached; empty -> no-op (degrades gracefully
    # without the news/Benzinga feed). (catalyst.py)
    # Hot-tape regime + HQ countries for the REGIME-AWARE catalyst tilt (Ross
    # 06-10: in a hot tape the no-news foreign small caps run; news names fade).
    # Both best-effort: regime from the bridge's own signals (no fetch); country
    # from the yfinance fundamentals CACHE only (a miss fires a background fill
    # so the next tick has it — the viability tick never blocks on network).
    try:
        from .catalyst import hot_tape_regime

        meta["hot_tape"] = bool(hot_tape_regime(meta.get("ross_signals")))
    except Exception:
        pass
    try:
        _countries = {}
        for _s in symbols:
            _c = _symbol_country(_s)
            if _c:
                _countries[_s] = _c
        if _countries:
            meta["symbol_countries"] = _countries
    except Exception:
        pass

    try:
        from .catalyst import theme_catalyst_symbols

        _theme = theme_catalyst_symbols()
        if _theme:
            meta["theme_symbols"] = sorted(_theme)  # JSON-safe (regression guard #528)
    except Exception:
        pass

    _cat: set[str] = set()
    try:
        from .catalyst import all_catalyst_symbols

        _cat = all_catalyst_symbols() or set()
        if _cat:
            # MUST be a list, not a set: meta flows into the brain_node_states
            # local_state JSONB and a set is not JSON-serializable ("Object of type
            # set is not JSON serializable"), which would fail the ENTIRE viability
            # write and leave every symbol stale. (regression guard for #528)
            meta["catalyst_symbols"] = sorted(_cat)
    except Exception:
        _cat = set()

    # E2: STRONG-catalyst boost set (FDA/trial/partnership/contract/M&A/beat headlines Ross
    # FAVORS). Same once-per-pass best-effort fetch as the weak set; JSON-safe sorted list.
    # Empty / absent feed -> no-op. docs/STRATEGY/CC_REPORTS/2026-06-24_ross-course-study.md
    # (Computed BEFORE the weak set: the reverse-split-squeeze refinement below needs the
    # strong set as the "fresh REAL news" confirmation.)
    _strong: set[str] = set()
    try:
        from .catalyst import strong_catalyst_symbols

        _strong = strong_catalyst_symbols()
    except Exception:
        _strong = set()

    # SIGN-REFINEMENT context for the two catalyst sign-corrections (Ross SS101 reverse-split
    # squeeze + private-placement-at/above-market). All equity-only, all best-effort + fail-open:
    #   * recent reverse splits (Polygon corp-action feed, via edgar)  -> SS101 recency gate
    #   * post-split float (Polygon share count, cached per process)    -> adaptive low-float cut
    #   * live last prices (snapshot batch)                             -> PP price-vs-market sign
    # When any feed is absent the refinement no-ops and the weak set stays the bare keyword
    # classification (byte-identical). Bounded to the equity movers so the crypto-only lane
    # does zero extra work. The whole block is best-effort; a miss never strips the weak set.
    _eq_movers = [
        str(s).upper()
        for s, sig in (meta.get("ross_signals") or {}).items()
        if isinstance(sig, dict) and not str(s).upper().endswith("-USD")
    ]
    _recent_splits: set[str] = set()
    _floats: dict[str, float] = {}
    _last_prices: dict[str, float] = {}
    _pp_bullish: set[str] = set()
    _rs_flag = bool(getattr(settings, "chili_momentum_reverse_split_recency_enabled", True))
    _pp_flag = bool(getattr(settings, "chili_momentum_private_placement_sign_enabled", True))
    if _eq_movers and _rs_flag:
        try:
            from .edgar import recent_reverse_split_symbols

            _rs_days = int(getattr(settings, "chili_momentum_reverse_split_recency_days", 30))
            _recent_splits = recent_reverse_split_symbols(_eq_movers, max_age_days=_rs_days)
        except Exception:
            _recent_splits = set()
        try:
            from ...massive_client import get_ticker_float

            for _s in _recent_splits:  # float only for the few recent-split names (cheap, cached)
                _fv = get_ticker_float(_s)
                if _fv:
                    _floats[_s] = float(_fv)
        except Exception:
            _floats = {}
    if _eq_movers and _pp_flag:
        try:
            from ...massive_client import get_quotes_batch
            from .catalyst import private_placement_at_or_above_market_symbols

            _q = get_quotes_batch(_eq_movers) or {}
            for _k, _v in _q.items():
                try:
                    _lp = (_v or {}).get("last_price")
                    if _lp:
                        _last_prices[str(_k).upper()] = float(_lp)
                except (TypeError, ValueError):
                    continue
            _pp_bullish = private_placement_at_or_above_market_symbols(last_prices=_last_prices)
        except Exception:
            _pp_bullish = set()

    # Gap #12: WEAK-catalyst de-boost set (dilution/compliance/legal headlines Ross distrusts),
    # now SIGN-REFINED: a recent reverse split with fresh REAL news + low post-split float (SS101
    # squeeze) and an at/above-market private placement are REMOVED from the de-boost. Any
    # context absent / flag OFF -> bare keyword classification (byte-identical). JSON-safe list.
    _weak: set[str] = set()
    try:
        from .catalyst import weak_catalyst_symbols

        _weak = weak_catalyst_symbols(
            recent_split_symbols=_recent_splits or None,
            floats=_floats or None,
            strong_news_symbols=_strong or None,
            private_placement_at_or_above_market=_pp_bullish or None,
        ) or set()
        if _weak:
            meta["weak_catalyst_symbols"] = sorted(_weak)
    except Exception:
        _weak = set()

    # A10 (Ross CLRO-lesson 2026-07-02): PERSIST today's dilution/weak-flagged symbols into
    # momentum_dilution_history (own-headline serial-diluter memory). One idempotent row per
    # (symbol, observed_day); a symbol flagged on >= adaptive-K distinct days earns a DECAYING
    # selection derate downstream (score_viability), never a hard ban. Fail-open (best-effort;
    # never breaks the tick). Flag OFF / empty -> no write.
    if _weak:
        try:
            from .dilution_history import persist_dilution_flags

            persist_dilution_flags(db, _weak, correlation_id=correlation_id)
        except Exception:
            _log.debug("[pipeline] dilution-history persist failed", exc_info=True)

    # SS101 low-float-squeeze BOOST: the recent-reverse-split names that EARNED the de-boost
    # exemption are folded into the STRONG set so the EXISTING catalyst grade delta carries the
    # boost (viability needs no change). Empty / flag OFF / context absent -> no addition
    # (byte-identical). Equity-only; fail-open.
    try:
        if _rs_flag and _recent_splits and _strong:
            from .catalyst import recent_reverse_split_squeeze_symbols

            _squeeze = recent_reverse_split_squeeze_symbols(
                recent_split_symbols=_recent_splits or None,
                floats=_floats or None,
                strong_news_symbols=_strong or None,
            )
            if _squeeze:
                _strong = set(_strong) | _squeeze
    except Exception:
        pass

    if _strong:
        meta["strong_catalyst_symbols"] = sorted(_strong)

    # FAKE-catalyst credibility set (Ross AS101/HVM101): UNVERIFIED / hacked-PR / unsolicited-
    # buyout / rumor / pump headlines Ross DISTRUSTS (they round-trip fully). Same once-per-pass
    # best-effort fetch; JSON-safe sorted list. Empty / absent feed / flag OFF -> no-op.
    _fake: set[str] = set()
    try:
        from .catalyst import fake_catalyst_symbols

        _fake = fake_catalyst_symbols() or set()
        if _fake:
            meta["fake_catalyst_symbols"] = sorted(_fake)
    except Exception:
        _fake = set()

    # ── FIX E: PER-TICKER catalyst-news repair for the IN-PLAY movers ───────────────────
    # The firehose sets above (all/strong/weak/fake) bury low-float micro-caps under large-cap
    # news (decisive w0av0u3qy probe: 0/11 Ross names tagged). Run a PER-TICKER fresh-news pass
    # over the names the lane is actually arming (the equity ross_signals keys) and UNION the
    # graded hits into every catalyst set, so a real catalyst on a low-float actually tags. The
    # per-ticker tags ADD to (never remove from) the firehose tags. Selection tilt only; freshness
    # still enforced inside the accessor (a stale headline never tags — the honest feed-lag
    # constraint). Flag OFF / no movers / absent feed -> no change (firehose-only, byte-identical).
    try:
        if bool(getattr(settings, "chili_momentum_catalyst_tagging_repair_enabled", True)) and _eq_movers:
            from .catalyst import per_ticker_catalyst_tags

            _pt_all, _pt_strong, _pt_weak, _pt_fake = per_ticker_catalyst_tags(_eq_movers)
            if _pt_all or _pt_strong or _pt_weak or _pt_fake:
                # union the per-ticker tags into the in-memory sets the downstream tilt reads
                # (_cat/_strong/_weak/_fake are all initialized to set() above -> safe).
                _cat = set(_cat) | _pt_all
                _strong = set(_strong) | _pt_strong
                _weak = set(_weak) | _pt_weak
                _fake = set(_fake) | _pt_fake
                # re-stamp the JSON-safe meta sets (sorted lists) so the persisted viability
                # explain/regime context carries the repaired tags for A/B + the auto-theme path
                if _cat:
                    meta["catalyst_symbols"] = sorted(_cat)
                if _strong:
                    meta["strong_catalyst_symbols"] = sorted(_strong)
                if _weak:
                    meta["weak_catalyst_symbols"] = sorted(_weak)
                if _fake:
                    meta["fake_catalyst_symbols"] = sorted(_fake)
                # COUNTERFACTUAL for live A/B: which names the per-ticker pass tagged that the
                # firehose did NOT (the operator reads tag-rate + downstream PnL delta, flips
                # chili_momentum_catalyst_tagging_repair_enabled off if net-negative).
                meta["catalyst_repair_fix_e"] = {
                    "per_ticker_tagged": sorted(_pt_all),
                    "strong": sorted(_pt_strong),
                    "weak": sorted(_pt_weak),
                    "fake": sorted(_pt_fake),
                    "n_movers_scanned": len(_eq_movers),
                }
    except Exception:
        pass

    # ── Ross-batch2: ACTION-COMPLETENESS + DOLLAR-AMOUNT catalyst grade (QUCY-vs-ILLR) ──────
    # Refine the STRONG-catalyst selection boost by HEADLINE VERB QUALITY + DOLLAR MAGNITUDE:
    # a completed-action / big-dollar headline (ILLR "$400M to acquire") gets an ADDED positive
    # delta; a tentative / pursuit headline (QUCY "approves pursuit") an ADDED negative delta.
    # ADAPTIVE dollar-vs-market-cap when the mover's market cap is on its signal (a $400M deal on
    # a $200M-cap micro-float is material regardless of the absolute tier). Same per-pass news
    # fetch the strong/weak graders use (no new feed). Flag OFF / no signal / absent feed -> empty
    # map -> the strong boost is byte-identical. JSON-safe {ticker: delta} stamped into meta so
    # viability's catalyst_grade_selection_delta can add it. docs/.../AUDIT_REPORT_BATCH2.md row #9
    try:
        if bool(getattr(settings, "chili_momentum_catalyst_action_grading_enabled", True)):
            from .catalyst import action_grade_deltas_by_symbol

            _mkt_caps: dict[str, float] = {}
            for _s, _sig in (meta.get("ross_signals") or {}).items():
                if not isinstance(_sig, dict):
                    continue
                _su = str(_s).upper()
                if _su.endswith("-USD"):
                    continue
                _mc = _pick_num(_sig, ("market_cap", "marketcap"))
                if _mc and _mc > 0:
                    _mkt_caps[_su] = float(_mc)
            _action_deltas = action_grade_deltas_by_symbol(market_caps=_mkt_caps or None)
            if _action_deltas:
                # JSON-safe (str keys, float values); only non-zero deltas are present.
                meta["catalyst_action_deltas"] = {
                    str(k): round(float(v), 6) for k, v in _action_deltas.items()
                }
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
            "ross_below_floor",
            "catalyst_symbols",
            "weak_catalyst_symbols",
            "strong_catalyst_symbols",
            "fake_catalyst_symbols",
            "catalyst_action_deltas",
            "sympathy_symbols",
            "theme_sympathy_symbols",
            "top_market_gainers",
            "dilution_symbols",
            "close_strength_priors",
            "hot_tape",
            "symbol_countries",
            "theme_symbols",
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
        # L2/order-flow utilization: viability's microstructure rules (book_imbalance
        # boost/penalty) were designed in Phase 4a but no producer ever filled the
        # field. Fill it per-symbol from the LIVE feeds now running in this process
        # (#596): Coinbase level2 ring buffer for crypto, Massive WS NBBO sizes for
        # equities. None (no feed for the symbol) -> unchanged behavior.
        _imb = _live_book_imbalance(sym, db=db)
        # Order-flow imbalance (Cont/Kukanov/Stoikov) + micro-price (Stoikov) edge
        # from the SAME live feeds, fed in as a small agreement-guarded viability
        # tilt (research: OFI is the strongest L2 short-horizon predictor).
        _ofi, _mpe = _live_ofi_microprice(sym, db=db)
        _overrides: dict[str, Any] = {}
        if _imb is not None:
            _overrides["book_imbalance"] = _imb
        if _ofi is not None:
            _overrides["ofi"] = _ofi
        if _mpe is not None:
            _overrides["micro_price_edge"] = _mpe
        # executed-tape aggressor imbalance (CONFIRMS OFI; scales the OFI tilt, never votes alone).
        # None when the trade-tape bridge / hours / watch are absent -> not in overrides -> tilt
        # mult 1.0 -> byte-identical to the bare OFI tilt (no regression).
        _tf = _live_trade_flow(sym, db=db)
        if _tf is not None:
            _overrides["trade_flow"] = _tf
        sym_feats = replace(feats, **_overrides) if _overrides else feats
        # 10s-candle breakout tilt (Ross's 10s chart, LIVE + ON): a fresh ABCD/flat-top
        # BREAKOUT on a crypto name nudges its viability UP (small, bounded, kill-switch
        # chili_tenbeat_entry_tilt_weight). A fired breakout is bullish ⇒ naturally
        # agreement-guarded for the long-only lane. The detections keep accruing forward-
        # returns so the live A/B (realized-with-tilt vs counterfactual) keeps measuring;
        # revert with the kill-switch if the A/B turns negative.
        _tb_w = float(getattr(settings, "chili_tenbeat_entry_tilt_weight", 0.0) or 0.0)
        _tbk = None
        if _tb_w > 0:
            try:
                from ..fast_path.tenbeat_candle_log import latest_tenbeat_breakout
                _tbk = latest_tenbeat_breakout(sym, db)
            except Exception:
                _tbk = None
        for family in iter_momentum_families():
            vr = score_viability(sym, family, ctx, sym_feats, db=db)
            d = vr.to_public_dict()
            if _tb_w > 0 and _tbk is not None:
                d["viability"] = min(1.0, float(d.get("viability") or 0.0) + _tb_w * _tbk)
                d["tenbeat_breakout_tilt"] = round(_tb_w * _tbk, 4)
            d["scope"] = scope
            d["label"] = family.label
            d["entry_style"] = family.entry_style
            d["default_stop_logic"] = family.default_stop_logic
            d["default_exit_logic"] = family.default_exit_logic
            rows.append(d)

    rows.sort(key=lambda r: r["viability"], reverse=True)
    top = rows[0] if rows else {}

    now = decision_at.isoformat()
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

    try:
        # Feature probes above are best-effort reads; clear any aborted probe transaction
        # before the durable BrainNodeState/evolution/viability writes.
        db.rollback()
    except Exception:
        _log.debug("[momentum_neural] pre-persistence rollback skipped", exc_info=True)

    hub = get_or_create_state(db, HUB_NODE_ID)
    hub.local_state = hub_payload
    hub.last_activated_at = decision_at
    hub.updated_at = decision_at

    pool = get_or_create_state(db, VIABILITY_NODE_ID)
    pool.local_state = viability_payload
    pool.last_activated_at = decision_at
    pool.updated_at = decision_at

    record_evolution_trace(
        db,
        snapshot={
            "top_family_id": top.get("family_id"),
            "top_viability": top.get("viability"),
            "session_label": ctx.session_label,
        },
        observed_at=decision_at,
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
            observed_at=decision_at,
        )
        if n:
            log_tick("persisted viability rows=%s", n)
    except Exception as e:
        _log.warning("[momentum_neural] viability persistence failed: %s", e)
        persistence_ok = False

    log_tick(
        "tick symbols=%s families=%s top=%s corr=%s",
        len(symbols),
        len(rows) // max(len(symbols), 1),
        top.get("family_id"),
        correlation_id,
    )
    return {"ok": True, "rows": len(rows), "top_family": top.get("family_id"), "persistence_ok": persistence_ok}
