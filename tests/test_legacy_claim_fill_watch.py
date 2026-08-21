"""P0 BCCQ 2026-08-21 — ang legacy time-share claim ay hindi dapat humarang sa
sariling fill-poll ng session.

Ang mekanismo: ang legacy escape ay gumagawa ng claim na WALANG
adaptive_risk_reservation_request (by design); ang owner-claim recovery ay
nagba-block nang deterministic sa kawalan nito — walang event, walang log —
bago pa ang CHUNK 3-B fast ack-poll, kaya ang na-fill na entry (92sh @ 10.97,
filled 60s pagkatapos ng submit) ay hindi kailanman na-adopt sa loob ng 2h40m.
Runnable: pytest tests/test_legacy_claim_fill_watch.py -v
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import app.services.trading.momentum_neural.live_runner as lr

_LR = "app.services.trading.momentum_neural.live_runner"


def test_legacy_marker_detected_in_role_metadata():
    claim = {"metadata": {"role_metadata": {"legacy_timeshare_sizing": True}}}
    assert lr._legacy_timeshare_claim(claim) is True


def test_legacy_marker_detected_at_metadata_top_level():
    claim = {"metadata": {"legacy_timeshare_sizing": True}}
    assert lr._legacy_timeshare_claim(claim) is True


def test_adaptive_claim_is_not_legacy():
    claim = {"metadata": {"role_metadata": {}}}
    assert lr._legacy_timeshare_claim(claim) is False
    assert lr._legacy_timeshare_claim(None) is False


def _sess():
    return SimpleNamespace(
        id=14732,
        symbol="BCCQ",
        execution_family="alpaca_spot",
        risk_snapshot_json={},
    )


def _claim(metadata):
    return {
        "phase": "claimed",
        "action": "entry",
        "owner_session_id": 14732,
        "client_order_id": "chili_ml_e_14732_x",
        "claim_token": "tok-1",
        "broker_order_id": "6111719d-test",
        "metadata": metadata,
    }


def _recover(claim):
    with patch(f"{_LR}._frozen_alpaca_account_scope", return_value="alpaca:paper"), \
            patch(f"{_LR}.read_action_claim", return_value=(True, claim)), \
            patch(f"{_LR}._strict_alpaca_account_identity",
                  return_value=(True, {"reason": "ok"})):
        return lr._recover_owner_alpaca_entry_claim(
            object(), _sess(), object(),
            le={}, product_id="BCCQ", operator_paused=False,
        )


def test_legacy_claim_passes_through_nonblocking():
    """ANG P0 CASE: legacy claim (walang adaptive payload) -> HINDI humaharang,
    kaya aabot ang tick sa CHUNK 3-B poll at maa-adopt ang fill."""
    res = _recover(_claim({"role_metadata": {
        "legacy_timeshare_sizing": True, "alpaca_account_id": "acct-1",
    }}))
    assert res["block_new_entries"] is False
    assert res["active"] is False
    assert res["reason"] == "legacy_timeshare_claim_pass_through"


def test_adaptive_claim_without_payload_still_blocks():
    """Ang tunay na adaptive claim na nawalan ng payload ay HARANG pa rin —
    hindi pinaluwag ng fix ang adaptive-mode guard."""
    res = _recover(_claim({"role_metadata": {"alpaca_account_id": "acct-1"}}))
    assert res["block_new_entries"] is True
    assert res["reason"] == "adaptive_risk_reservation_request_missing"


def test_silent_block_now_emits_once():
    """Ang dating ganap na tahimik na caller return ay nag-e-emit na ng event sa
    unang tama (at sa pagbabago ng dahilan lang — hindi kada tick)."""
    import inspect

    src = inspect.getsource(lr.tick_live_session)
    guard_at = src.index("_alpaca_owner_recovery_blocks_entries\n        and sess.state not in _HELD_LIVE_STATES")
    emit_at = src.index("live_entry_owner_claim_reconcile_block", guard_at)
    ret_at = src.index('"pending": (', guard_at)
    assert guard_at < emit_at < ret_at, "the block-return must emit before returning"
    sig_at = src.index('le.get("owner_recovery_block_sig") != _orb_sig', guard_at)
    assert sig_at < emit_at, "emit must be de-duplicated by reason signature"
