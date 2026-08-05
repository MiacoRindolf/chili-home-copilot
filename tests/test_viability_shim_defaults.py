"""Ang fallback defaults ng ViabilitySettingsProjection ay dapat TUMUGMA sa
`app/config.py`.

BAKIT ITO MAHALAGA (natuklasan 2026-08-05). Ang `from_runtime` ay bumubuo ng
projection gamit ang ``getattr(source, name, default)`` — kaya ang `defaults`
dict nito ang umiiral sa BAWAT attribute na wala sa source. Apat ang dating
salungat sa config, at LAHAT sa permissive na direksyon:

    live_eligible_max_spread_bps   0.0   vs 300.0   (0.0 = WALANG toxic ceiling)
    risk_max_spread_bps_abs_cap    1500  vs 300
    a_setup_quality_floor_enabled  False vs True
    no_signal_derank_enabled       False vs True

Dormant ito noon — lahat ng tunay na caller ay nagpapasa ng buong `settings` —
pero ang epekto ng isang partial/mock source ay TAHIMIK na paluwagin ang isang
live-money gate. Ang test na ito ang humuhuli ng anumang bagong paglihis, sa
alinmang direksyon, nang hindi kinakailangang alalahanin ang dalawang lugar.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.services.trading.momentum_neural.viability import ViabilitySettingsProjection


def _shim_defaults() -> dict[str, object]:
    """Ang `defaults` dict na aktwal na ginagamit ng ``from_runtime``.

    Kinukuha ito sa pamamagitan ng pagpapasa ng WALANG-LAMANG source, kaya bawat
    field ay bumabagsak sa fallback nito — ang eksaktong landas na binabantayan.
    """

    class _Empty:
        pass

    proj = ViabilitySettingsProjection.from_runtime(_Empty())
    return {f: getattr(proj, f) for f in ViabilitySettingsProjection.__annotations__}


def test_shim_defaults_tumutugma_sa_config():
    cfg = Settings(_env_file=None)
    mismatched: list[str] = []
    for name, shim_val in _shim_defaults().items():
        if not hasattr(cfg, name):
            continue
        cfg_val = getattr(cfg, name)
        # Ang trade_flow_threshold ay SINADYANG dynamic: bumabagsak ito sa
        # naresolbang ofi_threshold kapag wala sa source (dokumentado sa
        # from_runtime), kaya hindi ito dapat itali sa config default.
        if name == "chili_momentum_trade_flow_threshold":
            continue
        if cfg_val != shim_val:
            mismatched.append(f"{name}: shim={shim_val!r} config={cfg_val!r}")
    assert not mismatched, (
        "Ang fallback defaults ng ViabilitySettingsProjection ay lumihis sa "
        "app/config.py. Isalin ang shim sa config (huwag baguhin ang config para "
        "tumugma sa shim) — permissive ang naging direksyon ng lahat ng dating "
        "paglihis:\n  " + "\n  ".join(mismatched)
    )


@pytest.mark.parametrize(
    "field,expected",
    [
        ("chili_momentum_live_eligible_max_spread_bps", 300.0),
        ("chili_momentum_risk_max_spread_bps_abs_cap", 300.0),
        ("chili_momentum_a_setup_quality_floor_enabled", True),
        ("chili_momentum_no_signal_derank_enabled", True),
    ],
)
def test_ang_apat_na_dating_permissive_ay_naitama(field, expected):
    """Tahasang naka-pin ang apat na tunay na nagkaproblema, para malinaw ang
    intensyon kahit maglipat ng ibang default sa hinaharap."""
    assert _shim_defaults()[field] == expected


def test_walang_toxic_spread_ceiling_na_nawawala_sa_partial_source():
    """Ang pinaka-mapanganib na kaso, tahasang isinulat: ang 0.0 ay hindi
    ibig sabihing 'mahigpit' kundi 'WALANG disqualification'."""
    proj = _shim_defaults()
    ceiling = proj["chili_momentum_live_eligible_max_spread_bps"]
    assert ceiling > 0.0, (
        "Ang 0.0 na ceiling ay nangangahulugang walang spread disqualification "
        "kahit sa sirang/naka-halt na quote — fail-OPEN sa live-money gate."
    )
