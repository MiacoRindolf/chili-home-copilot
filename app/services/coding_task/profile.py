"""B3 step 1: public module boundary for coding-profile write helpers.

Callers should import from this module instead of ``service``. The underlying
implementation still lives in ``service.py`` until a follow-up moves the
function body across; this shim establishes the new boundary so new code
can target it immediately.
"""
from __future__ import annotations

from .service import update_coding_profile

__all__ = ["update_coding_profile"]
