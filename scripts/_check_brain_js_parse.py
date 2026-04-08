"""One-off: verify brain.html inline scripts parse as ES5 (esprima)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import esprima
except ImportError:
    print("pip install esprima", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    raw = (root / "app/templates/brain.html").read_text(encoding="utf-8")
    # Non-greedy: Jinja may contain `default({})` — do not use [^}]* here.
    s = re.sub(r"\{\{\s*trading_brain_desk_config\s*\|[\s\S]*?\}\}", "{}", raw)
    s = re.sub(r"\{\{\s*planner_task_id\s*\|[\s\S]*?\}\}", "null", s)
    s = re.sub(r"\{\{\s*planner_project_id\s*\|[\s\S]*?\}\}", "null", s)
    scripts = re.findall(r"<script>([\s\S]*?)</script>", s)
    print("script_blocks", len(scripts))
    for i, sc in enumerate(scripts):
        print("block", i, "chars", len(sc), end=" ")
        try:
            esprima.parseScript(sc)
            print("OK")
        except Exception as e:
            print("FAIL", e)
            if i == len(scripts) - 1 and "Line" in str(e):
                # Show source context for the main brain bundle
                lines = sc.splitlines()
                try:
                    import re as _re

                    m = _re.search(r"Line (\d+):", str(e))
                    ln = int(m.group(1)) if m else 1
                except Exception:
                    ln = 1
                lo, hi = max(0, ln - 5), min(len(lines), ln + 5)
                for j in range(lo, hi):
                    print(f"  {j+1}: {lines[j][:140]}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
