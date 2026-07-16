"""June-only effective fee rates by liquidity/side from cached fills."""
import json
from collections import defaultdict

fills = json.load(open('scripts/_cx_cache/cb_fills.json'))
agg = defaultdict(lambda: [0, 0.0, 0.0])
for f in fills:
    day = (f.get('trade_time') or '')[:10]
    if day < '2026-06-01':
        continue
    size = float(f.get('size') or 0)
    price = float(f.get('price') or 0)
    siq = f.get('size_in_quote')
    siq = (siq is True) or (str(siq).lower() == 'true')
    notional = size if siq else size * price
    fee = float(f.get('commission') or 0)
    k = (f.get('liquidity_indicator'), f.get('side'))
    agg[k][0] += 1
    agg[k][1] += notional
    agg[k][2] += fee
for k, v in sorted(agg.items()):
    print(k, 'n=%d notional=%.2f fees=%.4f eff_bps=%.1f' % (v[0], v[1], v[2], v[2] / v[1] * 1e4 if v[1] else 0))
prods = defaultdict(int)
for f in fills:
    if (f.get('trade_time') or '')[:10] >= '2026-06-01':
        prods[f.get('product_id')] += 1
print(dict(sorted(prods.items(), key=lambda x: -x[1])))
# per-fill eff bps distribution June 7+ (current tier window)
rates = defaultdict(list)
for f in fills:
    if (f.get('trade_time') or '')[:10] < '2026-06-07':
        continue
    size = float(f.get('size') or 0); price = float(f.get('price') or 0)
    siq = f.get('size_in_quote'); siq = (siq is True) or (str(siq).lower() == 'true')
    notional = size if siq else size * price
    fee = float(f.get('commission') or 0)
    if notional > 0:
        rates[f.get('liquidity_indicator')].append(fee / notional * 1e4)
for k, v in rates.items():
    v = sorted(v)
    print('Jun7+ %s n=%d median=%.1fbps min=%.1f max=%.1f' % (k, len(v), v[len(v)//2], v[0], v[-1]))
