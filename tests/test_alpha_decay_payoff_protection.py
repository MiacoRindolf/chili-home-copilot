from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading import alpha_decay
from app.services.trading import dynamic_priors


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)
        self.filters = []
        self.orderings = []

    def filter(self, *args):
        self.filters.append(args)
        return self

    def order_by(self, *args):
        self.orderings.append(args)
        return self

    def all(self):
        return list(self._rows)


class _FakeDecayDb:
    def __init__(self, *, patterns=None, trades=None, paper_trades=None):
        self.patterns = list(patterns or [])
        self.trades = list(trades or [])
        self.paper_trades = list(paper_trades or [])
        self.queries = {}

    def query(self, model):
        if model is alpha_decay.ScanPattern:
            query = _FakeQuery(self.patterns)
            self.queries[model] = query
            return query
        if model is alpha_decay.Trade:
            query = _FakeQuery(self.trades)
            self.queries[model] = query
            return query
        if model is alpha_decay.PaperTrade:
            query = _FakeQuery(self.paper_trades)
            self.queries[model] = query
            return query
        raise AssertionError(f"unexpected query model: {model!r}")


def test_payoff_ratio_shields_wr_only_alpha_decay(monkeypatch):
    monkeypatch.setattr(
        alpha_decay,
        "_settings_get",
        lambda name, default: {
            "chili_pattern_demote_payoff_ratio_floor": 1.5,
            "chili_pattern_demote_payoff_ratio_min_n": 5,
        }.get(name, default),
    )
    pattern = SimpleNamespace(payoff_ratio=4.9, payoff_ratio_n=86)

    assert alpha_decay._should_skip_decay_for_payoff(
        pattern,
        wr_decay_fired=True,
        return_decay_fired=False,
    )


def test_alpha_decay_win_rate_rejects_boolean_nonfinite_and_out_of_range():
    assert alpha_decay._win_rate_or_none(True) is None
    assert alpha_decay._win_rate_or_none(float("nan")) is None
    assert alpha_decay._win_rate_or_none(-0.01) is None
    assert alpha_decay._win_rate_or_none(1.01) is None
    assert alpha_decay._win_rate_or_none(0.55) == 0.55


def test_alpha_decay_threshold_normalizers_reject_boolean_values():
    assert alpha_decay._positive_int_or_default(True, 30) == 30
    assert alpha_decay._positive_int_or_default(0.5, 30) == 30
    assert alpha_decay._positive_int_or_default(12.9, 30) == 12
    assert alpha_decay._positive_int_or_default(5, 0) == 5
    assert alpha_decay._probability_or_default(True, 0.12) == 0.12
    assert alpha_decay._probability_or_default(0.25, 0.12) == 0.25
    assert alpha_decay._finite_float_or_default(True, -1.0) == -1.0
    assert alpha_decay._finite_float_or_default(-0.5, -1.0) == -0.5


def test_alpha_decay_skips_wr_decay_when_benchmark_is_invalid(monkeypatch):
    monkeypatch.setattr(alpha_decay, "trade_return_pct", lambda trade: trade.return_pct)
    monkeypatch.setattr(alpha_decay, "paper_trade_return_pct", lambda trade: None)
    monkeypatch.setattr(dynamic_priors, "population_win_rate", lambda db, **kwargs: None)

    pattern = SimpleNamespace(
        id=42,
        name="invalid-benchmark-pattern",
        oos_win_rate=True,
        win_rate=None,
        payoff_ratio=None,
        payoff_ratio_n=None,
    )
    trades = [
        SimpleNamespace(scan_pattern_id=42, return_pct=-0.1, pnl=-1.0)
        for _ in range(alpha_decay.MIN_TRADES_FOR_DECAY_CHECK)
    ]
    db = _FakeDecayDb(patterns=[pattern], trades=trades)

    out = alpha_decay.check_alpha_decay(
        db,
        auto_demote=False,
        regime_adaptive=False,
        return_floor=-1.0,
    )

    assert out["checked"] == 1
    assert out["healthy"] == 0
    assert out["decayed"] == []


def test_alpha_decay_boolean_wr_gap_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(alpha_decay, "trade_return_pct", lambda trade: trade.return_pct)
    monkeypatch.setattr(alpha_decay, "paper_trade_return_pct", lambda trade: None)

    pattern = SimpleNamespace(
        id=42,
        name="boolean-gap-pattern",
        oos_win_rate=0.80,
        win_rate=None,
        payoff_ratio=None,
        payoff_ratio_n=None,
    )
    trades = [
        SimpleNamespace(scan_pattern_id=42, return_pct=-0.1, pnl=-1.0)
        for _ in range(alpha_decay.MIN_TRADES_FOR_DECAY_CHECK)
    ]
    db = _FakeDecayDb(patterns=[pattern], trades=trades)

    out = alpha_decay.check_alpha_decay(
        db,
        auto_demote=False,
        regime_adaptive=False,
        wr_gap=True,
        return_floor=-1.0,
    )

    assert out["checked"] == 1
    assert out["healthy"] == 0
    assert [row["pattern_id"] for row in out["decayed"]] == [42]
    assert "WR decay" in out["decayed"][0]["reason"]


def test_alpha_decay_applies_user_zero_scope_to_all_queries(monkeypatch):
    monkeypatch.setattr(alpha_decay, "trade_return_pct", lambda trade: trade.return_pct)
    monkeypatch.setattr(alpha_decay, "paper_trade_return_pct", lambda trade: None)

    pattern = SimpleNamespace(
        id=42,
        name="user-zero-scope-pattern",
        oos_win_rate=0.80,
        win_rate=None,
        payoff_ratio=None,
        payoff_ratio_n=None,
    )
    trades = [
        SimpleNamespace(scan_pattern_id=42, return_pct=0.1, pnl=1.0)
        for _ in range(alpha_decay.MIN_TRADES_FOR_DECAY_CHECK)
    ]
    db = _FakeDecayDb(patterns=[pattern], trades=trades)

    alpha_decay.check_alpha_decay(
        db,
        user_id=0,
        auto_demote=False,
        regime_adaptive=False,
    )

    assert len(db.queries[alpha_decay.ScanPattern].filters) == 2
    assert len(db.queries[alpha_decay.Trade].filters) == 2
    assert len(db.queries[alpha_decay.PaperTrade].filters) == 2


def test_alpha_decay_passes_user_scope_to_population_prior(monkeypatch):
    monkeypatch.setattr(alpha_decay, "trade_return_pct", lambda trade: trade.return_pct)
    monkeypatch.setattr(alpha_decay, "paper_trade_return_pct", lambda trade: None)
    calls = []

    def _population_win_rate(db, **kwargs):
        calls.append(kwargs)
        return 0.80

    monkeypatch.setattr(dynamic_priors, "population_win_rate", _population_win_rate)

    pattern = SimpleNamespace(
        id=42,
        name="scoped-prior-pattern",
        oos_win_rate=None,
        win_rate=None,
        payoff_ratio=None,
        payoff_ratio_n=None,
    )
    trades = [
        SimpleNamespace(scan_pattern_id=42, return_pct=0.1, pnl=1.0)
        for _ in range(alpha_decay.MIN_TRADES_FOR_DECAY_CHECK)
    ]
    db = _FakeDecayDb(patterns=[pattern], trades=trades)

    alpha_decay.check_alpha_decay(
        db,
        user_id=0,
        auto_demote=False,
        regime_adaptive=False,
    )

    assert calls == [{"user_id": 0}]


def test_payoff_ratio_does_not_shield_negative_return_decay(monkeypatch):
    monkeypatch.setattr(
        alpha_decay,
        "_settings_get",
        lambda name, default: {
            "chili_pattern_demote_payoff_ratio_floor": 1.5,
            "chili_pattern_demote_payoff_ratio_min_n": 5,
        }.get(name, default),
    )
    pattern = SimpleNamespace(payoff_ratio=2.8, payoff_ratio_n=30)

    assert not alpha_decay._should_skip_decay_for_payoff(
        pattern,
        wr_decay_fired=True,
        return_decay_fired=True,
    )


def test_payoff_ratio_shield_requires_minimum_sample(monkeypatch):
    monkeypatch.setattr(
        alpha_decay,
        "_settings_get",
        lambda name, default: {
            "chili_pattern_demote_payoff_ratio_floor": 1.5,
            "chili_pattern_demote_payoff_ratio_min_n": 5,
        }.get(name, default),
    )
    pattern = SimpleNamespace(payoff_ratio=10.0, payoff_ratio_n=4)

    assert not alpha_decay._should_skip_decay_for_payoff(
        pattern,
        wr_decay_fired=True,
        return_decay_fired=False,
    )


def test_payoff_ratio_shield_rejects_boolean_pattern_metrics(monkeypatch):
    monkeypatch.setattr(
        alpha_decay,
        "_settings_get",
        lambda name, default: {
            "chili_pattern_demote_payoff_ratio_floor": 1.5,
            "chili_pattern_demote_payoff_ratio_min_n": 5,
        }.get(name, default),
    )

    assert not alpha_decay._payoff_ratio_protects_from_wr_decay(
        SimpleNamespace(payoff_ratio=True, payoff_ratio_n=86)
    )
    assert not alpha_decay._payoff_ratio_protects_from_wr_decay(
        SimpleNamespace(payoff_ratio=4.9, payoff_ratio_n=True)
    )


def test_payoff_ratio_shield_rejects_boolean_settings(monkeypatch):
    monkeypatch.setattr(
        alpha_decay,
        "_settings_get",
        lambda name, default: {
            "chili_pattern_demote_payoff_ratio_floor": True,
            "chili_pattern_demote_payoff_ratio_min_n": True,
        }.get(name, default),
    )
    pattern = SimpleNamespace(payoff_ratio=1.2, payoff_ratio_n=1)

    assert not alpha_decay._payoff_ratio_protects_from_wr_decay(pattern)


def test_payoff_ratio_shield_fractional_min_sample_setting_uses_default(monkeypatch):
    monkeypatch.setattr(
        alpha_decay,
        "_settings_get",
        lambda name, default: {
            "chili_pattern_demote_payoff_ratio_floor": 1.5,
            "chili_pattern_demote_payoff_ratio_min_n": 0.5,
        }.get(name, default),
    )
    pattern = SimpleNamespace(payoff_ratio=4.9, payoff_ratio_n=1)

    assert not alpha_decay._payoff_ratio_protects_from_wr_decay(pattern)


def test_alpha_decay_evidence_uses_return_sign_when_pnl_missing():
    win = alpha_decay._return_evidence_record(
        pnl_pct=16.0,
        pnl=None,
        source="paper",
    )
    loss = alpha_decay._return_evidence_record(
        pnl_pct=-8.0,
        pnl=None,
        source="paper",
    )

    assert win is not None
    assert win["win"] is True
    assert win["pnl"] is None
    assert loss is not None
    assert loss["win"] is False
    assert loss["pnl"] is None


def test_alpha_decay_evidence_rejects_boolean_return_samples():
    assert alpha_decay._return_evidence_record(
        pnl_pct=True,
        pnl=10.0,
        source="paper",
    ) is None
    assert alpha_decay._return_evidence_record(
        pnl_pct=False,
        pnl=-10.0,
        source="live",
    ) is None


def test_alpha_decay_half_life_evidence_rejects_malformed_inputs():
    now = datetime(2026, 5, 28, 12, 0)

    assert alpha_decay._half_life_evidence_record(
        exit_date=now,
        return_pct=1.2,
    ) == {"exit_ts": now.timestamp(), "return_pct": 1.2}
    assert alpha_decay._half_life_evidence_record(
        exit_date=now,
        return_pct=True,
    ) is None
    assert alpha_decay._half_life_evidence_record(
        exit_date="2026-05-28",
        return_pct=1.2,
    ) is None


def test_estimate_half_life_skips_malformed_evidence_rows(monkeypatch):
    monkeypatch.setattr(alpha_decay, "trade_return_pct", lambda trade: trade.return_pct)
    monkeypatch.setattr(alpha_decay, "paper_trade_return_pct", lambda trade: trade.return_pct)

    base = datetime(2026, 5, 28, 12, 0)
    valid = [
        SimpleNamespace(
            exit_date=base + timedelta(days=i),
            return_pct=1.0 if i < 4 else -1.0,
        )
        for i in range(9)
    ]
    malformed = [
        SimpleNamespace(exit_date=base + timedelta(days=20), return_pct=True),
        SimpleNamespace(exit_date="2026-06-18", return_pct=-1.0),
        SimpleNamespace(exit_date=base + timedelta(days=21), return_pct=float("nan")),
    ]
    db = _FakeDecayDb(trades=[*valid, *malformed])

    assert alpha_decay.estimate_half_life(db, pattern_id=42) is None


def test_estimate_half_life_applies_user_zero_scope(monkeypatch):
    monkeypatch.setattr(alpha_decay, "trade_return_pct", lambda trade: trade.return_pct)
    monkeypatch.setattr(alpha_decay, "paper_trade_return_pct", lambda trade: trade.return_pct)

    db = _FakeDecayDb(
        trades=[SimpleNamespace(exit_date=datetime(2026, 5, 28), return_pct=1.0)],
        paper_trades=[SimpleNamespace(exit_date=datetime(2026, 5, 29), return_pct=1.0)],
    )

    assert alpha_decay.estimate_half_life(db, pattern_id=42, user_id=0) is None
    assert len(db.queries[alpha_decay.Trade].filters) == 2
    assert len(db.queries[alpha_decay.PaperTrade].filters) == 2


def test_alpha_decay_dollar_mean_ignores_missing_pnl():
    evidence = [
        {"pnl": None},
        {"pnl": 12.0},
        {"pnl": -4.0},
    ]

    assert alpha_decay._mean_known_pnl(evidence) == 4.0


def test_alpha_decay_dollar_mean_ignores_boolean_and_nonfinite_pnl():
    evidence = [
        {"pnl": True},
        {"pnl": float("nan")},
        {"pnl": 12.0},
        {"pnl": -4.0},
    ]

    assert alpha_decay._mean_known_pnl(evidence) == 4.0


def test_alpha_decay_live_pnl_fallback_includes_partial_option_leg():
    trade = SimpleNamespace(
        entry_price=1.25,
        quantity=1.0,
        pnl=-10.0,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert alpha_decay._trade_realized_pnl_with_raw_fallback(trade) == pytest.approx(10.0)


def test_alpha_decay_paper_pnl_fallback_includes_partial_option_leg():
    paper_trade = SimpleNamespace(
        entry_price=1.25,
        quantity=1.0,
        pnl=-10.0,
        direction="long",
        signal_json={"asset_type": "options"},
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert alpha_decay._paper_realized_pnl_with_raw_fallback(paper_trade) == pytest.approx(10.0)
