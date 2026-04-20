"""P1.3 production-wiring tests — walk-forward aggregation inside
``web_pattern_researcher._walk_forward_verdict_for_pattern``.

Scope
-----
Unit-tests the aggregation helper that converts per-ticker walk-forward
results into a single tri-state verdict for ``brain_apply_oos_promotion_gate``.

Does NOT exercise:
* The outer ``_quick_backtest_pattern`` flow (that requires a full DB +
  ``backtest_pattern`` network fetch — covered by regression suites).
* ``run_walk_forward`` itself (covered by ``test_walk_forward.py``, 26 cases).

Headline guarantees
-------------------
1. **Flag OFF is a complete no-op** — no imports, no fetches, no network
   calls. ``verdict=None`` + ``audit={enabled: False, reason: flag_off}``.
   This is the migration-safety contract.
2. **Majority rule across tickers** — ≥50% of successful per-ticker WF
   runs must pass for the pattern-level verdict to be ``True``. Below
   the floor → ``False`` (hard reject). Fewer than ``_WALK_FORWARD_MIN_TICKERS``
   successful runs → ``None`` (pending, not enough evidence).
3. **Fail-open on exceptions** — any walk-forward exception returns
   ``None``, never propagates, and records the exception class in audit.
4. **Empty conditions short-circuits** — a pattern whose ``rules_json``
   has no conditions returns ``None`` without calling ``run_walk_forward``.
"""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.trading import web_pattern_researcher as wpr


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def _wf_on(monkeypatch):
    """Turn on chili_walk_forward_enabled for the test body."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_walk_forward_enabled", True)
    yield


@pytest.fixture
def _wf_off(monkeypatch):
    """Ensure chili_walk_forward_enabled is off."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_walk_forward_enabled", False)
    yield


def _mk_pattern(name: str = "demo", conditions: list | None = None) -> SimpleNamespace:
    """Synth a minimal ScanPattern-like object with a rules_json payload.

    ``conditions=None`` means "use the default condition". Pass an explicit
    empty list ``[]`` to exercise the no-conditions code path — we must NOT
    use the ``or``-falsy idiom here because that would silently replace
    ``[]`` with the default.
    """
    if conditions is None:
        conditions = [{"ind": "rsi_14", "op": "<", "val": 30}]
    rules_obj = {"conditions": conditions}
    return SimpleNamespace(
        id=123,
        name=name,
        rules_json=json.dumps(rules_obj),
    )


def _mk_wf_result(*, ok: bool, passes_gate, **kwargs) -> dict:
    """Synth a run_walk_forward return value matching its frozen shape."""
    return {
        "ok": ok,
        "ticker": kwargs.get("ticker", "AAA"),
        "pattern_name": kwargs.get("pattern_name", "demo"),
        "passes_gate": passes_gate,
        "gate_reason": kwargs.get("gate_reason", "ok" if passes_gate else "low_pass_fraction"),
        "aggregate": {
            "n_folds": kwargs.get("n_folds", 5),
            "n_passes": kwargs.get("n_passes", 4 if passes_gate else 1),
            "pass_fraction": kwargs.get("pass_fraction", 0.8 if passes_gate else 0.2),
            "mean_test_win_rate": kwargs.get("mean_test_win_rate", 55.0),
            "std_test_win_rate": 3.0,
            "total_test_trades": 100,
        },
        "folds": [],
        "params": {},
    }


# ─── Flag-off migration safety ───────────────────────────────────────────


class TestFlagOffIsNoOp:
    """The headline migration-safety guarantee."""

    def test_flag_off_returns_none_verdict(self, _wf_off):
        pattern = _mk_pattern()
        verdict, audit = wpr._walk_forward_verdict_for_pattern(
            pattern, ["AAPL", "MSFT"], interval="1d"
        )
        assert verdict is None

    def test_flag_off_audit_records_reason(self, _wf_off):
        pattern = _mk_pattern()
        _, audit = wpr._walk_forward_verdict_for_pattern(
            pattern, ["AAPL", "MSFT"], interval="1d"
        )
        assert audit == {"enabled": False, "reason": "flag_off"}

    def test_flag_off_does_not_call_run_walk_forward(self, _wf_off):
        """No network fetch under flag-off — the socket-exhaustion guard
        that P1.4's first wiring pass taught us to care about.
        """
        pattern = _mk_pattern()
        with patch(
            "app.services.backtest_service.run_walk_forward"
        ) as mock_wf:
            wpr._walk_forward_verdict_for_pattern(
                pattern, ["AAPL"], interval="1d"
            )
            mock_wf.assert_not_called()


# ─── Per-ticker aggregation rules ────────────────────────────────────────


class TestAggregation:
    """Flag-on: how per-ticker verdicts combine into a pattern-level verdict."""

    def test_all_tickers_pass_verdict_true(self, _wf_on):
        pattern = _mk_pattern()
        tickers = ["AAA", "BBB", "CCC"]
        per_ticker = {t: _mk_wf_result(ok=True, passes_gate=True) for t in tickers}

        def fake_wf(ticker, conditions, **kwargs):
            return per_ticker[ticker]

        with patch("app.services.backtest_service.run_walk_forward", side_effect=fake_wf):
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, tickers, interval="1d"
            )
        assert verdict is True
        assert audit["n_ran"] == 3
        assert audit["n_passed"] == 3
        assert audit["reason"] == "majority_passed"
        assert audit["ticker_pass_fraction"] == 1.0

    def test_majority_pass_verdict_true(self, _wf_on):
        """2 of 3 pass → 0.67 ≥ 0.5 threshold → pattern-level True."""
        pattern = _mk_pattern()
        mapping = {
            "AAA": _mk_wf_result(ok=True, passes_gate=True),
            "BBB": _mk_wf_result(ok=True, passes_gate=True),
            "CCC": _mk_wf_result(ok=True, passes_gate=False),
        }

        with patch(
            "app.services.backtest_service.run_walk_forward",
            side_effect=lambda ticker, conditions, **kw: mapping[ticker],
        ):
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, list(mapping.keys()), interval="1d"
            )
        assert verdict is True
        assert audit["n_passed"] == 2
        assert audit["n_ran"] == 3
        assert audit["ticker_pass_fraction"] == pytest.approx(0.667, rel=0.01)

    def test_exact_half_passes_verdict_true(self, _wf_on):
        """Threshold is ≥0.5, not >0.5 — exact half must pass."""
        pattern = _mk_pattern()
        mapping = {
            "AAA": _mk_wf_result(ok=True, passes_gate=True),
            "BBB": _mk_wf_result(ok=True, passes_gate=False),
        }
        with patch(
            "app.services.backtest_service.run_walk_forward",
            side_effect=lambda ticker, conditions, **kw: mapping[ticker],
        ):
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, list(mapping.keys()), interval="1d"
            )
        assert verdict is True
        assert audit["ticker_pass_fraction"] == 0.5

    def test_minority_pass_verdict_false(self, _wf_on):
        """1 of 3 passes → 0.33 < 0.5 → pattern-level False (hard reject)."""
        pattern = _mk_pattern()
        mapping = {
            "AAA": _mk_wf_result(ok=True, passes_gate=True),
            "BBB": _mk_wf_result(ok=True, passes_gate=False),
            "CCC": _mk_wf_result(ok=True, passes_gate=False),
        }
        with patch(
            "app.services.backtest_service.run_walk_forward",
            side_effect=lambda ticker, conditions, **kw: mapping[ticker],
        ):
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, list(mapping.keys()), interval="1d"
            )
        assert verdict is False
        assert audit["reason"] == "majority_failed"

    def test_all_tickers_fail_verdict_false(self, _wf_on):
        pattern = _mk_pattern()
        mapping = {t: _mk_wf_result(ok=True, passes_gate=False) for t in ("A", "B", "C")}
        with patch(
            "app.services.backtest_service.run_walk_forward",
            side_effect=lambda ticker, conditions, **kw: mapping[ticker],
        ):
            verdict, _ = wpr._walk_forward_verdict_for_pattern(
                pattern, list(mapping.keys()), interval="1d"
            )
        assert verdict is False


# ─── Insufficient evidence → pending ─────────────────────────────────────


class TestInsufficientEvidence:
    """Fewer than ``_WALK_FORWARD_MIN_TICKERS`` successful runs → None."""

    def test_zero_tickers_ran_verdict_none(self, _wf_on):
        """All tickers failed to complete WF (e.g. insufficient history).
        n_ran=0 → below floor → pending, not False."""
        pattern = _mk_pattern()
        mapping = {t: _mk_wf_result(ok=False, passes_gate=None,
                                    gate_reason="insufficient_history")
                   for t in ("A", "B", "C")}
        with patch(
            "app.services.backtest_service.run_walk_forward",
            side_effect=lambda ticker, conditions, **kw: mapping[ticker],
        ):
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, list(mapping.keys()), interval="1d"
            )
        assert verdict is None
        assert audit["reason"] == "insufficient_ticker_coverage"
        assert audit["n_ran"] == 0

    def test_one_ticker_ran_verdict_none(self, _wf_on):
        """One ticker succeeded, two errored → n_ran=1 < floor (2) → pending."""
        pattern = _mk_pattern()
        mapping = {
            "A": _mk_wf_result(ok=True, passes_gate=True),
            "B": _mk_wf_result(ok=False, passes_gate=None),
            "C": _mk_wf_result(ok=False, passes_gate=None),
        }
        with patch(
            "app.services.backtest_service.run_walk_forward",
            side_effect=lambda ticker, conditions, **kw: mapping[ticker],
        ):
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, list(mapping.keys()), interval="1d"
            )
        assert verdict is None
        assert audit["n_ran"] == 1
        assert audit["reason"] == "insufficient_ticker_coverage"


# ─── Fail-open on exceptions ─────────────────────────────────────────────


class TestFailOpen:
    """Any exception in the WF path returns None, never raises."""

    def test_per_ticker_exception_is_caught(self, _wf_on):
        """One ticker raises, two succeed → pattern verdict still computable."""
        pattern = _mk_pattern()

        def fake_wf(ticker, conditions, **kwargs):
            if ticker == "BAD":
                raise RuntimeError("synthetic polygon 500")
            return _mk_wf_result(ok=True, passes_gate=True, ticker=ticker)

        with patch(
            "app.services.backtest_service.run_walk_forward",
            side_effect=fake_wf,
        ):
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, ["AAA", "BAD", "BBB"], interval="1d"
            )
        # 2 ran + passed, 1 errored. 2 >= floor → majority → True.
        assert verdict is True
        assert audit["n_ran"] == 2
        # The errored ticker's per-ticker audit should carry an "error" key.
        bad_row = next(r for r in audit["per_ticker"] if r["ticker"] == "BAD")
        assert "error" in bad_row
        assert "RuntimeError" in bad_row["error"]

    def test_import_failure_returns_none(self, _wf_on):
        """If ``run_walk_forward`` itself can't import, we return None
        rather than crash the promotion flow."""
        pattern = _mk_pattern()
        # Temporarily shadow the backtest_service module with a stub
        # that raises on attribute lookup of run_walk_forward.
        import app.services.backtest_service as real_bs
        broken = types.ModuleType("app.services.backtest_service")

        def _raise(*a, **k):
            raise ImportError("synthetic import failure")

        broken.run_walk_forward = _raise  # type: ignore[attr-defined]
        # Copy everything else so `from ..backtest_service import run_walk_forward`
        # inside the helper resolves to the raiser.
        for k in dir(real_bs):
            if not k.startswith("_") and k != "run_walk_forward":
                setattr(broken, k, getattr(real_bs, k))

        saved = sys.modules["app.services.backtest_service"]
        sys.modules["app.services.backtest_service"] = broken
        try:
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, ["AAA", "BBB"], interval="1d"
            )
        finally:
            sys.modules["app.services.backtest_service"] = saved

        # Each per-ticker call caught its own ImportError → no tickers
        # actually ran → n_ran=0 → insufficient_ticker_coverage → None.
        # (An outer exception would also yield None via the broad except;
        # either path satisfies the fail-open contract.)
        assert verdict is None


# ─── Empty / malformed rules ─────────────────────────────────────────────


class TestRulesValidation:
    """Patterns with no usable conditions short-circuit to None."""

    def test_no_conditions_returns_none(self, _wf_on):
        pattern = _mk_pattern(conditions=[])
        with patch(
            "app.services.backtest_service.run_walk_forward"
        ) as mock_wf:
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, ["AAA", "BBB"], interval="1d"
            )
            mock_wf.assert_not_called()
        assert verdict is None
        assert audit["reason"] == "no_conditions"

    def test_malformed_rules_json_returns_none(self, _wf_on):
        pattern = SimpleNamespace(
            id=1, name="bad", rules_json="{not valid json",
        )
        with patch(
            "app.services.backtest_service.run_walk_forward"
        ) as mock_wf:
            verdict, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, ["AAA", "BBB"], interval="1d"
            )
            mock_wf.assert_not_called()
        assert verdict is None
        assert audit["reason"] == "no_conditions"

    def test_empty_rules_json_returns_none(self, _wf_on):
        pattern = SimpleNamespace(id=1, name="empty", rules_json=None)
        with patch(
            "app.services.backtest_service.run_walk_forward"
        ) as mock_wf:
            verdict, _ = wpr._walk_forward_verdict_for_pattern(
                pattern, ["AAA", "BBB"], interval="1d"
            )
            mock_wf.assert_not_called()
        assert verdict is None


# ─── Audit shape ─────────────────────────────────────────────────────────


class TestAuditShape:
    """Confirms the audit dict is JSON-safe and carries the fields
    an operator needs to understand the pattern-level verdict."""

    def test_audit_includes_per_ticker_diagnostics(self, _wf_on):
        pattern = _mk_pattern()
        mapping = {
            "A": _mk_wf_result(ok=True, passes_gate=True, n_folds=6, n_passes=5,
                               pass_fraction=0.83, mean_test_win_rate=58.0),
            "B": _mk_wf_result(ok=True, passes_gate=False, n_folds=6, n_passes=2,
                               pass_fraction=0.33, mean_test_win_rate=42.0),
        }
        with patch(
            "app.services.backtest_service.run_walk_forward",
            side_effect=lambda ticker, conditions, **kw: mapping[ticker],
        ):
            _, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, list(mapping.keys()), interval="1d"
            )
        assert audit["enabled"] is True
        assert audit["tickers_requested"] == ["A", "B"]
        assert len(audit["per_ticker"]) == 2
        a_row = next(r for r in audit["per_ticker"] if r["ticker"] == "A")
        assert a_row["ok"] is True
        assert a_row["passes_gate"] is True
        assert a_row["n_folds"] == 6
        assert a_row["n_passes"] == 5
        assert a_row["pass_fraction"] == 0.83
        assert a_row["mean_test_win_rate"] == 58.0

    def test_audit_is_json_serializable(self, _wf_on):
        pattern = _mk_pattern()
        mapping = {
            "A": _mk_wf_result(ok=True, passes_gate=True),
            "B": _mk_wf_result(ok=True, passes_gate=False),
        }
        with patch(
            "app.services.backtest_service.run_walk_forward",
            side_effect=lambda ticker, conditions, **kw: mapping[ticker],
        ):
            _, audit = wpr._walk_forward_verdict_for_pattern(
                pattern, list(mapping.keys()), interval="1d"
            )
        # Must round-trip through json — operator forensics land in
        # ``oos_validation_json`` which is a Postgres JSONB column.
        roundtripped = json.loads(json.dumps(audit))
        assert roundtripped["n_ran"] == 2
        assert roundtripped["verdict"] is True
