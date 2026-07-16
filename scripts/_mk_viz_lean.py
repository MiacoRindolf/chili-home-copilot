import json, os

SRC = os.path.join(os.path.dirname(__file__), "_viz_replay.json")
OUT = os.path.join(os.path.dirname(__file__), "_viz_lean.json")
d = json.load(open(SRC))
want = ["NXTS", "CRMT", "SKYQ", "ICCM"]  # 3 winners + 1 loser(re-entry)


def m(hhmm):
    try:
        return int(hhmm[:2]) * 60 + int(hhmm[3:5])
    except Exception:
        return 0


by = {s["sym"]: s for s in d["symbols"]}
tmins = []
for w in want:
    for t in (by.get(w, {}).get("trades") or []):
        tmins.append(m(t["t"]))
        tmins.append(m(t.get("exit_t") or t["t"]))
lo, hi = min(tmins) - 45, max(tmins) + 45
out = {"date": d.get("date"), "lo": lo, "hi": hi, "symbols": []}
for w in want:
    s = by.get(w)
    if not s:
        continue
    cs = [[c[0], round(c[1], 3), round(c[2], 3), round(c[3], 3), round(c[4], 3)]
          for c in s["candles"] if lo <= m(c[0]) <= hi]
    trs = [{"t": t["t"], "xt": t.get("exit_t"), "e": t["entry"], "x": t["exit"],
            "sl": round(t.get("stop") or 0, 3), "tg": round(t.get("target") or 0, 3),
            "why": t["why"], "usd": t["usd"]} for t in (s.get("trades") or [])]
    out["symbols"].append({"sym": w, "c": cs, "tr": trs})
json.dump(out, open(OUT, "w"), separators=(",", ":"))
print("bytes=", os.path.getsize(OUT), "window=", lo, hi,
      "counts=", {s["sym"]: len(s["c"]) for s in out["symbols"]})
