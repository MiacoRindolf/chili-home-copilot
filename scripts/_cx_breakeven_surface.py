"""_cx_breakeven_surface.py — analytic fee-aware break-even surface for the crypto lane.

Model (matches momentum_neural exit engine):
  stop distance s (fraction), first target at T*s (T = reward:risk),
  loss: -s on full size; win: partial f @ T*s, balance -> BE ratchet + trail.
  Runner conditional on win: prob q ends at BE (0R), else exits ~T*s (trail floor).
  Fees: entry fee on notional, exit fee on exit notional (taker both ways, or
  maker entry + taker exit).

Outputs:
  1. expectancy per trade (% of notional) across stop x target x winrate x fee config
  2. break-even stop size s* per (winrate, T, fee config)
  3. min 1m/5m ATR%% floor implied by s* via the engine's stop_atr_mult=0.60
     (stop = max(0.3%, 0.60 x ATR%) -> ATR_floor = s*/0.60)
"""
from __future__ import annotations

F = 0.5      # scale_out_fraction (engine default)
Q = 0.5      # P(runner returns to BE | first target hit) — conservative midpoint

# (label, entry_bps, exit_bps)
CONFIGS = [
    ("taker/taker intro 120", 120.0, 120.0),
    ("taker/taker stated 60", 60.0, 60.0),
    ("taker/taker actual 50", 50.0, 50.0),
    ("maker-entry actual 25/50", 25.0, 50.0),
    ("maker/maker actual 25/25", 25.0, 25.0),
    ("taker/taker adv2 35", 35.0, 35.0),
    ("maker-entry adv2 15/35", 15.0, 35.0),
]


def expectancy(p: float, s: float, T: float, fe: float, fx: float,
               ladder: bool = True) -> float:
    """E[net return as fraction of entry notional]."""
    fe /= 1e4
    fx /= 1e4
    if ladder:
        # win: f exits at +T*s, runner (1-f): q -> BE(0), (1-q) -> +T*s (trail ~floor)
        win_gross = F * T * s + (1 - F) * (1 - Q) * T * s
        win_exit_notional = F * (1 + T * s) + (1 - F) * (Q * 1.0 + (1 - Q) * (1 + T * s))
    else:
        win_gross = T * s
        win_exit_notional = 1 + T * s
    loss_gross = -s
    loss_exit_notional = 1 - s
    win_net = win_gross - fe - fx * win_exit_notional
    loss_net = loss_gross - fe - fx * loss_exit_notional
    return p * win_net + (1 - p) * loss_net


def breakeven_stop(p: float, T: float, fe: float, fx: float, ladder=True) -> float | None:
    lo, hi = 1e-5, 0.5
    if expectancy(p, hi, T, fe, fx, ladder) <= 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if expectancy(p, mid, T, fe, fx, ladder) > 0:
            hi = mid
        else:
            lo = mid
    return hi


def main():
    print(f"ladder model: partial f={F} @ first target, runner BE-prob q={Q}, trail~target floor")
    print("effective avg win (R units): "
          f"T=2 -> {F*2 + (1-F)*(1-Q)*2:.2f}R, T=3 -> {F*3 + (1-F)*(1-Q)*3:.2f}R\n")

    print("=== EXPECTANCY (% of notional per trade) — stops x targets x fee configs ===")
    for p in (0.40, 0.45, 0.50):
        print(f"\n-- win rate {p*100:.0f}% (win = first target hit) --")
        hdr = f"{'config':28}" + "".join(f"  s={s*100:.1f}T{T:.0f}" for s in (0.012, 0.02, 0.03) for T in (2.0, 3.0))
        print(hdr)
        for label, fe, fx in CONFIGS:
            row = f"{label:28}"
            for s in (0.012, 0.02, 0.03):
                for T in (2.0, 3.0):
                    e = expectancy(p, s, T, fe, fx)
                    row += f"  {e*100:+6.2f}"
            print(row + "   (% per trade)")

    print("\n=== BREAK-EVEN STOP SIZE s* (%%) and implied ATR floor (s*/0.60) ===")
    print(f"{'config':28} {'p':>4} {'T':>3} {'s*':>7} {'ATR_floor':>10}")
    for label, fe, fx in CONFIGS:
        for p in (0.40, 0.45, 0.50):
            for T in (2.0, 3.0):
                s_star = breakeven_stop(p, T, fe, fx)
                if s_star is None:
                    print(f"{label:28} {p:.2f} {T:.0f}R  NEVER       -")
                else:
                    print(f"{label:28} {p:.2f} {T:.0f}R {s_star*100:6.2f}% {s_star/0.60*100:9.2f}%")

    print("\n=== same, simple 2-outcome model (no ladder; win = full T*s) ===")
    print(f"{'config':28} {'p':>4} {'T':>3} {'s*':>7} {'ATR_floor':>10}")
    for label, fe, fx in CONFIGS:
        for p in (0.40, 0.45, 0.50):
            for T in (2.0, 3.0):
                s_star = breakeven_stop(p, T, fe, fx, ladder=False)
                if s_star is None:
                    print(f"{label:28} {p:.2f} {T:.0f}R  NEVER       -")
                else:
                    print(f"{label:28} {p:.2f} {T:.0f}R {s_star*100:6.2f}% {s_star/0.60*100:9.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
