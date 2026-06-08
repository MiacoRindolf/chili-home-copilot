"""READ-ONLY dry-run: prove the selection->entry alignment before changing live selection.

The momentum lane SELECTS 24h-cumulative movers (ross_momentum.score_universe ranks
RVOL/gap/daily-change over the whole day), but by the time the pullback-break entry gate
evaluates them on intraday bars, many have FADED into a deep retracement -> the gate reads
`pullback_too_deep` and never fires (live diagnostic 2026-06-07: 10/10 live-eligible
candidates `pullback_too_deep`; lane = 0 entries).

This harness sweeps `entry_gates.pullback_break_confirmation` over recent bars for the
CURRENT live candidates (exactly the names auto_arm feeds to the gate) and splits the
result by `ross_momentum.intraday_impulse_freshness` (is the name still near its recent
high, so a SHALLOW pullback is available?). It reports, at BOTH 5m and 1m:

  * BASELINE fire-rate over all candidate-bars (reproduces the audit's ~0.56% / today's 0).
  * FRESH-only fire-rate  -> the metric the new selection must beat (materially > 0).
  * FADED-only fire-rate  -> expected ~0, `pullback_too_deep`-dominated.
  * a genuine-shallow check on the firing bars (retrace <= threshold, NOT loosened).

It NEVER writes the DB, places an order, or arms a session. docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md (ME-3).

Usage:
  conda run -n chili-env python scripts/d-momentum-selection-entry-alignment-dryrun.py \
      --intervals 5m,1m --sweep-bars 60 --max-symbols 25
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.trading import MomentumSymbolViability  # noqa: E402
from app.services.trading.market_data import fetch_ohlcv_df  # noqa: E402
from app.services.trading.momentum_neural.entry_gates import (  # noqa: E402
    pullback_break_confirmation,
)
from app.services.trading.momentum_neural.ross_momentum import (  # noqa: E402
    intraday_impulse_freshness,
)

# Indicator memory is short (EMA-9, volume_ratio over 20) so a trailing window of this
# many bars reproduces the full-history last-bar gate decision while keeping the sweep
# fast. Not a trading parameter — purely a compute window for the dry-run.
_TRAILING_WINDOW = 140
_MIN_BARS = 25  # gate needs >=10 + a full 20-bar look window; freshness needs >=5


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):5.2f}%" if d else "  n/a"


def _live_candidates(limit: int) -> tuple[list[tuple[str, float, bool]], str]:
    """(symbol, viability, live_eligible) for the crypto names auto_arm would scan.

    Mirrors auto_arm._fresh_live_eligible_candidates: scope='symbol', -USD, freshest
    per symbol. Tries the strict live-eligible+fresh set first (what arming actually
    sees); broadens to any recent -USD viability row when that is empty so the gate
    behaviour can still be characterised.
    """
    from datetime import datetime, timedelta

    max_age = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
    cutoff = datetime.utcnow() - timedelta(seconds=max_age)
    with SessionLocal() as db:
        def _collect(q, n):
            best: dict[str, tuple[str, float, bool]] = {}
            for row in q.limit(n * 30).all():
                sym = str(row.symbol).upper()
                v = float(row.viability_score or 0.0)
                if sym not in best or v > best[sym][1]:
                    best[sym] = (sym, v, bool(row.live_eligible))
            return sorted(best.values(), key=lambda t: t[1], reverse=True)[:n]

        base = db.query(MomentumSymbolViability).filter(
            MomentumSymbolViability.scope == "symbol",
            MomentumSymbolViability.symbol.like("%-USD%"),
        )
        strict = _collect(
            base.filter(
                MomentumSymbolViability.live_eligible.is_(True),
                MomentumSymbolViability.freshness_ts >= cutoff,
            ).order_by(MomentumSymbolViability.viability_score.desc()),
            limit,
        )
        if strict:
            return strict, "db_live_eligible_fresh"
        recent = _collect(
            base.order_by(MomentumSymbolViability.freshness_ts.desc()),
            limit,
        )
        if recent:
            return recent, "db_recent_any_-USD"
    return [], "empty"


def _venue_rebuild(limit: int) -> tuple[list[tuple[str, float, bool]], str]:
    """Fallback: rebuild the crypto universe straight from the venue (Coinbase 24h
    stats) and Ross-rank it — the same coarse selection the live bridge performs."""
    try:
        from app.services.trading_scheduler import _build_crypto_momentum_universe
        from app.services.trading.momentum_neural.ross_momentum import score_universe

        uni = _build_crypto_momentum_universe()
        if not uni:
            return [], "venue_empty"
        ranked = score_universe({str(r.get("symbol")).upper(): r for r in uni if r.get("symbol")})
        ordered = sorted(ranked.values(), key=lambda s: s.rank)
        return [(s.symbol, float(s.score), True) for s in ordered[:limit]], "venue_rebuild_ross_ranked"
    except Exception as ex:  # pragma: no cover - depends on live venue creds
        print(f"  [venue rebuild failed: {ex}]")
        return [], "venue_failed"


def _sweep_symbol(df, *, interval: str, sweep_bars: int, threshold: float):
    """Yield (reason, ok, is_fresh, retrace) for each of the last `sweep_bars` bars,
    evaluating the gate + freshness AS OF that bar (trailing-window slice)."""
    n = len(df)
    if n < _MIN_BARS:
        return
    start = max(_MIN_BARS - 1, n - int(sweep_bars))
    for c in range(start, n):
        lo = max(0, c - _TRAILING_WINDOW + 1)
        sl = df.iloc[lo:c + 1]
        try:
            ok, reason, dbg = pullback_break_confirmation(sl, entry_interval=interval)
        except Exception:
            ok, reason, dbg = False, "gate_exception", {}
        fr = intraday_impulse_freshness(sl, retracement_threshold=threshold)
        retr = dbg.get("retrace")
        yield reason, bool(ok), bool(fr.is_fresh), (float(retr) if retr is not None else None)


def _run_interval(candidates, *, interval: str, sweep_bars: int, threshold: float):
    all_reasons: Counter = Counter()
    fresh_reasons: Counter = Counter()
    faded_reasons: Counter = Counter()
    evals = fresh_evals = faded_evals = 0
    fire = fresh_fire = faded_fire = 0
    fire_retraces: list[float] = []
    snapshot: list[tuple] = []  # (symbol, reason@last, fresh@last, score@last, retrace@last)

    for sym, viab, _le in candidates:
        try:
            df = fetch_ohlcv_df(sym, interval=interval, period="5d")
        except Exception as ex:
            snapshot.append((sym, f"fetch_err:{ex}", None, None, None))
            continue
        if df is None or getattr(df, "empty", True) or len(df) < _MIN_BARS:
            snapshot.append((sym, "insufficient_bars", None, None, None))
            continue
        last_reason = last_fresh = last_score = last_retr = None
        for reason, ok, is_fresh, retr in _sweep_symbol(
            df, interval=interval, sweep_bars=sweep_bars, threshold=threshold
        ):
            evals += 1
            all_reasons[reason] += 1
            if ok:
                fire += 1
                if retr is not None:
                    fire_retraces.append(retr)
            if is_fresh:
                fresh_evals += 1
                fresh_reasons[reason] += 1
                if ok:
                    fresh_fire += 1
            else:
                faded_evals += 1
                faded_reasons[reason] += 1
                if ok:
                    faded_fire += 1
            last_reason, last_fresh, last_retr = reason, is_fresh, retr
        # snapshot = the most recent bar (what auto_arm/live_runner sees right now)
        fr_last = intraday_impulse_freshness(df.iloc[-_TRAILING_WINDOW:], retracement_threshold=threshold)
        snapshot.append((sym, last_reason, fr_last.is_fresh, fr_last.score, last_retr))

    print(f"\n[interval={interval}]  symbols={len(candidates)}  bar-evals={evals}")
    if not evals:
        print("  (no evaluable bars)")
        return None
    print(f"  BASELINE fire-rate (pullback_break_ok, ALL bars): {_pct(fire, evals)}   "
          f"[audit baseline ~0.56%]")
    print("  reasons(all):   " + "  ".join(f"{k}={v}" for k, v in all_reasons.most_common()))
    print(f"  is_fresh bars:  {fresh_evals}/{evals} ({_pct(fresh_evals, evals).strip()})")
    print(f"  >> FRESH-only fire-rate: {_pct(fresh_fire, fresh_evals)}   (metric to beat baseline)")
    print("     reasons(fresh): " + "  ".join(f"{k}={v}" for k, v in fresh_reasons.most_common()))
    print(f"  >> FADED-only fire-rate: {_pct(faded_fire, faded_evals)}   (expect ~0)")
    print("     reasons(faded): " + "  ".join(f"{k}={v}" for k, v in faded_reasons.most_common()))
    if fire_retraces:
        print(f"  genuine-shallow check (firing bars): n={len(fire_retraces)} "
              f"retrace max={max(fire_retraces):.3f} mean={sum(fire_retraces)/len(fire_retraces):.3f} "
              f"(must be <= {threshold})")
    print("  per-symbol snapshot (most-recent bar):")
    print(f"    {'symbol':<14}{'gate_reason':<22}{'fresh?':<8}{'score':<8}{'retrace'}")
    for sym, reason, fresh, score, retr in snapshot:
        s_score = f"{score:.3f}" if isinstance(score, float) else "-"
        s_retr = f"{retr:.3f}" if isinstance(retr, float) else "-"
        print(f"    {sym:<14}{str(reason):<22}{str(fresh):<8}{s_score:<8}{s_retr}")
    return {
        "interval": interval, "evals": evals, "fire": fire,
        "fresh_evals": fresh_evals, "fresh_fire": fresh_fire,
        "faded_evals": faded_evals, "faded_fire": faded_fire,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--intervals", default="5m,1m")
    ap.add_argument("--sweep-bars", type=int, default=60)
    ap.add_argument("--max-symbols", type=int, default=25)
    ap.add_argument("--threshold", type=float,
                    help="freshness retracement_threshold (defaults to the gate's 0.50)")
    ap.add_argument("--venue", action="store_true", help="force venue rebuild instead of DB")
    args = ap.parse_args()

    threshold = args.threshold if args.threshold is not None else 0.50
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]

    candidates, source = ([], "")
    if args.venue:
        candidates, source = _venue_rebuild(args.max_symbols)
    if not candidates:
        candidates, source = _live_candidates(args.max_symbols)
    if not candidates:
        candidates, source = _venue_rebuild(args.max_symbols)

    print("=== Momentum selection->entry alignment dry-run (READ-ONLY) ===")
    print(f"candidate source: {source}   symbols: {len(candidates)}   "
          f"freshness threshold: {threshold}")
    print(f"entry_trigger_mode={getattr(settings, 'chili_momentum_entry_trigger_mode', '?')}  "
          f"live_pullback_interval={getattr(settings, 'chili_momentum_pullback_entry_interval', '?')}")
    if not candidates:
        print("\nNO CANDIDATES from DB or venue — cannot run. Is the viability refresh job live / Coinbase connected?")
        return 2
    print("candidates: " + ", ".join(f"{s}({v:.3f})" for s, v, _ in candidates))

    summaries = []
    for itv in intervals:
        s = _run_interval(candidates, interval=itv, sweep_bars=args.sweep_bars, threshold=threshold)
        if s:
            summaries.append(s)

    print("\n=== VERDICT ===")
    for s in summaries:
        base = _pct(s["fire"], s["evals"]).strip()
        fr = _pct(s["fresh_fire"], s["fresh_evals"]).strip()
        fa = _pct(s["faded_fire"], s["faded_evals"]).strip()
        print(f"  {s['interval']:<4} baseline={base}  FRESH-only={fr}  FADED-only={fa}  "
              f"(fresh bars {s['fresh_evals']}/{s['evals']})")
    print("\nInterpretation: FRESH-only fire-rate >> baseline (and FADED ~0) means the freshness")
    print("filter feeds the gate names that actually fire shallow pullbacks. Pick the interval")
    print("with the higher FRESH-only fire-rate at an acceptable shallow-retrace profile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
