"""Ang loss guard ay tumatanggi sa BAWAT alpaca na sesyon (2026-08-26).

ANG PUWANG. Ang branch ay WALANG-KONDISYON::

    if family in {"alpaca_spot", "alpaca_short"}:
        _gap("loss_guard_alpaca_cycle_settlement_unavailable", session_id)
        continue

Walang tinitingnang ebidensya. Ang bawat alpaca na sesyon na PUMASOK ay
nagpapawalang-bisa sa buong loss history ⇒ `history_unavailable=True` ⇒ ang
auto-arm ay bumabalik ng `skipped=loss_guard_history_unavailable` sa natitirang
bahagi ng araw.

⚠️ BAKIT NGAYON LANG ITO NAKITA. Sa 14 na araw ay **IISA** lamang ang alpaca na
outcome na may klasipikasyong `entered` -- ang sistema ay hindi nagsusulat ng
entered outcome, kaya ang branch ay tahimik na natutulog. Ang UNANG tapat na
hilera (CDTG 2026-08-26, −$40.10 mula sa aktwal na presyo ng broker fill) ang
nagpaharang sa buong lane.

ANG NAKASULAT NA DAHILAN ay ang PEKENG ZERO na bayarin na inilalagay ng legacy
reconciler: maaari nitong i-label na `reconciled` ang isang resulta dahil lamang
sa isang ginawa-gawang zero na hindi NULL. **Iyon ay tungkol sa PINAGMULAN ng
numero, hindi sa pamilya ng venue.**

⚠️ NANANATILING FAIL-CLOSED. Kailangan ng LAHAT ng apat: reconciled, tunay na
`broker_reconciled_at`, finite na `broker_realized_pnl_usd`, at POSITIBONG
`broker_notional_basis_usd` -- ang huli ang mismong hindi kayang gawin ng
ginawa-gawang zero. At inuulit ng mga tseke sa IBABA ang lahat ng ito at
nagdadagdag pa ng sign at bps/notional na pagkakatugma.

Runnable: pytest tests/test_loss_guard_accepts_broker_truth.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services.trading.momentum_neural.risk_policy import (
    _alpaca_loss_history_broker_truth,
)


class _Outcome:
    def __init__(self, **kw):
        self.broker_recon_status = kw.get("status", "reconciled")
        self.broker_reconciled_at = kw.get(
            "reconciled_at", datetime(2026, 8, 26, 12, 0, 28, tzinfo=timezone.utc))
        self.broker_realized_pnl_usd = kw.get("pnl", -40.100068)
        self.broker_notional_basis_usd = kw.get("notional", 261.660068)


def test_the_measured_CDTG_row_now_counts():
    """ANG PANGUNAHING KASO -- ang tunay na hilera mula sa broker orders:
    191 @ 1.369948 pumasok, 191 @ 1.16 lumabas."""
    assert _alpaca_loss_history_broker_truth(_Outcome()) is True


def test_the_fabricated_zero_is_still_rejected():
    """⚠️ ANG MISMONG ALALAHANIN NG ORIHINAL NA KOMENTO. Ang legacy reconciler ay
    naglalagay ng zero at tinatawag itong reconciled; ang zero na notional basis
    ay nagpapatunay na walang SUKAT ang cycle."""
    assert _alpaca_loss_history_broker_truth(_Outcome(notional=0.0)) is False
    assert _alpaca_loss_history_broker_truth(_Outcome(notional=-1.0)) is False


@pytest.mark.parametrize("kw", [
    {"status": "unreconciled_no_fills"},
    {"status": "phantom_no_broker_match"},
    {"status": ""},
    {"status": None},
])
def test_anything_not_reconciled_is_rejected(kw):
    assert _alpaca_loss_history_broker_truth(_Outcome(**kw)) is False


@pytest.mark.parametrize("kw", [
    {"reconciled_at": None},
    {"reconciled_at": "2026-08-26T12:00:28Z"},   # string, hindi datetime
    {"pnl": None},
    {"pnl": float("nan")},
    {"pnl": float("inf")},
    {"notional": None},
    {"notional": float("nan")},
])
def test_any_missing_leg_fails_closed(kw):
    """⚠️ ANG DIREKSYON NG KALIGTASAN. Kulang ang KAHIT ISA sa apat ⇒ ang
    parehong gap gaya ng dati."""
    assert _alpaca_loss_history_broker_truth(_Outcome(**kw)) is False


def test_the_knob_reverts_it(monkeypatch):
    """Gawi bago ang 2026-08-26: tanggihan ang LAHAT ng alpaca."""
    monkeypatch.setattr(
        settings, "chili_momentum_loss_guard_alpaca_broker_truth_enabled",
        False, raising=False)
    assert _alpaca_loss_history_broker_truth(_Outcome()) is False


def test_the_branch_still_gaps_when_truth_is_missing():
    """BANTAY SA WIRING. Ang helper ay walang silbi kung hindi ito tinatawag ng
    branch, at ang gap ay dapat manatili para sa lahat ng iba."""
    import inspect

    from app.services.trading.momentum_neural import risk_policy

    src = inspect.getsource(risk_policy)
    assert "_alpaca_loss_history_broker_truth(outcome)" in src
    assert '_gap("loss_guard_alpaca_cycle_settlement_unavailable", session_id)' in src
    # ⚠️ Ang walang-kondisyong anyo ay hindi dapat mabuhay.
    assert 'if family in {"alpaca_spot", "alpaca_short"}:\n            _gap(' not in src


def test_the_downstream_checks_were_not_removed():
    """⚠️⚠️ ITO ANG DAHILAN KUNG BAKIT LIGTAS ANG PAGBABAGO. Ang branch ay
    dumadaloy na ngayon sa mga umiiral nang mahigpit na tseke -- kung may
    tumanggal sa mga iyon, ang helper ay magiging tanging tanod, na hindi ang
    disenyo."""
    import inspect

    from app.services.trading.momentum_neural import risk_policy

    src = inspect.getsource(risk_policy)
    for probe in (
        '_gap("loss_guard_broker_reconciliation_unavailable", session_id)',
        '_gap("loss_guard_broker_reconciled_at_unavailable", session_id)',
        '_gap("loss_guard_broker_label_not_available_as_of", session_id)',
        '_gap("loss_guard_broker_label_precedes_terminal", session_id)',
        '_gap("loss_guard_broker_pnl_nonfinite_or_unavailable", session_id)',
        '_gap("loss_guard_broker_label_sign_mismatch", session_id)',
    ):
        assert probe in src, "nawawala ang downstream na tseke: %s" % probe


def test_non_alpaca_families_never_reach_this_helper():
    """Ang helper ay tungkol sa alpaca lamang; ang ibang pamilya ay dumadaloy na
    sa mga downstream na tseke gaya ng dati. Bantay lamang sa hugis."""
    import inspect

    from app.services.trading.momentum_neural import risk_policy

    src = inspect.getsource(risk_policy)
    idx = src.find("_alpaca_loss_history_broker_truth(outcome)")
    assert idx > 0
    window = src[max(0, idx - 200): idx]
    assert 'family in {"alpaca_spot", "alpaca_short"}' in window
