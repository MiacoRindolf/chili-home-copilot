"""File-level analysis: complexity, function/class counts, naming conventions."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from ...models.code_brain import CodeRepo, CodeSnapshot

logger = logging.getLogger(__name__)

_PY_FUNC = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)
_PY_CLASS = re.compile(r"^\s*class\s+(\w+)\s*[:(]", re.MULTILINE)
_PY_IMPORT = re.compile(r"^\s*(?:from\s+\S+\s+)?import\s+", re.MULTILINE)

_JS_FUNC = re.compile(
    r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(?|(\w+)\s*\([^)]*\)\s*\{)",
    re.MULTILINE,
)
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?class\s+(\w+)", re.MULTILINE)

_INDENT_RE = re.compile(r"^(\s+)\S", re.MULTILINE)


def _estimate_complexity(lines: List[str], language: Optional[str]) -> float:
    """Rough cyclomatic complexity estimate using branching keywords."""
    branch_kw = {"if", "elif", "else", "for", "while", "except", "catch",
                 "case", "switch", "&&", "||", "?"}
    score = 1
    for line in lines:
        stripped = line.strip()
        tokens = stripped.split()
        for kw in branch_kw:
            if kw in tokens or kw in stripped:
                score += 1
                break
    return min(score / max(len(lines), 1) * 100, 100.0)


def _detect_naming_convention(names: List[str]) -> str:
    snake = sum(1 for n in names if "_" in n and n == n.lower())
    camel = sum(1 for n in names if not "_" in n and n[0].islower() and any(c.isupper() for c in n[1:])) if names else 0
    pascal = sum(1 for n in names if not "_" in n and n[0].isupper()) if names else 0
    total = len(names) or 1
    if snake / total > 0.6:
        return "snake_case"
    if camel / total > 0.4:
        return "camelCase"
    if pascal / total > 0.4:
        return "PascalCase"
    return "mixed"


def _detect_indent_style(content: str) -> str:
    indents = _INDENT_RE.findall(content)
    if not indents:
        return "unknown"
    tabs = sum(1 for i in indents if "\t" in i)
    spaces = len(indents) - tabs
    if tabs > spaces:
        return "tabs"
    widths = [len(i) for i in indents if " " in i and "\t" not in i]
    if widths:
        avg = sum(widths) / len(widths)
        return f"{round(avg)}spaces"
    return "spaces"


def analyze_file(file_path: str, language: Optional[str] = None) -> Dict:
    """Analyze a single file. Returns metrics dict."""
    p = Path(file_path)
    if not p.is_file():
        return {"error": f"Not a file: {file_path}"}

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": str(e)}

    lines = content.splitlines()
    func_names: List[str] = []
    class_names: List[str] = []

    if language in ("python",):
        func_names = _PY_FUNC.findall(content)
        class_names = _PY_CLASS.findall(content)
    elif language in ("javascript", "typescript"):
        raw = _JS_FUNC.findall(content)
        func_names = [n for groups in raw for n in groups if n]
        class_names = _JS_CLASS.findall(content)

    complexity = _estimate_complexity(lines, language)
    naming = _detect_naming_convention(func_names + class_names)
    indent = _detect_indent_style(content)

    blank_lines = sum(1 for l in lines if not l.strip())
    comment_lines = 0
    for l in lines:
        s = l.strip()
        if language in ("python",) and s.startswith("#"):
            comment_lines += 1
        elif language in ("javascript", "typescript") and (s.startswith("//") or s.startswith("/*")):
            comment_lines += 1

    return {
        "line_count": len(lines),
        "blank_lines": blank_lines,
        "comment_lines": comment_lines,
        "function_count": len(func_names),
        "class_count": len(class_names),
        "complexity_score": round(complexity, 2),
        "naming_convention": naming,
        "indent_style": indent,
        "function_names": func_names[:50],
        "class_names": class_names[:20],
    }


def analyze_repo_files(db: Session, repo_id: int) -> Dict:
    """Run analysis on all indexed snapshots for a repo, updating their metrics."""
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}

    snapshots = db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo_id).all()
    total_complexity = 0.0
    analyzed = 0
    all_func_names: List[str] = []
    all_class_names: List[str] = []

    for snap in snapshots:
        full_path = Path(repo.path) / snap.file_path
        if not full_path.is_file():
            continue

        result = analyze_file(str(full_path), snap.language)
        if "error" in result:
            continue

        snap.function_count = result["function_count"]
        snap.class_count = result["class_count"]
        snap.complexity_score = result["complexity_score"]
        snap.line_count = result["line_count"]

        total_complexity += result["complexity_score"]
        analyzed += 1
        all_func_names.extend(result.get("function_names", []))
        all_class_names.extend(result.get("class_names", []))

    db.commit()

    avg_complexity = total_complexity / analyzed if analyzed else 0

    return {
        "analyzed": analyzed,
        "avg_complexity": round(avg_complexity, 2),
        "naming_convention": _detect_naming_convention(all_func_names + all_class_names),
        "total_functions": len(all_func_names),
        "total_classes": len(all_class_names),
    }
