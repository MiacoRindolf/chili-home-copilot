from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models.trading import PaperTrade, ScanPattern, Trade
from app.services.trading import alpha_decay


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


def test_alpha_decay_dollar_mean_ignores_missing_pnl():
    evidence = [
        {"pnl": None},
        {"pnl": 12.0},
        {"pnl": -4.0},
    ]

    assert alpha_decay._mean_known_pnl(evidence) == 4.0


def test_alpha_decay_rolling_evidence_stats_single_summary():
    evidence = [
        {"win": True, "pnl_pct": 2.0, "pnl": 8.0},
        {"win": False, "pnl_pct": -1.0, "pnl": None},
        {"win": True, "pnl_pct": 3.0, "pnl": 4.0},
        {"win": False, "pnl_pct": -2.0, "pnl": 0.0},
    ]

    live_wr, avg_ret_pct, avg_pnl = alpha_decay._rolling_evidence_stats(evidence)

    assert live_wr == 0.5
    assert avg_ret_pct == 0.5
    assert avg_pnl == 4.0


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, *, patterns, trade_rows, paper_rows):
        self.patterns = patterns
        self.trade_rows = trade_rows
        self.paper_rows = paper_rows
        self.query_calls = []

    def query(self, *args):
        self.query_calls.append(args)
        if len(args) == 1 and args[0] is ScanPattern:
            return _FakeQuery(self.patterns)
        keys = tuple(getattr(arg, "key", None) for arg in args)
        if keys == alpha_decay._TRADE_EVIDENCE_FIELDS:
            return _FakeQuery(self.trade_rows)
        if keys == alpha_decay._PAPER_EVIDENCE_FIELDS:
            return _FakeQuery(self.paper_rows)
        if keys == alpha_decay._TRADE_HALF_LIFE_FIELDS:
            return _FakeQuery(self.trade_rows)
        if keys == alpha_decay._PAPER_HALF_LIFE_FIELDS:
            return _FakeQuery(self.paper_rows)
        raise AssertionError(f"unexpected query shape: {keys!r}")


def test_check_alpha_decay_reads_recent_evidence_as_columns(monkeypatch):
    pattern = SimpleNamespace(
        id=42,
        name="Mean Reversion",
        oos_win_rate=0.9,
        win_rate=None,
        payoff_ratio=None,
        payoff_ratio_n=None,
    )
    trade_rows = [
        (42, -1.0, 100.0, 1.0, "equity", "", {}, 99.0, "long"),
        (42, -1.0, 100.0, 1.0, "equity", "", {}, 99.0, "long"),
        (42, -1.0, 100.0, 1.0, "equity", "", {}, 99.0, "long"),
    ]
    paper_rows = [
        (42, None, 100.0, 1.0, {}, 99.0, "long", -1.0),
        (42, None, 100.0, 1.0, {}, 99.0, "long", -1.0),
    ]
    db = _FakeDb(patterns=[pattern], trade_rows=trade_rows, paper_rows=paper_rows)

    result = alpha_decay.check_alpha_decay(
        db,
        auto_demote=False,
        regime_adaptive=False,
        wr_gap=0.1,
        return_floor=-100.0,
    )

    query_keys = [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls]
    assert db.query_calls[0] == (ScanPattern,)
    assert query_keys[1] == alpha_decay._TRADE_EVIDENCE_FIELDS
    assert query_keys[2] == alpha_decay._PAPER_EVIDENCE_FIELDS
    assert not any(len(call) == 1 and call[0] is Trade for call in db.query_calls)
    assert not any(len(call) == 1 and call[0] is PaperTrade for call in db.query_calls)
    assert result["checked"] == 1
    assert result["decayed"][0]["pattern_id"] == 42
    assert result["decayed"][0]["trades"] == 5


def test_estimate_half_life_reads_evidence_as_columns():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    returns = [1.0, 1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0, -1.0, 1.0, -1.0]
    trade_rows = [
        (start + timedelta(days=i), None, 100.0, 1.0, "equity", "", {}, 100.0 + ret, "long")
        for i, ret in enumerate(returns[:8])
    ]
    paper_rows = [
        (start + timedelta(days=i + 8), None, 100.0, 1.0, {}, 100.0 + ret, "long", ret)
        for i, ret in enumerate(returns[8:])
    ]
    db = _FakeDb(patterns=[], trade_rows=trade_rows, paper_rows=paper_rows)

    half_life = alpha_decay.estimate_half_life(db, pattern_id=42)

    query_keys = [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls]
    assert query_keys == [
        alpha_decay._TRADE_HALF_LIFE_FIELDS,
        alpha_decay._PAPER_HALF_LIFE_FIELDS,
    ]
    assert not any(len(call) == 1 and call[0] is Trade for call in db.query_calls)
    assert not any(len(call) == 1 and call[0] is PaperTrade for call in db.query_calls)
    assert half_life is not None
    assert half_life > 0.0
