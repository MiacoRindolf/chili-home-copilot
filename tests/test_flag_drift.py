"""Flag-drift detector — ginagawang nakikita ang env-vs-default na hindi pagkakatugma.

KONTEKSTO: may TATLONG runtime vector (scheduler container na may 134 env pin;
captured-paper HOST process na nagba-load ng `.env`; replay container na walang
override). Dahil TAHIMIK ang pagkakaiba, isang araw ang nagamit sa maling premise.
Hindi pinipilit ng detector ang pagkakapareho — ipinapakita lang niya ang totoo.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.trading.momentum_neural.flag_drift import boolean_flag_drift


@dataclass
class _F:
    default: object


_FIELDS = {
    "chili_momentum_alpha_enabled": _F(True),
    "chili_momentum_beta_enabled": _F(False),
    "chili_momentum_gamma_seconds": _F(600.0),
    "chili_momentum_delta_label": _F("hello"),
}


def test_walang_drift_kapag_tugma():
    env = {
        "CHILI_MOMENTUM_ALPHA_ENABLED": "1",
        "CHILI_MOMENTUM_BETA_ENABLED": "0",
    }
    assert boolean_flag_drift(env, _FIELDS) == []


def test_nahuhuli_ang_magkabilang_direksyon():
    env = {
        "CHILI_MOMENTUM_ALPHA_ENABLED": "false",  # default True
        "CHILI_MOMENTUM_BETA_ENABLED": "on",      # default False
    }
    assert boolean_flag_drift(env, _FIELDS) == [
        ("chili_momentum_alpha_enabled", False, True),
        ("chili_momentum_beta_enabled", True, False),
    ]


def test_hindi_boolean_na_field_ay_nilalaktawan():
    # Ang numeric at string na setting ay may sariling semantics — hindi dapat
    # mag-imbento ang detector ng drift na hindi niya sigurado.
    env = {
        "CHILI_MOMENTUM_GAMMA_SECONDS": "300",
        "CHILI_MOMENTUM_DELTA_LABEL": "iba",
    }
    assert boolean_flag_drift(env, _FIELDS) == []


def test_hindi_mabasang_halaga_ay_nilalaktawan():
    assert boolean_flag_drift({"CHILI_MOMENTUM_BETA_ENABLED": "siguro"}, _FIELDS) == []


def test_hindi_kilalang_key_ay_nilalaktawan():
    assert boolean_flag_drift({"CHILI_MOMENTUM_WALA_ITO": "1"}, _FIELDS) == []
    assert boolean_flag_drift({"IBANG_PREFIX_ENABLED": "1"}, _FIELDS) == []


def test_deterministikong_pagkakasunod():
    env = {
        "CHILI_MOMENTUM_BETA_ENABLED": "1",
        "CHILI_MOMENTUM_ALPHA_ENABLED": "0",
    }
    out = boolean_flag_drift(env, _FIELDS)
    assert [n for n, _, _ in out] == sorted(n for n, _, _ in out)


def test_totoong_settings_ay_hindi_bumabagsak():
    # Smoke laban sa TUNAY na Settings — walang exception, tamang hugis.
    from app.config import Settings

    out = boolean_flag_drift(
        {"CHILI_MOMENTUM_SCALE_GRID_ENABLED": "0"}, Settings.model_fields
    )
    assert out == [("chili_momentum_scale_grid_enabled", False, True)]
