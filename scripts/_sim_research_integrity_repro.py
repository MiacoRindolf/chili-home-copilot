"""Reproduce the research_integrity strict-mode lookahead/causality failure.

Scheduler logs (2026-06-08):
  WARNING [research_integrity] Causality check failed: 1 mismatch(es) (bars=[4903])
  ERROR   [research_integrity] Strict mode: lookahead/causality failed for LINK-USD c33ccc77d28bf0db
  WARNING [research_integrity] Causality check failed: 1 mismatch(es) (bars=[8437])
  ERROR   [research_integrity] Strict mode: lookahead/causality failed for ETH-USD c33ccc77d28bf0db

This script:
  1. Finds the ScanPattern whose conditions fingerprint == the logged hash.
  2. Fetches the SAME crypto OHLCV the scheduler backtests.
  3. Runs the integrity check exactly as enrich_pattern_backtest_result does
     (reference arrays computed WITH interval; truncation recompute WITHOUT) and
     prints the full mismatch detail: which indicator key, precomputed vs
     truncated_recompute value, at which bar.
  4. Independently probes that key for causality (does the truncated slice value
     equal the full-series value bar-by-bar?) to localise the non-causal step.
"""
from __future__ import annotations

import json
import sys

import pandas as pd

from app.db import SessionLocal
from app.models.trading import ScanPattern
from app.services.backtest_service import _compute_series_for_conditions
from app.services.trading.market_data import fetch_ohlcv_df as _fetch_ohlcv_df
from app.services.trading.research_integrity import (
    build_research_integrity_report,
    check_signal_bar_alignment,
    rules_json_fingerprint,
)

TARGET_FP = "c33ccc77d28bf0db"
SYMBOLS = ["LINK-USD", "ETH-USD", "XRP-USD"]


def _conditions_for_fp(fp: str) -> tuple[ScanPattern | None, list[dict]]:
    db = SessionLocal()
    try:
        rows = db.query(ScanPattern).all()
        for p in rows:
            rules = p.rules_json
            if isinstance(rules, str):
                try:
                    rules = json.loads(rules)
                except Exception:
                    continue
            conds = (rules or {}).get("conditions", [])
            if not conds:
                continue
            if rules_json_fingerprint(conds) == fp:
                return p, conds
        return None, []
    finally:
        db.close()


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work.index = pd.to_datetime(work.index)
    if work.index.tz is not None:
        work.index = work.index.tz_localize(None)
    return work


def main() -> int:
    pat, conditions = _conditions_for_fp(TARGET_FP)
    if not conditions:
        print(f"!! No active ScanPattern matches fingerprint {TARGET_FP}")
        print("   (pattern may be inactive/deleted; falling back to manual probe is needed)")
        return 2

    interval = (pat.timeframe or "1d") if pat else "1d"
    print(f"== Pattern #{pat.id} '{pat.name}' fp={TARGET_FP} timeframe={interval}")
    print(f"   asset_class={pat.asset_class} active={pat.active}")
    print("   conditions:")
    for c in conditions:
        print(f"     - {json.dumps(c, default=str)}")
    print()

    # The scheduler fetches with a long window; mirror generously so we reach
    # the deep bars (4903 / 8437) where the mismatch fired.
    period_map = {"1m": "7d", "5m": "60d", "15m": "60d", "1h": "2y", "4h": "2y", "1d": "max"}
    period = period_map.get(interval, "max")

    for sym in SYMBOLS:
        print(f"-- {sym} interval={interval} period={period}")
        try:
            df = _fetch_ohlcv_df(sym, period=period, interval=interval)
        except Exception as ex:
            print(f"   fetch failed: {ex}")
            continue
        if df is None or df.empty or len(df) < 30:
            print(f"   insufficient data ({0 if df is None else len(df)} bars)")
            continue
        work = _norm(df)
        n = len(work)
        print(f"   fetched {n} bars  {work.index[0]} -> {work.index[-1]}")

        # Reference arrays: computed WITH interval (matches backtest_service:2038).
        ref = _compute_series_for_conditions(work, conditions, interval=interval)

        # Run the real guard (recompute path drops interval, as in research_integrity).
        report = build_research_integrity_report(work, conditions, ref, max_check_bars=48)
        ok = report.get("lookahead_ok", True)
        print(f"   lookahead_ok={ok} checked_bars={report.get('causality_checked_bars')}")
        for m in report.get("mismatches", []):
            print(f"   MISMATCH bar={m.get('bar')} key={m.get('key')!r} "
                  f"precomputed={m.get('precomputed')!r} truncated={m.get('truncated_recompute')!r}")
            if "error" in m:
                print(f"            error={m['error']}")

        # Localise: for each mismatching key, probe whether the divergence is
        # (a) interval-drop (reference WITH interval vs recompute WITHOUT) or
        # (b) a genuinely non-causal computation (recompute WITH interval still
        #     differs from the full-series value at that bar).
        bad_keys = {m.get("key") for m in report.get("mismatches", []) if m.get("key") != "__compute__"}
        for key in sorted(k for k in bad_keys if k):
            print(f"   >> probing key={key!r}")
            for m in report.get("mismatches", []):
                if m.get("key") != key:
                    continue
                i = m.get("bar")
                sub = work.iloc[: i + 1].copy()
                fresh_no_iv = _compute_series_for_conditions(sub, conditions)
                fresh_iv = _compute_series_for_conditions(sub, conditions, interval=interval)
                v_full = ref[key][i] if i < len(ref.get(key, [])) else None
                v_trunc_no_iv = fresh_no_iv[key][-1] if key in fresh_no_iv else None
                v_trunc_iv = fresh_iv[key][-1] if key in fresh_iv else None
                print(f"      bar={i}: full={v_full!r}  trunc(noiv)={v_trunc_no_iv!r}  trunc(iv)={v_trunc_iv!r}")
                if v_trunc_iv == v_full and v_trunc_no_iv != v_full:
                    print("      -> INTERVAL-DROP artifact (recompute w/ interval matches; w/o doesn't)")
                elif v_trunc_iv != v_full:
                    print("      -> GENUINELY NON-CAUSAL (recompute w/ interval still differs from full)")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
