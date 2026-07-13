"""Code Agent: analyze request -> gather context -> plan -> edit -> validate.

Uses a two-step LLM flow (plan-then-edit) so every diff is generated with
the real file contents in context, eliminating placeholder/hallucinated code.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...config import settings
from ...models.code_brain import CodeHotspot, CodeInsight, CodeRepo, CodeSnapshot
from . import insights as insights_mod
from .indexer import get_accessible_repo_ids, get_accessible_repos
from .runtime import resolve_repo_runtime_path
from .search import search_code

logger = logging.getLogger(__name__)

_MAX_FILE_LINES = 600
_MAX_FILES_PER_EDIT = 8
_MAX_EDITOR_HANDOFF_CHARS = 12_000
_MUTATING_PLAN_ACTIONS = frozenset({"modify", "create"})


def _snapshots_by_repo(db: Session, repo_ids: list[int]) -> dict[int, list[CodeSnapshot]]:
    if not repo_ids:
        return {}
    rows = db.query(CodeSnapshot).filter(CodeSnapshot.repo_id.in_(repo_ids)).all()
    grouped: dict[int, list[CodeSnapshot]] = {repo_id: [] for repo_id in repo_ids}
    for row in rows:
        grouped.setdefault(int(row.repo_id), []).append(row)
    return grouped


def _gather_context(
    db: Session,
    repo_id: Optional[int],
    prompt: str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Build rich context from the Code Brain for the LLM."""
    context: Dict[str, Any] = {
        "repos": [],
        "insights": [],
        "hotspots": [],
        "relevant_files": [],
    }

    if repo_id:
        repos = []
        for repo in get_accessible_repos(db, user_id=user_id, include_shared=True):
            if int(repo.id) == int(repo_id):
                repos = [repo]
                break
    else:
        repos = get_accessible_repos(db, user_id=user_id, include_shared=True)

    for repo in repos:
        lang_stats = json.loads(repo.language_stats) if repo.language_stats else {}
        context["repos"].append({
            "id": repo.id,
            "name": repo.name,
            "path": repo.host_path or repo.path,
            "runtime_path": str(resolve_repo_runtime_path(repo) or ""),
            "file_count": repo.file_count,
            "total_lines": repo.total_lines,
            "languages": lang_stats,
            "frameworks": repo.framework_tags.split(",") if repo.framework_tags else [],
        })

    repo_ids = [int(repo.id) for repo in repos]
    all_insights = insights_mod.get_insights(db, repo_id=repo_id, repo_ids=repo_ids if repo_id is None else None)
    context["insights"] = all_insights[:30]

    for repo in repos:
        hotspots = (
            db.query(CodeHotspot)
            .filter(CodeHotspot.repo_id == repo.id)
            .order_by(CodeHotspot.combined_score.desc())
            .limit(10)
            .all()
        )
        for h in hotspots:
            context["hotspots"].append({
                "file": h.file_path,
                "repo": repo.name,
                "churn": round(h.churn_score, 3),
                "complexity": round(h.complexity_score, 3),
            })

    # Use semantic search for relevance instead of naive path-word matching
    for repo in repos:
        try:
            results = search_code(db, prompt, repo_id=repo.id, limit=20)
            seen: set[str] = set()
            for r in results:
                fp = r["file"]
                if fp in seen:
                    continue
                seen.add(fp)
                context["relevant_files"].append({
                    "file": fp,
                    "repo": repo.name,
                    "repo_path": str(resolve_repo_runtime_path(repo) or repo.host_path or repo.path),
                    "language": None,
                    "lines": 0,
                    "complexity": 0,
                    "relevance": r.get("score", 0.5),
                    "symbol": r.get("symbol", ""),
                })
        except Exception:
            pass

    # Fallback: if search returned nothing, use path-word matching
    if not context["relevant_files"]:
        prompt_lower = prompt.lower()
        snapshots_by_repo = _snapshots_by_repo(db, repo_ids)
        for repo in repos:
            for snap in snapshots_by_repo.get(int(repo.id), []):
                file_lower = snap.file_path.lower()
                relevance = sum(1 for word in prompt_lower.split() if len(word) > 2 and word in file_lower)
                if relevance > 0:
                    context["relevant_files"].append({
                        "file": snap.file_path,
                        "repo": repo.name,
                        "repo_path": str(resolve_repo_runtime_path(repo) or repo.host_path or repo.path),
                        "language": snap.language,
                        "lines": snap.line_count,
                        "complexity": snap.complexity_score,
                        "relevance": relevance,
                    })
        context["relevant_files"].sort(key=lambda x: -x["relevance"])
        context["relevant_files"] = context["relevant_files"][:20]

    return context


def _build_plan_prompt(context: Dict[str, Any]) -> str:
    """System prompt for Step 1: produce a structured plan only, no diffs."""
    parts = [
        "You are Chili Code Agent, an expert software engineer with deep knowledge of the user's codebases.",
        "You have been continuously learning the user's projects.",
        "",
    ]

    if context["repos"]:
        parts.append("## Registered Repositories")
        for r in context["repos"]:
            langs = ", ".join(f"{k}: {v}" for k, v in sorted(r["languages"].items(), key=lambda x: -x[1])[:5])
            fws = ", ".join(r["frameworks"]) if r["frameworks"] else "none detected"
            parts.append(f"- **{r['name']}** ({r['path']}): {r['file_count']} files, {r['total_lines']} lines | Languages: {langs} | Frameworks: {fws}")
        parts.append("")

    if context["insights"]:
        parts.append("## Discovered Patterns & Conventions")
        for ins in context["insights"][:15]:
            parts.append(f"- [{ins['category']}] {ins['description']} (confidence: {ins['confidence']:.0%})")
        parts.append("")

    if context["hotspots"]:
        parts.append("## Code Hotspots (high churn + complexity)")
        for h in context["hotspots"][:10]:
            parts.append(f"- {h['file']} (churn: {h['churn']}, complexity: {h['complexity']})")
        parts.append("")

    if context["relevant_files"]:
        parts.append("## Potentially Relevant Files")
        for f in context["relevant_files"][:15]:
            sym = f" (contains: {f['symbol']})" if f.get("symbol") else ""
            parts.append(f"- {f['file']}{sym}")
        parts.append("")

    parts.extend([
        "## Your Task",
        "Analyze the user's request and produce ONLY a structured plan.",
        "Do NOT generate any code or diffs yet.",
        "",
        "## Required Output Format",
        "Return a JSON object with this exact structure:",
        "```json",
        '{',
        '  "analysis": "one paragraph understanding of the request",',
        '  "files": [',
        '    {"path": "relative/file/path.py", "action": "modify|create", "description": "what to change and why"}',
        '  ],',
        '  "notes": "any caveats or alternative approaches"',
        '}',
        "```",
        "",
        "RULES:",
        "- Only include files that actually exist in the repository (listed above) or new files to create.",
        "- Be specific about what needs to change in each file.",
        "- Limit to the most important files (max 8).",
    ])

    return "\n".join(parts)


def _build_editor_handoff(
    plan: object,
    target_path: object = None,
    max_chars: object = _MAX_EDITOR_HANDOFF_CHARS,
) -> str:
    """Render a structured plan as deterministic, bounded editor context.

    Valid plan data is lossless when it fits. Under pressure, explanatory
    text and non-target records are reduced before contracts owned by the
    emphasized target. The input is never mutated.
    """

    def normalized_path(value: object) -> Optional[str]:
        if not isinstance(value, str):
            return None
        result = value.strip().replace("\\", "/")
        if not result or any(ord(char) < 32 for char in result):
            return None
        return result

    if max_chars is None:
        budget = _MAX_EDITOR_HANDOFF_CHARS
    elif isinstance(max_chars, bool):
        budget = int(max_chars)
    else:
        try:
            budget = int(max_chars)
        except (TypeError, ValueError, OverflowError):
            budget = _MAX_EDITOR_HANDOFF_CHARS
    budget = max(0, budget)
    if budget < 2:
        return "{}"[:budget]

    source = plan if isinstance(plan, Mapping) else {}
    target = normalized_path(target_path)

    files_with_priority: List[tuple[Dict[str, Any], bool]] = []
    raw_files = source.get("files")
    if isinstance(raw_files, (list, tuple)):
        for raw_file in raw_files:
            if not isinstance(raw_file, Mapping):
                continue
            path = normalized_path(raw_file.get("path"))
            responsibility = raw_file.get("description")
            if not isinstance(responsibility, str):
                responsibility = raw_file.get("responsibility")
            if path is None or not isinstance(responsibility, str):
                continue
            item: Dict[str, Any] = {
                "path": path,
                "responsibility": responsibility,
            }
            action = raw_file.get("action")
            if isinstance(action, str) and action:
                item["action"] = action
            optional = raw_file.get("optional")
            if isinstance(optional, bool):
                item["optional"] = optional
            algorithm = raw_file.get("algorithm")
            if isinstance(algorithm, str) and algorithm.strip():
                item["algorithm"] = algorithm[:2_000]
            for field in ("required_primitives", "forbidden_shortcuts"):
                values = raw_file.get(field)
                if not isinstance(values, (list, tuple)):
                    continue
                normalized_values = [
                    value[:500]
                    for raw_value in values
                    if isinstance(raw_value, str)
                    and (value := raw_value.strip())
                ][:8]
                if normalized_values:
                    item[field] = normalized_values
            files_with_priority.append((item, bool(target and path == target)))

    contracts_with_priority: List[tuple[Dict[str, Any], bool]] = []
    raw_contracts = source.get("contract_coverage")
    if isinstance(raw_contracts, (list, tuple)):
        for raw_contract in raw_contracts:
            if not isinstance(raw_contract, Mapping):
                continue
            contract = raw_contract.get("contract")
            postcondition = raw_contract.get("postcondition")
            raw_owners = raw_contract.get("owner_paths")
            if (
                not isinstance(contract, str)
                or not contract.strip()
                or not isinstance(postcondition, str)
                or not postcondition.strip()
                or not isinstance(raw_owners, (list, tuple))
            ):
                continue
            owners = [
                owner
                for value in raw_owners
                if (owner := normalized_path(value)) is not None
            ]
            if not owners:
                continue
            item = {
                "contract": contract,
                "owner_paths": owners,
                "postcondition": postcondition,
            }
            contracts_with_priority.append(
                (item, bool(target and target in owners))
            )

    # Stable partitioning emphasizes the target without disturbing the plan's
    # causal order within target-owned and cross-file groups.
    files_with_priority.sort(key=lambda value: not value[1])
    contracts_with_priority.sort(key=lambda value: not value[1])

    payload: Dict[str, Any] = {}
    if target is not None:
        payload["target_path"] = target
    dimension = source.get("dimension")
    if isinstance(dimension, str):
        payload["dimension"] = dimension
    analysis = source.get("analysis")
    if isinstance(analysis, str):
        payload["analysis"] = analysis
    notes = source.get("notes")
    if isinstance(notes, str):
        payload["notes"] = notes
    payload["files"] = [item for item, _is_target in files_with_priority]
    payload["contract_coverage"] = [
        item for item, _is_target in contracts_with_priority
    ]

    pretty = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if len(pretty) <= budget:
        return pretty

    def render() -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    compact = render()
    if len(compact) <= budget:
        return compact

    def shrink_text(container: Dict[str, Any], key: str) -> bool:
        """Shrink one field as little as possible; report when it now fits."""
        original = container.get(key)
        if not isinstance(original, str) or not original:
            return False
        container[key] = ""
        if len(render()) > budget:
            return False

        best = ""
        low = 0
        high = len(original) - 1
        while low <= high:
            middle = (low + high) // 2
            candidate = original[:middle] + "..."
            container[key] = candidate
            if len(render()) <= budget:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        container[key] = best
        return True

    if "notes" in payload and shrink_text(payload, "notes"):
        return render()

    files = payload["files"]
    contracts = payload["contract_coverage"]
    file_target_flags = [is_target for _item, is_target in files_with_priority]
    contract_target_flags = [
        is_target for _item, is_target in contracts_with_priority
    ]

    # Keep every path/record for as long as possible. Target descriptions and
    # target-owned contract text are deliberately reduced last.
    for index in reversed(range(len(files))):
        if not file_target_flags[index] and shrink_text(
            files[index], "responsibility"
        ):
            return render()
    for index in reversed(range(len(files))):
        if not file_target_flags[index] and shrink_text(
            files[index], "algorithm"
        ):
            return render()
    for index in reversed(range(len(contracts))):
        if not contract_target_flags[index] and shrink_text(
            contracts[index], "postcondition"
        ):
            return render()
    if "analysis" in payload and shrink_text(payload, "analysis"):
        return render()
    for index in reversed(range(len(files))):
        if file_target_flags[index] and shrink_text(
            files[index], "responsibility"
        ):
            return render()
    for index in reversed(range(len(files))):
        if file_target_flags[index] and shrink_text(
            files[index], "algorithm"
        ):
            return render()
    for index in reversed(range(len(contracts))):
        if contract_target_flags[index] and shrink_text(
            contracts[index], "postcondition"
        ):
            return render()
    for index in reversed(range(len(contracts))):
        if not contract_target_flags[index] and shrink_text(
            contracts[index], "contract"
        ):
            return render()
    for index in reversed(range(len(contracts))):
        if contract_target_flags[index] and shrink_text(
            contracts[index], "contract"
        ):
            return render()

    # Structural overhead can itself exceed very small bounds. Remove optional
    # metadata, then non-target records, before touching target-owned contracts.
    for item in files:
        item.pop("optional", None)
        item.pop("action", None)
        if len(render()) <= budget:
            return render()
    payload.pop("dimension", None)
    if len(render()) <= budget:
        return render()

    for index in reversed(range(len(contracts))):
        if contract_target_flags[index]:
            continue
        contracts.pop(index)
        contract_target_flags.pop(index)
        if len(render()) <= budget:
            return render()
    for index in reversed(range(len(files))):
        if file_target_flags[index]:
            continue
        files.pop(index)
        file_target_flags.pop(index)
        if len(render()) <= budget:
            return render()

    payload.pop("notes", None)
    if len(render()) <= budget:
        return render()
    payload.pop("analysis", None)
    if len(render()) <= budget:
        return render()

    while contracts:
        contracts.pop()
        if len(render()) <= budget:
            return render()
    while files:
        files.pop()
        if len(render()) <= budget:
            return render()
    payload.pop("target_path", None)
    rendered = render()
    if len(rendered) <= budget:
        return rendered
    return "{}" if budget >= 2 else "{}"[:budget]


def _build_edit_prompt(file_path: str, file_content: str, change_description: str, conventions: List[str]) -> str:
    """System prompt for Step 2: generate SEARCH/REPLACE edits for one file.

    Exact-match search/replace blocks (Aider-style) instead of unified
    diffs: small local models emit malformed diff hunks constantly, while
    copying exact lines and writing replacements is reliable. The diff that
    downstream ``git apply`` consumes is generated programmatically from
    the applied result, so it is always well-formed.
    """
    parts = [
        "You are Chili Code Agent. You are editing a specific file.",
        "",
        "Emit one or more edit blocks in EXACTLY this format:",
        "",
        "<<<<<<< SEARCH",
        "(lines copied EXACTLY from the file)",
        "=======",
        "(the replacement lines)",
        ">>>>>>> REPLACE",
        "",
        "STRICT RULES:",
        "- Every edit MUST be based ONLY on the provided file content and required change.",
        "- SEARCH text must be copied character-for-character from the file below (indentation matters).",
        "- SEARCH text must appear EXACTLY ONCE in the file — include enough surrounding lines to make it unique.",
        "- Several small blocks are better than one large block.",
        "- To insert new code, SEARCH for the nearest existing line(s) and repeat them in REPLACE together with the new code.",
        "- Do NOT use placeholder code like '# Implementation here' or 'pass' for real logic.",
        "- If the change cannot be made from the provided content, explain why in plain text instead of guessing.",
        "",
    ]

    if conventions:
        parts.append("## Project Conventions to Follow")
        for c in conventions[:5]:
            parts.append(f"- {c}")
        parts.append("")

    parts.extend([
        f"## File: {file_path}",
        "```",
        file_content,
        "```",
        "",
        "## Required Change",
        change_description,
        "",
        "## Output",
        "Return ONLY the edit block(s) in the format above. No other text.",
    ])

    return "\n".join(parts)


def _read_file_content(repo_path: str, file_path: str, max_lines: int = _MAX_FILE_LINES) -> Optional[str]:
    """Read file content for LLM context. Returns None if unreadable."""
    try:
        full = Path(repo_path) / file_path
        if not full.is_file():
            return None
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines truncated)"
        return "\n".join(lines)
    except Exception:
        return None


def _read_file_full(repo_path: str, file_path: str) -> Optional[str]:
    """Full, untruncated file content — the matching/apply substrate.

    Edits are validated and applied against THIS, never against the
    (possibly elided) prompt rendering, so a legitimate edit below any
    prompt elision point still applies.
    """
    try:
        full = Path(repo_path) / file_path
        if not full.is_file():
            return None
        return full.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


_PATH_IN_TEXT_RE = re.compile(
    r"[A-Za-z0-9_\-./\\]+\.(?:py|dart|ts|tsx|js|jsx|md|yml|yaml|toml|json|ps1|sql)\b"
)


def _existing_paths_in_text(repo_path: str, text: str) -> List[str]:
    """Path-like tokens from the task text that EXIST in the repo, with at
    least one directory segment (a bare filename is not an explicit target).
    These are operator-stated facts — the strongest grounding signal a plan
    can get."""
    root = Path(repo_path)
    out: List[str] = []
    for m in _PATH_IN_TEXT_RE.finditer(text or ""):
        rel = m.group(0).replace("\\", "/").lstrip("./")
        if "/" not in rel or rel in out:
            continue
        try:
            if (root / rel).is_file():
                out.append(rel)
        except OSError:
            continue
    return out


def _resolve_planned_path(repo_path: str, fpath: str) -> Optional[str]:
    """Self-heal small-model path slips. Planners (especially local models)
    drop directory segments — live case: planned app/services/scorer.py for
    app/services/code_dispatch/scorer.py despite the exact path in the task.
    When the planned path doesn't exist, find same-basename files and pick
    the one sharing the most trailing segments with the plan. Returns None
    when nothing matches or the best match is ambiguous (never guess)."""
    root = Path(repo_path)
    rel_plan = fpath.replace("\\", "/")
    name = Path(rel_plan).name
    if not name:
        return None
    if (root / rel_plan).is_file():
        return rel_plan
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", "build", "dist"}
    cands: List[str] = []
    try:
        for p in root.rglob(name):
            if any(part in skip for part in p.parts):
                continue
            cands.append(p.relative_to(root).as_posix())
            if len(cands) >= 50:
                break
    except OSError:
        return None
    if not cands:
        return None

    want = Path(rel_plan).parts

    def _shared_tail(rel: str) -> int:
        n = 0
        for a, b in zip(reversed(want), reversed(Path(rel).parts)):
            if a != b:
                break
            n += 1
        return n

    cands.sort(key=_shared_tail, reverse=True)
    if len(cands) > 1 and _shared_tail(cands[0]) == _shared_tail(cands[1]):
        return None
    return cands[0]


def _prompt_file_budget_chars() -> int:
    """Character budget for rendering a file into the edit prompt. Derived
    from the output-token knob (≈4 chars/token, same order as the output
    budget) rather than a second hardcoded line cap — the historical
    600-line head-truncation meant the model literally could not see or
    edit the bottom of larger files."""
    return int(getattr(settings, "chili_code_gen_max_tokens", 16384)) * 4


def _elide_for_prompt(content: str) -> str:
    """Fit content into the prompt budget: head + tail with an explicit
    elision marker (the model sees both ends; matching still runs against
    the full file)."""
    budget = _prompt_file_budget_chars()
    if len(content) <= budget:
        return content
    head = int(budget * 0.6)
    tail = budget - head
    omitted = len(content) - head - tail
    return (
        content[:head]
        + f"\n... ({omitted} characters elided — ask for a narrower change if the edit target is in this region) ...\n"
        + content[-tail:]
    )


_SEARCH_REPLACE_RE = re.compile(
    r"<<<<<<<\s*SEARCH\r?\n(.*?)\r?\n?=======\r?\n(.*?)\r?\n?>>>>>>>\s*REPLACE",
    re.DOTALL,
)


def _parse_search_replace_blocks(reply: str) -> List[tuple]:
    """Extract (search, replace) pairs from the model reply."""
    return [(m.group(1), m.group(2)) for m in _SEARCH_REPLACE_RE.finditer(reply or "")]


def _retryable_edit_adapter_rejection(warnings: List[str]) -> bool:
    retryable_markers = (
        "SEARCH text not found",
        "not unique",
        "full-file fallback rejected embedded SEARCH/REPLACE markers",
        "full-file fallback rejected a unified diff fence",
        "full-file fallback requires exactly one fenced block",
    )
    return any(
        marker in str(warning)
        for warning in warnings
        for marker in retryable_markers
    )


def _extract_full_file_replacement(
    reply: str,
    file_path: str,
    original: str,
) -> Dict[str, Any]:
    """Accept one guarded full-file fence from small local models.

    The fallback is intentionally stricter than SEARCH/REPLACE: exactly one
    fenced block, bounded size change, meaningful similarity to the supplied
    source, and syntax validation for Python. The caller still generates the
    unified diff mechanically and runs normal validation/tests.
    """
    fences = re.findall(
        r"```([A-Za-z0-9_+.-]*)[ \t]*\r?\n(.*?)```",
        reply or "",
        re.DOTALL,
    )
    if len(fences) != 1:
        return {
            "new_content": None,
            "warnings": [f"full-file fallback requires exactly one fenced block, found {len(fences)}"],
        }
    language, candidate = fences[0]
    if language.lower() in {"diff", "patch", "udiff"} or (
        candidate.startswith("--- ") and "\n+++ " in candidate and "\n@@" in candidate
    ):
        return {
            "new_content": None,
            "warnings": ["full-file fallback rejected a unified diff fence"],
        }
    if any(
        marker in candidate
        for marker in ("<<<<<<< SEARCH", "=======", ">>>>>>> REPLACE")
    ):
        return {
            "new_content": None,
            "warnings": ["full-file fallback rejected embedded SEARCH/REPLACE markers"],
        }
    if original.endswith("\n") and not candidate.endswith("\n"):
        candidate += "\n"
    if not candidate.strip() or candidate.rstrip() == original.rstrip():
        return {"new_content": None, "warnings": ["full-file fallback made no change"]}
    original_size = max(1, len(original))
    size_ratio = len(candidate) / original_size
    if size_ratio < 0.25 or size_ratio > 4.0:
        return {
            "new_content": None,
            "warnings": [f"full-file fallback size ratio {size_ratio:.2f} is outside 0.25..4.0"],
        }
    import difflib

    similarity = difflib.SequenceMatcher(None, original, candidate).ratio()
    if similarity < 0.35:
        return {
            "new_content": None,
            "warnings": [f"full-file fallback similarity {similarity:.2f} is below 0.35"],
        }
    if Path(file_path).suffix.lower() == ".py":
        import ast

        try:
            ast.parse(candidate, filename=file_path)
        except SyntaxError as exc:
            return {
                "new_content": None,
                "warnings": [f"full-file fallback Python syntax error: {exc}"],
            }
    return {
        "new_content": candidate,
        "warnings": [
            f"accepted guarded full-file fallback similarity={similarity:.2f} size_ratio={size_ratio:.2f}"
        ],
    }


def _semantic_replacement_warnings(file_path: str, content: str) -> List[str]:
    """Reject a small set of mechanically contradictory Python constants."""
    if Path(file_path).suffix.lower() != ".py":
        return []
    import ast

    try:
        tree = ast.parse(content, filename=file_path)
    except SyntaxError:
        return []

    known_false = {"", "0", "false", "no", "off", "disabled", "none", "null"}
    known_true = {"1", "true", "yes", "on", "enabled"}
    warnings: list[str] = []

    def target_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id.upper()
        return ""

    for node in ast.walk(tree):
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        name = target_name(target) if target is not None else ""
        if not name or value is None or not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            continue
        literals = {
            str(item.value).strip().lower()
            for item in value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        if "TRUE" in name and "VALUE" in name:
            contradictions = sorted(literals & known_false)
            if contradictions:
                warnings.append(
                    f"{name} contains known false literal(s): {', '.join(repr(v) for v in contradictions)}"
                )
        if "FALSE" in name and "VALUE" in name:
            contradictions = sorted(literals & known_true)
            if contradictions:
                warnings.append(
                    f"{name} contains known true literal(s): {', '.join(repr(v) for v in contradictions)}"
                )
    return warnings


_UNICODE_FOLD = (
    ("—", "-"), ("–", "-"),  # em/en dash -> hyphen
    ("‘", "'"), ("’", "'"),  # curly single quotes
    ("“", '"'), ("”", '"'),  # curly double quotes
    (" ", " "),                     # nbsp
)


def _normalize_match_line(s: str) -> str:
    for a, b in _UNICODE_FOLD:
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s.strip())


def _fuzzy_exact_window(content: str, search: str) -> Optional[str]:
    """Map a whitespace/unicode-normalized SEARCH onto the EXACT original
    span of the file. Small models fold em-dashes to hyphens and re-flow
    whitespace when copying lines (live: task 37 run 685 — the docstring's
    'gpt-4o' line uses an em-dash, qwen wrote a hyphen, exact match missed).
    Same uniqueness rule as exact matching: exactly ONE window or nothing —
    fuzziness never licenses guessing."""
    s_lines = search.splitlines()
    norm_s = [_normalize_match_line(l) for l in s_lines]
    if not any(norm_s):
        return None
    c_lines = content.splitlines(keepends=True)
    norm_c = [_normalize_match_line(l) for l in c_lines]
    n = len(norm_s)
    hits = [i for i in range(len(norm_c) - n + 1) if norm_c[i:i + n] == norm_s]
    if len(hits) != 1:
        return None
    i = hits[0]
    return "".join(c_lines[i:i + n])


def _whitespace_reflow_exact_span(content: str, search: str) -> Optional[str]:
    """Map a uniquely reflowed SEARCH onto the exact source span.

    Local models sometimes copy one expression as a single line even when the
    formatter split it across several lines. This matcher permits whitespace
    changes only: every non-whitespace token must remain byte-for-byte equal,
    and exactly one source span must match.
    """
    tokens = re.findall(r"\S+", search)
    if (
        len(tokens) < 2
        or len(tokens) > 400
        or sum(len(token) for token in tokens) < 16
    ):
        return None
    pattern = re.compile(r"\s+".join(re.escape(token) for token in tokens))
    matches = list(pattern.finditer(content))
    if len(matches) != 1:
        return None
    match = matches[0]
    return content[match.start() : match.end()]


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _shift_indent_like(model_search: str, actual_window: str, replace: str) -> str:
    """Undo a uniform indentation shift the model applied while copying.

    Compares the leading whitespace of the first non-blank line of the
    model's SEARCH vs the file's actual window; applies the inverse delta
    to every REPLACE line. No-op when the indents already agree or the
    shift is not uniform-prefix-shaped."""
    s_first = next((l for l in model_search.splitlines() if l.strip()), "")
    w_first = next((l for l in actual_window.splitlines() if l.strip()), "")
    s_ind, w_ind = _leading_ws(s_first), _leading_ws(w_first)
    if s_ind == w_ind:
        return replace
    out: List[str] = []
    for line in replace.splitlines():
        if not line.strip():
            out.append(line)
        elif s_ind and line.startswith(s_ind):
            # model over-indented: swap its prefix for the file's real one
            out.append(w_ind + line[len(s_ind):])
        elif not s_ind:
            # model under-indented (search at col 0, file indented)
            out.append(w_ind + line)
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if replace.endswith("\n") else "")


def _apply_search_replace(content: str, blocks: List[tuple]) -> Dict[str, Any]:
    """Apply SEARCH/REPLACE blocks against FULL file content.

    Exact-match + uniqueness enforcement is the anti-hallucination check:
    a search string that is not in the file (hallucinated code) or matches
    twice (ambiguous) rejects that block. Returns new_content (None when
    nothing applied), applied count, and per-block warnings.
    """
    new_content = content
    warnings: List[str] = []
    applied = 0
    satisfied = 0
    rejected = False
    for i, (search, replace) in enumerate(blocks, start=1):
        if not search.strip():
            warnings.append(f"block {i}: empty SEARCH text")
            rejected = True
            continue
        count = new_content.count(search)
        if count == 0:
            # Forgiving fallback: re-find the block via whitespace/unicode
            # normalization, then operate on the EXACT original span.
            window = _fuzzy_exact_window(new_content, search)
            match_kind = "whitespace/unicode normalization"
            if window is None:
                window = _whitespace_reflow_exact_span(new_content, search)
                match_kind = "unique whitespace reflow"
            if window is not None:
                # The model's copy often carries a uniform indentation shift
                # (live: run 687 re-indented a module docstring by 4 spaces →
                # SyntaxError, caught by AST validation). Re-anchor the
                # REPLACE to the file's real indentation by applying the
                # SEARCH→window indent delta.
                replace = _shift_indent_like(search, window, replace)
                if window.endswith("\n") and not replace.endswith("\n"):
                    replace = replace + "\n"
                search = window
                count = new_content.count(search)
                warnings.append(
                    f"block {i}: matched via {match_kind}"
                )
        if count == 0:
            if replace.strip() and new_content.count(replace) == 1:
                warnings.append(
                    f"block {i}: replacement is already satisfied in the current file"
                )
                satisfied += 1
                continue
            snippet = search.strip().replace("\n", " | ")[:120]
            warnings.append(
                f"block {i}: SEARCH text not found in file (hallucinated or "
                f"stale content): {snippet!r}"
            )
            rejected = True
            continue
        if count > 1:
            warnings.append(
                f"block {i}: SEARCH text matches {count} times — not unique, add surrounding lines"
            )
            rejected = True
            continue
        if search == replace:
            warnings.append(f"block {i}: replacement is an identity and already satisfied")
            satisfied += 1
            continue
        new_content = new_content.replace(search, replace, 1)
        applied += 1
    if rejected:
        warnings.append(
            "atomic edit rejected: every SEARCH/REPLACE block in a file must apply or already be satisfied"
        )
        return {
            "new_content": None,
            "applied": 0,
            "already_satisfied": False,
            "warnings": warnings,
        }
    return {
        "new_content": new_content if applied else None,
        "applied": applied,
        "already_satisfied": bool(satisfied) and not applied,
        "warnings": warnings,
    }


def _unified_diff_text(file_path: str, old: str, new: str) -> str:
    """Machine-generated unified diff (git-apply-compatible a/ b/ headers).
    Generated from the applied result, so it is always well-formed —
    the model never has to emit diff syntax."""
    import difflib

    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    # Guard the no-trailing-newline edge: keep git apply happy by ensuring
    # the last line carries a newline in both sides of the diff input.
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
    )


def _parse_plan_json(reply: str) -> Optional[Dict[str, Any]]:
    """Extract the JSON plan from the LLM's Step 1 response."""
    # Try to find JSON block
    m = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", reply, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Decode a balanced raw object. Regex extraction truncates as soon as a
    # plan grows nested evidence such as contract_coverage entries.
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", reply or ""):
        try:
            value, _end = decoder.raw_decode((reply or "")[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("files"), list):
            return value
    return None


def _is_mutating_plan_action(value: object) -> bool:
    if not isinstance(value, str):
        return False
    action = value.strip().lower().replace("-", "_").replace(" ", "_")
    return action in _MUTATING_PLAN_ACTIONS


def _validate_diff(diff_text: str, file_path: str, file_content: Optional[str]) -> Dict[str, Any]:
    """Validate a generated diff against the real file content."""
    result = {"valid": True, "warnings": []}

    if not file_content:
        result["warnings"].append(f"Cannot validate: file '{file_path}' not readable")
        return result

    real_lines = set(file_content.splitlines())
    removed_lines = []
    for line in diff_text.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            removed_lines.append(line[1:])

    bad_count = 0
    for rl in removed_lines:
        stripped = rl.strip()
        if stripped and stripped not in {l.strip() for l in real_lines}:
            bad_count += 1

    if bad_count > 0 and removed_lines:
        pct = bad_count / len(removed_lines) * 100
        if pct > 50:
            result["valid"] = False
            result["warnings"].append(
                f"{bad_count}/{len(removed_lines)} removed lines do not match the actual file. "
                "This diff may contain hallucinated code."
            )
        elif pct > 20:
            result["warnings"].append(
                f"{bad_count}/{len(removed_lines)} removed lines could not be verified."
            )

    return result


async def run_code_agent(
    db: Session,
    prompt: str,
    repo_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute the two-step Code Agent flow: Plan -> Edit.

    Phase F.10: every LLM call here goes through the Universal Gateway
    with a specific purpose tag so the brain can apply the right
    routing strategy per call site (plan = tree-decomposed, edit/create
    = augmented). Gateway failures fail closed so paid calls keep their
    budget, policy, and observability controls.
    """
    from ...openai_client import is_local_code_configured

    if not is_local_code_configured():
        return {
            "error": (
                "Local coding model not configured. Set OLLAMA_HOST and "
                "CHILI_CODE_LOCAL_MODEL; premium keys are not required."
            )
        }

    # Local helper so the three call sites below stay tidy. Each call
    # gets a different purpose so the policy table can route them
    # individually.
    from ..context_brain.llm_gateway import gateway_chat as _gateway_chat

    def llm_chat(messages, system_prompt=None, trace_id="llm",
                 user_message="", max_tokens=1024, strict_escalation=True,
                 _purpose="code_dispatch_plan"):
        try:
            return _gateway_chat(
                messages=messages,
                purpose=_purpose,
                system_prompt=system_prompt,
                trace_id=trace_id,
                user_message=user_message,
                max_tokens=max_tokens,
                strict_escalation=strict_escalation,
                user_id=user_id,
                db=db,
                local_only=True,
            )
        except Exception as _e:
            logger.warning(
                "[code_agent] gateway_chat failed (%s); direct_openai_bypass_disabled",
                _e,
            )
            return {"reply": "", "model": "gateway_error", "tokens_used": 0}

    # Track gateway calls so we can record outcomes against them at the end.
    _dispatch_log_ids: list[tuple[int, str, bool]] = []  # (gateway_log_id, purpose, success)

    context = _gather_context(db, repo_id, prompt, user_id=user_id)

    if not context["repos"]:
        return {"error": "No repos registered. Add a repo first via the Brain UI."}

    # ── Step 1: Plan ─────────────────────────────────────────────
    plan_system = _build_plan_prompt(context)
    plan_result = llm_chat(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=plan_system,
        trace_id="code-agent-plan",
        user_message=prompt,
        max_tokens=settings.chili_code_gen_max_tokens,
    )
    plan_reply = plan_result.get("reply", "")
    plan_model = plan_result.get("model", "unknown")

    # ── DO NOT REMOVE — masking-bug guard (CHILI Dispatch Phase D.0) ──
    # The legacy LLM cascade returns {"reply": "", "model": "error"} when every
    # cascade tier failed. Without this check, the empty sentinel was being
    # persisted as a fake success-shaped row in coding_agent_suggestion
    # (model='error', diffs=[]), masking real failures from the dispatch
    # loop and polluting distillation training data.
    if plan_model == "error" or not plan_reply.strip():
        logger.warning(
            "[code_agent] LLM cascade returned empty/error sentinel "
            "(model=%s, reply_len=%d) — surfacing as error",
            plan_model, len(plan_reply),
        )
        return {
            "error": (
                "LLM cascade exhausted: every configured tier (OpenAI / Groq / Gemini) "
                "returned an empty response. Check provider keys, daily quotas, and "
                "auth-failed sticky markers in the scheduler-worker logs."
            ),
            "model": plan_model,
        }

    plan_json = _parse_plan_json(plan_reply)

    # Record the plan's gateway_log_id (its outcome quality is set at the end).
    _plan_log_id = plan_result.get("gateway_log_id") if isinstance(plan_result, dict) else None
    if _plan_log_id:
        _dispatch_log_ids.append((int(_plan_log_id), "code_dispatch_plan", False))

    if not plan_json or not plan_json.get("files"):
        # Plan didn't produce a usable plan — still a soft failure.
        try:
            from ..context_brain.outcome_tracker import record_dispatch_outcome
            for log_id, purpose, _ in _dispatch_log_ids:
                record_dispatch_outcome(
                    db, gateway_log_id=log_id, success=False,
                    purpose=purpose,
                    detail={"reason": "plan_no_files", "files_changed": 0},
                )
        except Exception:
            pass
        return {
            "response": plan_reply,
            "model": plan_model,
            "diffs": [],
            "files_changed": [],
            "validation": [],
            "context_used": _context_summary(context, 0),
        }

    analysis = plan_json.get("analysis", "")
    plan_files = [
        item
        for item in plan_json.get("files", [])
        if isinstance(item, dict) and _is_mutating_plan_action(item.get("action"))
    ][:_MAX_FILES_PER_EDIT]
    notes = plan_json.get("notes", "")

    # Plan grounding: when the TASK ITSELF names existing file path(s), the
    # plan must target them — small models routinely wander to a similar
    # file from the gathered context (live: task 36 attempt 4 edited
    # agent_suggest.py instead of the explicitly-named
    # code_dispatch/scorer.py, burning a 17-minute run on the wrong file).
    _repo_root_for_grounding = context["repos"][0]["path"] if context["repos"] else ""
    if _repo_root_for_grounding:
        explicit = _existing_paths_in_text(_repo_root_for_grounding, prompt)
        if explicit:
            planned_set = {str(pf.get("path", "")).replace("\\", "/") for pf in plan_files}
            if not planned_set & set(explicit):
                logger.info(
                    "[code_agent] plan grounded: task names %s but plan targeted %s — pinning plan to the named path(s)",
                    explicit, sorted(planned_set),
                )
                base_desc = (plan_files[0].get("description") if plan_files else "") or prompt
                plan_files = [
                    {"path": p, "action": "modify", "description": base_desc}
                    for p in explicit[:_MAX_FILES_PER_EDIT]
                ]

    # Collect conventions for the edit prompts
    conventions = [
        ins["description"] for ins in context["insights"]
        if ins.get("category") in ("convention", "pattern")
    ][:5]

    # Resolve repo paths
    repo_path_map: Dict[str, str] = {}
    for r in context["repos"]:
        repo_path_map[r["name"]] = r["path"]
    default_repo_path = context["repos"][0]["path"] if context["repos"] else ""
    editor_plan = dict(plan_json)
    editor_plan["files"] = plan_files

    # ── Step 2: Edit each file ───────────────────────────────────
    all_diffs: List[str] = []
    files_changed: List[str] = []
    validations: List[Dict[str, Any]] = []
    edit_sections: List[str] = []

    for pf in plan_files:
        fpath = pf.get("path", "")
        action = pf.get("action", "modify")
        description = pf.get("description", "")
        editor_handoff = _build_editor_handoff(
            editor_plan,
            target_path=fpath,
        )

        if action == "create":
            edit_sections.append(f"### New file: {fpath}\n{description}")
            # For new files, ask LLM to generate the full file content
            create_prompt = (
                f"You are creating a new file: {fpath}\n\n"
                "Implement the emphasized target in this coordinated plan:\n"
                f"{editor_handoff}\n\n"
                "Follow the project conventions:\n" +
                "\n".join(f"- {c}" for c in conventions[:3]) +
                "\n\nReturn ONLY the file content wrapped in a code block."
            )
            create_result = llm_chat(
                messages=[{"role": "user", "content": create_prompt}],
                system_prompt="You are Chili Code Agent. Generate clean, production-quality code.",
                trace_id=f"code-agent-create-{fpath}",
                user_message=description,
                max_tokens=settings.chili_code_gen_max_tokens,
                _purpose="code_dispatch_create",
            )
            create_reply = create_result.get("reply", "")
            _create_log_id = create_result.get("gateway_log_id") if isinstance(create_result, dict) else None
            m = re.search(r"```\w*\n(.*?)```", create_reply, re.DOTALL)
            if m:
                new_content = m.group(1).strip()
                semantic_warnings = _semantic_replacement_warnings(fpath, new_content)
                if semantic_warnings:
                    validations.append(
                        {
                            "file": fpath,
                            "valid": False,
                            "warnings": semantic_warnings,
                        }
                    )
                    edit_sections.append(
                        f"Could not generate safe content for {fpath}: "
                        + "; ".join(semantic_warnings)
                    )
                    if _create_log_id:
                        _dispatch_log_ids.append(
                            (int(_create_log_id), "code_dispatch_create", False)
                        )
                    continue
                diff = f"--- /dev/null\n+++ b/{fpath}\n@@ -0,0 +1,{len(new_content.splitlines())} @@\n"
                diff += "\n".join("+" + l for l in new_content.splitlines())
                all_diffs.append(diff)
                files_changed.append(fpath)
                validations.append({"file": fpath, "valid": True, "warnings": ["New file"]})
                if _create_log_id:
                    _dispatch_log_ids.append((int(_create_log_id), "code_dispatch_create", True))
            else:
                edit_sections.append(f"Could not generate content for {fpath}")
                if _create_log_id:
                    _dispatch_log_ids.append((int(_create_log_id), "code_dispatch_create", False))
            continue

        # Full content is the matching/apply substrate; the prompt gets an
        # elided rendering when the file exceeds the prompt budget. Edits
        # below any elision point still apply (the old 600-line head
        # truncation made the bottom of large files uneditable).
        file_content = _read_file_full(default_repo_path, fpath)
        if file_content is None:
            # Self-heal planner path slips (unique basename match) before
            # giving up — small models drop directory segments constantly.
            healed = _resolve_planned_path(default_repo_path, fpath)
            if healed and healed != fpath:
                logger.info("[code_agent] healed planned path %s -> %s", fpath, healed)
                fpath = healed
                file_content = _read_file_full(default_repo_path, fpath)
        if file_content is None:
            # Try other repos
            for rp in repo_path_map.values():
                file_content = _read_file_full(rp, fpath)
                if file_content is not None:
                    break

        if file_content is None:
            validations.append({
                "file": fpath,
                "valid": False,
                "warnings": [f"File not found in any registered repo: {fpath}"],
            })
            edit_sections.append(f"### {fpath}\nFile not found -- skipped.")
            continue

        edit_system = _build_edit_prompt(
            fpath,
            _elide_for_prompt(file_content),
            "Implement the emphasized target in this coordinated plan:\n"
            + editor_handoff,
            conventions,
        )
        edit_result = llm_chat(
            messages=[{"role": "user", "content": f"Apply the change to {fpath} as described."}],
            system_prompt=edit_system,
            trace_id=f"code-agent-edit-{fpath}",
            user_message=description,
            max_tokens=settings.chili_code_gen_max_tokens,
            _purpose="code_dispatch_edit",
        )
        edit_reply = edit_result.get("reply", "")
        _edit_log_id = edit_result.get("gateway_log_id") if isinstance(edit_result, dict) else None

        _edit_succeeded_at_least_one = False
        sr_blocks = _parse_search_replace_blocks(edit_reply)
        if sr_blocks:
            outcome = _apply_search_replace(file_content, sr_blocks)
            if outcome["new_content"] is not None:
                semantic_warnings = _semantic_replacement_warnings(
                    fpath,
                    outcome["new_content"],
                )
                if semantic_warnings:
                    validations.append(
                        {
                            "file": fpath,
                            "valid": False,
                            "warnings": semantic_warnings,
                            "semantic_polarity_guard": True,
                        }
                    )
                    edit_sections.append(
                        f"### {fpath}\n**Edits rejected** -- "
                        + "; ".join(semantic_warnings)
                    )
                else:
                    d = _unified_diff_text(fpath, file_content, outcome["new_content"])
                    if d.strip():
                        all_diffs.append(d)
                        if fpath not in files_changed:
                            files_changed.append(fpath)
                        edit_sections.append(f"### {fpath}\n```diff\n{d}\n```")
                        _edit_succeeded_at_least_one = True
                    validations.append({
                        "file": fpath,
                        "valid": True,
                        "warnings": outcome["warnings"],
                        "applied_blocks": outcome["applied"],
                        "total_blocks": len(sr_blocks),
                    })
            else:
                validations.append({
                    "file": fpath,
                    "valid": False,
                    "warnings": outcome["warnings"] or ["no SEARCH/REPLACE block applied"],
                })
                edit_sections.append(
                    f"### {fpath}\n**Edits rejected** -- {'; '.join(outcome['warnings'])}"
                )
        else:
            full_file = _extract_full_file_replacement(edit_reply, fpath, file_content)
            if full_file["new_content"] is not None:
                semantic_warnings = _semantic_replacement_warnings(
                    fpath,
                    full_file["new_content"],
                )
                if semantic_warnings:
                    validations.append(
                        {
                            "file": fpath,
                            "valid": False,
                            "warnings": semantic_warnings,
                            "semantic_polarity_guard": True,
                        }
                    )
                    edit_sections.append(
                        f"### {fpath}\n**Full-file edit rejected** -- "
                        + "; ".join(semantic_warnings)
                    )
                else:
                    d = _unified_diff_text(fpath, file_content, full_file["new_content"])
                    all_diffs.append(d)
                    if fpath not in files_changed:
                        files_changed.append(fpath)
                    edit_sections.append(f"### {fpath}\n```diff\n{d}\n```")
                    validations.append(
                        {
                            "file": fpath,
                            "valid": True,
                            "warnings": full_file["warnings"],
                            "full_file_fallback": True,
                        }
                    )
                    _edit_succeeded_at_least_one = True
            else:
                # Legacy fallback: some cloud models still answer with a raw
                # unified diff; keep accepting it, validated against FULL content.
                diffs_in_reply = re.findall(r"```diff\n(.*?)```", edit_reply, re.DOTALL)
            if full_file["new_content"] is None and diffs_in_reply:
                for d in diffs_in_reply:
                    # Models often emit bare hunks (first line '@@ …') —
                    # git apply rejects "patch fragment without header"
                    # (live: one-off run 2026-06-12). We know the target
                    # file, so synthesize the headers.
                    if d.lstrip().startswith("@@"):
                        d = f"--- a/{fpath}\n+++ b/{fpath}\n{d.lstrip()}"
                    validation = _validate_diff(d, fpath, file_content)
                    validations.append({"file": fpath, **validation})
                    if validation["valid"]:
                        all_diffs.append(d)
                        if fpath not in files_changed:
                            files_changed.append(fpath)
                        edit_sections.append(f"### {fpath}\n```diff\n{d}\n```")
                        _edit_succeeded_at_least_one = True
                    else:
                        edit_sections.append(
                            f"### {fpath}\n**Diff rejected** -- {'; '.join(validation['warnings'])}"
                        )
            elif full_file["new_content"] is None:
                edit_sections.append(f"### {fpath}\n{edit_reply}")
        if _edit_log_id:
            _dispatch_log_ids.append(
                (int(_edit_log_id), "code_dispatch_edit", _edit_succeeded_at_least_one)
            )

    # ── Assemble final response ──────────────────────────────────
    response_parts = [f"## Analysis\n{analysis}"]
    response_parts.append(
        "## Plan\n" +
        "\n".join(f"- **{pf['path']}** ({pf.get('action','modify')}): {pf.get('description','')}" for pf in plan_files)
    )
    if edit_sections:
        response_parts.append("## Changes\n" + "\n\n".join(edit_sections))
    if notes:
        response_parts.append(f"## Notes\n{notes}")

    # Add validation summary
    invalid = [v for v in validations if not v.get("valid")]
    if invalid:
        warn_lines = [f"- **{v['file']}**: {'; '.join(v.get('warnings', []))}" for v in invalid]
        response_parts.append("## Validation Warnings\n" + "\n".join(warn_lines))

    # F.5 — record dispatch outcomes against every gateway call we made.
    # Plan succeeds when at least one file was actually changed; create/edit
    # entries already carry their own per-file success bit.
    try:
        from ..context_brain.outcome_tracker import record_dispatch_outcome
        plan_success = bool(files_changed)
        invalid_count = sum(1 for v in validations if not v.get("valid"))
        for log_id, purpose, per_file_success in _dispatch_log_ids:
            ok = per_file_success if purpose != "code_dispatch_plan" else plan_success
            record_dispatch_outcome(
                db,
                gateway_log_id=log_id,
                success=ok,
                purpose=purpose,
                detail={
                    "files_changed": len(files_changed),
                    "files_planned": len(plan_files),
                    "invalid_diffs": invalid_count,
                },
            )
    except Exception as _e:  # pragma: no cover
        logger.warning("[code_agent] failed to record dispatch outcomes: %s", _e)

    return {
        "response": "\n\n".join(response_parts),
        "model": plan_result.get("model", "unknown"),
        "diffs": all_diffs,
        "files_changed": files_changed,
        "validation": validations,
        "context_used": _context_summary(context, len(plan_files)),
    }


def _context_summary(context: Dict[str, Any], files_edited: int) -> Dict[str, Any]:
    return {
        "repos": len(context["repos"]),
        "insights": len(context["insights"]),
        "hotspots": len(context["hotspots"]),
        "relevant_files": len(context["relevant_files"]),
        "files_in_plan": files_edited,
    }
