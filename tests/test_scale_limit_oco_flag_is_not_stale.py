"""A plain scale limit must never be mistaken for a protected OCO tranche.

THE DEFECT (measured 2026-09-09). `scale_limit_is_oco` is written True in exactly two
places and was written False in NONE, and it survived recycle while the rest of its own
family (`order_id`, `px`, `qty`, `adopted_qty`, `source`) was cleared. So after any leg
that placed an OCO, the flag stayed True for the remainder of the session.

WHAT THAT COSTS. `_ensure_alpaca_deadman_stop`'s head guard reads it: when it is True the
TRANCHE SPLIT branch arms the deadman for Q - f, on the stated reasoning that "ang OCO(f)
ay may SARILING stop, kaya ang deadman ay sumasakop LAMANG sa runner". A plain limit has no
such stop — it rests ABOVE the market — so f shares were left with no protective stop at
all, silently, with no event. That is the same shape as the S4 defect PATH B's third
revision was written to close.

The reachable writers are `scale_out_limit_placed` and the `sell_into_strength` ladder;
the latter emitted 827 times in the 2026-09-08 bench, so this is a hot path, not a corner.

DB-free — both properties are static.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SRC = (Path(__file__).resolve().parents[1]
        / "app/services/trading/momentum_neural/live_runner.py")


@pytest.fixture(scope="module")
def src() -> str:
    return _SRC.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def recycle_keys(src: str) -> set:
    for n in ast.walk(ast.parse(src)):
        tgt = n.target if isinstance(n, ast.AnnAssign) else (
            n.targets[0] if isinstance(n, ast.Assign) and n.targets else None)
        if isinstance(tgt, ast.Name) and tgt.id == "_RECYCLE_ENTRY_STATE_KEYS":
            return {e.value for e in ast.walk(n.value)
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    pytest.fail("_RECYCLE_ENTRY_STATE_KEYS not found")


def test_every_writer_of_the_order_id_also_states_whether_it_is_an_oco(src: str):
    """The flag must describe the order actually recorded. Any writer of
    scale_limit_order_id that does not set scale_limit_is_oco inherits whatever the
    previous leg left behind — which is exactly how f shares went unprotected."""
    lines = src.splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if not re.search(r'le\["scale_limit_order_id"\]\s*=', line):
            continue
        window = "\n".join(lines[i:i + 14])
        if "scale_limit_is_oco" not in window:
            offenders.append(i + 1)
    assert not offenders, (
        f"scale_limit_order_id written without stating scale_limit_is_oco at lines "
        f"{offenders} — a stale True there arms the deadman for Q-f and leaves the "
        f"tranche naked"
    )


def test_the_whole_scale_limit_identity_family_is_cleared_on_recycle(recycle_keys: set):
    """Half-clearing a family is worse than not clearing it: the surviving half
    describes an order that no longer exists."""
    for key in ("scale_limit_order_id", "scale_limit_px", "scale_limit_qty",
                "scale_limit_adopted_qty", "scale_limit_source",
                "scale_limit_is_oco", "scale_limit_client_order_id",
                "scale_limit_oco_stop", "scale_limit_oco_legs",
                "scale_limit_place_intent"):
        assert key in recycle_keys, f"{key} survives recycle and describes a dead order"


def test_the_flag_is_now_written_both_ways(src: str):
    """Before the fix it was written True twice and False never. A flag with one
    reachable value is not a flag — it is a latch."""
    assert re.search(r'scale_limit_is_oco"\]\s*=\s*True', src)
    assert re.search(r'scale_limit_is_oco"\]\s*=\s*False', src), (
        "nothing sets the flag False; a stale True can still survive"
    )
