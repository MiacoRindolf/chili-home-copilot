"""Convert nested structures to JSON-serializable Python types.

Starlette's ``JSONResponse`` uses ``json.dumps(..., allow_nan=False)``, so **NaN and
Infinity must never** reach the encoder — they raise ``ValueError`` and produce 500s.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def to_jsonable(obj: Any) -> Any:
    """Recursively coerce values so ``json.dumps(..., allow_nan=False)`` succeeds."""
    if obj is None:
        return None

    # bool is a subclass of int — handle before int
    if type(obj) is bool:
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        return float(obj)

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        try:
            f = float(obj)
            return None if not math.isfinite(f) else f
        except Exception:
            return str(obj)

    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]

    # numpy / pandas scalar (and similar)
    if hasattr(obj, "item"):
        try:
            return to_jsonable(obj.item())
        except Exception:
            pass

    try:
        iv = int(obj)
        # Only treat as int if lossless (avoid turning float 1.5 into 1)
        if type(obj) not in (float, int) and not isinstance(obj, bool):
            if iv == obj:
                return iv
    except Exception:
        pass

    try:
        fv = float(obj)
        if math.isfinite(fv):
            return fv
        return None
    except Exception:
        pass

    return str(obj)
