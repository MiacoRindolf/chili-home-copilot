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

from typing import Any

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
            from .pipeline import _live_ofi_microprice

            ofi, micro = _live_ofi_microprice(symbol, db=l2_db, as_of=l2_as_of)
            if ofi is not None:
                f["ofi"] = float(ofi)
            if micro is not None:
                f["micro_edge_bps"] = float(micro)
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
        except Exception:
            pass
        return f or None
    except Exception:
        return None
