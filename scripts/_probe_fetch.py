import pandas as pd
from app.services.trading.market_data import fetch_ohlcv_df as f
for sym in ["LINK-USD","ETH-USD"]:
    for period in ["max","5y","3y"]:
        try:
            df = f(sym, period=period, interval="4h")
            if df is None or df.empty:
                print(f"{sym} {period}: empty", flush=True); continue
            idx = pd.to_datetime(df.index)
            dmin = pd.Series(idx).diff().dropna().median()
            print(f"{sym} {period:4}: bars={len(df):6} span={idx[0]} -> {idx[-1]} median_delta={dmin}", flush=True)
        except Exception as e:
            print(f"{sym} {period}: ERR {e}", flush=True)
