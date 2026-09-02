"""PATH B marketable-partial price cost — the CORRECTED population.

Reproduces every number in ``docs/DESIGN/PARTIAL_EXIT_MARKETABLE.md`` from the
two committed evidence files. **DB-free**: the tape reads that produced the
evidence were done once, bounded per symbol/window, and their output is frozen
in ``scripts/research/data/``. Re-running this script cannot touch the live DB.

WHY THIS SCRIPT EXISTS. The first pass of this analysis reported the price cost
over *nine* rows: three where the NBBO bid reached the scale-out target while
the position was still open ("window A"), and six where it only reached the
target AFTER the position had already exited ("window B"). The marketable
variant **cannot fire in window B** -- the position is closed, there is nothing
left to sell -- so crediting it with a fill at a post-terminal bid compares it
against a trade it never makes. That error moved the headline by 8.6 bps at
instant fill and by 35.8 bps at the 1 s bound, in the direction that flattered
the recommendation.

This script separates the two populations and prices them the way they actually
differ:

  window A (n=3)  both variants act. Marketable sells f into the bid that just
                  touched T; the resting limit is credited (generously, queue
                  position waived) with a fill at T. Price difference is the
                  round-trip decay of the bid.
  window B (n=6)  ONLY the resting limit acts, and it fills while f is NAKED --
                  after the R stop has already taken the runner out. The
                  marketable variant never fires and f leaves with the position
                  at the terminal exit price. This is a real difference and it
                  favours the limit on this sample, by $169.83.
  neither (n=4)   the bid never reached T at all. The two variants are
                  byte-identical; delta is exactly $0.

Run:  python scripts/research/path_b_marketable_price_cost.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
CSV_PATH = DATA / "path_b_partial_fill_certainty_and_cost.csv"
JSON_PATH = DATA / "path_b_variant3_bid_touch.json"

#: Measured sell-side slippage vs the NBBO bid at the fill instant, Alpaca live,
#: n=10 usable ``live_exit_filled`` (bps): -485.2, -189.3, -119.8, -25.3, 0, 0,
#: 0, +13.8, +19.1, +45.1. MEDIAN 0.0, MEAN -74.2. The two large negatives are
#: stops firing through a collapsing bid, not sells into a rising one, so the
#: mean is wrong-signed as an assumption for a partial that fires into strength.
#: Both are reported anyway -- the first pass published the median row ONLY,
#: which is the row that most favours the recommendation.
SLIPPAGE_LADDER_BPS = (0.0, 2.8, 35.0, 54.0, 74.2, 150.0)


def _f(value: str | float | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_cases() -> list[dict]:
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)]
    out = []
    for r in rows:
        if str(r.get("verdict", "")).startswith("EXCLUDED"):
            continue
        win = ("A" if r.get("limit_filled_A") in ("True", "true", "1")
               else "B" if r.get("limit_filled_B") in ("True", "true", "1")
               else "NONE")
        out.append({
            "session_id": int(r["session_id"]),
            "symbol": r["symbol"],
            "window": win,
            "f_shares": _f(r["f_shares"]) or 0.0,
            "target_price": _f(r["target_price"]),
            "exit_price_used": _f(r["exit_price_used"]),
            "life_s": _f(r["life_s"]) or 0.0,
            "limit_B_proceeds": _f(r["limit_B_proceeds"]),
            "stop_distance": _f(r["stop_distance"]),
        })
    return out


def load_window_a() -> list[dict]:
    rows = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    return [r for r in rows if r["window"] == "A"]


def main() -> None:
    cases = load_cases()
    win_a = load_window_a()

    print("=" * 78)
    print("PATH B MARKETABLE PARTIAL -- CORRECTED PRICE COST")
    print("=" * 78)

    by_win = {"A": [], "B": [], "NONE": []}
    for c in cases:
        by_win[c["window"]].append(c)
    print(f"\nPopulation: n={len(cases)} usable "
          f"(window A={len(by_win['A'])}, window B={len(by_win['B'])}, "
          f"neither={len(by_win['NONE'])})")
    print("  3 further sessions excluded with unrecoverable terminals "
          "(COIW 14842, BDRX 15344, AEMD 18035);")
    print("  1 with no tape inside its 12 s life (SDOT 14825).")
    print("  The BDRX exclusion is NOT neutral: it is the known +4.0R round trip")
    print("  that gave it all back -- the most informative 'target reached then")
    print("  surrendered' case in the window.")

    # ---------------------------------------------------------- window A ----
    print("\n" + "-" * 78)
    print("WINDOW A (n=%d) -- the ONLY rows where the marketable variant exists"
          % len(win_a))
    print("-" * 78)
    hdr = (f"{'sess':<7}{'sym':<7}{'f':>5}{'target':>9}{'bid@trig':>10}"
           f"{'ttf_s':>9}{'bid+1s':>9}{'bid+5s':>9}")
    print(hdr)
    print("-" * len(hdr))
    for r in win_a:
        print(f"{r['session_id']:<7}{str(r['symbol'])[:6]:<7}{r['f_shares']:>5}"
              f"{r['target']:>9.4f}{r['bid_trig']:>10.4f}{r['ttf_s']:>9.1f}"
              f"{r['1s']:>9.4f}{r['5s']:>9.4f}")

    limit_total = sum(r["f_shares"] * r["target"] for r in win_a)
    print(f"\n  resting-limit proceeds on the f shares: ${limit_total:,.2f}")
    print("  (the limit is modelled GENEROUSLY: credited with a fill the first")
    print("   instant bid >= target, queue position at the level waived)")

    print(f"\n  {'fill assumption':<34}{'proceeds':>12}{'delta $':>11}{'bps':>10}")
    print("  " + "-" * 65)
    rows_out = []
    for label, key in (("instant fill at the touched bid", "bid_trig"),
                       ("worst bid within 1 s", "1s"),
                       ("worst bid within 5 s", "5s"),
                       ("worst bid within 15 s", "15s")):
        got = sum(r["f_shares"] * r[key] for r in win_a)
        d = got - limit_total
        bps = d / limit_total * 1e4
        rows_out.append((label, got, d, bps))
        print(f"  {label:<34}{got:>12,.2f}{d:>+11.2f}{bps:>+10.1f}")

    print("\n  The 1 s row is the BINDING one: the API round trip is 0.3-0.8 s")
    print("  (place_rtt_s p50 0.109 / p95 0.452 / max 0.860, n=137).")
    print("  NOTE: -83.9 bps EXCEEDS the 69.4 bps median tape spread. The first")
    print("  pass claimed the cost sat 'inside one spread'. On the corrected")
    print("  population it does not -- it is ~1.2 spreads.")

    # --------------------------------------------- slippage sensitivity ----
    print("\n  SLIPPAGE SENSITIVITY on the instant-fill row (the first pass")
    print("  hardcoded 0.0 bps and published no ladder):")
    print(f"    {'slippage':>12}{'proceeds':>12}{'delta $':>11}{'bps':>10}")
    print("    " + "-" * 45)
    instant = sum(r["f_shares"] * r["bid_trig"] for r in win_a)
    for s in SLIPPAGE_LADDER_BPS:
        got = instant * (1.0 - s / 1e4)
        d = got - limit_total
        note = ""
        if abs(s - 2.8) < 0.05:
            note = "  <-- BREAK-EVEN"
        if abs(s - 74.2) < 0.05:
            note = "  <-- measured MEAN"
        print(f"    {s:>10.1f}b{got:>12,.2f}{d:>+11.2f}{d/limit_total*1e4:>+10.1f}{note}")
    print("\n    The instant-fill advantage is +2.8 bps and it goes NEGATIVE at")
    print("    2.8 bps of slippage. It is razor thin, not a margin.")

    # ---------------------------------------------------------- window B ----
    print("\n" + "-" * 78)
    print("WINDOW B (n=%d) -- the marketable variant CANNOT fire here"
          % len(by_win["B"]))
    print("-" * 78)
    print("The bid reached T only AFTER the position was already out. The resting")
    print("limit fills there -- while f is NAKED, the R stop having already taken")
    print("the runner. The marketable variant never fires; f leaves with the")
    print("position at the terminal exit price.\n")
    hdr2 = (f"{'sess':<7}{'sym':<7}{'f':>5}{'limit_$':>11}{'exit_px':>10}"
            f"{'mkt_$':>11}{'delta':>10}")
    print(hdr2)
    print("-" * len(hdr2))
    tot_lim = tot_mkt = 0.0
    for c in by_win["B"]:
        lim = c["limit_B_proceeds"] or 0.0
        mkt = c["f_shares"] * (c["exit_price_used"] or 0.0)
        tot_lim += lim
        tot_mkt += mkt
        print(f"{c['session_id']:<7}{str(c['symbol'])[:6]:<7}{c['f_shares']:>5.0f}"
              f"{lim:>11.2f}{c['exit_price_used']:>10.4f}{mkt:>11.2f}"
              f"{mkt - lim:>+10.2f}")
    print("-" * len(hdr2))
    print(f"{'TOTAL':<19}{tot_lim:>11.2f}{'':>10}{tot_mkt:>11.2f}"
          f"{tot_mkt - tot_lim:>+10.2f}")
    print(f"\n  The resting limit wins window B by ${tot_lim - tot_mkt:,.2f}.")
    print("  EVERY DOLLAR OF IT IS EARNED BY HOLDING f WITH NO DOWNSIDE STOP")
    print("  until the market came back. 6 of 6 recovered. Measured naked")
    print("  mark-to-market downside across the six: $25.05 (excursions -0.7%,")
    print("  -1.3%, +1.9%, -2.9%, -3.2%, -0.7%). UPC held naked f shares for")
    print("  7,528 s (2 h 05 m) to earn $51.21.")
    print("  THIS CORPUS CONTAINS ZERO OBSERVATIONS OF THE D1 TAIL -- the")
    print("  gap-down where the R stop fires and f rides down against an")
    print("  unreachable limit. $169.83 has no measured downside attached to it")
    print("  and MUST NOT be read as an expected value.")

    # -------------------------------------------------- full population ----
    print("\n" + "-" * 78)
    print("FULL POPULATION (n=%d) -- the honest bottom line on price" % len(cases))
    print("-" * 78)
    a_instant = rows_out[0][2]
    a_1s = rows_out[1][2]
    print(f"  window A delta, instant fill : {a_instant:>+10.2f}")
    print(f"  window A delta, 1 s bound    : {a_1s:>+10.2f}")
    print(f"  window B delta               : {tot_mkt - tot_lim:>+10.2f}")
    print(f"  neither (n={len(by_win['NONE'])}) delta          : "
          f"{0.0:>+10.2f}   (byte-identical)")
    print("  " + "-" * 44)
    print(f"  TOTAL, instant fill          : {a_instant + tot_mkt - tot_lim:>+10.2f}")
    print(f"  TOTAL, 1 s bound             : {a_1s + tot_mkt - tot_lim:>+10.2f}")
    print("\n  ON PRICE, OVER THIS SAMPLE, THE RESTING LIMIT WINS. Say it plainly.")
    print("  The recommendation does NOT rest on price -- see the exposure")
    print("  comparison below and the 300 s ceiling finding in the design doc.")

    # ------------------------------------------------ exposure seconds -----
    print("\n" + "-" * 78)
    print("NAKED EXPOSURE -- the comparison that actually decides this")
    print("-" * 78)
    print("Under rev4 as written the PATCH fires AT ENTRY (LR:33724,")
    print("_place_scale_out_limit is called immediately after the entry fill is")
    print("booked, comment: 'rest the scale-out limit AT the target now'). So f is")
    print("carved out of the deadman from entry to terminal in EVERY case,")
    print("whether or not the bid ever reaches T.\n")
    total_naked = sum(c["life_s"] for c in cases)
    xpon = next(c for c in cases if c["symbol"] == "XPON")
    print(f"  rev4 resting: f carved in {len(cases)}/{len(cases)} cases")
    print(f"    total naked-seconds : {total_naked:>12,.1f} s  "
          f"({total_naked / 3600:.2f} h)")
    print(f"    mean per case       : {total_naked / len(cases):>12,.1f} s")
    print(f"    ex-XPON (outlier {xpon['life_s']:,.0f} s): "
          f"{total_naked - xpon['life_s']:,.1f} s over {len(cases) - 1} cases "
          f"= {(total_naked - xpon['life_s']) / (len(cases) - 1):,.1f} s mean")

    # deferred-PATCH marketable: only the window-A cases ever carve f out, and
    # only for PATCH RTT + sell RTT.
    patch_rtt_p95 = 0.26      # 200-254 ms measured PATCH probe on a status=new stop
    sell_rtt_p95 = 0.452      # place_rtt_s p95, n=137
    per_case = patch_rtt_p95 + sell_rtt_p95
    n_fire = len(win_a)
    print(f"\n  deferred-PATCH marketable: f carved in {n_fire}/{len(cases)} cases")
    print(f"    per case (p95 PATCH {patch_rtt_p95:.3f} s + p95 sell "
          f"{sell_rtt_p95:.3f} s): {per_case:.3f} s")
    print(f"    total naked-seconds : {n_fire * per_case:>12,.1f} s")
    print(f"\n  RATIO: {total_naked / (n_fire * per_case):,.0f}x reduction "
          f"in total naked exposure")
    print(f"  ex-XPON: {(total_naked - xpon['life_s']) / (n_fire * per_case):,.0f}x")
    print("\n  In 10 of 13 cases the deferred design NEVER TOUCHES THE STOP AT ALL.")
    print("  That is the property the resting design cannot have, because it")
    print("  commits to the carve-out at entry, before it knows whether the bid")
    print("  will ever reach the target.")

    # ----------------------------------------------------- disagreements ---
    print("\n" + "-" * 78)
    print("A NON-FINDING, DEMOTED")
    print("-" * 78)
    print("The first pass reported '0 of 13 disagreements between the limit's")
    print("fill model and the marketable trigger' as one of three mechanism")
    print("findings. It is an IDENTITY, not a measurement: both predicates are")
    print("literally `bid >= target`, so zero disagreements is guaranteed by")
    print("construction and would hold on any sample, including an empty one.")
    print("It is removed from the evidence list. The informative version -- how")
    print("often the ask or mid touched T without the bid following -- is a")
    print("DIFFERENT comparison against the mid-trigger set, and is reported in")
    print("the design doc as such.")
    print("\nDone.")


if __name__ == "__main__":
    main()
