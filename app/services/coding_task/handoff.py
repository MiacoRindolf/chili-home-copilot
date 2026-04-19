"""B3 step 1: public module boundary for the read-only handoff/summary builders.

Callers should import from this module instead of ``service``. The underlying
implementation still lives in ``service.py`` until a follow-up moves the
function body across; this shim establishes the new boundary so new code
can target it immediately.
"""
from __future__ import annotations

from .service import (
    build_handoff_dict,
    get_coding_summary_dict,
    list_blockers_dict,
)

__all__ = [
    "build_handoff_dict",
    "get_coding_summary_dict",
    "list_blockers_dict",
]
