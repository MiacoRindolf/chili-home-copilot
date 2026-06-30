"""Flag-set VERSION-DIFF harness for the momentum replay (STEP 1 of the version-agnostic backtest).

GOAL: run the SAME recorded day twice — version A vs version B, each a different env-flag-set —
and print the trade DIFFERENCE, so the operator sees exactly what a flag change does. This works on
the EXISTING replay with NO refactor: both runs use identical orchestration + identical OHLCV/tape +
an IDENTICAL pinned equity basis, so the replay's known unreliabilities (absolute $, the reimplemented
decision path, the live-equity basis) CANCEL in the DIFF, leaving only the flag effect.

WHY a pinned basis matters
--------------------------
run_replay sizes off the live account equity at run time (chili_replay_equity_basis_usd=0.0 ->
risk_policy._account_equity_usd). If A and B captured equity at two different instants they would size
differently for a reason that has nothing to do with the flags. So the harness captures the basis ONCE
(or takes --basis) and pins CHILI_REPLAY_EQUITY_BASIS_USD to that SAME value for both runs. The $ is
still only relative (the replay's $ are unreliable in absolute terms) — but it is the SAME basis on both
sides, so the Δ$ is a faithful relative signal. The run-R is the trustworthy magnitude signal.

ISOLATION
---------
Each replay runs in its OWN subprocess (conda run -n chili-env python scripts/_replay_v2.py <date>
--json), with its env = the parent env + the flagspec + the pinned basis. Subprocess isolation means
A's flags can never leak into B's process-level ``settings`` (settings is read once per process at
import). The subprocess prints the full result dict as JSON to stdout; the harness parses both.

USAGE
-----
    python scripts/_replay_version_diff.py YYYY-MM-DD \
        --a 'CHILI_MOMENTUM_REPLAY_ENGINE_ON=1' \
        --b 'CHILI_MOMENTUM_REPLAY_ENGINE_ON=0' \
        [--basis 22551] [--armed-source live] [--label-a vA --label-b vB] [--json-out]

A flagspec is a comma-separated KEY=VAL env list, e.g.
    'CHILI_MOMENTUM_REPLAY_ENGINE_ON=1,CHILI_MOMENTUM_REPLAY_FIDELITY_V2=0'.
Empty ('') is allowed (run with no extra flags = current HEAD defaults).

OHLCV NOTE: run_replay fetches OHLCV live (Massive), which 502s on weekends. The harness does not crash
on a failed/empty run — if either subprocess returns an error result (or no trades because the tape/OHLCV
is unavailable), the diff still prints and the VERDICT honestly reports the failure.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPLAY_CLI = os.path.join(_HERE, "_replay_v2.py")


# ── flagspec parsing ──────────────────────────────────────────────────────────
def parse_flagspec(spec: str) -> dict[str, str]:
    """'K1=V1,K2=V2' -> {'K1':'V1','K2':'V2'}. Empty/whitespace -> {}. Tolerant of
    stray spaces around keys/values; a bare token with no '=' is rejected loudly."""
    out: dict[str, str] = {}
    if not spec:
        return out
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "=" not in tok:
            raise SystemExit(f"bad flagspec token (no '='): {tok!r}")
        k, v = tok.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ── basis capture (pin equity ONCE so both runs size identically) ──────────────
def capture_basis_usd() -> float | None:
    """Capture the current live account equity ONCE via the SAME basis the replay would
    otherwise read per-run, so we can pin it for BOTH runs (deterministic + reproducible).
    Returns None if the broker equity is unavailable (e.g. no token / weekend) — the caller
    then runs WITHOUT pinning a basis (both runs still get the SAME unpinned fallback path,
    so the basis still cancels; it is just not reproducible across days)."""
    try:
        from app.services.trading.momentum_neural.risk_policy import _account_equity_usd
        from app.services.trading.execution_family_registry import (
            EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        )

        eq = _account_equity_usd(
            EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
            apply_margin_multiple=False,
            prefer_equity=True,
        )
        return float(eq) if eq and float(eq) > 0 else None
    except Exception:
        return None


# ── subprocess replay run ──────────────────────────────────────────────────────
def run_one(date: str, flags: dict[str, str], basis_usd: float | None,
            armed_source: str, label: str) -> dict:
    """Run the replay in an ISOLATED subprocess with env = parent + flags + pinned basis,
    persist OFF, --json. Returns the parsed result dict (with an injected '_diff_meta'). On
    any failure (non-JSON stdout, crash, OHLCV/tape down) returns a structured error dict so
    the diff never crashes."""
    env = dict(os.environ)
    # persist off is forced by --json in the CLI; pin the basis for both runs.
    if basis_usd is not None and basis_usd > 0:
        env["CHILI_REPLAY_EQUITY_BASIS_USD"] = repr(float(basis_usd))
    for k, v in flags.items():
        env[k] = v

    cmd = [sys.executable, _REPLAY_CLI, date, "--json", f"--armed-source={armed_source}"]
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return {"date": date, "error": "subprocess_timeout", "trades": [],
                "total_usd": 0.0, "wins": 0, "losses": 0, "_diff_meta": {"label": label}}
    stdout = (proc.stdout or "").strip()
    # The result JSON is the last non-empty stdout line (engine logs go to stderr/logging;
    # but be defensive — take the last line that parses as a dict).
    parsed: dict | None = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            cand = json.loads(line)
            if isinstance(cand, dict):
                parsed = cand
                break
        except Exception:
            continue
    if parsed is None:
        return {
            "date": date, "error": "no_parseable_result",
            "trades": [], "total_usd": 0.0, "wins": 0, "losses": 0,
            "_diff_meta": {"label": label, "returncode": proc.returncode,
                           "stderr_tail": (proc.stderr or "")[-800:],
                           "stdout_tail": stdout[-800:]},
        }
    parsed.setdefault("trades", [])
    parsed["_diff_meta"] = {"label": label, "returncode": proc.returncode,
                            "flags": flags, "basis_usd": basis_usd}
    return parsed


# ── ledger keying + numeric helpers ─────────────────────────────────────────────
def _name_set(result: dict) -> set[str]:
    return {str(t.get("sym")).upper() for t in result.get("trades", []) if t.get("sym")}


def _by_name(result: dict) -> dict[str, list[dict]]:
    """Group trades by symbol (a name can be entered more than once a day)."""
    out: dict[str, list[dict]] = {}
    for t in result.get("trades", []):
        out.setdefault(str(t.get("sym")).upper(), []).append(t)
    return out


def _sum_run_r(result: dict) -> float:
    s = 0.0
    for t in result.get("trades", []):
        rr = t.get("run_r")
        if isinstance(rr, (int, float)):
            s += float(rr)
    return s


def _fnum(x):
    return float(x) if isinstance(x, (int, float)) else None


def _fmt(x, spec="%.3f"):
    return (spec % x) if isinstance(x, (int, float)) else "—"


def _fmt_delta(a, b, spec="%+.3f"):
    """A vs B numeric delta (B - A); '—' when either side is missing."""
    fa, fb = _fnum(a), _fnum(b)
    if fa is None or fb is None:
        return "—"
    return spec % (fb - fa)


# ── report ──────────────────────────────────────────────────────────────────────
def print_report(date: str, ra: dict, rb: dict, la: str, lb: str,
                 basis_usd: float | None, armed_source: str) -> None:
    na, nb = _name_set(ra), _name_set(rb)
    a_only = sorted(na - nb)
    b_only = sorted(nb - na)
    both = sorted(na & nb)
    ba, bb = _by_name(ra), _by_name(rb)

    bar = "=" * 78
    print(bar)
    print(f"REPLAY VERSION-DIFF — {date}   (armed_source={armed_source})")
    print(f"  A [{la}]: flags={ra['_diff_meta'].get('flags') or '(defaults)'}")
    print(f"  B [{lb}]: flags={rb['_diff_meta'].get('flags') or '(defaults)'}")
    _basis_str = f"${basis_usd:,.0f} (pinned, identical both runs)" if basis_usd else \
        "UNPINNED (broker equity unavailable; both runs share the same fallback basis)"
    print(f"  equity basis: {_basis_str}")
    # surface a failed/empty run honestly
    for r, lbl in ((ra, la), (rb, lb)):
        if r.get("error"):
            print(f"  ⚠ run {lbl} ERROR: {r['error']}  "
                  f"(rc={r['_diff_meta'].get('returncode')})")
            _st = r["_diff_meta"].get("stderr_tail")
            if _st:
                print(f"     stderr tail: ...{_st.strip()[-300:]}")
    print(bar)

    # 1) NAME-SET diff (the selection/arming delta)
    print("\n— NAME-SET DIFF (selection / arming delta) —")
    print(f"  A-only ({len(a_only)}): {', '.join(a_only) or '(none)'}")
    print(f"  B-only ({len(b_only)}): {', '.join(b_only) or '(none)'}")
    print(f"  BOTH   ({len(both)}): {', '.join(both) or '(none)'}")

    # 2) SHARED names — per-name A-vs-B deltas
    if both:
        print("\n— SHARED NAMES (A vs B per-trade deltas) —")
        hdr = ("  %-6s %-5s | %-9s %-9s %-7s %-6s %-6s | %s" %
               ("SYM", "side", "entry", "exit", "qty", "runR", "ΔrunR", "why (A → B)"))
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for sym in both:
            # pair up trades index-wise; a name re-entered a different number of times
            # on each side is itself a signal (printed as the surplus rows).
            ta, tb = ba.get(sym, []), bb.get(sym, [])
            n = max(len(ta), len(tb))
            for i in range(n):
                a = ta[i] if i < len(ta) else None
                b = tb[i] if i < len(tb) else None

                def g(t, k):
                    return t.get(k) if t else None

                entry_s = "%s→%s" % (_fmt(g(a, "entry")), _fmt(g(b, "entry")))
                exit_s = "%s→%s" % (_fmt(g(a, "exit")), _fmt(g(b, "exit")))
                qty_s = "%s→%s" % (_fmt(g(a, "qty"), "%.0f"), _fmt(g(b, "qty"), "%.0f"))
                rr_s = "%s→%s" % (_fmt(g(a, "run_r"), "%.2f"), _fmt(g(b, "run_r"), "%.2f"))
                drr = _fmt_delta(g(a, "run_r"), g(b, "run_r"), "%+.2f")
                why_a = (g(a, "why") or "—")
                why_b = (g(b, "why") or "—")
                why_s = why_a if why_a == why_b else f"{why_a} → {why_b}"
                ent_ts = "%s→%s" % (g(a, "t") or "—", g(b, "t") or "—")
                tag = "" if (a and b) else ("  [A-only-leg]" if a and not b else "  [B-only-leg]")
                print("  %-6s %-5s | %-9s %-9s %-7s %-6s %-6s | %s%s" %
                      (sym, ent_ts, entry_s, exit_s, qty_s, rr_s, drr, why_s, tag))

    # 3) AGGREGATE
    def _agg(r):
        return (len(r.get("trades", [])), int(r.get("wins") or 0),
                int(r.get("losses") or 0), _sum_run_r(r), float(r.get("total_usd") or 0.0))
    na_, wa, la_, rra, usda = _agg(ra)
    nb_, wb, lb_, rrb, usdb = _agg(rb)
    print("\n— AGGREGATE (A vs B) —")
    print("  %-22s %12s %12s %12s" % ("metric", la, lb, "Δ (B−A)"))
    print("  %-22s %12d %12d %+12d" % ("trades", na_, nb_, nb_ - na_))
    print("  %-22s %12d %12d %+12d" % ("wins", wa, wb, wb - wa))
    print("  %-22s %12d %12d %+12d" % ("losses", la_, lb_, lb_ - la_))
    print("  %-22s %12.2f %12.2f %+12.2f" % ("total run-R (trust)", rra, rrb, rrb - rra))
    print("  %-22s %12.0f %12.0f %+12.0f" % ("total $ (RELATIVE only)", usda, usdb, usdb - usda))
    print("    note: $ are replay-RELATIVE (known-unreliable absolute); run-R is the trustworthy signal.")

    # 4) VERDICT (one line)
    print("\n— VERDICT —")
    print("  version %s vs %s: +%d entries, -%d entries, ΔrunR=%+.2f, Δ$ (relative)=%+.0f" %
          (lb, la, len(b_only), len(a_only), rrb - rra, usdb - usda))
    if ra.get("error") or rb.get("error"):
        print("  ⚠ a run errored (see above) — the diff above reflects whatever ledger was returned; "
              "re-run when Massive/OHLCV + the tape are available for a faithful comparison.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Flag-set version-diff harness for the momentum replay.")
    ap.add_argument("date", help="YYYY-MM-DD (the recorded day to replay)")
    ap.add_argument("--a", default="", help="flagspec A (comma-separated KEY=VAL env list)")
    ap.add_argument("--b", default="", help="flagspec B (comma-separated KEY=VAL env list)")
    ap.add_argument("--basis", type=float, default=None,
                    help="pin CHILI_REPLAY_EQUITY_BASIS_USD for BOTH runs (default: capture live equity once)")
    ap.add_argument("--armed-source", default="live", choices=["asof", "live", "full_pipeline"],
                    help="armed_source passed to run_replay (default: live)")
    ap.add_argument("--label-a", default="A", help="label for version A in the report")
    ap.add_argument("--label-b", default="B", help="label for version B in the report")
    ap.add_argument("--json-out", action="store_true",
                    help="also emit the machine-readable diff payload (both ledgers + name-set diff) as JSON")
    args = ap.parse_args()

    flags_a = parse_flagspec(args.a)
    flags_b = parse_flagspec(args.b)

    # pin the basis ONCE (or take --basis) so both runs size identically.
    basis_usd = args.basis
    if basis_usd is None:
        basis_usd = capture_basis_usd()
        if basis_usd:
            print(f"[basis] captured live equity ${basis_usd:,.2f} — pinning for both runs", file=sys.stderr)
        else:
            print("[basis] live equity unavailable — running UNPINNED (both runs share the same fallback)",
                  file=sys.stderr)

    print(f"[run A: {args.label_a}] flags={flags_a or '(defaults)'} ...", file=sys.stderr)
    ra = run_one(args.date, flags_a, basis_usd, args.armed_source, args.label_a)
    print(f"[run B: {args.label_b}] flags={flags_b or '(defaults)'} ...", file=sys.stderr)
    rb = run_one(args.date, flags_b, basis_usd, args.armed_source, args.label_b)

    print_report(args.date, ra, rb, args.label_a, args.label_b, basis_usd, args.armed_source)

    if args.json_out:
        na, nb = _name_set(ra), _name_set(rb)
        payload = {
            "date": args.date, "basis_usd": basis_usd, "armed_source": args.armed_source,
            "a": {"label": args.label_a, "flags": flags_a, "error": ra.get("error"),
                  "trades": ra.get("trades", []), "total_usd": ra.get("total_usd"),
                  "wins": ra.get("wins"), "losses": ra.get("losses"), "run_r_sum": _sum_run_r(ra)},
            "b": {"label": args.label_b, "flags": flags_b, "error": rb.get("error"),
                  "trades": rb.get("trades", []), "total_usd": rb.get("total_usd"),
                  "wins": rb.get("wins"), "losses": rb.get("losses"), "run_r_sum": _sum_run_r(rb)},
            "name_set_diff": {"a_only": sorted(na - nb), "b_only": sorted(nb - na),
                              "both": sorted(na & nb)},
        }
        print("\n=== JSON ===")
        print(json.dumps(payload, default=str))

    # exit 0 even on a single-run error (the diff is still informative); exit 2 only if BOTH errored.
    if ra.get("error") and rb.get("error"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
