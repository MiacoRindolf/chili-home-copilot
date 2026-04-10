"""Print CPU/RAM specs (host). Run: conda run -n chili-env python scripts/print_hw_specs.py"""
from __future__ import annotations

import os
import platform
import subprocess
import sys


def wmic(query: str) -> str | None:
    try:
        r = subprocess.run(
            ["wmic", *query.split()],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def main() -> None:
    lines: list[str] = []
    lines.append("=== Platform ===")
    lines.append(f"system: {platform.system()} {platform.release()}")
    lines.append(f"machine: {platform.machine()}")
    lines.append(f"python: {sys.version.split()[0]}")

    lines.append("")
    lines.append("=== Logical CPUs (Python / scheduling) ===")
    n = os.cpu_count()
    lines.append(f"os.cpu_count(): {n}")

    lines.append("")
    lines.append("=== WMIC (Windows CPU detail) ===")
    if platform.system() == "Windows":
        for label, q in [
            ("Cores (NumberOfCores)", "cpu get NumberOfCores /format:list"),
            ("Logical (NumberOfLogicalProcessors)", "cpu get NumberOfLogicalProcessors /format:list"),
            ("Model (Name)", "cpu get Name /format:list"),
        ]:
            out = wmic(q)
            lines.append(f"{label}:")
            lines.append(out or "  (unavailable)")
            lines.append("")
    else:
        lines.append("(skipped — not Windows)")

    lines.append("=== RAM ===")
    try:
        import psutil

        vm = psutil.virtual_memory()
        lines.append(f"psutil.virtual_memory().total: {vm.total / (1024**3):.2f} GB")
        lines.append(f"available: {vm.available / (1024**3):.2f} GB")
        lines.append(f"psutil.cpu_count(logical=True): {psutil.cpu_count(logical=True)}")
        lines.append(f"psutil.cpu_count(logical=False): {psutil.cpu_count(logical=False)}")
    except ImportError:
        lines.append("psutil not installed — install for RAM: pip install psutil")

    lines.append("")
    lines.append("=== Docker hint ===")
    lines.append(
        'If Docker shows "CPU 3200%" with 100% = 1 core, that implies 32 logical CPUs '
        f"(os.cpu_count={n})."
    )

    text = "\n".join(lines)
    print(text, flush=True)


if __name__ == "__main__":
    main()
