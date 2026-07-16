"""B7: quick symbol/episode census from the extracted episodes CSV."""
import csv
from collections import Counter

rows = list(csv.DictReader(open("scripts/_cx_cache/b7_episodes.csv")))
syms = Counter(r["symbol"] for r in rows)
print("episodes:", len(rows), "distinct symbols:", len(syms))
for s, n in syms.most_common():
    print(f"{s:>12} {n}")
