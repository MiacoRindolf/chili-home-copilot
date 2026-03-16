"""Architecture graph: parse imports, build dependency edges, detect cycles."""
from __future__ import annotations

import ast
import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models.code_brain import CodeDependency, CodeRepo, CodeSnapshot

logger = logging.getLogger(__name__)

_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+.*?\s+from\s+['"](.+?)['"]"""
    r"""|require\s*\(\s*['"](.+?)['"]\s*\)"""
    r"""|export\s+.*?\s+from\s+['"](.+?)['"])""",
    re.MULTILINE,
)


def _resolve_python_import(module_name: str, repo_path: Path, source_dir: Path) -> Optional[str]:
    """Try to resolve a dotted module name to a relative file path inside the repo."""
    parts = module_name.split(".")
    # Try as package path
    for ext in (".py", "/__init__.py"):
        candidate = repo_path / "/".join(parts)
        test = str(candidate) + ext
        if os.path.isfile(test):
            return str(Path(test).relative_to(repo_path)).replace("\\", "/")
    # Relative to source directory
    for ext in (".py", "/__init__.py"):
        candidate = source_dir / "/".join(parts)
        test = str(candidate) + ext
        if os.path.isfile(test):
            return str(Path(test).relative_to(repo_path)).replace("\\", "/")
    return None


def _parse_python_imports(file_path: str, repo_path: Path) -> List[Dict[str, str]]:
    """Extract import edges from a Python file using ast."""
    full = repo_path / file_path
    edges: List[Dict[str, str]] = []
    try:
        source = full.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=file_path)
    except Exception:
        return edges

    source_dir = full.parent

    for node in ast.walk(tree):
        module_name = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                target = _resolve_python_import(module_name, repo_path, source_dir)
                if target and target != file_path:
                    edges.append({"target": target, "import_name": module_name})
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                level = node.level or 0
                if level > 0:
                    rel_parts = list(source_dir.relative_to(repo_path).parts)
                    up = level - 1
                    if up < len(rel_parts):
                        base = "/".join(rel_parts[: len(rel_parts) - up])
                        module_name = base.replace("/", ".") + "." + node.module if node.module else base.replace("/", ".")
                    else:
                        module_name = node.module
                else:
                    module_name = node.module
                target = _resolve_python_import(module_name, repo_path, source_dir)
                if target and target != file_path:
                    edges.append({"target": target, "import_name": module_name})
    return edges


def _resolve_js_import(specifier: str, repo_path: Path, source_dir: Path) -> Optional[str]:
    """Resolve a JS/TS import specifier to a relative file path."""
    if not specifier.startswith("."):
        return None  # external package
    candidate = (source_dir / specifier).resolve()
    for ext in ("", ".js", ".ts", ".tsx", ".jsx", "/index.js", "/index.ts"):
        test = str(candidate) + ext
        if os.path.isfile(test):
            try:
                return str(Path(test).relative_to(repo_path)).replace("\\", "/")
            except ValueError:
                return None
    return None


def _parse_js_imports(file_path: str, repo_path: Path) -> List[Dict[str, str]]:
    """Extract import edges from a JS/TS file using regex."""
    full = repo_path / file_path
    edges: List[Dict[str, str]] = []
    try:
        source = full.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return edges

    source_dir = full.parent
    for m in _JS_IMPORT_RE.finditer(source):
        specifier = m.group(1) or m.group(2) or m.group(3)
        if not specifier:
            continue
        target = _resolve_js_import(specifier, repo_path, source_dir)
        if target and target != file_path:
            edges.append({"target": target, "import_name": specifier})
    return edges


def _find_cycles(adj: Dict[str, List[str]]) -> List[List[str]]:
    """DFS-based cycle detection. Returns list of cycles (each a list of file paths)."""
    visited: set[str] = set()
    on_stack: set[str] = set()
    cycles: List[List[str]] = []

    def dfs(node: str, path: List[str]):
        if node in on_stack:
            idx = path.index(node)
            cycles.append(path[idx:])
            return
        if node in visited:
            return
        visited.add(node)
        on_stack.add(node)
        for neighbor in adj.get(node, []):
            dfs(neighbor, path + [neighbor])
        on_stack.discard(node)

    for n in adj:
        if n not in visited:
            dfs(n, [n])
    return cycles


def build_dependency_graph(db: Session, repo_id: int) -> Dict[str, Any]:
    """Build the full import graph for a repo: parse imports, detect cycles, store edges."""
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}

    repo_path = Path(repo.path)
    if not repo_path.is_dir():
        return {"error": f"Path not found: {repo.path}"}

    snapshots = db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo_id).all()
    db.query(CodeDependency).filter(CodeDependency.repo_id == repo_id).delete()

    adj: Dict[str, List[str]] = defaultdict(list)
    edges: List[CodeDependency] = []
    edge_count = 0

    for snap in snapshots:
        if snap.language == "python":
            imports = _parse_python_imports(snap.file_path, repo_path)
        elif snap.language in ("javascript", "typescript"):
            imports = _parse_js_imports(snap.file_path, repo_path)
        else:
            continue

        for imp in imports:
            adj[snap.file_path].append(imp["target"])
            edges.append(CodeDependency(
                repo_id=repo_id,
                source_file=snap.file_path,
                target_file=imp["target"],
                import_name=imp.get("import_name"),
            ))
            edge_count += 1

    cycles = _find_cycles(adj)
    circular_files: set[str] = set()
    for cycle in cycles:
        circular_files.update(cycle)

    for edge in edges:
        if edge.source_file in circular_files and edge.target_file in circular_files:
            edge.is_circular = True

    db.bulk_save_objects(edges)
    db.commit()

    return {
        "edges": edge_count,
        "circular_count": len(cycles),
        "files_in_cycles": len(circular_files),
    }


def get_graph_data(db: Session, repo_id: int) -> Dict[str, Any]:
    """Return graph data for the UI: nodes, edges, stats."""
    deps = db.query(CodeDependency).filter(CodeDependency.repo_id == repo_id).all()
    if not deps:
        return {"nodes": [], "edges": [], "stats": {}}

    nodes_set: set[str] = set()
    edge_list = []
    in_degree: Dict[str, int] = defaultdict(int)
    out_degree: Dict[str, int] = defaultdict(int)
    circular_count = 0

    for d in deps:
        nodes_set.add(d.source_file)
        nodes_set.add(d.target_file)
        edge_list.append({
            "source": d.source_file,
            "target": d.target_file,
            "import_name": d.import_name,
            "is_circular": d.is_circular,
        })
        out_degree[d.source_file] += 1
        in_degree[d.target_file] += 1
        if d.is_circular:
            circular_count += 1

    # Coupling per directory
    dir_edges: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for d in deps:
        s_dir = str(Path(d.source_file).parent)
        t_dir = str(Path(d.target_file).parent)
        if s_dir != t_dir:
            dir_edges[s_dir][t_dir] += 1

    coupling = []
    for src_dir, targets in dir_edges.items():
        for tgt_dir, count in targets.items():
            coupling.append({"source_dir": src_dir, "target_dir": tgt_dir, "edge_count": count})
    coupling.sort(key=lambda x: x["edge_count"], reverse=True)

    most_depended = sorted(in_degree.items(), key=lambda x: x[1], reverse=True)[:10]
    most_dependent = sorted(out_degree.items(), key=lambda x: x[1], reverse=True)[:10]

    nodes = [{"file": f, "in": in_degree.get(f, 0), "out": out_degree.get(f, 0)} for f in nodes_set]

    return {
        "nodes": nodes,
        "edges": edge_list[:500],
        "stats": {
            "total_nodes": len(nodes_set),
            "total_edges": len(edge_list),
            "circular_edges": circular_count,
            "most_depended_on": [{"file": f, "count": c} for f, c in most_depended],
            "most_dependent": [{"file": f, "count": c} for f, c in most_dependent],
            "coupling": coupling[:20],
        },
    }
