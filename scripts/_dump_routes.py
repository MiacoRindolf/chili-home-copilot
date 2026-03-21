"""One-off: dump FastAPI routes for REFACTOR_AUDIT.md (delete after audit or keep)."""
import sys
sys.path.insert(0, ".")
from app.main import app

rows = []
for route in app.routes:
    if hasattr(route, "path"):
        if hasattr(route, "methods"):
            for m in route.methods:
                if m != "HEAD":
                    rows.append((m, route.path))
        else:
            rows.append(("WS", route.path))
rows.sort(key=lambda x: (x[1], x[0]))
print(f"TOTAL_ROUTES {len(rows)}")
for m, p in rows:
    print(f"{m}\t{p}")
