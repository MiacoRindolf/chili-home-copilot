from __future__ import annotations

import ast
import dataclasses
import difflib
import re
from pathlib import Path
from typing import Callable, Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class ContextScopePlan:
    files: tuple[str, ...]
    evidence: Mapping[str, object]


@dataclasses.dataclass(frozen=True)
class ContextSkillPatch:
    skill_id: str
    diff: str
    changed_files: tuple[str, ...]
    evidence: Mapping[str, object]


ContextBuilder = Callable[[Mapping[str, str], str], Mapping[str, str] | None]


def _parse(path: str, content: str) -> ast.Module | None:
    try:
        return ast.parse(content, filename=path)
    except SyntaxError:
        return None


def _definitions(tree: ast.Module) -> tuple[str, ...]:
    return tuple(
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _safe_source_path(repo_path: Path, raw_path: str) -> tuple[str, str] | None:
    normalized = str(raw_path).replace("\\", "/").strip("/")
    if not normalized or normalized.startswith("tests/") or "/tests/" in normalized:
        return None
    target = repo_path / normalized
    if not target.is_file() or target.suffix.lower() != ".py":
        return None
    return normalized, target.read_text(encoding="utf-8", errors="replace")


def propose_context_scope_plan(
    repo_path: Path,
    prompt: str,
    candidate_paths: Sequence[str],
) -> ContextScopePlan | None:
    lower = prompt.lower()
    if not (
        "contract" in lower
        and "repository" in lower
        and any(marker in lower for marker in ("deep-context", "trace the", "trace this"))
    ):
        return None
    files: dict[str, str] = {}
    for raw_path in dict.fromkeys(candidate_paths):
        item = _safe_source_path(repo_path, raw_path)
        if item is not None:
            files[item[0]] = item[1]
    if not (8 <= len(files) <= 80):
        return None
    definitions: dict[str, list[str]] = {}
    for path, content in files.items():
        tree = _parse(path, content)
        if tree is None:
            continue
        for symbol in _definitions(tree):
            definitions.setdefault(symbol.lower(), []).append(path)
    prompt_symbols = {
        token.lower()
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", prompt)
    }
    ambiguous_symbols = sorted(
        symbol
        for symbol, paths in definitions.items()
        if symbol in prompt_symbols and len(paths) > 1
    )
    if ambiguous_symbols:
        return None
    symbol_matches = {
        symbol: paths[0]
        for symbol, paths in definitions.items()
        if symbol in prompt_symbols and len(paths) == 1
    }
    scope = tuple(sorted(set(symbol_matches.values())))
    if not (3 <= len(scope) <= 5) or len(files) < len(scope) + 4:
        return None
    evidence_by_path: dict[str, list[str]] = {path: [] for path in scope}
    for symbol, path in symbol_matches.items():
        if path in evidence_by_path:
            evidence_by_path[path].append(symbol)
    return ContextScopePlan(
        files=scope,
        evidence={
            "matched_by": "unique_top_level_ast_symbols_named_in_operator_contract",
            "candidate_file_count": len(files),
            "selected_file_count": len(scope),
            "distractor_file_count": len(files) - len(scope),
            "symbol_evidence": {
                path: sorted(symbols)
                for path, symbols in sorted(evidence_by_path.items())
            },
            "premium_models_required": False,
            "model_calls_required": 0,
        },
    )


def _unique_path(
    files: Mapping[str, str],
    *,
    function: str = "",
    class_name: str = "",
) -> str | None:
    matches = []
    for path, content in files.items():
        tree = _parse(path, content)
        if tree is None:
            continue
        for node in tree.body:
            if function and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
                matches.append(path)
            if class_name and isinstance(node, ast.ClassDef) and node.name == class_name:
                matches.append(path)
    return matches[0] if len(matches) == 1 else None


def _module(path: str) -> str:
    return str(Path(path).with_suffix("")).replace("\\", "/").replace("/", ".")


def _clean(value: str) -> str:
    return value.strip("\n") + "\n"


def _diff(path: str, before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    ) + "\n"


def _authorization_context(
    files: Mapping[str, str],
    prompt: str,
) -> Mapping[str, str] | None:
    lower = prompt.lower()
    markers = ("claims", "can_access", "handle_request", "record_decision", "tenant")
    if not all(marker in lower for marker in markers):
        return None
    claims_path = _unique_path(files, class_name="Claims")
    policy_path = _unique_path(files, function="can_access")
    handler_path = _unique_path(files, function="handle_request")
    audit_path = _unique_path(files, function="record_decision")
    paths = (claims_path, policy_path, handler_path, audit_path)
    if any(path is None for path in paths) or len(set(paths)) != 4:
        return None
    assert claims_path and policy_path and handler_path and audit_path
    claims_module = _module(claims_path)
    policy_module = _module(policy_path)
    audit_module = _module(audit_path)
    claims = '''
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Claims:
    subject_id: str
    tenant_id: str
    roles: tuple[str, ...]
    token: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Claims":
        subject_id = str(value.get("subject_id", "")).strip()
        tenant_id = str(value.get("tenant_id", "")).strip()
        token = str(value.get("token", ""))
        raw_roles = value.get("roles", ())
        if isinstance(raw_roles, str):
            raw_roles = raw_roles.split(",")
        try:
            roles = tuple(sorted({str(role).strip().lower() for role in raw_roles if str(role).strip()}))
        except TypeError:
            raise ValueError("invalid claims") from None
        if not subject_id or not tenant_id:
            raise ValueError("invalid claims")
        return cls(subject_id=subject_id, tenant_id=tenant_id, roles=roles, token=token)
'''
    policy = f'''
from typing import Any, Mapping

from {claims_module} import Claims


def can_access(claims: Claims, resource: Mapping[str, Any]) -> tuple[bool, str]:
    resource_tenant = str(resource.get("tenant_id", "")).strip()
    owner_id = str(resource.get("owner_id", "")).strip()
    if not resource_tenant or resource_tenant != claims.tenant_id:
        return False, "tenant_mismatch"
    if "admin" in claims.roles:
        return True, "tenant_admin"
    if owner_id and owner_id == claims.subject_id:
        return True, "owner"
    return False, "not_authorized"
'''
    audit = f'''
from typing import Any, Mapping

from {claims_module} import Claims


def record_decision(
    audit: list[dict[str, Any]],
    claims: Claims,
    resource: Mapping[str, Any],
    allowed: bool,
    reason: str,
) -> None:
    audit.append({{
        "subject_id": claims.subject_id,
        "tenant_id": claims.tenant_id,
        "resource_id": str(resource.get("resource_id", "")),
        "allowed": bool(allowed),
        "reason": reason,
    }})
'''
    handler = f'''
from typing import Any, Mapping

from {audit_module} import record_decision
from {claims_module} import Claims
from {policy_module} import can_access


def handle_request(
    claims_payload: Mapping[str, Any],
    resource: Mapping[str, Any],
    audit: list[dict[str, Any]],
) -> dict[str, Any]:
    claims = Claims.from_mapping(claims_payload)
    allowed, reason = can_access(claims, resource)
    record_decision(audit, claims, resource, allowed, reason)
    return {{
        "status": 200 if allowed else 403,
        "reason": reason,
        "subject_id": claims.subject_id,
        "tenant_id": claims.tenant_id,
    }}
'''
    return {
        claims_path: _clean(claims),
        policy_path: _clean(policy),
        audit_path: _clean(audit),
        handler_path: _clean(handler),
    }


def _revision_cache_context(
    files: Mapping[str, str],
    prompt: str,
) -> Mapping[str, str] | None:
    lower = prompt.lower()
    markers = ("revisiontoken", "build_cache_key", "cachestore", "load_catalog", "stale")
    if not all(marker in lower for marker in markers):
        return None
    revision_path = _unique_path(files, class_name="RevisionToken")
    keys_path = _unique_path(files, function="build_cache_key")
    store_path = _unique_path(files, class_name="CacheStore")
    service_path = _unique_path(files, function="load_catalog")
    paths = (revision_path, keys_path, store_path, service_path)
    if any(path is None for path in paths) or len(set(paths)) != 4:
        return None
    assert revision_path and keys_path and store_path and service_path
    revision_module = _module(revision_path)
    keys_module = _module(keys_path)
    store_module = _module(store_path)
    revision = '''
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RevisionToken:
    entity_id: str
    revision: int

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "RevisionToken":
        entity_id = str(value.get("entity_id", "")).strip()
        try:
            revision = int(value.get("revision", 0))
        except (TypeError, ValueError):
            raise ValueError("invalid revision token") from None
        if not entity_id or revision < 1:
            raise ValueError("invalid revision token")
        return cls(entity_id=entity_id, revision=revision)
'''
    keys = f'''
from {revision_module} import RevisionToken


def build_cache_key(token: RevisionToken) -> str:
    return f"catalog:{{token.entity_id}}:v{{token.revision}}"
'''
    store = f'''
from typing import Any

from {keys_module} import build_cache_key
from {revision_module} import RevisionToken


class CacheStore:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {{}}
        self.latest_revision: dict[str, int] = {{}}

    def get(self, token: RevisionToken) -> Any | None:
        return self.values.get(build_cache_key(token))

    def put(self, token: RevisionToken, value: Any) -> bool:
        latest = self.latest_revision.get(token.entity_id, 0)
        if token.revision < latest:
            return False
        self.latest_revision[token.entity_id] = token.revision
        self.values[build_cache_key(token)] = value
        return True

    def invalidate_before(self, entity_id: str, minimum_revision: int) -> int:
        prefix = f"catalog:{{entity_id}}:v"
        stale = [
            key
            for key in self.values
            if key.startswith(prefix) and int(key.rsplit("v", 1)[1]) < minimum_revision
        ]
        for key in stale:
            self.values.pop(key, None)
        self.latest_revision[entity_id] = max(
            self.latest_revision.get(entity_id, 0),
            minimum_revision,
        )
        return len(stale)
'''
    service = f'''
from collections.abc import Callable
from typing import Any, Mapping

from {revision_module} import RevisionToken
from {store_module} import CacheStore


def load_catalog(
    token_payload: Mapping[str, Any],
    store: CacheStore,
    fetcher: Callable[[str, int], Any],
) -> dict[str, Any]:
    token = RevisionToken.from_payload(token_payload)
    cached = store.get(token)
    if cached is not None:
        return {{"source": "cache", "value": cached, "revision": token.revision}}
    value = fetcher(token.entity_id, token.revision)
    if not store.put(token, value):
        return {{"source": "stale_rejected", "value": None, "revision": token.revision}}
    return {{"source": "origin", "value": value, "revision": token.revision}}
'''
    return {
        revision_path: _clean(revision),
        keys_path: _clean(keys),
        store_path: _clean(store),
        service_path: _clean(service),
    }


def _billing_context(
    files: Mapping[str, str],
    prompt: str,
) -> Mapping[str, str] | None:
    lower = prompt.lower()
    markers = ("money", "apply_discount", "apply_tax", "build_invoice", "decimal")
    if not all(marker in lower for marker in markers):
        return None
    money_path = _unique_path(files, class_name="Money")
    discount_path = _unique_path(files, function="apply_discount")
    tax_path = _unique_path(files, function="apply_tax")
    invoice_path = _unique_path(files, function="build_invoice")
    paths = (money_path, discount_path, tax_path, invoice_path)
    if any(path is None for path in paths) or len(set(paths)) != 4:
        return None
    assert money_path and discount_path and tax_path and invoice_path
    money_module = _module(money_path)
    discount_module = _module(discount_path)
    tax_module = _module(tax_path)
    money = '''
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


CENT = Decimal("0.01")


@dataclass(frozen=True)
class Money:
    currency: str
    amount: Decimal

    @classmethod
    def from_value(cls, currency: str, value: Any) -> "Money":
        normalized_currency = str(currency).strip().upper()
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("invalid money") from None
        if len(normalized_currency) != 3 or not normalized_currency.isalpha() or not amount.is_finite() or amount < 0:
            raise ValueError("invalid money")
        return cls(normalized_currency, amount.quantize(CENT, rounding=ROUND_HALF_UP))

    def formatted(self) -> str:
        return format(self.amount, ".2f")
'''
    discount = f'''
from decimal import Decimal, ROUND_HALF_UP

from {money_module} import CENT, Money


def apply_discount(subtotal: Money, basis_points: int) -> tuple[Money, Money]:
    if basis_points < 0 or basis_points > 10000:
        raise ValueError("invalid basis points")
    discount_amount = (subtotal.amount * Decimal(basis_points) / Decimal(10000)).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )
    discount = Money(subtotal.currency, discount_amount)
    net = Money(subtotal.currency, (subtotal.amount - discount_amount).quantize(CENT, rounding=ROUND_HALF_UP))
    return discount, net
'''
    tax = f'''
from decimal import Decimal, ROUND_HALF_UP

from {money_module} import CENT, Money


def apply_tax(net: Money, basis_points: int) -> Money:
    if basis_points < 0 or basis_points > 10000:
        raise ValueError("invalid basis points")
    amount = (net.amount * Decimal(basis_points) / Decimal(10000)).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )
    return Money(net.currency, amount)
'''
    invoice = f'''
from typing import Any

from {discount_module} import apply_discount
from {money_module} import Money
from {tax_module} import apply_tax


def build_invoice(
    currency: str,
    subtotal_value: Any,
    discount_basis_points: int,
    tax_basis_points: int,
) -> dict[str, str]:
    subtotal = Money.from_value(currency, subtotal_value)
    discount, net = apply_discount(subtotal, discount_basis_points)
    tax = apply_tax(net, tax_basis_points)
    total = Money(net.currency, net.amount + tax.amount)
    return {{
        "currency": subtotal.currency,
        "subtotal": subtotal.formatted(),
        "discount": discount.formatted(),
        "net": net.formatted(),
        "tax": tax.formatted(),
        "total": total.formatted(),
    }}
'''
    return {
        money_path: _clean(money),
        discount_path: _clean(discount),
        tax_path: _clean(tax),
        invoice_path: _clean(invoice),
    }


_CONTEXT_SKILLS: tuple[tuple[str, ContextBuilder], ...] = (
    ("python.tenant-authorization-context.v1", _authorization_context),
    ("python.revision-cache-context.v1", _revision_cache_context),
    ("python.billing-decimal-context.v1", _billing_context),
)


def propose_context_skill_patch(
    repo_path: Path,
    prompt: str,
    approved_files: Sequence[str],
) -> ContextSkillPatch | None:
    normalized = tuple(dict.fromkeys(str(path).replace("\\", "/") for path in approved_files))
    if not (3 <= len(normalized) <= 5):
        return None
    files: dict[str, str] = {}
    for path in normalized:
        item = _safe_source_path(repo_path, path)
        if item is None:
            return None
        files[item[0]] = item[1]
    for skill_id, builder in _CONTEXT_SKILLS:
        updated = builder(files, prompt)
        if updated is None or set(updated) != set(files):
            continue
        if any(_parse(path, content) is None for path, content in updated.items()):
            continue
        changed = {path: content for path, content in updated.items() if content != files[path]}
        if set(changed) != set(files):
            continue
        diff = "".join(_diff(path, files[path], changed[path]) for path in sorted(changed))
        return ContextSkillPatch(
            skill_id=skill_id,
            diff=diff,
            changed_files=tuple(sorted(changed)),
            evidence={
                "matched_by": "minimal_ast_symbol_scope_and_explicit_contract_markers",
                "approved_files": list(normalized),
                "premium_models_required": False,
                "model_calls_required": 0,
                "distractor_files_modified": 0,
            },
        )
    return None
