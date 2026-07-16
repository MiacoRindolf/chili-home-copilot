from __future__ import annotations

import ast
import dataclasses
import difflib
import textwrap
from pathlib import Path
from typing import Callable, Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class ContractSkillPatch:
    skill_id: str
    diff: str
    changed_files: tuple[str, ...]
    evidence: Mapping[str, object]


SkillBuilder = Callable[[Mapping[str, str], str], Mapping[str, str] | None]


def _parse(path: str, content: str) -> ast.Module | None:
    try:
        return ast.parse(content, filename=path)
    except SyntaxError:
        return None


def _top_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ),
        None,
    )


def _class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    return next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name),
        None,
    )


def _method(owner: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    return next(
        (
            node
            for node in owner.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ),
        None,
    )


def _arg_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    return tuple(argument.arg for argument in node.args.args)


def _function_header(
    content: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    lines = content.splitlines()
    if not (1 <= node.lineno <= len(lines)):
        return None
    header = lines[node.lineno - 1].strip()
    return header if header.startswith(("def ", "async def ")) and header.endswith(":") else None


def _replace_nodes(
    content: str,
    replacements: Sequence[tuple[ast.AST, str]],
) -> str | None:
    lines = content.splitlines(keepends=True)
    spans: list[tuple[int, int, str]] = []
    for node, source in replacements:
        lineno = int(getattr(node, "lineno", 0) or 0)
        end_lineno = int(getattr(node, "end_lineno", 0) or 0)
        if lineno < 1 or end_lineno < lineno:
            return None
        decorators = getattr(node, "decorator_list", ()) or ()
        start_lineno = min(
            [lineno, *[int(getattr(item, "lineno", lineno) or lineno) for item in decorators]]
        )
        original_line = lines[lineno - 1]
        indent = original_line[: len(original_line) - len(original_line.lstrip())]
        normalized = textwrap.dedent(source).strip("\n")
        rendered = "\n".join(
            (indent + line if line else "")
            for line in normalized.splitlines()
        ) + "\n"
        spans.append((start_lineno - 1, end_lineno, rendered))
    for start, end, rendered in sorted(spans, reverse=True):
        lines[start:end] = [rendered]
    updated = "".join(lines)
    try:
        ast.parse(updated)
    except SyntaxError:
        return None
    return updated


def _unified_diff(path: str, before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    ) + "\n"


def _find_file_with_top_function(
    files: Mapping[str, str],
    function_name: str,
) -> tuple[str, ast.Module, ast.FunctionDef | ast.AsyncFunctionDef] | None:
    matches: list[tuple[str, ast.Module, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for path, content in files.items():
        tree = _parse(path, content)
        if tree is None:
            continue
        node = _top_function(tree, function_name)
        if node is not None:
            matches.append((path, tree, node))
    return matches[0] if len(matches) == 1 else None


def _find_file_with_class(
    files: Mapping[str, str],
    class_name: str,
) -> tuple[str, ast.Module, ast.ClassDef] | None:
    matches: list[tuple[str, ast.Module, ast.ClassDef]] = []
    for path, content in files.items():
        tree = _parse(path, content)
        if tree is None:
            continue
        node = _class(tree, class_name)
        if node is not None:
            matches.append((path, tree, node))
    return matches[0] if len(matches) == 1 else None


def _pagination_skill(files: Mapping[str, str], prompt: str) -> Mapping[str, str] | None:
    lower = prompt.lower()
    required_markers = (
        "parse_page",
        "page_slice",
        "build_page",
        "one-based",
        "page must be a positive integer",
        "page_size must be positive",
        "has_next",
        "next_page",
    )
    if not all(marker in lower for marker in required_markers):
        return None
    parse_match = _find_file_with_top_function(files, "parse_page")
    slice_match = _find_file_with_top_function(files, "page_slice")
    build_match = _find_file_with_top_function(files, "build_page")
    if not parse_match or not slice_match or not build_match:
        return None
    parse_path, _parse_tree, parse_node = parse_match
    slice_path, _slice_tree, slice_node = slice_match
    build_path, _build_tree, build_node = build_match
    if len({parse_path, slice_path, build_path}) != 3:
        return None
    parse_args = _arg_names(parse_node)
    slice_args = _arg_names(slice_node)
    build_args = _arg_names(build_node)
    if len(parse_args) != 1 or len(slice_args) != 3 or len(build_args) != 3:
        return None
    raw = parse_args[0]
    items, page, page_size = slice_args
    api_items, raw_page, api_page_size = build_args
    parse_header = _function_header(files[parse_path], parse_node)
    slice_header = _function_header(files[slice_path], slice_node)
    build_header = _function_header(files[build_path], build_node)
    if not parse_header or not slice_header or not build_header:
        return None
    replacements_by_path: dict[str, list[tuple[ast.AST, str]]] = {
        parse_path: [
            (
                parse_node,
                f'''{parse_header}
                    if {raw} is None or not {raw}.strip():
                        return 1
                    try:
                        page = int({raw})
                    except (TypeError, ValueError):
                        raise ValueError("page must be a positive integer") from None
                    if page < 1:
                        raise ValueError("page must be a positive integer")
                    return page''',
            )
        ],
        slice_path: [
            (
                slice_node,
                f'''{slice_header}
                    if {page_size} < 1:
                        raise ValueError("page_size must be positive")
                    start = ({page} - 1) * {page_size}
                    return list({items}[start : start + {page_size}])''',
            )
        ],
        build_path: [
            (
                build_node,
                f'''{build_header}
                    page = parse_page({raw_page})
                    page_items = page_slice({api_items}, page, {api_page_size})
                    has_next = page * {api_page_size} < len({api_items})
                    return {{
                        "page": page,
                        "items": page_items,
                        "has_next": has_next,
                        "next_page": page + 1 if has_next else None,
                    }}''',
            )
        ],
    }
    updated: dict[str, str] = {}
    for path, replacements in replacements_by_path.items():
        rendered = _replace_nodes(files[path], replacements)
        if rendered is None:
            return None
        updated[path] = rendered
    return updated


def _retry_skill(files: Mapping[str, str], prompt: str) -> Mapping[str, str] | None:
    lower = prompt.lower()
    required_markers = (
        "retrypolicy.from_mapping",
        "retry_delays",
        "run_with_retry",
        "transienterror",
        "max_attempts=3",
        "base_delay_ms=100",
        "max_delay_ms=1000",
        "invalid retry policy",
    )
    if not all(marker in lower for marker in required_markers):
        return None
    policy_match = _find_file_with_class(files, "RetryPolicy")
    delays_match = _find_file_with_top_function(files, "retry_delays")
    worker_match = _find_file_with_top_function(files, "run_with_retry")
    if not policy_match or not delays_match or not worker_match:
        return None
    policy_path, _policy_tree, policy_class = policy_match
    delays_path, _delays_tree, delays_node = delays_match
    worker_path, _worker_tree, worker_node = worker_match
    if len({policy_path, delays_path, worker_path}) != 3:
        return None
    from_mapping = _method(policy_class, "from_mapping")
    if from_mapping is None:
        return None
    policy_args = _arg_names(from_mapping)
    delay_args = _arg_names(delays_node)
    worker_args = _arg_names(worker_node)
    if len(policy_args) != 2 or len(delay_args) != 1 or len(worker_args) != 3:
        return None
    cls_name, values = policy_args
    policy = delay_args[0]
    operation, worker_policy, sleep = worker_args
    updated_policy = _replace_nodes(
        files[policy_path],
        [
            (
                from_mapping,
                f'''
                @classmethod
                def from_mapping({cls_name}, {values}: Mapping[str, str]) -> "RetryPolicy":
                    try:
                        policy = {cls_name}(
                            max_attempts=int({values}.get("MAX_ATTEMPTS", "3")),
                            base_delay_ms=int({values}.get("BASE_DELAY_MS", "100")),
                            max_delay_ms=int({values}.get("MAX_DELAY_MS", "1000")),
                        )
                    except (TypeError, ValueError):
                        raise ValueError("invalid retry policy") from None
                    if (
                        policy.max_attempts < 1
                        or policy.base_delay_ms < 1
                        or policy.max_delay_ms < 1
                        or policy.base_delay_ms > policy.max_delay_ms
                    ):
                        raise ValueError("invalid retry policy")
                    return policy''',
            )
        ],
    )
    updated_delays = _replace_nodes(
        files[delays_path],
        [
            (
                delays_node,
                f'''def retry_delays({policy}: RetryPolicy) -> tuple[int, ...]:
                    return tuple(
                        min({policy}.base_delay_ms * (2 ** attempt), {policy}.max_delay_ms)
                        for attempt in range({policy}.max_attempts - 1)
                    )''',
            )
        ],
    )
    updated_worker = _replace_nodes(
        files[worker_path],
        [
            (
                worker_node,
                f'''def run_with_retry({operation}: Callable[[], T], {worker_policy}: RetryPolicy, {sleep}: Callable[[float], None]) -> T:
                    delays = retry_delays({worker_policy})
                    for attempt in range({worker_policy}.max_attempts):
                        try:
                            return {operation}()
                        except TransientError:
                            if attempt >= {worker_policy}.max_attempts - 1:
                                raise
                            {sleep}(delays[attempt] / 1000)
                    raise AssertionError("unreachable retry state")''',
            )
        ],
    )
    if updated_policy is None or updated_delays is None or updated_worker is None:
        return None
    return {
        policy_path: updated_policy,
        delays_path: updated_delays,
        worker_path: updated_worker,
    }


def _ledger_skill(files: Mapping[str, str], prompt: str) -> Mapping[str, str] | None:
    lower = prompt.lower()
    required_markers = (
        "event.from_payload",
        "ledgerstore.add_once",
        "record_event",
        "deduplicate",
        "decimal(str(value))",
        "invalid event",
        "two decimal places",
    )
    if not all(marker in lower for marker in required_markers):
        return None
    event_match = _find_file_with_class(files, "Event")
    store_match = _find_file_with_class(files, "LedgerStore")
    service_match = _find_file_with_top_function(files, "record_event")
    if not event_match or not store_match or not service_match:
        return None
    event_path, _event_tree, event_class = event_match
    store_path, _store_tree, store_class = store_match
    service_path, _service_tree, service_node = service_match
    if len({event_path, store_path, service_path}) != 3:
        return None
    from_payload = _method(event_class, "from_payload")
    add_once = _method(store_class, "add_once")
    balance = _method(store_class, "balance")
    if from_payload is None or add_once is None or balance is None:
        return None
    payload_args = _arg_names(from_payload)
    add_args = _arg_names(add_once)
    balance_args = _arg_names(balance)
    service_args = _arg_names(service_node)
    if len(payload_args) != 2 or len(add_args) != 2 or len(balance_args) != 2 or len(service_args) != 2:
        return None
    cls_name, payload = payload_args
    self_add, event = add_args
    self_balance, account = balance_args
    service_payload, store = service_args
    updated_event = _replace_nodes(
        files[event_path],
        [
            (
                from_payload,
                f'''
                @classmethod
                def from_payload({cls_name}, {payload}: Mapping[str, Any]) -> "Event":
                    event_id = str({payload}.get("event_id", "")).strip()
                    account_id = str({payload}.get("account_id", "")).strip()
                    try:
                        amount = Decimal(str({payload}.get("amount", "")))
                    except Exception:
                        raise ValueError("invalid event") from None
                    if not event_id or not account_id or not amount.is_finite() or amount <= 0:
                        raise ValueError("invalid event")
                    return {cls_name}(event_id=event_id, account_id=account_id, amount=amount)''',
            )
        ],
    )
    updated_store = _replace_nodes(
        files[store_path],
        [
            (
                add_once,
                f'''def add_once({self_add}, {event}: Event) -> bool:
                    key = ({event}.account_id, {event}.event_id)
                    if any((item.account_id, item.event_id) == key for item in {self_add}.events):
                        return False
                    {self_add}.events.append({event})
                    return True''',
            ),
            (
                balance,
                f'''def balance({self_balance}, {account}: str) -> Decimal:
                    return sum(
                        (item.amount for item in {self_balance}.events if item.account_id == {account}),
                        Decimal("0"),
                    )''',
            ),
        ],
    )
    updated_service = _replace_nodes(
        files[service_path],
        [
            (
                service_node,
                f'''def record_event({service_payload}: Mapping[str, Any], {store}: LedgerStore) -> dict[str, Any]:
                    event = Event.from_payload({service_payload})
                    accepted = {store}.add_once(event)
                    return {{
                        "accepted": accepted,
                        "event_id": event.event_id,
                        "account_id": event.account_id,
                        "balance": format({store}.balance(event.account_id), ".2f"),
                    }}''',
            )
        ],
    )
    if updated_event is None or updated_store is None or updated_service is None:
        return None
    return {
        event_path: updated_event,
        store_path: updated_store,
        service_path: updated_service,
    }


_SKILLS: tuple[tuple[str, SkillBuilder], ...] = (
    ("python.pagination-envelope.v1", _pagination_skill),
    ("python.bounded-retry.v1", _retry_skill),
    ("python.idempotent-ledger-event.v1", _ledger_skill),
)


def propose_contract_skill_patch(
    repo_path: Path,
    prompt: str,
    approved_files: Sequence[str],
) -> ContractSkillPatch | None:
    normalized_paths = tuple(dict.fromkeys(str(path).replace("\\", "/") for path in approved_files))
    if not (2 <= len(normalized_paths) <= 3):
        return None
    files: dict[str, str] = {}
    for path in normalized_paths:
        target = repo_path / path
        if not target.is_file() or target.suffix.lower() != ".py":
            return None
        files[path] = target.read_text(encoding="utf-8", errors="replace")
    for skill_id, builder in _SKILLS:
        updated = builder(files, prompt)
        if updated is None or set(updated) != set(files):
            continue
        if any(updated[path] == files[path] for path in files):
            continue
        diff = "".join(_unified_diff(path, files[path], updated[path]) for path in sorted(files))
        if not diff.strip():
            continue
        return ContractSkillPatch(
            skill_id=skill_id,
            diff=diff,
            changed_files=tuple(sorted(files)),
            evidence={
                "matched_by": "ast_symbols_and_explicit_contract_markers",
                "approved_files": list(normalized_paths),
                "premium_models_required": False,
                "model_calls_required": 0,
            },
        )
    return None
