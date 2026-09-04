"""A scorer that failed to import must be reported WITH its import error.

MEASURED 2026-09-04: from a worktree without a ``.env`` the app Settings import fails
(``database_url Field required``), ``resolve_scorer`` returns its stub, and the report said
only "scorer exposes no classify_first_divergence" with every stage ``unavailable`` -- a
message that reads as a scorer regression when it is an environment gap.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rossbench_report.py"
_spec = importlib.util.spec_from_file_location("rossbench_report_scorer_unavailable", _PATH)
rr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rr  # dataclasses resolve the module through sys.modules
assert _spec.loader is not None
_spec.loader.exec_module(rr)


class _MissingScorer:
    unavailable_reason = "import app...ross_bench_scoring failed: ValidationError(database_url Field required)"


def test_stage_problem_carries_the_import_error():
    problems = list(rr.apply_scorer_stages([], _MissingScorer()))
    assert problems, "an unavailable scorer must be reported"
    assert "database_url Field required" in problems[0]
    assert "classify_first_divergence" in problems[0]


def test_rpi_problem_carries_the_import_error():
    _rpi, problems = rr.compute_rpi([], _MissingScorer(), arms=["base"], ross_equity={})
    assert any("database_url Field required" in p and "ross_parity_index" in p for p in problems), problems


def test_a_present_scorer_gets_no_prefix():
    class _Present:
        pass
    assert rr._scorer_why(_Present()) == ""
