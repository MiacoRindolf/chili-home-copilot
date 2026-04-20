"""B3 step 1: public module boundary for the validation runner.

Callers should import from this module instead of ``service``. The underlying
implementation still lives in ``service.py`` until a follow-up moves the
function body across; this shim establishes the new boundary so new code
can target it immediately.
"""
from __future__ import annotations

from .service import (
    get_run_detail_dict,
    list_validation_runs_metadata_dict,
    run_validation_for_task,
)

__all__ = [
    "get_run_detail_dict",
    "list_validation_runs_metadata_dict",
    "run_validation_for_task",
]
