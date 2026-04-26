"""Detect trivial or vacuous test patterns in applied diffs (quality gate before pytest)."""
from __future__ import annotations

import ast
import fnmatch
import os
from pathlib import Path
from typing import Any


def _is_test_file_basename(name: str) -> bool:
    b = os.path.basename(name).replace("\\", "/")
    return bool(fnmatch.fnmatch(b, "test_*.py") or b.startswith("test_") and b.endswith(".py"))


def _name_chain(node: ast.AST) -> str | None:
    """E.g. pytest.mark.xfail from Attribute chain."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _name_chain(node.value)
        if inner is None:
            return None
        return f"{inner}.{node.attr}"
    return None


def _decorator_lacks_skip_xfail_reason(dec: ast.expr) -> bool:
    """True if this decorator is pytest.mark.skip / skipif / xfail without a reason (or equivalent)."""
    if isinstance(dec, ast.Call):
        name = _name_chain(dec.func) or ""
    elif isinstance(dec, ast.Attribute):
        name = _name_chain(dec) or ""
    else:
        return False

    short = (name or "").lower()
    if "xfail" not in short and "skip" not in short:
        return False
    if "skipif" in short:  # skipif(condition, reason=) — different contract; not required here
        return False

    if not isinstance(dec, ast.Call):
        return True
    for kw in dec.keywords:
        if kw.arg == "reason":
            return False
    if dec.args and isinstance(dec.args[0], ast.Constant):
        c = dec.args[0]
        if isinstance(c.value, str) and c.value:
            return False
    return True


def _trivial_one_stmt(st: ast.stmt) -> str | None:
    """If this single statement is a trivial / vacuous test body, return kind; else None."""
    if isinstance(st, ast.Pass):
        return "trivial_pass"
    if isinstance(st, ast.Return):
        if st.value is None:
            return "trivial_return"
        if isinstance(st.value, ast.Constant):
            return "trivial_return_literal"
        if isinstance(st.value, ast.Name) and st.value.id in ("True", "False", "None"):
            return "trivial_return_name"
    if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
        return "trivial_expr_literal"
    if isinstance(st, ast.Expr) and isinstance(st.value, ast.Name) and st.value.id in (
        "True",
        "False",
        "None",
    ):
        return "trivial_expr_name"
    if isinstance(st, ast.Assert):
        t = st.test
        if isinstance(t, ast.Constant) and t.value is True:
            return "trivial_assert_true"
        if isinstance(t, ast.Constant) and t.value in (1, 0) and type(t.value) is int:
            return "trivial_assert_numeric"
        if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not) and isinstance(
            t.operand, ast.Constant
        ):
            if t.operand.value is False:
                return "trivial_assert_not_false"
        if isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq):
            a, b = t.left, t.comparators[0] if t.comparators else None
            if (
                isinstance(a, ast.Name)
                and isinstance(b, ast.Name)
                and a.id == b.id
            ):
                return "trivial_assert_self_eq"
    return None


def _function_is_test_with_bad_skip(x: ast.AST) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    if not isinstance(x, ast.FunctionDef) or not x.name.startswith("test_"):
        return out
    for d in x.decorator_list:
        if _decorator_lacks_skip_xfail_reason(d):
            try:
                snippet = ast.unparse(d) if hasattr(ast, "unparse") else str(d)  # type: ignore[attr-defined]
            except Exception:
                snippet = ""
            if "xfail" in snippet.lower() or "skip" in snippet.lower():
                out.append(
                    (int(x.lineno), "skip_or_xfail_no_reason", snippet[:200] or "decorator")
                )
    return out


def _snippet_for_fn(fn: ast.FunctionDef) -> str:
    try:
        if hasattr(ast, "unparse"):
            return ast.unparse(fn)[:200]  # type: ignore[attr-defined]
    except Exception:
        pass
    return fn.name


def diff_adds_trivial_tests(diff_files: list[str], worktree_path: str) -> list[dict[str, Any]]:
    """Return suspicious findings; empty = OK."""
    root = Path(worktree_path)
    findings: list[dict[str, Any]] = []

    for rel in diff_files or []:
        if not _is_test_file_basename(rel):
            continue
        path = root / rel.replace("\\", "/").lstrip("/")
        if not path.is_file():
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue

        test_fns: list[ast.FunctionDef] = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
        ]
        for fn in test_fns:
            for line, kind, snip in _function_is_test_with_bad_skip(fn):
                findings.append(
                    {"file": str(rel), "line": line, "kind": kind, "snippet": snip}
                )

        trivial_stamps: list[bool] = []
        for fn in test_fns:
            triv = bool(len(fn.body) == 1 and _trivial_one_stmt(fn.body[0]))
            trivial_stamps.append(triv)

        if test_fns and all(trivial_stamps):
            findings.append(
                {
                    "file": str(rel),
                    "line": 1,
                    "kind": "module_all_trivial",
                    "snippet": f"module {os.path.basename(rel)}: all {len(test_fns)} test(s) vacuous",
                }
            )
        else:
            for fn, triv in zip(test_fns, trivial_stamps, strict=True):
                if triv and fn.name.startswith("test_"):
                    k = _trivial_one_stmt(fn.body[0]) or "trivial"
                    findings.append(
                        {
                            "file": str(rel),
                            "line": int(fn.lineno),
                            "kind": k,
                            "snippet": _snippet_for_fn(fn),
                        }
                    )
    return findings
