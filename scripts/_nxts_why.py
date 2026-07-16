import json
j = json.load(open(r"D:\CHILI-Docker\chili-data\replays\2026-06-22.json"))
a = [x for x in j.get("armed_timeline", []) if x.get("sym") == "NXTS"]
print("NXTS armed:", json.dumps(a)[:400])
d = [x for x in j.get("divergence", []) if x.get("sym") == "NXTS"]
print("NXTS divergence:", json.dumps(d)[:600])
tr = j.get("decision_trace", [])
print("trace len:", len(tr), "| sample entry:", json.dumps(tr[0]) if tr else None)
nxts = [e for e in tr if "NXTS" in json.dumps(e)]
print("NXTS trace events (%d):" % len(nxts))
for e in nxts:
    print("  ", json.dumps(e))
# also: distinct gate_fail reasons across ALL symbols (what blocks entries in general)
import collections, re
fails = collections.Counter()
for e in tr:
    s = json.dumps(e)
    m = re.search(r"gate_fail:([a-z_]+)", s)
    if m:
        fails[m.group(1)] += 1
print("ALL gate_fail reasons:", fails.most_common(15))
