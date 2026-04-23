"""Semantic code search: index symbols, multi-strategy search, LLM-powered queries."""
from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models.code_brain import CodeInsight, CodeRepo, CodeSearchEntry, CodeSnapshot
from . import indexer as indexer_mod
from .runtime import resolve_repo_runtime_path

logger = logging.getLogger(__name__)

_JS_FUNC_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)",
    re.MULTILINE,
)
_JS_CLASS_RE = re.compile(r"(?:export\s+)?class\s+(\w+)", re.MULTILINE)
_JS_ARROW_RE = re.compile(
    r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(([^)]*)\)\s*=>",
    re.MULTILINE,
)


def _index_python_symbols(file_path: str, repo_path: Path) -> List[Dict[str, Any]]:
    """Extract functions, classes, methods from a Python file using ast."""
    full = repo_path / file_path
    symbols: List[Dict[str, Any]] = []
    try:
        source = full.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=file_path)
    except Exception:
        return symbols

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            args = ", ".join(a.arg for a in node.args.args)
            sig = f"def {node.name}({args})"
            doc = ast.get_docstring(node) or ""
            parent = getattr(node, "_parent_class", None)
            symbols.append({
                "name": node.name,
                "type": "method" if parent else "function",
                "signature": sig,
                "docstring": doc[:500],
                "line": node.lineno,
            })
        elif isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node) or ""
            symbols.append({
                "name": node.name,
                "type": "class",
                "signature": f"class {node.name}",
                "docstring": doc[:500],
                "line": node.lineno,
            })
            for child in ast.walk(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not node:
                    child._parent_class = node.name  # type: ignore[attr-defined]

    return symbols


def _index_js_symbols(file_path: str, repo_path: Path) -> List[Dict[str, Any]]:
    """Extract functions and classes from a JS/TS file using regex."""
    full = repo_path / file_path
    symbols: List[Dict[str, Any]] = []
    try:
        source = full.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return symbols

    lines = source.splitlines()

    for m in _JS_FUNC_RE.finditer(source):
        line_num = source[:m.start()].count("\n") + 1
        symbols.append({
            "name": m.group(1),
            "type": "function",
            "signature": f"function {m.group(1)}({m.group(2)})",
            "docstring": "",
            "line": line_num,
        })

    for m in _JS_CLASS_RE.finditer(source):
        line_num = source[:m.start()].count("\n") + 1
        symbols.append({
            "name": m.group(1),
            "type": "class",
            "signature": f"class {m.group(1)}",
            "docstring": "",
            "line": line_num,
        })

    for m in _JS_ARROW_RE.finditer(source):
        line_num = source[:m.start()].count("\n") + 1
        symbols.append({
            "name": m.group(1),
            "type": "function",
            "signature": f"const {m.group(1)} = ({m.group(2)}) =>",
            "docstring": "",
            "line": line_num,
        })

    return symbols


def index_symbols(db: Session, repo_id: int) -> Dict[str, Any]:
    """Walk repo snapshots and index all function/class symbols."""
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}

    repo_path = resolve_repo_runtime_path(repo)
    if repo_path is None or not repo_path.is_dir():
        return {"error": "Registered workspace is not reachable from the current runtime."}

    db.query(CodeSearchEntry).filter(CodeSearchEntry.repo_id == repo_id).delete()

    snaps = db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo_id).all()
    entries: List[CodeSearchEntry] = []
    symbol_count = 0

    for snap in snaps:
        if snap.language == "python":
            syms = _index_python_symbols(snap.file_path, repo_path)
        elif snap.language in ("javascript", "typescript"):
            syms = _index_js_symbols(snap.file_path, repo_path)
        else:
            continue

        for s in syms:
            entries.append(CodeSearchEntry(
                repo_id=repo_id,
                file_path=snap.file_path,
                symbol_name=s["name"],
                symbol_type=s["type"],
                signature=s.get("signature"),
                docstring=s.get("docstring"),
                line_number=s.get("line", 0),
            ))
            symbol_count += 1

    db.bulk_save_objects(entries)
    db.commit()
    return {"indexed": symbol_count}


def search_code(
    db: Session,
    query: str,
    repo_id: Optional[int] = None,
    repo_ids: Optional[List[int]] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Multi-strategy code search: symbol name, file path, docstring/signature."""
    q_lower = query.lower().strip()
    if not q_lower:
        return []

    base_q = db.query(CodeSearchEntry)
    if repo_id is not None:
        base_q = base_q.filter(CodeSearchEntry.repo_id == repo_id)
    elif repo_ids is not None:
        if not repo_ids:
            return []
        base_q = base_q.filter(CodeSearchEntry.repo_id.in_(repo_ids))

    # Strategy 1: exact symbol name match
    exact = base_q.filter(CodeSearchEntry.symbol_name.ilike(f"%{q_lower}%")).limit(limit).all()

    # Strategy 2: file path match
    path_matches = base_q.filter(CodeSearchEntry.file_path.ilike(f"%{q_lower}%")).limit(limit // 2).all()

    # Strategy 3: signature/docstring keyword match
    sig_matches = base_q.filter(CodeSearchEntry.signature.ilike(f"%{q_lower}%")).limit(limit // 2).all()
    doc_matches = base_q.filter(CodeSearchEntry.docstring.ilike(f"%{q_lower}%")).limit(limit // 2).all()

    seen: set[int] = set()
    results: List[Dict[str, Any]] = []

    def _add(entry: CodeSearchEntry, score: float):
        if entry.id in seen:
            return
        seen.add(entry.id)
        results.append({
            "file": entry.file_path,
            "symbol": entry.symbol_name,
            "type": entry.symbol_type,
            "signature": entry.signature,
            "docstring": (entry.docstring or "")[:200],
            "line": entry.line_number,
            "score": round(score, 2),
        })

    for e in exact:
        _add(e, 1.0 if e.symbol_name.lower() == q_lower else 0.8)
    for e in path_matches:
        _add(e, 0.5)
    for e in sig_matches:
        _add(e, 0.4)
    for e in doc_matches:
        _add(e, 0.3)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def search_with_llm(
    db: Session,
    query: str,
    repo_id: Optional[int] = None,
    user_id: Optional[int] = None,
    repo_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Natural-language code search: gather search results + insights, ask LLM."""
    if repo_id is None and repo_ids is None and user_id is not None:
        repo_ids = indexer_mod.get_accessible_repo_ids(db, user_id=user_id, include_shared=True)

    code_results = search_code(db, query, repo_id=repo_id, repo_ids=repo_ids, limit=15)

    insight_q = db.query(CodeInsight).filter(CodeInsight.active.is_(True))
    if repo_id is not None:
        insight_q = insight_q.filter(CodeInsight.repo_id == repo_id)
    elif repo_ids is not None:
        if not repo_ids:
            insights = []
        else:
            insights = insight_q.filter(CodeInsight.repo_id.in_(repo_ids)).limit(10).all()
    else:
        insights = insight_q.limit(10).all()
    if repo_id is not None or repo_ids is None:
        insights = insight_q.limit(10).all()

    context_parts = [f"Search results for: {query}"]
    for r in code_results[:10]:
        context_parts.append(f"- {r['type']} {r['symbol']} in {r['file']}:{r['line']} -> {r['signature']}")
    if insights:
        context_parts.append("\nRepo insights:")
        for ins in insights[:5]:
            context_parts.append(f"- {ins.category}: {ins.description}")

    context = "\n".join(context_parts)

    try:
        from ..llm_caller import call_llm
        answer = call_llm(
            [
                {"role": "system", "content": "You are a code search assistant. Based on the indexed search results and repo insights provided, answer the user's question about the codebase. Be specific about file paths and line numbers."},
                {"role": "user", "content": f"Question: {query}\n\nContext:\n{context}"},
            ],
            max_tokens=600,
            trace_id="code-search",
        )
        if not answer:
            answer = "Could not generate LLM answer."
    except Exception as e:
        logger.warning("[search] LLM search failed: %s", e)
        answer = "Could not generate LLM answer."

    return {
        "query": query,
        "answer": answer,
        "results": code_results[:10],
    }
