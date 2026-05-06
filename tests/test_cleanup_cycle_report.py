"""Tests for f-cleanup-cycle-report (Phase 8 of f-overnight-jumbo).

Drops the dead generate_and_store_cycle_report path and the
learning_cycle_report.py module. The function had one caller (the
gated-off run_learning_cycle); the module had no other public users.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_learning_cycle_report_module_deleted():
    """The dead module must be gone."""
    target = REPO / "app/services/trading/learning_cycle_report.py"
    assert not target.exists(), (
        f"learning_cycle_report.py was supposed to be deleted, "
        f"still exists at {target}"
    )


def test_learning_cycle_report_not_importable():
    """Trying to import the deleted module must fail (catches a
    future revert that adds it back without notice)."""
    import importlib
    try:
        importlib.import_module("app.services.trading.learning_cycle_report")
    except ImportError:
        return  # expected
    assert False, "learning_cycle_report module is back -- revert intended?"


def test_learning_py_no_longer_imports_cycle_report():
    """learning.py's run_learning_cycle had the only call site. After
    the cleanup the import + call must be gone (the cleanup marker
    comment is allowed to mention the dropped names for traceability)."""
    src = (REPO / "app/services/trading/learning.py").read_text()
    assert "from .learning_cycle_report" not in src, (
        "import statement must be gone"
    )
    # The CALL syntax `generate_and_store_cycle_report(` (with paren) is
    # what the brief asks to remove. The bare-name reference inside the
    # cleanup-marker comment is intentional and traceable.
    assert "generate_and_store_cycle_report(" not in src, (
        "function call site must be gone"
    )
    # The replacement comment / cleanup-marker must be present.
    assert "f-cleanup-cycle-report" in src


def test_architecture_metadata_dropped_cycle_report_step():
    """The CycleStepDef for cycle_report was removed from the
    architecture metadata; the f-cleanup-cycle-report marker comment
    is in its place."""
    src = (REPO / "app/services/trading/learning_cycle_architecture.py").read_text()
    assert 'sid="cycle_report"' not in src, (
        "cycle_report CycleStepDef should have been dropped"
    )
    assert "f-cleanup-cycle-report" in src
