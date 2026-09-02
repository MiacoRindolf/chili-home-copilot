"""Stop-out forensics dataset + counterfactual stop floors (2026-09-02).

Pure, DB-free helpers (``spread_units``, ``one_minute_true_ranges``,
``floor_stop``, ``simulate_with_stop``, ``latency_fill``) plus a CLI that
reads the event / tape dumps produced by bounded read-only psql pulls (the
exact queries are in docs/STRATEGY/CC_REPORTS/2026-09-02_stop-noise-floor-
forensics.md) and writes the CSV + summary.

The helpers are what the tests cover.  The CLI is glue over local text files —
it never touches the database.

Usage::

    python scripts/research/stop_noise_forensics_0902.py <scratch_dir> <out_csv> <out_md>

``scratch_dir`` must contain ``events_raw.txt``, ``sessions.tsv`` and a
``tape/`` folder with ``ticks2_<sid>_<sym>.txt`` / ``nbbo_<sid>_<sym>.txt``.
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ── pure helpers ──────────────────────────────────────────────────────────────

# per-second bucket: (epoch, low, high, bid_low, bid_high)
Sec = tuple[int, float, float, float | None, float | None]


def spread_units(stop_distance: float, bid: float | None, ask: float | None) -> float | None:
    """Stop distance expressed in units of the live quoted spread (ask-bid).

    ``None`` when the quote is unusable (missing, crossed, or zero-width).
    """
    try:
        d = float(stop_distance)
        b = float(bid)  # type: ignore[arg-type]
        a = float(ask)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    w = a - b
    if not (math.isfinite(d) and math.isfinite(w)) or w <= 0.0:
        return None
    return d / w


def one_minute_true_ranges(
    seconds: list[tuple[int, float, float]], *, end_epoch: float, minutes: int = 5
) -> list[float]:
    """1-minute true ranges for the ``minutes`` bars ending at ``end_epoch``.

    ``seconds`` = per-second ``(epoch, low, high)`` buckets (any order).
    A bar with no prints is skipped (a halt gap does not become a fake
    zero-range bar).  TR = max(h-l, |h-prev_close|, |l-prev_close|) with
    prev_close approximated by the mid of the previous bar's last populated
    second.
    """
    try:
        end = int(math.floor(float(end_epoch)))
    except (TypeError, ValueError):
        return []
    start = end - 60 * int(minutes)
    bars: dict[int, list[tuple[int, float, float]]] = {}
    for s, lo, hi in seconds:
        try:
            si = int(s)
            l = float(lo)
            h = float(hi)
        except (TypeError, ValueError):
            continue
        if si < start or si >= end or not (math.isfinite(l) and math.isfinite(h)):
            continue
        bars.setdefault((si - start) // 60, []).append((si, l, h))
    out: list[float] = []
    prev_close: float | None = None
    for k in sorted(bars):
        rows = sorted(bars[k])
        lo = min(r[1] for r in rows)
        hi = max(r[2] for r in rows)
        tr = hi - lo
        if prev_close is not None:
            tr = max(tr, abs(hi - prev_close), abs(lo - prev_close))
        out.append(tr)
        prev_close = (rows[-1][1] + rows[-1][2]) / 2.0
    return out


def floor_stop(
    *,
    entry: float,
    stop: float,
    min_distance: float,
    bound: float | None = None,
) -> float:
    """Widen-only floor: the long stop may not sit closer than ``min_distance``.

    Never TIGHTENS (identity when the stop is already outside the floor) and
    never widens below ``bound`` (the structural / prior stop) when one is
    given.  Garbage input => identity.
    """
    try:
        e = float(entry)
        s = float(stop)
        d = float(min_distance)
    except (TypeError, ValueError):
        return stop
    if not (math.isfinite(e) and math.isfinite(s) and math.isfinite(d)) or d <= 0.0:
        return s
    floored = min(s, e - d)
    if bound is not None:
        try:
            b = float(bound)
            if math.isfinite(b):
                floored = max(floored, min(s, b))
        except (TypeError, ValueError):
            pass
    return floored


def latency_fill(
    seconds: list[Sec], *, breach_epoch: float, latency_s: float, fallback: float
) -> float:
    """Fill price model: the BID ``latency_s`` after the breach second.

    Uses the bid-low of the first populated second at/after
    ``breach_epoch + latency_s`` (a market sell lands on the bid; the observed
    breach->fill latency of the real trade is what the caller passes).  Falls
    back to ``fallback`` when the tape ends first.
    """
    try:
        t = float(breach_epoch) + float(latency_s)
    except (TypeError, ValueError):
        return fallback
    cands = sorted((s for s in seconds if s[0] >= t), key=lambda r: r[0])
    for s, lo, hi, blo, bhi in cands[:5]:
        px = blo if blo is not None else lo
        if px is not None and math.isfinite(px) and px > 0:
            return float(px)
    return fallback


@dataclass
class SimResult:
    outcome: str  # 'stop' | 'target' | 'burst' | 'open' | 'no_tape'
    exit_epoch: float | None
    exit_price: float | None
    reason: str = ""
    burst_armed_epoch: float | None = None


def simulate_with_stop(
    seconds: list[Sec],
    *,
    entry_epoch: float,
    entry: float,
    stop: float,
    target: float | None,
    end_epoch: float,
    burst_enabled: bool = True,
    burst_min_move_pct: float = 1.5,
    burst_lookback_s: float = 60.0,
    burst_decision_s: float = 45.0,
    burst_latency_s: float = 12.0,
    stop_start_epoch: float | None = None,
    stop_latency_s: float = 0.0,
) -> SimResult:
    """Walk per-second buckets after entry with a given stop.

    Bid-based like the live runner: the stop breaches on ``bid_low <= stop``
    (trade low when no bid), the target on ``bid_high >= target``, the burst
    (#1277) arms the first second the bid is >= (1+pct) x the bid-low of the
    trailing ``burst_lookback_s`` window and exits ``burst_decision_s +
    burst_latency_s`` later.  Inside a second the stop is checked first
    (protective default).  ``stop_start_epoch`` lets a tighten's stop only bind
    from its own time.  A stop exit is priced with ``latency_fill`` at
    ``stop_latency_s`` after the breach.  'open' marks at the last bid when
    nothing fires by ``end_epoch``.
    """
    rows = sorted(
        (int(s), float(lo), float(hi), blo, bhi)
        for s, lo, hi, blo, bhi in seconds
        if s is not None and lo is not None and hi is not None
    )
    rows = [r for r in rows if entry_epoch < r[0] <= end_epoch]
    if not rows:
        return SimResult("no_tape", None, None, "no prints after entry")
    window: list[tuple[int, float]] = []
    burst_at: float | None = None
    stop_from = float(stop_start_epoch) if stop_start_epoch is not None else float(entry_epoch)
    for s, lo, hi, blo, bhi in rows:
        b_lo = blo if blo is not None else lo
        b_hi = bhi if bhi is not None else hi
        if s >= stop_from and b_lo <= stop:
            px = latency_fill(rows, breach_epoch=s, latency_s=stop_latency_s, fallback=stop)
            return SimResult("stop", s, px, "bid<=stop", burst_at)
        if target is not None and b_hi >= target:
            return SimResult("target", s, target, "bid>=target", burst_at)
        if burst_enabled:
            if burst_at is not None:
                if s - burst_at >= burst_decision_s + burst_latency_s:
                    return SimResult("burst", s, b_lo, "burst clock", burst_at)
            else:
                window.append((s, b_lo))
                window = [w for w in window if s - w[0] <= burst_lookback_s]
                lb_low = min(w[1] for w in window)
                if lb_low > 0 and b_hi >= lb_low * (1.0 + burst_min_move_pct / 100.0):
                    burst_at = float(s)
    last = rows[-1]
    return SimResult("open", last[0], last[3] if last[3] is not None else last[1], "horizon", burst_at)


# ── CLI glue (file readers; no DB) ────────────────────────────────────────────


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.strip()).replace(tzinfo=timezone.utc).timestamp()


def _load_events(path: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line == "SET":
                continue
            parts = line.split("|", 4)
            if len(parts) < 5:
                continue
            sid, sym, ts, et, pl = parts
            try:
                d = json.loads(pl)
            except Exception:
                d = {"_raw": pl}
            out.setdefault(sid, []).append({"sym": sym, "ts": ts, "epoch": _ts(ts), "type": et, "p": d})
    return out


def _f(x: str) -> float | None:
    try:
        return float(x) if x not in ("", None) else None
    except ValueError:
        return None


def _load_ticks(path: str) -> list[Sec]:
    rows: list[Sec] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line == "SET" or line.startswith("ERROR"):
                continue
            f = line.split("|")
            try:
                # s, min(price), max(price), n, size, min(bid), max(bid), max(ask), min(ask)
                rows.append((int(float(f[0])), float(f[1]), float(f[2]), _f(f[5]), _f(f[6])))
            except (ValueError, IndexError):
                continue
    return rows


def _load_nbbo(path: str) -> list[tuple[int, float, float, float]]:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line == "SET" or line.startswith("ERROR"):
                continue
            f = line.split("|")
            try:
                rows.append((int(float(f[0])), float(f[1]), float(f[2]), float(f[3])))
            except (ValueError, IndexError):
                continue
    return rows


def _nbbo_at(nbbo, epoch: float, back_s: float = 5.0, fwd_s: float = 0.0):
    best = None
    for s, b, a, sp in nbbo:
        if epoch - back_s <= s <= epoch + fwd_s:
            if best is None or abs(s - epoch) < abs(best[0] - epoch):
                best = (s, b, a, sp)
    return best


def _median_spread(nbbo, t0: float, t1: float) -> float | None:
    ws = [a - b for s, b, a, sp in nbbo if t0 <= s <= t1 and a > b > 0]
    return statistics.median(ws) if ws else None


STOP_CLASS = {"stop", "trail_stop", "deadman_stop", "grind_trail_stop"}
STOP_DRIVEN_BAILOUTS = {"max_loss_circuit"}
RULES: list[tuple[str, float | None, float | None]] = [
    ("F0_unchanged", None, None),
    ("F1_k1.5", 1.5, None), ("F1_k2", 2.0, None), ("F1_k3", 3.0, None),
    ("F2_m1", None, 1.0), ("F2_m1.5", None, 1.5), ("F2_m2", None, 2.0),
    ("F3_k1.5_m1", 1.5, 1.0), ("F3_k2_m1.5", 2.0, 1.5), ("F3_k3_m2", 3.0, 2.0),
]


@dataclass
class Row:
    sid: str
    sym: str
    fields: dict[str, Any] = field(default_factory=dict)


def build_rows(scratch: str) -> list[Row]:
    ev = _load_events(os.path.join(scratch, "events_raw.txt"))
    sessions = []
    with open(os.path.join(scratch, "sessions.tsv"), encoding="utf-8") as fh:
        for line in fh:
            f = line.rstrip("\n").split("\t")
            if len(f) >= 2:
                sessions.append((f[0], f[1]))
    rows: list[Row] = []
    for sid, sym in sessions:
        r = Row(sid, sym)
        F = r.fields
        E = ev.get(sid, [])
        ticks = _load_ticks(os.path.join(scratch, "tape", f"ticks2_{sid}_{sym}.txt"))
        nbbo = _load_nbbo(os.path.join(scratch, "tape", f"nbbo_{sid}_{sym}.txt"))
        secs3 = [(s, lo, hi) for s, lo, hi, *_ in ticks]
        F["tick_seconds"] = len(ticks)
        F["nbbo_seconds"] = len(nbbo)
        ent = next((e for e in E if e["type"] == "live_entry_filled"), None)
        if ent is None:
            F["unreadable"] = "no live_entry_filled event"
            rows.append(r)
            continue
        e0 = ent["epoch"]
        entry = float(ent["p"]["avg"])
        qty = float(ent["p"]["quantity"])
        F.update(entry_ts=ent["ts"], entry_px=entry, qty=qty, trigger=ent["p"].get("trigger_reason"))
        after = [e for e in E if e["epoch"] >= e0]
        ex = next((e for e in after if e["type"] == "live_exit_filled"), None)
        bail = next((e for e in after if e["type"] == "live_bailout"), None)
        mfe = next((e for e in after if e["type"] == "momentum_mfe_realized"), None)
        dm = next((e for e in after if e["type"] == "live_deadman_stop_placed"), None)
        tightens = [e for e in after if e["type"] == "viability_degraded_tighten" and (ex is None or e["epoch"] <= ex["epoch"])]
        clamps = [e for e in after if e["type"] == "trail_noise_floor_clamped" and (ex is None or e["epoch"] <= ex["epoch"])]
        armed = next((e for e in after if e["type"] == "live_trailing_armed"), None)
        accel = next((e for e in after if e["type"] == "live_tape_accel_reversal_exit" and e["p"].get("fired")), None)
        F["deadman_stop"] = dm["p"].get("stop_price") if dm else None
        F["deadman_inert_premarket"] = any(e["type"] == "live_deadman_stop_inert_until_rth" for e in after)
        mt = next((e for e in E if e["type"] == "momentum_mfe_target_applied"), None)
        F["target_r_applied"] = mt["p"].get("applied_target_r") if mt else None
        stop0 = None
        if tightens:
            stop0 = float(tightens[0]["p"]["old_stop"])
        elif mfe and mfe["p"].get("stop_distance"):
            stop0 = entry - float(mfe["p"]["stop_distance"])
        else:
            sb0 = next((e for e in after if e["type"] == "stop_breach_pending_confirm"), None)
            if sb0:
                stop0 = float(sb0["p"]["stop_price"])
        F["initial_stop"] = stop0
        F["initial_stop_dist"] = (entry - stop0) if stop0 else None
        F["initial_stop_pct"] = ((entry - stop0) / entry * 100.0) if stop0 else None
        F["tightens"] = "; ".join(
            f"{t['ts'][11:23]} viability {t['p']['old_stop']:.4f}->{t['p']['new_stop']:.4f} "
            f"(via {t['p'].get('admission_viability')}->{t['p'].get('current_viability')})" for t in tightens
        )
        F["n_tightens"] = len(tightens)
        F["trail_armed_ts"] = armed["ts"] if armed else None
        F["n_trail_noise_clamps"] = len(clamps)
        F["tape_accel_lock_stop"] = accel["p"].get("adaptive_stop") if accel else None
        F["tape_accel_lock_ts"] = accel["ts"] if accel else None
        if ex is None:
            F["exit_reason"] = (bail["p"].get("reason") if bail else None) or ("no live_exit_filled" if bail is None else "bailout_no_fill_event")
            F["unreadable"] = "no live_exit_filled event in session"
            F["bailout_ts"] = bail["ts"] if bail else None
            rows.append(r)
            continue
        fill = float(ex["p"]["fill_price"])
        pnl = float(ex["p"]["pnl_usd"])
        F.update(exit_ts=ex["ts"], exit_px=fill, pnl_usd=pnl, exit_reason=ex["p"].get("reason"))
        if bail and bail["epoch"] <= ex["epoch"]:
            F["bailout_reason"] = bail["p"].get("reason") or ("viability" if "viability_score" in bail["p"] else None)
            F["bailout_ts"] = bail["ts"]
        last_t = tightens[-1]["epoch"] if tightens else e0
        sb = next((e for e in after if e["type"] == "stop_breach_pending_confirm" and last_t <= e["epoch"] <= ex["epoch"]), None)
        if sb is None:
            sb = next((e for e in after if e["type"] == "stop_breach_pending_confirm" and e["epoch"] <= ex["epoch"]), None)
        stop_b = None
        breach_e = None
        breach_bid = None
        if F["exit_reason"] in STOP_CLASS and sb is not None:
            stop_b = float(sb["p"]["stop_price"])
            breach_e = sb["epoch"]
            breach_bid = sb["p"].get("bid")
            F["breach_ts"] = sb["ts"]
        elif F["exit_reason"] == "deadman_stop":
            stop_b = F["deadman_stop"]
            breach_e = e0
            F["breach_ts"] = ent["ts"]
        elif bail is not None:
            stop_b = float(tightens[-1]["p"]["new_stop"]) if tightens else stop0
            breach_e = bail["epoch"]
            breach_bid = bail["p"].get("bid")
            F["breach_ts"] = bail["ts"]
        F["stop_at_breach"] = stop_b
        F["breach_bid"] = breach_bid
        F["stop_dist_at_breach"] = (entry - stop_b) if stop_b else None
        F["stop_dist_at_breach_pct"] = ((entry - stop_b) / entry * 100.0) if stop_b else None
        lat = (ex["epoch"] - breach_e) if breach_e else None
        F["breach_to_fill_s"] = lat
        F["fill_slip_vs_stop"] = (stop_b - fill) if stop_b else None
        F["fill_slip_vs_stop_pct"] = ((stop_b - fill) / entry * 100.0) if stop_b else None
        q = _nbbo_at(nbbo, breach_e, back_s=10.0) if breach_e else None
        src = "nbbo_tape"
        if q is None:
            sp = next((e for e in after if e["type"] == "live_exit_stand_in_pricing" and e["epoch"] >= (breach_e or e0)), None)
            if sp and isinstance(sp["p"].get("execution_bbo"), dict):
                bb = sp["p"]["execution_bbo"]
                q = (sp["epoch"], float(bb["bid"]), float(bb["ask"]), float(bb.get("spread_bps") or 0))
                src = "stand_in_pricing_event"
        if q is None and breach_e:
            # embedded bid/ask on the trade ticks as a last resort
            tq = [t for t in ticks if breach_e - 10 <= t[0] <= breach_e and t[3] is not None]
            if tq:
                t = tq[-1]
                q = (t[0], t[3], None, None)
                src = "tick_embedded_bid_only"
        if q:
            F["breach_spread_bid"], F["breach_spread_ask"], F["breach_spread_bps"] = q[1], q[2], q[3]
            F["breach_spread_abs"] = (q[2] - q[1]) if q[2] is not None else None
            F["breach_spread_src"] = src
            F["stop_dist_in_spread_units"] = spread_units(entry - stop_b, q[1], q[2]) if (stop_b and q[2] is not None) else None
        else:
            F["breach_spread_src"] = "none"
        # was the breach a one-tick flicker or a persistent break?  (bid-based, 60s)
        if breach_e and stop_b:
            post_b = [(s, (blo if blo is not None else lo)) for s, lo, hi, blo, bhi in ticks if breach_e <= s <= breach_e + 60]
            F["breach_persist_s_of_60"] = sum(1 for s, b in post_b if b <= stop_b)
            F["breach_min_bid_60s"] = min((b for s, b in post_b), default=None)
            F["breach_dip_below_stop_pct"] = ((stop_b - F["breach_min_bid_60s"]) / entry * 100.0) if F["breach_min_bid_60s"] is not None else None
        F["entry_spread_med_abs"] = _median_spread(nbbo, e0 - 60, e0)
        F["entry_spread_med_bps"] = (F["entry_spread_med_abs"] / entry * 1e4) if F["entry_spread_med_abs"] else None
        F["initial_stop_in_entry_spread_units"] = spread_units(entry - stop0, 0.0, F["entry_spread_med_abs"]) if (stop0 and F["entry_spread_med_abs"]) else None
        if tightens:
            tt = tightens[-1]["epoch"]
            F["tighten_spread_med_abs"] = _median_spread(nbbo, tt - 60, tt)
            F["tighten_stop_in_spread_units"] = spread_units(entry - float(tightens[-1]["p"]["new_stop"]), 0.0, F["tighten_spread_med_abs"]) if F["tighten_spread_med_abs"] else None
        trs = one_minute_true_ranges(secs3, end_epoch=e0, minutes=5)
        F["tr1m_pre_entry_n"] = len(trs)
        F["tr1m_pre_entry_med"] = statistics.median(trs) if trs else None
        F["tr1m_pre_entry_med_pct"] = (F["tr1m_pre_entry_med"] / entry * 100.0) if F["tr1m_pre_entry_med"] else None
        F["initial_stop_in_tr1m_units"] = ((entry - stop0) / F["tr1m_pre_entry_med"]) if (stop0 and F["tr1m_pre_entry_med"]) else None
        pre = [(bhi if bhi is not None else hi) for s, lo, hi, blo, bhi in ticks if e0 < s <= (breach_e or ex["epoch"])]
        if pre:
            mfe_px = max(pre)
            F["mfe_bid_before_breach"] = mfe_px
            F["mfe_pct"] = (mfe_px - entry) / entry * 100.0
            F["mfe_r"] = ((mfe_px - entry) / (entry - stop0)) if stop0 else None
        F["mfe_r_event"] = mfe["p"].get("mfe_r") if mfe else None
        xe = ex["epoch"]
        for m in (5, 15, 30):
            hs = [(bhi if bhi is not None else hi) for s, lo, hi, blo, bhi in ticks if xe < s <= xe + 60 * m]
            F[f"post_max_{m}m_vs_entry_pct"] = ((max(hs) - entry) / entry * 100.0) if hs else None
        lows = [(blo if blo is not None else lo) for s, lo, hi, blo, bhi in ticks if xe < s <= xe + 1800]
        F["post_min_30m_vs_stop_pct"] = ((min(lows) - stop_b) / stop_b * 100.0) if (lows and stop_b) else None
        F["post_min_30m_px"] = min(lows) if lows else None
        F["tape_horizon_s_after_exit"] = (max(t[0] for t in ticks) - xe) if ticks else 0
        stop_driven = (F["exit_reason"] in STOP_CLASS) or (F.get("bailout_reason") in STOP_DRIVEN_BAILOUTS)
        F["cf_applicable"] = bool(stop_driven and stop0 and ticks)
        if not F["cf_applicable"]:
            rows.append(r)
            continue
        S_entry = F["entry_spread_med_abs"] or F.get("breach_spread_abs")
        TR = F["tr1m_pre_entry_med"]
        rr = float(F["target_r_applied"] or 2.0)
        horizon = xe + 1800
        lat_s = float(lat) if lat is not None else 12.0
        base_pnl_same: float | None = None
        for name, k, m in RULES:
            d_parts = []
            if k is not None and S_entry:
                d_parts.append(k * S_entry)
            if m is not None and TR:
                d_parts.append(m * TR)
            if name != "F0_unchanged" and not d_parts:
                F[f"{name}__init_stop"] = None
                F[f"{name}__outcome"] = "no_measure"
                continue
            dmin = max(d_parts) if d_parts else 0.0
            s_init = floor_stop(entry=entry, stop=stop0, min_distance=dmin) if dmin > 0 else stop0
            eff_init = s_init
            eff_tight = None
            if tightens:
                st = float(tightens[-1]["p"]["new_stop"])
                S_t = F.get("tighten_spread_med_abs") or S_entry
                d_t_parts = [x for x in ((k * S_t) if (k and S_t) else None, (m * TR) if (m and TR) else None) if x]
                d_t = max(d_t_parts) if d_t_parts else 0.0
                # bound = the stop in force before the tighten (the floored initial);
                # the tighten may never sit WIDER than it.
                s_tight = floor_stop(entry=entry, stop=st, min_distance=d_t, bound=eff_init) if d_t > 0 else st
                eff_tight = max(s_tight, eff_init)
            new_dist = entry - eff_init
            target = entry + rr * new_dist
            sim = simulate_with_stop(
                ticks, entry_epoch=e0, entry=entry, stop=eff_init, target=target,
                end_epoch=horizon, stop_latency_s=lat_s,
            )
            if eff_tight is not None and eff_tight > eff_init and sim.outcome != "stop":
                sim2 = simulate_with_stop(
                    ticks, entry_epoch=e0, entry=entry, stop=eff_tight, target=target,
                    end_epoch=horizon, stop_start_epoch=tightens[-1]["epoch"], stop_latency_s=lat_s,
                )
                if sim2.exit_epoch is not None and (sim.exit_epoch is None or sim2.exit_epoch <= sim.exit_epoch):
                    sim = sim2
            elif eff_tight is not None and eff_tight > eff_init and sim.outcome == "stop" and sim.exit_epoch and sim.exit_epoch > tightens[-1]["epoch"]:
                sim2 = simulate_with_stop(
                    ticks, entry_epoch=e0, entry=entry, stop=eff_tight, target=target,
                    end_epoch=horizon, stop_start_epoch=tightens[-1]["epoch"], stop_latency_s=lat_s,
                )
                if sim2.exit_epoch is not None and sim2.exit_epoch <= sim.exit_epoch:
                    sim = sim2
            px = sim.exit_price
            q_rf = math.floor(qty * (entry - stop0) / new_dist) if new_dist > 0 else qty
            pnl_same = (px - entry) * qty if px is not None else None
            pnl_rf = (px - entry) * q_rf if px is not None else None
            if name == "F0_unchanged":
                base_pnl_same = pnl_same
            changed = bool(abs(eff_init - stop0) > 1e-9 or (
                tightens and eff_tight is not None and abs(eff_tight - float(tightens[-1]["p"]["new_stop"])) > 1e-9
            ))
            F[f"{name}__init_stop"] = round(eff_init, 4)
            F[f"{name}__init_dist_pct"] = round(new_dist / entry * 100.0, 3)
            F[f"{name}__tighten_stop"] = round(eff_tight, 4) if eff_tight is not None else None
            F[f"{name}__changed"] = changed if name != "F0_unchanged" else False
            F[f"{name}__outcome"] = sim.outcome
            F[f"{name}__exit_px"] = round(px, 4) if px is not None else None
            F[f"{name}__exit_t_plus_s"] = round(sim.exit_epoch - e0, 1) if sim.exit_epoch else None
            F[f"{name}__qty_rf"] = q_rf
            F[f"{name}__pnl_same_qty"] = round(pnl_same, 2) if pnl_same is not None else None
            F[f"{name}__pnl_rf_qty"] = round(pnl_rf, 2) if pnl_rf is not None else None
            F[f"{name}__delta_vs_actual_same_qty"] = round(pnl_same - pnl, 2) if pnl_same is not None else None
            F[f"{name}__delta_vs_actual_rf_qty"] = round(pnl_rf - pnl, 2) if pnl_rf is not None else None
            F[f"{name}__delta_vs_base_same_qty"] = round(pnl_same - base_pnl_same, 2) if (pnl_same is not None and base_pnl_same is not None) else None
            F[f"{name}__delta_vs_base_rf_qty"] = round(pnl_rf - base_pnl_same, 2) if (pnl_rf is not None and base_pnl_same is not None) else None
        rows.append(r)
    return rows


def write_outputs(rows: list[Row], out_csv: str, out_md: str) -> None:
    keys: list[str] = ["session_id", "symbol"]
    for r in rows:
        for k in r.fields:
            if k not in keys:
                keys.append(k)
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            d = {"session_id": r.sid, "symbol": r.sym}
            d.update({k: r.fields.get(k) for k in keys if k not in d})
            w.writerow(d)
    lines = []
    app = [r for r in rows if r.fields.get("cf_applicable")]
    lines.append(
        f"rows={len(rows)} with_exit_fill={sum(1 for r in rows if r.fields.get('exit_px') is not None)} "
        f"cf_applicable(stop-distance-driven)={len(app)} unreadable={sum(1 for r in rows if r.fields.get('unreadable'))}"
    )
    lines.append("")
    lines.append("| rule | changed | stop-outs avoided N | avoided $ (baseline sim loss) | then: target / burst / wider-stop / open | extra loss $ (wider stop, vs baseline) | net $ vs baseline same-qty | net $ vs baseline risk-first qty | net R (same qty) | net $ vs ACTUAL same-qty |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, k, m in RULES:
        if name == "F0_unchanged":
            base_same = sum(float(r.fields.get(f"{name}__pnl_same_qty") or 0.0) for r in app)
            act = sum(float(r.fields.get("pnl_usd") or 0.0) for r in app)
            lines.append(f"| {name} (sim calibration) | 0/{len(app)} | - | baseline sim {base_same:+.2f} vs actual {act:+.2f} | - | - | - | - | - | {base_same - act:+.2f} |")
            continue
        ch = [r for r in app if r.fields.get(f"{name}__changed")]
        avoided = [r for r in ch if r.fields.get(f"{name}__outcome") in ("target", "burst", "open")]
        tgt = sum(1 for r in ch if r.fields.get(f"{name}__outcome") == "target")
        bst = sum(1 for r in ch if r.fields.get(f"{name}__outcome") == "burst")
        wid = [r for r in ch if r.fields.get(f"{name}__outcome") == "stop"]
        opn = sum(1 for r in ch if r.fields.get(f"{name}__outcome") == "open")
        avoided_usd = sum(float(r.fields.get("F0_unchanged__pnl_same_qty") or 0.0) for r in avoided)
        extra = sum(min(0.0, float(r.fields.get(f"{name}__delta_vs_base_same_qty") or 0.0)) for r in wid)
        net_same = sum(float(r.fields.get(f"{name}__delta_vs_base_same_qty") or 0.0) for r in ch)
        net_rf = sum(float(r.fields.get(f"{name}__delta_vs_base_rf_qty") or 0.0) for r in ch)
        net_act = sum(float(r.fields.get(f"{name}__delta_vs_actual_same_qty") or 0.0) for r in ch)
        net_r = sum(
            (float(r.fields.get(f"{name}__delta_vs_base_same_qty") or 0.0) / (float(r.fields["initial_stop_dist"]) * float(r.fields["qty"])))
            for r in ch if r.fields.get("initial_stop_dist")
        )
        lines.append(
            f"| {name} | {len(ch)}/{len(app)} | {len(avoided)} | {avoided_usd:+.2f} | {tgt} / {bst} / {len(wid)} / {opn} | "
            f"{extra:+.2f} | {net_same:+.2f} | {net_rf:+.2f} | {net_r:+.2f} | {net_act:+.2f} |"
        )
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2
    rows = build_rows(argv[1])
    write_outputs(rows, argv[2], argv[3])
    print(open(argv[3], encoding="utf-8").read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
