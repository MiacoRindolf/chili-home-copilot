"""Ross 08-21 (JUNS) — FACT-BASED reverse-split recency bonus.

Ang lumang headline-dependent na landas ay zero fires kailanman; ang bonus ay
fact-based na (kasapian sa recent-split set mula sa splits reference API).
PURE tests + source contracts. Runnable: pytest tests/test_reverse_split_fact_bonus.py -v
"""
from __future__ import annotations

import inspect

from unittest.mock import patch

from app.services.trading.momentum_neural.catalyst import (
    _catalyst_tilt,
    reverse_split_recency_viability_delta,
)

_CAT = "app.services.trading.momentum_neural.catalyst"


def test_member_with_low_float_gets_half_tilt():
    # 3 pangalan: ang cut ay batch-adaptive percentile; ang JUNS (5M) ay pinakamababa.
    d = reverse_split_recency_viability_delta(
        "JUNS",
        recent_split_symbols={"JUNS", "DPU"},
        floats={"JUNS": 5_000_000, "DPU": 80_000_000, "XXX": 40_000_000},
    )
    assert d == _catalyst_tilt() * 0.5
    assert d > 0


def test_member_without_float_data_gets_small_bonus():
    d = reverse_split_recency_viability_delta(
        "JUNS", recent_split_symbols={"JUNS"}, floats=None,
    )
    assert d == 0.03


def test_nonmember_zero():
    assert reverse_split_recency_viability_delta(
        "AAPL", recent_split_symbols={"JUNS"}, floats=None,
    ) == 0.0


def test_crypto_zero():
    assert reverse_split_recency_viability_delta(
        "BTC-USD", recent_split_symbols={"BTC-USD"}, floats=None,
    ) == 0.0


def test_flag_off_zero():
    with patch(f"{_CAT}.settings") as ms:
        ms.chili_momentum_reverse_split_recency_enabled = False
        assert reverse_split_recency_viability_delta(
            "JUNS", recent_split_symbols={"JUNS"}, floats=None,
        ) == 0.0


def test_never_negative_and_empty_safe():
    assert reverse_split_recency_viability_delta(
        "JUNS", recent_split_symbols=None, floats=None,
    ) == 0.0
    assert reverse_split_recency_viability_delta(
        "", recent_split_symbols={"JUNS"}, floats=None,
    ) == 0.0


def test_future_execution_dates_excluded_source_contract():
    """Audit 2026-08-21: ang splits API ay nagbabalik ng ANNOUNCED/future exec
    dates (DPU 12-17 sa 08-21) — dapat silang ibukod BAGO ang reverse check."""
    from app.services import massive_client

    src = inspect.getsource(massive_client.get_recent_reverse_split_dates)
    guard_at = src.index("xdate > date.today().isoformat()")
    reverse_at = src.index("sto < sfrom")
    assert guard_at < reverse_at, "future-date guard must run before the accept"


def test_scorer_block_meta_driven_no_schema_change():
    """Ang bonus ay meta-driven sa scorer (top-gainer pattern) — WALANG bagong
    field sa ViabilityExternalInputs (protektado ang captured bundle contract),
    at ang dilution carve-out ay kinikilala ang fact-bonus."""
    from app.services.trading.momentum_neural import viability

    fields = set(viability.ViabilityExternalInputs.__dataclass_fields__)
    assert "reverse_split_recency_delta" not in fields

    src = inspect.getsource(viability.score_viability_explicit)
    bonus_at = src.index("reverse_split_recency_deltas")
    dilution_at = src.index("dilution_history_derate_enabled")
    carve_at = src.index("_rs_fact_delta", bonus_at + 1)
    assert bonus_at < dilution_at, "bonus block must run before the dilution derate"
    assert src.index("_is_fresh_squeeze = True") > dilution_at, \
        "carve-out must accept the fact-bonus"
    assert carve_at, "carve-out references the fact delta"
    # WALANG import sa loob ng core — ang delta ay pipeline-computed (meta dict).
    import ast

    core_tree = ast.parse(src)
    assert not any(
        isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(core_tree)
    ), "explicit core must stay import-free (capture determinism)"
