"""0/17 forensics — step 4: per-trade attribution + tonight's-mechanics counterfactual.

Inputs (all cached by earlier steps):
  scripts/_cx_cache/cx_0of17_db.json            — sessions/outcomes/events/ledger/packets
  scripts/_cx_cache/cx_0of17_broker_fills.json  — REAL broker fills incl. commissions
  scripts/_cx_cache/candles/session_<sid>_<sym>.json — 1m candles (Coinbase Exchange)

Per trade:
  (a) fees       — real commissions (broker truth; DB recorded 0)
  (b) spread     — entry premium vs entry-minute open; exit discount vs exit-minute open
  (c) trigger    — 1m-EMA9 extension at entry (verticality retro-check), MFE/MAE in R
  (d) exit       — post-exit recovery (did price reclaim entry / +2R within 2h?)
  (e) regime     — pre-entry 90m trend, post-entry 4h best
Counterfactual: same entry, tonight's exits (0.33 partial @2R, BE after partial,
500bps ratchet trail + 5m-EMA9 anchor >=1R, variant max-hold), honest taker fees
(50bps/side current tier), verticality gate retro-applied.

Candle format: [epoch, low, high, open, close, volume], 1m, gaps = no trades.
"""
import json
import math
import pathlib
from datetime import datetime, timezone

CACHE = pathlib.Path(__file__).resolve().parent / "_cx_cache"
DB = json.loads((CACHE / "cx_0of17_db.json").read_text())
FILLS = json.loads((CACHE / "cx_0of17_broker_fills.json").read_text())

FEE_NOW = 0.0050      # 50bps taker (current realized tier, from broker fills)
TRAIL_BPS = 500.0     # flat band (config default floor=ceiling=500)
RR = 2.0              # first target = 2R
PARTIAL_FRAC = 0.33   # tonight's partial
VERT_MULT = 1.5       # chili_momentum_entry_verticality_atr_mult default


def parse_ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ema(vals: list[float], n: int) -> list[float]:
    out = []
    k = 2.0 / (n + 1)
    e = None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


# ── broker fill orders ─────────────────────────────────────────────────────
orders: dict[str, dict] = {}
for f in FILLS:
    o = orders.setdefault(f["order_id"], {
        "product": f["product_id"], "side": f["side"], "qty": 0.0, "notional": 0.0,
        "comm": 0.0, "first_t": f["trade_time"], "last_t": f["trade_time"]})
    sz = float(f["size"] or 0)
    px = float(f["price"] or 0)
    o["qty"] += sz
    o["notional"] += sz * px
    o["comm"] += float(f["commission"] or 0)
    o["first_t"] = min(o["first_t"], f["trade_time"])
    o["last_t"] = max(o["last_t"], f["trade_time"])
for o in orders.values():
    o["vwap"] = o["notional"] / o["qty"] if o["qty"] else None

# ── group ledger rows per session ──────────────────────────────────────────
sess_ledger: dict[int, dict] = {}
for r in DB["ledger"]:
    sid = int(r["session_id"]) if r["session_id"] else None
    if sid is None:
        continue
    d = sess_ledger.setdefault(sid, {"entries": [], "exits": []})
    (d["entries"] if r["event_type"] == "entry_fill" else d["exits"]).append(r)

packets = {p["automation_session_id"]: p for p in DB["packets"]}
outcomes = {o["session_id"]: o for o in DB["outcomes"]}

ev_by_sess: dict[int, list] = {}
for e in DB["events"]:
    ev_by_sess.setdefault(e["session_id"], []).append(e)


def match_sell_orders(product: str, t0: datetime, t1: datetime, qty_targets: list[float]):
    """SELL orders of this product whose fills fall in [t0,t1]; greedy qty match."""
    cands = [(oid, o) for oid, o in orders.items()
             if o["product"] == product and o["side"] == "SELL"
             and t0 <= parse_ts(o["first_t"]) <= t1]
    out = []
    used = set()
    for qt in qty_targets:
        best, bd = None, None
        for oid, o in cands:
            if oid in used:
                continue
            d = abs(o["qty"] - qt) / max(qt, 1e-9)
            if bd is None or d < bd:
                best, bd = oid, d
        if best is not None and (bd or 0) < 0.02:
            used.add(best)
            out.append(orders[best] | {"order_id": best})
        else:
            out.append(None)
    # leftovers (e.g. unbooked exits)
    leftovers = [orders[oid] | {"order_id": oid} for oid, o in cands if oid not in used]
    return out, leftovers


def sim_tonight(bars, entry_t: datetime, entry_px: float, qty: float,
                stop_mult: float, max_hold_s: float, trail_activate_bps: float):
    """Walk the 1m path from entry under tonight's exit ladder. Returns dict."""
    e0 = int(entry_t.timestamp()) // 60 * 60
    closes_before = [b[4] for b in bars if b[0] <= e0]
    trs = []
    prev_c = None
    for b in bars:
        if b[0] > e0:
            break
        tr = b[2] - b[1] if prev_c is None else max(b[2] - b[1], abs(b[2] - prev_c), abs(b[1] - prev_c))
        trs.append(tr)
        prev_c = b[4]
    atr = (sum(trs[-14:]) / len(trs[-14:])) if trs else 0.0
    atr_pct = atr / entry_px if entry_px else 0.0
    # 15m expected move (live `_expected_move_bps_from_ohlcv`): ATR(<=14) of 15m bars.
    m15: dict[int, list] = {}
    for b in bars:
        if b[0] > e0:
            break
        k = b[0] // 900
        cur = m15.setdefault(k, [b[1], b[2], None, None])  # lo, hi, open(first), close(last)
        cur[0] = min(cur[0], b[1])
        cur[1] = max(cur[1], b[2])
        if cur[2] is None:
            cur[2] = b[3]
        cur[3] = b[4]
    keys15 = sorted(m15)
    trs15 = []
    pc = None
    for k in keys15:
        lo, hi, _, cl = m15[k]
        trs15.append(hi - lo if pc is None else max(hi - lo, abs(hi - pc), abs(lo - pc)))
        pc = cl
    n15 = min(14, len(trs15))
    em_pct = (sum(trs15[-n15:]) / n15 / entry_px) if n15 else 0.0
    # live stop: max(0.3% floor, regime_atr x mult, 0.5 x 15m expected move)
    stop_frac = max(0.003, atr_pct * stop_mult, 0.5 * em_pct)
    r_dist = entry_px * stop_frac
    stop = entry_px - r_dist
    target = entry_px + RR * r_dist

    # verticality retro-gate
    e9 = ema(closes_before, 9)[-1] if closes_before else None
    ext = (entry_px / e9 - 1.0) if e9 else None
    vert_cap = max(0.005, atr_pct * VERT_MULT)
    vert_blocked = (ext is not None and ext > vert_cap)

    # 5m buckets for EMA9(5m)
    fwd = [b for b in bars if b[0] > e0]
    all_bars = sorted(bars, key=lambda b: b[0])
    hwm = entry_px
    partial_done = False
    realized = 0.0
    fees = entry_px * qty * FEE_NOW
    held = qty
    exit_legs = []
    trail_on = False
    end_reason = "end_of_data"
    fivem: dict[int, float] = {}
    for b in all_bars:  # last close per completed 5m bucket
        fivem[b[0] // 300] = b[4]

    def ema5_at(ts_epoch: int):
        buckets = sorted(k for k in fivem if k < ts_epoch // 300)
        if len(buckets) < 3:
            return None
        return ema([fivem[k] for k in buckets], 9)[-1]

    for b in fwd:
        ts, lo, hi, op, cl = b[0], b[1], b[2], b[3], b[4]
        if ts - int(entry_t.timestamp()) > max_hold_s:
            px = op
            realized += (px - entry_px) * held
            fees += px * held * FEE_NOW
            exit_legs.append(("max_hold", px, held))
            held = 0.0
            end_reason = "max_hold"
            break
        if lo <= stop:
            px = min(stop, op)
            realized += (px - entry_px) * held
            fees += px * held * FEE_NOW
            exit_legs.append(("stop" if not trail_on else "trail_stop", px, held))
            held = 0.0
            end_reason = "stop" if not partial_done else "trail_stop"
            break
        if not partial_done and hi >= target:
            pq = qty * PARTIAL_FRAC
            realized += (target - entry_px) * pq
            fees += target * pq * FEE_NOW
            held -= pq
            partial_done = True
            stop = max(stop, entry_px)  # breakeven ratchet
            exit_legs.append(("partial_2R", target, pq))
        hwm = max(hwm, hi)
        if hwm >= entry_px * (1 + trail_activate_bps / 1e4):
            trail_on = True
        if trail_on:
            cand = hwm * (1 - TRAIL_BPS / 1e4)
            ur = (hwm - entry_px) / r_dist
            if ur >= 1.0:
                e5 = ema5_at(ts)
                if e5 and 0 < e5 < hwm:
                    cand = max(cand, e5 - 0.25 * atr_pct * entry_px)
            if partial_done:
                cand = max(cand, entry_px)
            stop = max(stop, cand)
    if held > 0:
        px = fwd[-1][4] if fwd else entry_px
        realized += (px - entry_px) * held
        fees += px * held * FEE_NOW
        exit_legs.append((end_reason, px, held))
    return {
        "atr_pct": atr_pct, "em15_pct": em_pct, "stop_frac": stop_frac, "r_dist_usd": r_dist * qty,
        "vert_ext": ext, "vert_cap": vert_cap, "vert_blocked": vert_blocked,
        "gross": realized, "fees": fees, "net": realized - fees,
        "legs": exit_legs, "end": end_reason, "partial": partial_done,
    }


def bar_at(bars, t: datetime, tol_min: int = 3):
    """Bar at t's minute, else the nearest earlier bar within tol (thin tape)."""
    e = int(t.timestamp()) // 60 * 60
    best = None
    for b in bars:
        if b[0] == e:
            return b
        if e - tol_min * 60 <= b[0] < e and (best is None or b[0] > best[0]):
            best = b
    return best


rows = []
for sid, led in sorted(sess_ledger.items()):
    out = outcomes.get(sid) or {}
    sym = (led["entries"] or led["exits"])[0]["ticker"]
    bars = json.loads((CACHE / "candles" / f"session_{sid}_{sym}.json").read_text())
    bars.sort(key=lambda b: b[0])
    pk = packets.get(sid) or {}
    vp = pk.get("variant_params") or {}
    stop_mult = float(vp.get("stop_atr_mult") or 0.60)
    max_hold = float(vp.get("max_hold_seconds") or 3600.0)
    trail_act = float(vp.get("trail_activate_return_bps") or 40.0)

    # entry truth: broker order matched by order_id
    eq = sum(float(r["quantity"]) for r in led["entries"])
    e_notional = sum(float(r["quantity"]) * float(r["price"]) for r in led["entries"])
    e_px = e_notional / eq
    e_ord = [orders.get(r["entry_order_id"]) for r in led["entries"]]
    e_comm = sum(o["comm"] for o in e_ord if o)
    e_t = min(parse_ts(o["first_t"]) for o in e_ord if o) if any(e_ord) else parse_ts(out.get("started_at"))

    term = parse_ts(out["terminal_at"]) if out.get("terminal_at") else (
        parse_ts(out["ended_at"]) if out.get("ended_at") else e_t)
    sells, leftovers = match_sell_orders(
        sym, e_t, max(term, e_t), [float(r["quantity"]) for r in led["exits"]])
    x_comm = sum(s["comm"] for s in sells if s)
    x_qty = sum(float(r["quantity"]) for r in led["exits"])
    x_notional = sum(float(r["quantity"]) * float(r["price"]) for r in led["exits"])
    unbooked = ""
    if x_qty < eq * 0.98 and leftovers:   # unbooked exit (e.g. POLS, FIDA s20)
        for s in leftovers:
            if abs(s["qty"] - (eq - x_qty)) / eq < 0.05 or x_qty == 0:
                x_notional += s["notional"]
                x_qty += s["qty"]
                x_comm += s["comm"]
                sells.append(s)
                unbooked = "UNBOOKED_EXIT"
    x_px = x_notional / x_qty if x_qty else None
    x_t = max((parse_ts(s["last_t"]) for s in sells if s), default=None)

    gross = (x_px - e_px) * min(eq, x_qty) if x_px else None
    fees_real = e_comm + x_comm
    net_real = (gross - fees_real) if gross is not None else None

    # (b) friction vs minute-open
    eb = bar_at(bars, e_t)
    entry_prem_bps = (e_px / eb[3] - 1) * 1e4 if eb else None
    xb = bar_at(bars, x_t) if x_t else None
    exit_disc_bps = (xb[3] / x_px - 1) * 1e4 if (xb and x_px) else None

    # exit-intent lag: first exit attempt (incl. submit FAILURES) vs broker sell
    intent_ts = []
    for e in ev_by_sess.get(sid, []):
        if e["event_type"] in ("live_exit_submitted", "live_exit_pending_place",
                               "live_exit_pending_confirmation", "live_exit_pending_unconfirmed",
                               "live_bailout"):
            intent_ts.append(parse_ts(e["ts"]))
    for n in DB["noise_counts"]:
        if n["session_id"] == sid and n["event_type"] == "live_exit_submit_failed":
            intent_ts.append(parse_ts(n["first_ts"]))
    x_first = min((parse_ts(s["first_t"]) for s in sells if s), default=None)
    exit_lag_s = exit_lag_cost_bps = None
    if intent_ts and x_first:
        it = min(intent_ts)
        if it < x_first:
            exit_lag_s = (x_first - it).total_seconds()
            ib = bar_at(bars, it)
            if ib and x_px:
                exit_lag_cost_bps = (ib[3] / x_px - 1) * 1e4

    # (c)(e) path quality
    sim = sim_tonight(bars, e_t, e_px, eq, stop_mult, max_hold, trail_act)
    r_frac = sim["stop_frac"]
    e0 = int(e_t.timestamp()) // 60 * 60
    fwd_hold = [b for b in bars if e0 < b[0] <= e0 + max_hold]
    mfe_r = max(((b[2] - e_px) / (e_px * r_frac)) for b in fwd_hold) if fwd_hold else None
    mae_r = min(((b[1] - e_px) / (e_px * r_frac)) for b in fwd_hold) if fwd_hold else None
    # (d) post-exit recovery
    rec_be = rec_2r = None
    if x_t:
        x0 = int(x_t.timestamp()) // 60 * 60
        post = [b for b in bars if x0 < b[0] <= x0 + 7200]
        if post:
            ph = max(b[2] for b in post)
            rec_be = ph >= e_px
            rec_2r = ph >= e_px * (1 + RR * r_frac)
    pre = [b for b in bars if e0 - 5400 <= b[0] <= e0]
    pre_trend = (pre[-1][4] / pre[0][3] - 1) * 100 if len(pre) > 2 else None

    n_exit_fail = sum(int(n["n"]) for n in DB["noise_counts"]
                      if n["session_id"] == sid and n["event_type"] == "live_exit_submit_failed")

    # exit-policy baselines on the same entry (fees 50bps/side)
    def hold_close(minutes):
        w = [b for b in bars if e0 < b[0] <= e0 + minutes * 60]
        if not w:
            return None
        px = w[-1][4]
        return (px - e_px) * eq - (e_px + px) * eq * FEE_NOW

    w_hold = [b for b in bars if e0 < b[0] <= e0 + max_hold]
    oracle = None
    if w_hold:
        px = max(b[2] for b in w_hold)
        oracle = (px - e_px) * eq - (e_px + px) * eq * FEE_NOW

    rows.append({
        "b1_maxhold_close": round(hold_close(max_hold / 60), 2) if hold_close(max_hold / 60) is not None else None,
        "b2_4h_close": round(hold_close(240), 2) if hold_close(240) is not None else None,
        "b3_oracle_high": round(oracle, 2) if oracle is not None else None,
        "sid": sid, "symbol": sym, "class": out.get("outcome_class"), "reason": out.get("exit_reason"),
        "variant": out.get("variant_key"), "entry_t": e_t.isoformat(), "exit_t": x_t.isoformat() if x_t else None,
        "hold_min": round((x_t - e_t).total_seconds() / 60, 1) if x_t else None,
        "entry_px": e_px, "exit_px": x_px, "qty": eq, "notional": round(e_notional, 2),
        "gross_usd": round(gross, 2) if gross is not None else None,
        "fees_real_usd": round(fees_real, 2),
        "net_real_usd": round(net_real, 2) if net_real is not None else None,
        "db_pnl_usd": out.get("realized_pnl_usd"),
        "entry_prem_bps": round(entry_prem_bps, 1) if entry_prem_bps is not None else None,
        "exit_disc_bps": round(exit_disc_bps, 1) if exit_disc_bps is not None else None,
        "atr_pct": round(sim["atr_pct"] * 100, 2), "stop_pct": round(sim["stop_frac"] * 100, 2),
        "vert_ext_pct": round(sim["vert_ext"] * 100, 2) if sim["vert_ext"] is not None else None,
        "vert_blocked": sim["vert_blocked"],
        "mfe_r": round(mfe_r, 2) if mfe_r is not None else None,
        "mae_r": round(mae_r, 2) if mae_r is not None else None,
        "recovered_be_2h": rec_be, "recovered_2r_2h": rec_2r,
        "pre90m_trend_pct": round(pre_trend, 1) if pre_trend is not None else None,
        "exit_submit_fails": n_exit_fail,
        "exit_lag_s": round(exit_lag_s) if exit_lag_s is not None else None,
        "exit_lag_cost_bps": round(exit_lag_cost_bps, 1) if exit_lag_cost_bps is not None else None,
        "em15_pct": round(sim["em15_pct"] * 100, 2),
        "sim_gross": round(sim["gross"], 2), "sim_fees": round(sim["fees"], 2),
        "sim_net": round(sim["net"], 2), "sim_end": sim["end"], "sim_partial": sim["partial"],
        "sim_win": sim["net"] > 0, "unbooked": unbooked,
        "bars": len(bars),
    })

(CACHE / "cx_0of17_attrib.json").write_text(json.dumps(rows, indent=1))

# ── report ─────────────────────────────────────────────────────────────────
cols = ["sid", "symbol", "entry_t", "reason", "hold_min", "notional", "gross_usd", "fees_real_usd",
        "net_real_usd", "entry_prem_bps", "exit_disc_bps", "exit_lag_s", "exit_lag_cost_bps",
        "atr_pct", "em15_pct", "stop_pct", "vert_ext_pct", "vert_blocked", "mfe_r", "mae_r",
        "recovered_be_2h", "recovered_2r_2h", "exit_submit_fails", "sim_net", "sim_end",
        "sim_partial", "sim_win", "unbooked"]
print("\t".join(cols))
for r in rows:
    print("\t".join(str(r.get(c)) for c in cols))

tot = lambda k: sum(r[k] for r in rows if r.get(k) is not None)
print(f"\nTRADES n={len(rows)}  gross={tot('gross_usd'):+.2f}  fees_real={tot('fees_real_usd'):.2f}"
      f"  net_real={tot('net_real_usd'):+.2f}  db_recorded={tot('db_pnl_usd'):+.2f}")
print(f"SIM tonight: net={tot('sim_net'):+.2f}  wins={sum(1 for r in rows if r['sim_win'])}/{len(rows)}"
      f"  vert_blocked={sum(1 for r in rows if r['vert_blocked'])}")
g = [r for r in rows if not r["vert_blocked"]]
print(f"SIM tonight (verticality-gated): net={sum(r['sim_net'] for r in g):+.2f}"
      f"  wins={sum(1 for r in g if r['sim_win'])}/{len(g)}")

# ── attribution decomposition (real trades) ───────────────────────────────
fees = tot("fees_real_usd")
entry_fric = sum(max(r["entry_prem_bps"], 0) / 1e4 * r["notional"]
                 for r in rows if r.get("entry_prem_bps") is not None)
exit_fric = sum(max(r["exit_disc_bps"], 0) / 1e4 * r["notional"]
                for r in rows if r.get("exit_disc_bps") is not None)
net = tot("net_real_usd")
signal = net + fees + entry_fric + exit_fric  # mid-to-mid path residual
print(f"\nDECOMPOSITION of net_real {net:+.2f}:")
print(f"  fees (real commissions, all TAKER): -{fees:.2f}  ({100*fees/abs(net):.0f}%)")
print(f"  entry friction (fill above minute-open): -{entry_fric:.2f}  ({100*entry_fric/abs(net):.0f}%)")
print(f"  exit friction (fill below minute-open):  -{exit_fric:.2f}  ({100*exit_fric/abs(net):.0f}%)")
print(f"  adverse mid-to-mid path (signal/trigger): {signal:+.2f}  ({100*-signal/abs(net):.0f}%)")
print(f"\nBASELINES (same entries, 50bps/side):")
for k, lbl in (("b1_maxhold_close", "hold to variant max-hold, sell close"),
               ("b2_4h_close", "hold 4h, sell close"),
               ("b3_oracle_high", "ORACLE sell at max high in hold window")):
    vals = [r[k] for r in rows if r.get(k) is not None]
    print(f"  {lbl}: net={sum(vals):+.2f} wins={sum(1 for v in vals if v > 0)}/{len(vals)}")
zf = [r["sim_net"] + r["sim_fees"] for r in rows]
print(f"  tonight's mechanics at ZERO fees: net={sum(zf):+.2f} wins={sum(1 for v in zf if v > 0)}/{len(zf)}")
nwins = sum(1 for r in rows if (r.get('gross_usd') or 0) > 0)
print(f"  trades with POSITIVE gross (fees flipped): {nwins}/{len(rows)} — every trade lost fill-to-fill")
print(f"  recovered to entry within 2h of exit: {sum(1 for r in rows if r['recovered_be_2h'])}/{len(rows)}")
print(f"  reached +2R within 2h of exit: {sum(1 for r in rows if r['recovered_2r_2h'])}/{len(rows)}")
