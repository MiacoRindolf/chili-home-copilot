"""Bit-identity-under-truncation probe for the #871 indicators.
Generates a long synthetic series, computes each indicator on the FULL series,
then recomputes on every truncated prefix df[:i+1] and compares full[i] vs trunc[-1].
Reports ANY divergence and its magnitude — pins which indicator is non-causal.
"""
import numpy as np, pandas as pd
from app.services.trading.indicator_core import compute_rsi, compute_ema
import ta, sys
print("ta version:", getattr(ta, "__version__", "?"), flush=True)

# deterministic long random-walk close (~9000 4h bars), crypto-like positive prices
rng = np.random.default_rng(12345)
n = 9000
steps = rng.normal(0, 1, n).cumsum()
close = pd.Series(15.0 + steps*0.05 + np.sin(np.arange(n)/50.0), name="Close")
close = close.clip(lower=0.5)

def probe(name, fn, full):
    worst = 0.0; worst_i = -1; count = 0
    # sample densely across the tail where deep-bar failures were seen
    idxs = list(range(200, n, 7))
    for i in idxs:
        tv = fn(close.iloc[:i+1]).iloc[-1]
        fv = full.iloc[i]
        if pd.isna(tv) and pd.isna(fv):
            continue
        if pd.isna(tv) or pd.isna(fv):
            d = float('inf')
        else:
            d = abs(float(tv) - float(fv))
        if d > 1e-4:
            count += 1
            if d > worst:
                worst, worst_i = d, i
    print(f"{name:10}: divergences>1e-4={count:5}/{len(idxs)}  worst={worst:.6g} @bar={worst_i}", flush=True)
    return count, worst_i

probe("rsi_14", lambda s: compute_rsi(s,14), compute_rsi(close,14))
for span in (20,50,100):
    probe(f"ema_{span}", (lambda sp: (lambda s: compute_ema(s,sp)))(span), compute_ema(close,span))
