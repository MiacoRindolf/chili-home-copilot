#!/usr/bin/env python
"""Honest head-to-head: does CHILI's harness beat the bare frontier model?

This is the *opposite* of a self-graded scorecard. Every task has an objective,
hidden verifier (a real pytest run). A task is "passed" only when the model's
edit makes the tests go green — no model grades itself.

It measures two independent effects:

  * **Model lift**  — `bare-frontier` vs `bare-local`: same one-shot protocol,
    different base model (gpt-5.5 vs Llama-3.3-70B). Shows what the frontier
    *brain* buys you.
  * **Harness lift** — `harness-*` vs `bare-*`: same model, but the harness arm
    runs CHILI's core edit loop mechanism (generate → apply → run tests → feed
    the failure back → regenerate, up to N iterations). Shows what CHILI's
    *test-repair loop* buys you on top of the bare model.

The interesting claim — "CHILI can beat a frontier model used bare on this repo"
— is `harness-frontier` > `bare-frontier`. If the repair loop turns some
one-shot failures into passes, that delta is real and objective.

This intentionally re-implements a *minimal* version of the harness repair loop
rather than driving the full DB/worktree-coupled production loop, so the eval is
self-contained and reproducible. The repair loop is the mechanism that creates
the edge; this isolates it honestly.

Usage:
    # zero-cost self-test of the harness mechanics (no API calls):
    python scripts/frontier_headtohead.py --mock

    # real run (uses your OpenAI + Groq keys from .env; costs a few $ of gpt-5.5):
    python scripts/frontier_headtohead.py

    # pick arms / tasks / iterations:
    python scripts/frontier_headtohead.py --arms bare-frontier,harness-frontier --max-iters 3 --json
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_HEADTOHEAD.md"

# ── Objective tasks: subtle bugs, hidden pytest verifier, no infinite loops ──
# `fixed` is used ONLY by --mock; real models never see it.
TASKS: list[dict] = [
    {
        "id": "roman_numerals",
        "buggy": '''\
def roman_to_int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for ch in s:
        total += vals[ch]
    return total
''',
        "fixed": '''\
def roman_to_int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for i, ch in enumerate(s):
        if i + 1 < len(s) and vals[ch] < vals[s[i + 1]]:
            total -= vals[ch]
        else:
            total += vals[ch]
    return total
''',
        "test": '''\
from mod import roman_to_int

def test_basic():
    assert roman_to_int("III") == 3
    assert roman_to_int("LVIII") == 58

def test_subtractive():
    assert roman_to_int("IV") == 4
    assert roman_to_int("IX") == 9
    assert roman_to_int("MCMXCIV") == 1994
''',
    },
    {
        "id": "merge_intervals",
        "buggy": '''\
def merge_intervals(intervals):
    if not intervals:
        return []
    intervals = sorted(intervals)
    out = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s < out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out
''',
        "fixed": '''\
def merge_intervals(intervals):
    if not intervals:
        return []
    intervals = sorted(intervals)
    out = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out
''',
        "test": '''\
from mod import merge_intervals

def test_overlap():
    assert merge_intervals([[1, 3], [2, 6], [8, 10]]) == [[1, 6], [8, 10]]

def test_touching():
    assert merge_intervals([[1, 4], [4, 5]]) == [[1, 5]]

def test_empty():
    assert merge_intervals([]) == []
''',
    },
    {
        "id": "parse_duration",
        "buggy": '''\
import re

def parse_duration(s):
    total = 0
    m = re.match(r"(\\d+)h", s)
    if m:
        total += int(m.group(1)) * 3600
    return total
''',
        "fixed": '''\
import re

def parse_duration(s):
    total = 0
    for value, unit in re.findall(r"(\\d+)([hms])", s):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total
''',
        "test": '''\
from mod import parse_duration

def test_full():
    assert parse_duration("1h30m15s") == 5415

def test_partial():
    assert parse_duration("45m") == 2700
    assert parse_duration("90s") == 90

def test_hours():
    assert parse_duration("2h") == 7200
''',
    },
    {
        "id": "dedupe_order",
        "buggy": '''\
def dedupe(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            out.append(x)
        seen.add(x)
    return sorted(out)
''',
        "fixed": '''\
def dedupe(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            out.append(x)
        seen.add(x)
    return out
''',
        "test": '''\
from mod import dedupe

def test_order():
    assert dedupe([3, 1, 2, 1, 3]) == [3, 1, 2]

def test_strings():
    assert dedupe(["b", "a", "b", "c"]) == ["b", "a", "c"]
''',
    },
]

SYSTEM = (
    "You are an expert software engineer. Fix the bug so every test passes. "
    "Do not modify the tests. Output ONLY the complete corrected contents of "
    "mod.py inside a single ```python code block."
)

ARMS: dict[str, dict] = {
    "bare-frontier": {"model": "gpt-5.5", "base_url": "https://api.openai.com/v1",
                      "key_env": ["PAID_OPENAI_API_KEY", "OPENAI_API_KEY"], "mode": "oneshot"},
    "harness-frontier": {"model": "gpt-5.5", "base_url": "https://api.openai.com/v1",
                         "key_env": ["PAID_OPENAI_API_KEY", "OPENAI_API_KEY"], "mode": "harness"},
    "bare-local": {"model": "llama-3.3-70b-versatile", "base_url": "https://api.groq.com/openai/v1",
                   "key_env": ["LLM_API_KEY"], "mode": "oneshot"},
    "harness-local": {"model": "llama-3.3-70b-versatile", "base_url": "https://api.groq.com/openai/v1",
                      "key_env": ["LLM_API_KEY"], "mode": "harness"},
}
DEFAULT_ARMS = ["bare-frontier", "harness-frontier", "bare-local", "harness-local"]


def _load_env_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    env_path = REPO_ROOT / ".env"
    if env_path.is_file():
        for line in io.open(env_path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip().strip('"').strip("'")
    return keys


def _resolve_key(arm: dict, env_keys: dict[str, str]) -> str:
    for name in arm["key_env"]:
        if env_keys.get(name):
            return env_keys[name]
    return ""


def _extract_code(reply: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", reply, re.DOTALL)
    return (m.group(1) if m else reply).strip() + "\n"


def _run_pytest(workdir: Path) -> tuple[bool, str]:
    # Pin rootdir to the temp dir (a local pytest.ini is written by the caller)
    # and run from inside it, so pytest never walks up and scans the drive root
    # (a Windows PermissionError on C:\Documents and Settings otherwise).
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "--rootdir", str(workdir), "test_mod.py"],
        cwd=str(workdir), capture_output=True, text=True, timeout=90,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def _real_call(arm: dict, key: str, messages: list[dict], max_tokens: int) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=arm["base_url"], timeout=120, max_retries=2)
    model = arm["model"]
    # Mirror CHILI's param handling: gpt-5*/o-series reject custom temperature;
    # openai.com uses max_completion_tokens, others use max_tokens.
    kwargs: dict = {"model": model, "messages": messages}
    if not (model.startswith("gpt-5") or model.startswith(("o1", "o3", "o4"))):
        kwargs["temperature"] = 0
    if "openai.com" in arm["base_url"]:
        kwargs["max_completion_tokens"] = max_tokens
    else:
        kwargs["max_tokens"] = max_tokens
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _make_caller(mock: bool, env_keys: dict[str, str]) -> Callable:
    def call(arm: dict, task: dict, messages: list[dict], attempt: int, max_tokens: int) -> str:
        if mock:
            # Deterministic: first attempt stays buggy (tests fail), repair fixes it.
            body = task["buggy"] if attempt == 0 else task["fixed"]
            return f"```python\n{body}```"
        key = _resolve_key(arm, env_keys)
        if not key:
            raise RuntimeError(f"no API key for arm {arm['model']} (tried {arm['key_env']})")
        return _real_call(arm, key, messages, max_tokens)

    return call


def run_arm(arm_label: str, arm: dict, task: dict, caller: Callable, max_iters: int,
            max_tokens: int) -> dict:
    iters = max_iters if arm["mode"] == "harness" else 1
    user = (
        f"File `mod.py`:\n```python\n{task['buggy']}```\n\n"
        f"Test `test_mod.py` (do not modify):\n```python\n{task['test']}```\n\n"
        "The tests currently fail. Return the COMPLETE corrected `mod.py` as one ```python block."
    )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    t0 = time.time()
    last_output = ""
    with tempfile.TemporaryDirectory(prefix="h2h-") as d:
        workdir = Path(d)
        (workdir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (workdir / "test_mod.py").write_text(task["test"], encoding="utf-8")
        for attempt in range(iters):
            try:
                reply = caller(arm, task, messages, attempt, max_tokens)
            except Exception as e:  # noqa: BLE001
                return {"passed": False, "attempts": attempt, "error": str(e)[:200],
                        "seconds": round(time.time() - t0, 1)}
            code = _extract_code(reply)
            (workdir / "mod.py").write_text(code, encoding="utf-8")
            passed, last_output = _run_pytest(workdir)
            if passed:
                return {"passed": True, "attempts": attempt + 1, "error": "",
                        "seconds": round(time.time() - t0, 1)}
            if arm["mode"] == "harness" and attempt + 1 < iters:
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content":
                                 f"The tests still fail:\n```\n{last_output}\n```\n"
                                 "Return the corrected COMPLETE `mod.py` as one ```python block."})
    return {"passed": False, "attempts": iters, "error": "", "seconds": round(time.time() - t0, 1)}


def render(results: dict, arms: list[str], tasks: list[str], mock: bool) -> str:
    lines = [
        "# CHILI Frontier Head-to-Head",
        "",
        f"- Mode: {'MOCK (no API calls)' if mock else 'LIVE'}",
        f"- Arms: {', '.join(arms)}",
        f"- Tasks: {len(tasks)} (objective pytest verifier each)",
        "",
        "| Task | " + " | ".join(arms) + " |",
        "| --- | " + " | ".join("---" for _ in arms) + " |",
    ]
    for tid in tasks:
        row = [tid]
        for a in arms:
            r = results[a][tid]
            if r.get("error"):
                row.append(f"ERR")
            else:
                mark = "PASS" if r["passed"] else "fail"
                row.append(f"{mark} (n={r['attempts']}, {r['seconds']}s)")
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## Pass rate", ""]
    lines.append("| Arm | Passed | Rate |")
    lines.append("| --- | --- | --- |")
    for a in arms:
        p = sum(1 for tid in tasks if results[a][tid].get("passed"))
        lines.append(f"| {a} | {p}/{len(tasks)} | {round(100 * p / len(tasks))}% |")
    lines += [
        "",
        "## How to read this",
        "",
        "- **Model lift** = `bare-frontier` − `bare-local` (same one-shot protocol, "
        "different base model).",
        "- **Harness lift** = `harness-*` − `bare-*` (same model; the harness arm runs "
        "the generate→test→repair loop).",
        "- **The headline claim** (\"CHILI beats the bare frontier model on this repo\") "
        "holds iff `harness-frontier` > `bare-frontier`.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arms", default=",".join(DEFAULT_ARMS),
                    help="comma-separated subset of: " + ", ".join(ARMS))
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    ap.add_argument("--max-iters", type=int, default=3, help="repair iterations for harness arms")
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--mock", action="store_true", help="self-test mechanics with no API calls")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    arms = [a.strip() for a in args.arms.split(",") if a.strip() in ARMS]
    if not arms:
        print("no valid arms", file=sys.stderr)
        return 2
    task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()] or [t["id"] for t in TASKS]
    tasks_by_id = {t["id"]: t for t in TASKS}
    env_keys = _load_env_keys()
    caller = _make_caller(args.mock, env_keys)

    results: dict[str, dict] = {a: {} for a in arms}
    for a in arms:
        for tid in task_ids:
            r = run_arm(a, ARMS[a], tasks_by_id[tid], caller, args.max_iters, args.max_tokens)
            results[a][tid] = r
            status = "PASS" if r["passed"] else ("ERR:" + r["error"] if r.get("error") else "fail")
            print(f"[{a:18}] {tid:16} {status} (n={r['attempts']}, {r['seconds']}s)")

    markdown = render(results, arms, task_ids, args.mock)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print("\n" + markdown)
    if args.json:
        print(json.dumps(results, indent=2))
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
