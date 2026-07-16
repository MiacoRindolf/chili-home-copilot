"""Burst frequency (Ross-style 5m impulse) from cached 5m candles: a bar with
>=2% open->high AND >=3x median bar dollar-volume. ~25h window. Cross with
spread to show the majors-slow-but-tight vs alts-fast-but-wide tradeoff."""
import json
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "_cx_cache"
c5 = json.load(open(CACHE / "candles_5m.json"))
books = json.load(open(CACHE / "books.json"))["samples"]
stats = json.load(open(CACHE / "stats_all.json"))["stats"]


def spread_med(pid):
    sp = []
    for s in books.get(pid, []):
        if s.get("status") != 200 or not s.get("bids") or not s.get("asks"):
            continue
        try:
            bb, ba = float(s["bids"][0][0]), float(s["asks"][0][0])
        except (TypeError, ValueError, IndexError):
            continue
        if bb > 0 and ba >= bb:
            sp.append((ba - bb) / ((bb + ba) / 2) * 1e4)
    sp.sort()
    return sp[len(sp) // 2] if sp else None


def dv24(pid):
    d = (stats.get(pid) or {}).get("data") or {}
    try:
        return float(d.get("volume") or 0) * float(d.get("last") or 0)
    except (TypeError, ValueError):
        return 0.0


rows = []
for pid, rec in c5.items():
    bars = rec.get("bars")
    if not bars:
        continue
    seen, uniq = set(), []
    for b in bars:
        if b[0] not in seen:
            seen.add(b[0])
            uniq.append(b)
    uniq.sort(key=lambda b: b[0])
    if len(uniq) < 10:
        continue
    dvs = sorted(float(b[5]) * (float(b[4]) + float(b[3])) / 2 for b in uniq)
    med = dvs[len(dvs) // 2] or 0.0
    bursts = 0
    for b in uniq:
        o, h = float(b[3]), float(b[2])
        dvol = float(b[5]) * (float(b[4]) + o) / 2
        if o > 0 and (h / o - 1) >= 0.02 and med > 0 and dvol >= 3 * med:
            bursts += 1
    days = (uniq[-1][0] - uniq[0][0]) / 86400 or 1
    rows.append((pid, bursts / days, spread_med(pid), dv24(pid) / 1e6, len(uniq)))

rows.sort(key=lambda r: -(r[1] or 0))
print("%-13s %9s %9s %10s %6s" % ("pair", "bursts/d", "sp_med", "dv24h_M", "nbar"))
for pid, bpd, sp, dvm, n in rows:
    print("%-13s %9.2f %9s %10.1f %6d" % (
        pid, bpd, ("%.1f" % sp) if sp is not None else "?", dvm, n))
