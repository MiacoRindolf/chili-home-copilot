from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.services.trading.shadow_testing as shadow_testing
from app.services.trading.shadow_testing import _extract_trade_returns


def _shadow_rows(
    n: int = shadow_testing.MIN_TRADES_FOR_COMPARISON,
    *,
    entry_date=None,
    group: str | None = None,
) -> list[object]:
    return [SimpleNamespace(entry_date=entry_date, _group=group) for _ in range(n)]


def _patch_all_significant_better_tests(monkeypatch) -> None:
    better_result = {
        "p_value": 0.001,
        "significant": True,
        "variant_better": True,
    }
    monkeypatch.setattr(
        shadow_testing,
        "_welch_return_ttest",
        lambda _control, _variant: dict(better_result),
    )
    monkeypatch.setattr(
        shadow_testing,
        "_bootstrap_sharpe_difference",
        lambda _control, _variant: dict(better_result),
    )
    monkeypatch.setattr(
        shadow_testing,
        "_sharpe_ratio_ztest",
        lambda _control, _variant: dict(better_result),
    )


class _OneShotPatternQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *_args):
        return self

    def first(self):
        return self.row


class _CreateShadowTestDb:
    def __init__(self, control, variant):
        self._rows = [control, variant]
        self.commits = 0

    def query(self, _model):
        return _OneShotPatternQuery(self._rows.pop(0))

    def commit(self):
        self.commits += 1


class _NoQueryDb:
    def query(self, _model):
        raise AssertionError("invalid registration should not query")


def test_sharpe_tests_abstain_on_zero_variance_samples() -> None:
    control = [-2.0, -1.0, 0.5, -0.5] * 8
    flat_variant = [0.2] * 32

    boot = shadow_testing._bootstrap_sharpe_difference(control, flat_variant, n_resamples=100)
    z_test = shadow_testing._sharpe_ratio_ztest(control, flat_variant)

    assert boot["significant"] is False
    assert boot["variant_better"] is False
    assert boot["p_value"] == 1.0
    assert boot["reason"] == "insufficient_sharpe_variance"
    assert z_test["significant"] is False
    assert z_test["variant_better"] is False
    assert z_test["p_value"] == 1.0
    assert z_test["reason"] == "insufficient_sharpe_variance"


def test_extract_trade_returns_options_use_contract_aware_realized_pnl() -> None:
    now = datetime(2026, 5, 28, 12, 0)
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=40.0,
        pnl_pct=5000.0,
        direction="long",
        entry_date=now - timedelta(days=2),
        exit_date=now,
        signal_json={"asset_type": "options", "option_meta": {"strike": 500.0}},
    )

    returns, hold_days = _extract_trade_returns([trade])

    assert returns == pytest.approx([16.0])
    assert hold_days == pytest.approx([2.0])


def test_extract_trade_returns_skips_unpriced_option_legacy_pct() -> None:
    trade = SimpleNamespace(
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        pnl=None,
        pnl_pct=17755.61,
        direction="long",
        entry_date=datetime(2026, 5, 28, 12, 0),
        exit_date=datetime(2026, 5, 28, 13, 0),
        signal_json={"asset_type": "options", "option_meta": {"strike": 700.0}},
    )

    returns, hold_days = _extract_trade_returns([trade])

    assert returns == []
    assert hold_days == []


def test_extract_trade_returns_keeps_stock_legacy_pct() -> None:
    trade = SimpleNamespace(
        entry_price=None,
        exit_price=None,
        quantity=1.0,
        pnl=None,
        pnl_pct=3.5,
        direction="long",
        entry_date=None,
        exit_date=None,
        signal_json={"asset_type": "stock"},
    )

    returns, hold_days = _extract_trade_returns([trade])

    assert returns == pytest.approx([3.5])
    assert hold_days == pytest.approx([1.0])


def test_extract_trade_returns_skips_inverted_or_malformed_hold_windows() -> None:
    valid = SimpleNamespace(
        entry_price=None,
        exit_price=None,
        quantity=1.0,
        pnl=None,
        pnl_pct=3.5,
        direction="long",
        entry_date=datetime(2026, 5, 28, 12, 0),
        exit_date=datetime(2026, 5, 29, 12, 0),
        signal_json={"asset_type": "stock"},
    )
    inverted = SimpleNamespace(
        entry_price=None,
        exit_price=None,
        quantity=1.0,
        pnl=None,
        pnl_pct=99.0,
        direction="long",
        entry_date=datetime(2026, 5, 29, 12, 0),
        exit_date=datetime(2026, 5, 28, 12, 0),
        signal_json={"asset_type": "stock"},
    )
    malformed = SimpleNamespace(
        entry_price=None,
        exit_price=None,
        quantity=1.0,
        pnl=None,
        pnl_pct=88.0,
        direction="long",
        entry_date="not-a-date",
        exit_date="2026-05-29T12:00:00Z",
        signal_json={"asset_type": "stock"},
    )

    returns, hold_days = _extract_trade_returns([valid, inverted, malformed])

    assert returns == pytest.approx([3.5])
    assert hold_days == pytest.approx([1.0])


def test_extract_trade_returns_normalizes_timezone_hold_windows() -> None:
    trade = SimpleNamespace(
        entry_price=None,
        exit_price=None,
        quantity=1.0,
        pnl=None,
        pnl_pct=4.0,
        direction="long",
        entry_date=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
        exit_date="2026-05-30T12:00:00Z",
        signal_json={"asset_type": "stock"},
    )

    returns, hold_days = _extract_trade_returns([trade])

    assert returns == pytest.approx([4.0])
    assert hold_days == pytest.approx([2.0])


def test_create_shadow_test_assigns_fresh_normalized_paper_book(monkeypatch):
    monkeypatch.setattr(
        shadow_testing,
        "_utcnow",
        lambda: datetime(2026, 6, 3, 12, 0),
    )
    control = SimpleNamespace(id=1, name="control")
    original_book = {"entries": [{"paper_trade_id": 7}]}
    variant = SimpleNamespace(
        id=2,
        name="variant",
        paper_book_json=original_book,
    )
    db = _CreateShadowTestDb(control, variant)

    out = shadow_testing.create_shadow_test(
        db,
        1,
        2,
        min_trades=True,
        min_days=0.5,
    )

    assert out["ok"] is True
    assert out["min_trades"] == shadow_testing.MIN_TRADES_FOR_COMPARISON
    assert out["min_days"] == shadow_testing.MIN_DAYS_FOR_COMPARISON
    assert db.commits == 1
    assert variant.paper_book_json is not original_book
    assert variant.paper_book_json["entries"] == [{"paper_trade_id": 7}]
    assert variant.paper_book_json["shadow_test"] == {
        "control_id": 1,
        "variant_id": 2,
        "started_at": "2026-06-03T12:00:00Z",
        "min_trades": shadow_testing.MIN_TRADES_FOR_COMPARISON,
        "min_days": shadow_testing.MIN_DAYS_FOR_COMPARISON,
        "status": "running",
    }


def test_create_shadow_test_never_floors_fractional_min_gates(monkeypatch):
    monkeypatch.setattr(
        shadow_testing,
        "_utcnow",
        lambda: datetime(2026, 6, 3, 12, 0),
    )
    control = SimpleNamespace(id=1, name="control")
    variant = SimpleNamespace(id=2, name="variant", paper_book_json={})
    db = _CreateShadowTestDb(control, variant)

    out = shadow_testing.create_shadow_test(
        db,
        1,
        2,
        min_trades=40.9,
        min_days="7.5",
    )

    assert out["ok"] is True
    assert out["min_trades"] == 41
    assert out["min_days"] == 8
    assert variant.paper_book_json["shadow_test"]["min_trades"] == 41
    assert variant.paper_book_json["shadow_test"]["min_days"] == 8


def test_create_shadow_test_rejects_invalid_or_same_pattern_ids_before_query():
    out = shadow_testing.create_shadow_test(_NoQueryDb(), True, 2)

    assert out == {
        "ok": False,
        "error": "invalid_pattern_id",
        "control_id": None,
        "variant_id": 2,
    }

    out = shadow_testing.create_shadow_test(_NoQueryDb(), 3, 3.0)

    assert out == {
        "ok": False,
        "error": "shadow_test_same_pattern",
        "pattern_id": 3,
    }


def test_evaluate_shadow_test_does_not_promote_significant_worse_variant(monkeypatch):
    control_rows = _shadow_rows()
    variant_rows = _shadow_rows()

    def _get_closed_trades(_db, pattern_id):
        return control_rows if pattern_id == 1 else variant_rows

    def _extract(rows):
        if rows is control_rows:
            return [2.0] * len(control_rows), [1.0] * len(control_rows)
        return [-2.0] * len(variant_rows), [1.0] * len(variant_rows)

    worse_result = {
        "p_value": 0.001,
        "significant": True,
        "variant_better": False,
    }

    monkeypatch.setattr(shadow_testing, "_get_closed_trades", _get_closed_trades)
    monkeypatch.setattr(shadow_testing, "_extract_trade_returns", _extract)
    monkeypatch.setattr(
        shadow_testing,
        "_welch_return_ttest",
        lambda _control, _variant: dict(worse_result),
    )
    monkeypatch.setattr(
        shadow_testing,
        "_bootstrap_sharpe_difference",
        lambda _control, _variant: dict(worse_result),
    )
    monkeypatch.setattr(
        shadow_testing,
        "_sharpe_ratio_ztest",
        lambda _control, _variant: dict(worse_result),
    )

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out["significant_tests"] == 3
    assert out["tests_passed"] == 0
    assert out["promote_variant"] is False
    assert out["recommendation"] == "KEEP control (variant not significantly better)"


def test_evaluate_shadow_test_malformed_stat_outputs_fail_closed(monkeypatch):
    control_rows = _shadow_rows(group="control")
    variant_rows = _shadow_rows(group="variant")

    def _get_closed_trades(_db, pattern_id):
        return control_rows if pattern_id == 1 else variant_rows

    def _extract(rows):
        if rows and getattr(rows[0], "_group", None) == "control":
            return [1.0] * len(control_rows), [1.0] * len(control_rows)
        return [2.0] * len(variant_rows), [1.0] * len(variant_rows)

    monkeypatch.setattr(shadow_testing, "_get_closed_trades", _get_closed_trades)
    monkeypatch.setattr(shadow_testing, "_extract_trade_returns", _extract)
    monkeypatch.setattr(
        shadow_testing,
        "_welch_return_ttest",
        lambda _control, _variant: {
            "p_value": "bad",
            "significant": True,
            "variant_better": True,
        },
    )
    monkeypatch.setattr(
        shadow_testing,
        "_bootstrap_sharpe_difference",
        lambda _control, _variant: {
            "p_value": 0.001,
            "significant": True,
            "variant_better": "yes",
        },
    )
    monkeypatch.setattr(
        shadow_testing,
        "_sharpe_ratio_ztest",
        lambda _control, _variant: {
            "p_value": 0.001,
            "significant": True,
            "variant_better": True,
        },
    )

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out["significant_tests"] == 2
    assert out["tests_passed"] == 1
    assert out["promote_variant"] is False
    assert out["paired_ttest"]["significant"] is False
    assert out["paired_ttest"]["p_value_adjusted"] == 1.0
    assert out["bootstrap_sharpe"]["significant"] is True


def test_evaluate_shadow_test_filters_malformed_extracted_samples(monkeypatch):
    control_rows = _shadow_rows(group="control")
    variant_rows = _shadow_rows(group="variant")

    def _get_closed_trades(_db, pattern_id):
        return control_rows if pattern_id == 1 else variant_rows

    def _extract(rows):
        if rows and getattr(rows[0], "_group", None) == "control":
            return (
                [1.0] * 28 + [float("nan"), 2.0],
                [1.0] * 29,
            )
        return [2.0] * len(variant_rows), [1.0] * len(variant_rows)

    monkeypatch.setattr(shadow_testing, "_get_closed_trades", _get_closed_trades)
    monkeypatch.setattr(shadow_testing, "_extract_trade_returns", _extract)
    _patch_all_significant_better_tests(monkeypatch)

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out == {
        "ok": False,
        "reason": "insufficient_control_trades",
        "n": 28,
        "raw_n": 30,
        "min_trades": 30,
    }


def test_evaluate_shadow_test_rejects_invalid_or_same_pattern_ids_before_query():
    out = shadow_testing.evaluate_shadow_test(_NoQueryDb(), True, 2)

    assert out == {
        "ok": False,
        "reason": "invalid_pattern_id",
        "control_id": None,
        "variant_id": 2,
    }

    out = shadow_testing.evaluate_shadow_test(_NoQueryDb(), 3, "3")

    assert out == {
        "ok": False,
        "reason": "shadow_test_same_pattern",
        "pattern_id": 3,
    }


def test_evaluate_shadow_test_normalizes_pattern_ids_for_evidence(monkeypatch):
    calls = []
    control_rows = _shadow_rows()
    variant_rows = _shadow_rows()

    def _get_closed_trades(_db, pattern_id):
        calls.append(pattern_id)
        return control_rows if pattern_id == 1 else variant_rows

    def _extract(rows):
        if rows is control_rows:
            return [1.0] * len(control_rows), [1.0] * len(control_rows)
        return [2.0] * len(variant_rows), [1.0] * len(variant_rows)

    monkeypatch.setattr(shadow_testing, "_get_closed_trades", _get_closed_trades)
    monkeypatch.setattr(shadow_testing, "_extract_trade_returns", _extract)
    _patch_all_significant_better_tests(monkeypatch)

    out = shadow_testing.evaluate_shadow_test(object(), "1", "2")

    assert calls == [1, 2]
    assert out["promote_variant"] is True


def test_evaluate_shadow_test_promotes_two_significant_better_tests(monkeypatch):
    control_rows = _shadow_rows()
    variant_rows = _shadow_rows()

    def _get_closed_trades(_db, pattern_id):
        return control_rows if pattern_id == 1 else variant_rows

    def _extract(rows):
        if rows is control_rows:
            return [1.0] * len(control_rows), [1.0] * len(control_rows)
        return [2.0] * len(variant_rows), [1.0] * len(variant_rows)

    better_result = {
        "p_value": 0.001,
        "significant": True,
        "variant_better": True,
    }
    worse_result = {
        "p_value": 0.001,
        "significant": True,
        "variant_better": False,
    }

    monkeypatch.setattr(shadow_testing, "_get_closed_trades", _get_closed_trades)
    monkeypatch.setattr(shadow_testing, "_extract_trade_returns", _extract)
    monkeypatch.setattr(
        shadow_testing,
        "_welch_return_ttest",
        lambda _control, _variant: dict(better_result),
    )
    monkeypatch.setattr(
        shadow_testing,
        "_bootstrap_sharpe_difference",
        lambda _control, _variant: dict(better_result),
    )
    monkeypatch.setattr(
        shadow_testing,
        "_sharpe_ratio_ztest",
        lambda _control, _variant: dict(worse_result),
    )

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out["significant_tests"] == 3
    assert out["tests_passed"] == 2
    assert out["variant_positive_expectancy"] is True
    assert out["promote_variant"] is True


def test_evaluate_shadow_test_does_not_promote_less_bad_losing_variant(monkeypatch):
    control_rows = _shadow_rows()
    variant_rows = _shadow_rows()

    def _get_closed_trades(_db, pattern_id):
        return control_rows if pattern_id == 1 else variant_rows

    def _extract(rows):
        if rows is control_rows:
            return [-2.0] * len(control_rows), [1.0] * len(control_rows)
        return [-1.0] * len(variant_rows), [1.0] * len(variant_rows)

    monkeypatch.setattr(shadow_testing, "_get_closed_trades", _get_closed_trades)
    monkeypatch.setattr(shadow_testing, "_extract_trade_returns", _extract)
    _patch_all_significant_better_tests(monkeypatch)

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out["tests_passed"] == 3
    assert out["variant_positive_expectancy"] is False
    assert out["promote_variant"] is False
    assert (
        out["recommendation"]
        == "KEEP control (variant lacks positive realized expectancy)"
    )


def test_evaluate_shadow_test_blocks_registered_test_before_min_days(monkeypatch):
    monkeypatch.setattr(
        shadow_testing,
        "_shadow_test_meta",
        lambda _db, _variant_pattern_id: {
            "control_id": 1,
            "variant_id": 2,
            "started_at": "2026-06-01T12:00:00Z",
            "min_trades": 30,
            "min_days": 7,
        },
    )
    monkeypatch.setattr(
        shadow_testing,
        "_utcnow",
        lambda: datetime(2026, 6, 3, 12, 0),
    )
    monkeypatch.setattr(
        shadow_testing,
        "_get_closed_trades",
        lambda *_args: pytest.fail("trade evidence should not be read before day gate"),
    )

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out == {
        "ok": False,
        "reason": "insufficient_shadow_test_days",
        "days": 2.0,
        "min_days": 7,
    }


def test_evaluate_shadow_test_blocks_mismatched_registered_identity(monkeypatch):
    monkeypatch.setattr(
        shadow_testing,
        "_shadow_test_meta",
        lambda _db, _variant_pattern_id: {
            "control_id": 99,
            "variant_id": 2,
            "started_at": "2026-05-01T12:00:00Z",
            "min_trades": 30,
            "min_days": 7,
        },
    )
    monkeypatch.setattr(
        shadow_testing,
        "_get_closed_trades",
        lambda *_args: pytest.fail("trade evidence should not be read on identity mismatch"),
    )

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out == {
        "ok": False,
        "reason": "shadow_test_identity_mismatch",
        "control_id": 1,
        "variant_id": 2,
        "registered_control_id": 99,
        "registered_variant_id": 2,
    }


def test_evaluate_shadow_test_uses_registered_min_trades_after_return_filter(monkeypatch):
    post_start = datetime(2026, 5, 2, 12, 0)
    control_rows = _shadow_rows(40, entry_date=post_start, group="control")
    variant_rows = _shadow_rows(40, entry_date=post_start, group="variant")

    monkeypatch.setattr(
        shadow_testing,
        "_shadow_test_meta",
        lambda _db, _variant_pattern_id: {
            "control_id": 1,
            "variant_id": 2,
            "started_at": "2026-05-01T12:00:00Z",
            "min_trades": 40,
            "min_days": 7,
        },
    )
    monkeypatch.setattr(
        shadow_testing,
        "_utcnow",
        lambda: datetime(2026, 6, 3, 12, 0),
    )

    def _get_closed_trades(_db, pattern_id):
        return control_rows if pattern_id == 1 else variant_rows

    def _extract(rows):
        if rows and getattr(rows[0], "_group", None) == "control":
            return [1.0] * 39, [1.0] * 39
        return [2.0] * len(variant_rows), [1.0] * len(variant_rows)

    monkeypatch.setattr(shadow_testing, "_get_closed_trades", _get_closed_trades)
    monkeypatch.setattr(shadow_testing, "_extract_trade_returns", _extract)
    _patch_all_significant_better_tests(monkeypatch)

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out == {
        "ok": False,
        "reason": "insufficient_control_trades",
        "n": 39,
        "raw_n": 40,
        "min_trades": 40,
    }


def test_evaluate_shadow_test_excludes_pre_start_registered_trades(monkeypatch):
    pre_start = datetime(2026, 4, 30, 12, 0)
    post_start = datetime(2026, 5, 2, 12, 0)
    control_rows = [
        *_shadow_rows(11, entry_date=pre_start),
        *_shadow_rows(29, entry_date=post_start),
    ]
    variant_rows = _shadow_rows(30, entry_date=post_start)

    monkeypatch.setattr(
        shadow_testing,
        "_shadow_test_meta",
        lambda _db, _variant_pattern_id: {
            "control_id": 1,
            "variant_id": 2,
            "started_at": "2026-05-01T12:00:00Z",
            "min_trades": 30,
            "min_days": 7,
        },
    )
    monkeypatch.setattr(
        shadow_testing,
        "_utcnow",
        lambda: datetime(2026, 6, 3, 12, 0),
    )

    def _get_closed_trades(_db, pattern_id):
        return control_rows if pattern_id == 1 else variant_rows

    monkeypatch.setattr(shadow_testing, "_get_closed_trades", _get_closed_trades)
    monkeypatch.setattr(
        shadow_testing,
        "_extract_trade_returns",
        lambda *_args: pytest.fail("stale rows should fail sample gate first"),
    )

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out == {
        "ok": False,
        "reason": "insufficient_control_trades",
        "n": 29,
        "raw_n": 40,
        "min_trades": 30,
    }


def test_evaluate_shadow_test_ceil_fractional_registered_min_trades(monkeypatch):
    post_start = datetime(2026, 5, 2, 12, 0)
    control_rows = _shadow_rows(40, entry_date=post_start)
    variant_rows = _shadow_rows(40, entry_date=post_start)

    monkeypatch.setattr(
        shadow_testing,
        "_shadow_test_meta",
        lambda _db, _variant_pattern_id: {
            "control_id": 1,
            "variant_id": 2,
            "started_at": "2026-05-01T12:00:00Z",
            "min_trades": 40.1,
            "min_days": 7,
        },
    )
    monkeypatch.setattr(
        shadow_testing,
        "_utcnow",
        lambda: datetime(2026, 6, 3, 12, 0),
    )

    def _get_closed_trades(_db, pattern_id):
        return control_rows if pattern_id == 1 else variant_rows

    monkeypatch.setattr(shadow_testing, "_get_closed_trades", _get_closed_trades)

    out = shadow_testing.evaluate_shadow_test(object(), 1, 2)

    assert out == {
        "ok": False,
        "reason": "insufficient_control_trades",
        "n": 40,
        "raw_n": 40,
        "min_trades": 41,
    }
