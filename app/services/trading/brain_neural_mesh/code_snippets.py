"""Code snippet resolution from ``code_ref`` labels (extracted from brain_network_graph).

Used by the unified neural graph projection to provide source code excerpts
in node detail views.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

# Per-callable line cap: run_learning_cycle is ~700 lines; keep headroom for growth.
# Total cap: multi-part code_ref (e.g. ``a + b``) concatenates several callables.
_MAX_LINES_PER_CALLABLE = 20_000
_MAX_SNIPPET_CHARS = 2_000_000

# Bump when snippet rules change so in-process cache does not serve stale truncated text.
_SNIPPET_CACHE_VERSION = 3

_SHORT_MODULE_PREFIX: dict[str, str] = {
    "learning": "app.services.trading.learning",
    "prescreener": "app.services.trading.prescreener",
    "prescreen_job": "app.services.trading.prescreen_job",
    "scanner": "app.services.trading.scanner",
    "trading_scheduler": "app.services.trading_scheduler",
    "journal": "app.services.trading.journal",
    "pattern_ml": "app.services.trading.pattern_ml",
    "alerts": "app.services.trading.alerts",
    "learning_cycle_report": "app.services.trading.learning_cycle_report",
}

# Bare symbol names that are not on ``learning`` but appear in compound code_ref strings.
_BARE_SYMBOL_MODULE: dict[str, str] = {
    "log_learning_event": "app.services.trading.learning_events",
}

_SNIPPET_CACHE: dict[tuple[int, str], str] = {}


def _strip_trailing_parens_annotation(s: str) -> str:
    s = s.strip()
    while True:
        new = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
        if new == s:
            break
        s = new
    return s


def _cluster_synthetic_ref(ref: str) -> bool:
    return ref.strip().startswith("run_learning_cycle →")


def _first_dotted_identifier(token: str) -> str | None:
    m = re.match(r"^([\w.]+)", token.strip())
    return m.group(1) if m else None


def _resolve_ref_piece(piece: str, inherit_module: str | None) -> tuple[str, str] | None:
    piece = _strip_trailing_parens_annotation(piece)
    if not piece:
        return None
    head = _first_dotted_identifier(piece) or ""
    if not head:
        return None

    if head.startswith("app."):
        mod, _, attr = head.rpartition(".")
        if mod and attr:
            return mod, attr
        return None

    if "." in head:
        short, _, attr = head.partition(".")
        base = _SHORT_MODULE_PREFIX.get(short)
        if base:
            return base, attr
        return None

    mod = _BARE_SYMBOL_MODULE.get(head) or inherit_module or "app.services.trading.learning"
    return mod, head


def _split_ref_pieces(ref: str) -> list[str]:
    ref = _strip_trailing_parens_annotation(ref)
    return [p.strip() for p in re.split(r"\s+\+\s+", ref) if p.strip()]


def _snippet_for_callable(obj: Any, qual: str) -> str:
    try:
        lines, start = inspect.getsourcelines(obj)
    except (OSError, TypeError) as exc:
        return f"# {qual}\n# (source unavailable: {exc})\n"
    out = lines
    if len(out) > _MAX_LINES_PER_CALLABLE:
        extra = len(out) - _MAX_LINES_PER_CALLABLE
        end_line = start + len(out) - 1
        tail = start + _MAX_LINES_PER_CALLABLE - 1
        out = out[:_MAX_LINES_PER_CALLABLE] + [
            f"\n# ... truncated: omitted {extra} line(s); "
            f"source spans ~{start}–{end_line}, showing ~{start}–{tail} ...\n"
        ]
    return f"# --- {qual} (line {start}) ---\n" + "".join(out)


def build_code_snippet_from_ref(code_ref: str) -> str:
    """Resolve ``code_ref`` labels from ``learning_cycle_architecture`` to Python source excerpts."""
    ref = (code_ref or "").strip()
    if not ref:
        return ""
    ck = (_SNIPPET_CACHE_VERSION, ref)
    if ck in _SNIPPET_CACHE:
        return _SNIPPET_CACHE[ck]
    if _cluster_synthetic_ref(ref):
        out = (
            "# This cluster groups several steps inside run_learning_cycle.\n"
            "# Open the root node or step nodes for callable source.\n"
            f"# Reference: {ref}\n"
        )
        _SNIPPET_CACHE[ck] = out
        return out

    pieces = _split_ref_pieces(ref)
    if not pieces:
        out = "# (empty code reference)\n"
        _SNIPPET_CACHE[ck] = out
        return out

    chunks: list[str] = []
    inherit: str | None = None
    for piece in pieces:
        resolved = _resolve_ref_piece(piece, inherit)
        if not resolved:
            chunks.append(f"# Could not resolve: {piece!r}\n\n")
            continue
        mod_name, attr = resolved
        try:
            mod = importlib.import_module(mod_name)
            obj = getattr(mod, attr)
        except Exception as exc:
            chunks.append(f"# {mod_name}.{attr} — could not load: {exc}\n\n")
            continue
        inherit = mod_name
        if callable(obj):
            chunks.append(_snippet_for_callable(obj, f"{mod_name}.{attr}"))
        else:
            chunks.append(
                f"# {mod_name}.{attr} is not callable (type={type(obj).__name__})\n\n"
            )
        chunks.append("\n")

    out = "".join(chunks).strip()
    if len(out) > _MAX_SNIPPET_CHARS:
        out = (
            out[: _MAX_SNIPPET_CHARS - 120]
            + f"\n\n# ... truncated: total snippet exceeded {_MAX_SNIPPET_CHARS} characters ...\n"
        )
    _SNIPPET_CACHE[ck] = out
    return out
