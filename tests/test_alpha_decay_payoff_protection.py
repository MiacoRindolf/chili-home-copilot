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
