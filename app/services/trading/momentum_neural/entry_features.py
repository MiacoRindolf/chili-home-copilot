"""Shared entry-moment FEATURE VECTOR capture for the momentum lane (2026-06-23).

ONE pure function used by BOTH the replay (replay_v2) and the live runner so the
winner/loser DISCRIMINATOR dataset is parity-identical across paths. Every field is
AS-OF the entry fill (lookahead-free): the function reads only its arguments, an
optional as-of L2 snapshot, and front_side_state on a completed-bars frame. Best-effort
— any field that errors is omitted; returns None on total failure. NO DB writes, no
wall-clock, no globals, so identical inputs yield byte-identical output (the dual-path
parity CLAUDE.md requires; see tests/test_entry_feature_parity.py).

The labeled (features, run_r/outcome) dataset trains the META-LABELING de-rate
(reference_meta_labeling_discriminator): a secondary model that SIZES the primary
momentum signal, never vetoes — so a below-VWAP explosive winner (CRVO/CLWT) is never
killed. NOTE: `minute_vol` is genuinely lookahead in replay (next-minute tape diff) and
None live — capture it but EXCLUDE it from any discriminator/meta-label fit.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
from typing import Any, Callable, Iterator

from .replay_errors import ReplayOhlcvInputUnavailableError

_CANON_COLS = ("High", "Low", "Close", "Volume")


def capture_entry_features(
    symbol: str,
    *,
    fill_px: float,
    stop: float,
    target: float,
    qty: float,
    want_qty: float,
    spread_bps: float,
    atr_pct: float,
    stop_atr_pct_eff: float,
    mid: float,
    dollar_vol: float | None,
    liq_mult: float,
    fire_ts: Any,
    entry_fidelity: str,
    trigger_debug: dict | None = None,
    session_df: Any = None,
    df_cols: tuple = _CANON_COLS,
    minute_vol: float | None = None,
    l2_db: Any = None,
    l2_as_of: Any = None,
    macro: dict | None = None,
) -> dict | None:
    f: dict[str, Any] = {}
    try:
        if isinstance(trigger_debug, dict):
            for k in ("vol_ratio", "sustained_rvol", "vwap", "pullback_ordinal"):
                v = trigger_debug.get(k)
                if v is not None:
                    try:
                        f[k] = float(v)
                    except Exception:
                        pass
            bs = trigger_debug.get("back_side")
            if bs is not None:
                f["back_side"] = 1.0 if bs else 0.0
            pv = trigger_debug.get("vwap")
            if pv is not None:
                try:
                    f["px_vs_rolling_vwap"] = float((fill_px - float(pv)) / float(pv))
                except Exception:
                    pass
        f["spread_bps"] = float(spread_bps)
        f["atr_pct"] = float(atr_pct)
        f["stop_pct_eff"] = float(stop_atr_pct_eff)
        if fill_px > stop:
            f["rr"] = float((target - fill_px) / (fill_px - stop))
        if dollar_vol is not None:
            f["dollar_vol"] = float(dollar_vol)
        if minute_vol is not None:
            f["minute_vol"] = float(minute_vol)
        f["liq_mult"] = float(liq_mult)
        f["partial"] = float(qty / want_qty) if want_qty and want_qty > 0 else 1.0
        f["price"] = float(fill_px)
        f["premarket"] = 1.0 if str(fire_ts)[11:16] < "13:30" else 0.0
        f["ws_tick"] = 1.0 if entry_fidelity == "ws_tick" else 0.0
        # order-flow as-of the fire instant (replay: historical table at l2_as_of;
        # live: in-process WS ring with l2_as_of=None). Fail-open to absent fields.
        try:
            from .pipeline import _live_book_imbalance, _live_ofi_microprice, _live_trade_flow

            ofi, micro = _live_ofi_microprice(symbol, db=l2_db, as_of=l2_as_of)
            if ofi is not None:
                f["ofi"] = float(ofi)
            if micro is not None:
                f["micro_edge_bps"] = float(micro)
            # DEPTH-normalized book imbalance (the research's NOBI / 5-level depth signal —
            # equity reads imbalance5 from iqfeed_depth_snapshots, crypto the WS book). Book
            # STATE, orthogonal to OFI FLOW. Live-fresh (15s window) so replay -> None/imputed.
            _bi = _live_book_imbalance(symbol, db=l2_db)
            if _bi is not None:
                f["book_imbalance"] = float(_bi)
            # TRADE-FLOW: signed-volume AGGRESSOR imbalance from the trade TAPE (equity: IQFeed L1
            # trade-tape iqfeed_trade_ticks; crypto: microstructure). Ross's "ask getting eaten" =
            # real thrust — distinct from OFI (book FLOW) + book_imbalance (book STATE). as_of-symmetric.
            _tf = _live_trade_flow(symbol, db=l2_db, as_of=l2_as_of)
            if _tf is not None:
                f["trade_flow"] = float(_tf)
        except Exception:
            pass
        # session structure on completed bars (front_side_state — premarket-inclusive,
        # fail-open on thin data). NOTE: session_vwap/extension do NOT separate winners
        # from losers (proven 2026-06-23, AUC~0.51) — captured for the model to weigh,
        # NEVER as a veto.
        try:
            from .ross_momentum import front_side_state

            if session_df is not None:
                cols = tuple(df_cols) if df_cols else _CANON_COLS
                _df = session_df
                if cols != _CANON_COLS:
                    _df = session_df.rename(
                        columns={cols[0]: "High", cols[1]: "Low", cols[2]: "Close", cols[3]: "Volume"}
                    )
                fs = front_side_state(_df)
                for k in ("front_side_score", "vwap_dist_sigma", "retrace_from_hod", "day_range_pos"):
                    v = getattr(fs, k, None)
                    if v is not None:
                        try:
                            f[k] = float(v)
                        except Exception:
                            pass
                f["is_backside"] = 1.0 if getattr(fs, "is_backside", False) else 0.0
                f["above_vwap"] = 1.0 if getattr(fs, "above_vwap", True) else 0.0
                sv = getattr(fs, "session_vwap", None)
                if sv:
                    f["px_vs_session_vwap"] = float((fill_px - float(sv)) / float(sv))
                # MESO: volatility-CONTRACTION tightness (Crabel C-E / VCP — coiling precedes
                # expansion). recent-quarter avg bar-range vs full-session avg: <1 = contraction
                # (the statistically-supported breakout precursor). Session-relative FRACTION (the
                # quarter scales with session length — no fixed/magic window). The meta-label learns
                # the weight; data-snooping-guarded by the perm-null + confidence-shrinkage.
                try:
                    _hi = _df["High"].astype(float).values
                    _lo = _df["Low"].astype(float).values
                    _nb = len(_hi)
                    if _nb >= 8:
                        _rng = _hi - _lo
                        _full_rng = float(_rng.mean())
                        _q = max(2, _nb // 4)
                        if _full_rng > 0:
                            f["range_contraction"] = float(_rng[-_q:].mean()) / _full_rng
                except Exception:
                    pass
        except Exception:
            pass
        # MACRO-REGIME features (computed by the caller via macro_regime_features, passed in to
        # keep this fn pure/parity-testable). Daniel-Moskowitz: momentum follow-through crashes
        # in high-vol bear regimes -> the model weighs the bear x vol interaction.
        if isinstance(macro, dict):
            for k, v in macro.items():
                if v is not None:
                    try:
                        f[k] = float(v)
                    except Exception:
                        pass
        return f or None
    except Exception:
        return None


# Tiny production TTL cache so SPY/IWM are not refetched per candidate. Captured paper and
# ReplayV3 bind a fresh run-local cache; they never inherit this process-global state.
_MACRO_CACHE: dict = {}
_BOUND_MACRO_CACHE: contextvars.ContextVar[dict | None] = (
    contextvars.ContextVar("_chili_bound_macro_feature_cache", default=None)
)


@contextmanager
def macro_feature_cache(cache: dict) -> Iterator[None]:
    """Bind capture/replay-local macro state; never inherit process-global rows."""

    if not isinstance(cache, dict):
        raise TypeError("macro feature cache must be a dict")
    token = _BOUND_MACRO_CACHE.set(cache)
    try:
        yield
    finally:
        _BOUND_MACRO_CACHE.reset(token)


def macro_regime_features(
    now_ts: float | None = None,
    *,
    fetcher: Callable[..., Any] | None = None,
) -> dict:
    """Lookahead-free MACRO-REGIME features (Daniel-Moskowitz panic-regime encoding):
    BEAR indicator (market below its 20d trend) x trailing realized VOLATILITY interaction —
    momentum follow-through historically CRASHES in high-vol bear regimes (Sharpe flips
    +0.016 -> -0.042). Best-effort IO (SPY = market, IWM = small-cap proxy); absent fields are
    simply omitted (the model median-imputes). Cached ~300s to avoid per-candidate refetch."""
    import time as _t

    out: dict = {}
    try:
        import numpy as np

        if fetcher is None:
            from ..market_data import fetch_ohlcv_df as _fetcher
        else:
            _fetcher = fetcher

        ts = now_ts if now_ts is not None else _t.time()
        cache = _BOUND_MACRO_CACHE.get()
        if cache is None:
            cache = _MACRO_CACHE
        for sym, key, is_smallcap in (("SPY", "spy", False), ("IWM", "iwm", True)):
            try:
                cached = cache.get(sym)
                if cached and (ts - cached[0]) < 300.0:
                    c = cached[1]
                else:
                    df = _fetcher(sym, interval="1d", period="3mo")
                    if df is None or len(df) < 21:
                        continue
                    c = df["Close"].astype(float).values[-21:]
                    cache[sym] = (ts, c)
                out[f"{key}_trend"] = 1.0 if float(c[-1]) >= float(c[-20:].mean()) else 0.0
                if is_smallcap:
                    rets = np.diff(np.log(c))
                    out["mkt_vol"] = float(np.std(rets) * (252.0 ** 0.5))
            except ReplayOhlcvInputUnavailableError:
                raise
            except Exception:
                continue
        bear = 1.0 - out.get("iwm_trend", out.get("spy_trend", 1.0))
        if "mkt_vol" in out:
            out["bear_x_vol"] = bear * out["mkt_vol"]   # the panic-regime interaction

        # VIX TERM-STRUCTURE SLOPE (Johnson 2017, JFQA): VIX3M/VIX — >1 contango (risk-on),
        # <1 backwardation (panic onset). Carries the PRICE of variance risk, ORTHOGONAL to
        # VIX level + bear×vol. The model should de-rate longs as it inverts toward backwardation.
        try:
            cached = cache.get("VIXSLOPE")
            if cached and (ts - cached[0]) < 300.0:
                out["vix_slope"] = cached[1]
            else:
                _v = _fetcher("^VIX", interval="1d", period="5d")
                _v3 = _fetcher("^VIX3M", interval="1d", period="5d")
                if _v is not None and _v3 is not None and len(_v) and len(_v3):
                    _vix = float(_v["Close"].astype(float).values[-1])
                    _vix3 = float(_v3["Close"].astype(float).values[-1])
                    if _vix > 0:
                        out["vix_slope"] = _vix3 / _vix
                        cache["VIXSLOPE"] = (ts, out["vix_slope"])
        except ReplayOhlcvInputUnavailableError:
            raise
        except Exception:
            pass

        # FOMC-CYCLE PHASE (Cieslak-Morse-Vissing-Jorgensen): even weeks (0/2/4/6) since the last
        # FOMC meeting carry the equity risk-on / high-beta premium; small-cap momentum longs ARE
        # high-beta risk-on -> the model can up-weight in even weeks. Deterministic, lookahead-free.
        # NOTE: scheduled 2026 FOMC announcement dates — verify/extend annually; if stale, the
        # meta-label simply down-weights the feature (safe).
        try:
            from datetime import datetime, timezone

            _fomc = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
                     "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
            _today = (datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)).date()
            _past = [d for d in (datetime.strptime(x, "%Y-%m-%d").date() for x in _fomc) if d <= _today]
            if _past:
                _days = (_today - max(_past)).days
                out["fomc_even_week"] = 1.0 if ((_days // 7) % 2 == 0) else 0.0
        except Exception:
            pass
    except ReplayOhlcvInputUnavailableError:
        raise
    except Exception:
        pass
    return out
