import json, sys
j = json.load(open(sys.argv[1], encoding="utf-8"))
r = j.get("result", {})
plan = r.get("plan") or r.get("design") or json.dumps(r)
dest = sys.argv[2] if len(sys.argv) > 2 else None
if dest:
    open(dest, "w", encoding="utf-8").write(plan)
    print("wrote", len(plan), "chars to", dest)
else:
    print(plan)
