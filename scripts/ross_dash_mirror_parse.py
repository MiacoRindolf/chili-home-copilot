"""Ross Day-Trade-Dash mirror parser (#1250, 2026-08-30).

Ang operator ay may bayad na Warrior Trading dashboard — ang MISMONG mata ni
Ross (HOD Momentum na may strategy labels niya, Top Gainers, Continuation,
5 Pillars). Ang browser monitor ay nagsa-save ng page text; ang parser na ito
ay ginagawa itong structured JSON mirror file na binabasa ng lane intake
(``_ross_dash_mirror_symbols`` sa auto_arm) bilang union sa arm prefilter —
kapareho ng velocity union (#1242): karagdagang mata, hindi kapalit ng
sariling viability/gates (lahat ng downstream quality gates ay buo pa rin).

Usage: python scripts/ross_dash_mirror_parse.py --in page.txt --out mirror.json
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9}
_TIME_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s*(am|pm)$", re.I)
_BURST_RE = re.compile(r"^\((\d+) in (\d+)sec\)$")
_SYM_RE = re.compile(r"^[A-Z]{1,5}$")
# hilera ng numero na nagtatapos sa strategy text, hal.:
# "3.21 3.38M 45.54M 23,101.93 13,456,548.39 33.75 33.75 1.92M Medium Float - ..."
_HOD_NUMS_RE = re.compile(
    r"^([\d.]+)\s+([\d.,]+[KMB]?)\s+([\d.,]+[KMB]?)\s+([\d,.]+)\s+([\d,.]+)\s+"
    r"([-\d.]+)\s+([-\d.]+)\s+([\d.,]+[KMB]?)\s+(.+)$"
)
# Top Gainers / Continuation: pct [arrow] SYM price vol float rvold rvol5 ...
_GAINER_RE = re.compile(
    r"^([-\d.]+)\s*[▲▼]?\s*([A-Z]{1,5})\s+([\d.]+)\s+([\d.,]+[KMB]?)\s+"
    r"([\d.,]+[KMB]?)\s+([\d,.]+)\s+([\d,.]+)"
)
_GAINER_PCT_ONLY_RE = re.compile(r"^([-\d.]+)\s*[▲▼]?$")
_NUMS_ONLY_RE = re.compile(r"^([\d.]+)\s+([\d.,]+[KMB]?)\s+([\d.,]+[KMB]?)\s+([\d,.]+)\s+([\d,.]+)")


def _num(s: str) -> float | None:
    try:
        s = str(s or "").strip().replace(",", "")
        if not s:
            return None
        if s[-1].upper() in _SUFFIX:
            return float(s[:-1]) * _SUFFIX[s[-1].upper()]
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_dashboard_text(text: str) -> dict:
    lines = [ln.strip() for ln in str(text or "").splitlines()]
    hod: list[dict] = []
    gainers: list[dict] = []
    continuation: list[dict] = []

    # mga seksyon: hinahati sa mga header na kilala
    section = None
    pend_time: str | None = None
    pend_sym: str | None = None
    pend_pct: float | None = None
    for ln in lines:
        if not ln:
            continue
        low = ln.lower()
        if low.startswith("time symbol / news"):
            section = "hod"
            pend_time = pend_sym = None
            continue
        if low.startswith("change from close(%)"):
            # header ng gainers section (ang unang paglitaw pagkatapos ng hod)
            if section in ("hod", None):
                section = "gainers"
                pend_pct = pend_sym = None
            continue
        if low.startswith("moving - 2 week"):
            section = "continuation"
            pend_pct = pend_sym = None
            continue
        if "charts powered by" in low:
            break

        if section == "hod":
            # single-line variant: "05:30:13 am BRNX 4.96 402.35K ... Strategy"
            m1 = re.match(
                r"^(\d{2}:\d{2}:\d{2})\s*(am|pm)\s+([A-Z]{1,5})\s+(.+)$",
                ln, re.I,
            )
            if m1 and _HOD_NUMS_RE.match(m1.group(4)):
                mm = _HOD_NUMS_RE.match(m1.group(4))
                hod.append({
                    "time_et": f"{m1.group(1)} {m1.group(2).lower()}",
                    "symbol": m1.group(3),
                    "price": _num(mm.group(1)),
                    "volume": _num(mm.group(2)),
                    "float": _num(mm.group(3)),
                    "rvol_daily": _num(mm.group(4)),
                    "rvol_5min_pct": _num(mm.group(5)),
                    "gap_pct": _num(mm.group(6)),
                    "change_pct": _num(mm.group(7)),
                    "short_interest": _num(mm.group(8)),
                    "strategy": mm.group(9).strip(),
                })
                pend_time = pend_sym = None
                continue
            m = _TIME_RE.match(ln)
            if m:
                pend_time = f"{m.group(1)} {m.group(2).lower()}"
                pend_sym = None
                continue
            if _BURST_RE.match(ln):
                continue
            if _SYM_RE.match(ln) and pend_time:
                pend_sym = ln
                continue
            m = _HOD_NUMS_RE.match(ln)
            if m and pend_time and pend_sym:
                hod.append({
                    "time_et": pend_time,
                    "symbol": pend_sym,
                    "price": _num(m.group(1)),
                    "volume": _num(m.group(2)),
                    "float": _num(m.group(3)),
                    "rvol_daily": _num(m.group(4)),
                    "rvol_5min_pct": _num(m.group(5)),
                    "gap_pct": _num(m.group(6)),
                    "change_pct": _num(m.group(7)),
                    "short_interest": _num(m.group(8)),
                    "strategy": m.group(9).strip(),
                })
                pend_sym = None
                continue
        elif section in ("gainers", "continuation"):
            m = _GAINER_RE.match(ln)
            row = None
            if m:
                row = {
                    "pct": _num(m.group(1)),
                    "symbol": m.group(2),
                    "price": _num(m.group(3)),
                    "volume": _num(m.group(4)),
                    "float": _num(m.group(5)),
                    "rvol_daily": _num(m.group(6)),
                    "rvol_5min_pct": _num(m.group(7)),
                }
            else:
                # hati sa tatlong linya: "20.51 ▼" / "WBUY" / "1.00 18.21M ..."
                mp = _GAINER_PCT_ONLY_RE.match(ln)
                if mp and _num(mp.group(1)) is not None:
                    pend_pct = _num(mp.group(1))
                    pend_sym = None
                    continue
                if _SYM_RE.match(ln) and pend_pct is not None:
                    pend_sym = ln
                    continue
                mn = _NUMS_ONLY_RE.match(ln)
                if mn and pend_pct is not None and pend_sym:
                    row = {
                        "pct": pend_pct,
                        "symbol": pend_sym,
                        "price": _num(mn.group(1)),
                        "volume": _num(mn.group(2)),
                        "float": _num(mn.group(3)),
                        "rvol_daily": _num(mn.group(4)),
                        "rvol_5min_pct": _num(mn.group(5)),
                    }
                    pend_pct = None
                    pend_sym = None
            if row and row.get("symbol"):
                (gainers if section == "gainers" else continuation).append(row)

    # intake symbols: LAHAT ng HOD alerts (curated ni Ross ang mga strategy) +
    # gainers na >= 10% (explosive class; iwasang i-union ang buong 100-row
    # ladder ng maliliit na movers)
    symbols: list[str] = []
    seen: set[str] = set()
    for r in hod:
        s = r["symbol"]
        if s not in seen:
            seen.add(s)
            symbols.append(s)
    for r in gainers:
        if (r.get("pct") or 0) >= 10.0 and r["symbol"] not in seen:
            seen.add(r["symbol"])
            symbols.append(r["symbol"])
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "warrior_day_trade_dash",
        "hod_alerts": hod,
        "top_gainers": gainers,
        "continuation": continuation,
        "symbols": symbols,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args()
    import io

    text = io.open(args.inp, encoding="utf-8", errors="replace").read()
    mirror = parse_dashboard_text(text)
    tmp = args.out + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        json.dump(mirror, f, ensure_ascii=False)
    import os

    os.replace(tmp, args.out)
    print(
        f"mirror: {len(mirror['hod_alerts'])} hod, "
        f"{len(mirror['top_gainers'])} gainers, "
        f"{len(mirror['continuation'])} continuation, "
        f"{len(mirror['symbols'])} intake symbols"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
