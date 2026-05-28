from types import SimpleNamespace

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
