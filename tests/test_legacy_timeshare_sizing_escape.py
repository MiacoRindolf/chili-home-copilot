"""Time-share runner escape (2026-08-17, operator decision Option C).

Ang alpaca_spot adaptive-risk sizing ay nangangailangan ng capture provider na
tanging ang sealed captured-paper service ang nakakapag-install — kaya ang
ordinaryong time-share lane ay istrukturang hindi makapag-post ng entry
(`builder_missing_capture_binding` sa bawat attempt, buong umaga ng 08-17).

Ang escape: kapag ang `chili_momentum_legacy_alpaca_dispatch_enabled` ay ON at
WALANG installed capture provider, ang lane ay tumatakbo sa LEGACY sizing —
(a) walang raise sa adaptive builder, (b) tinatanggap ng claim/place seams ang
kawalan ng adaptive triple, at (c) ang `alpaca_paper_hard_loss_cap_usd` ay
nagbabalik ng TUNAY na equity-relative na per-trade cap. Kapag MAY provider
(sealed/replay/tests), ang adaptive path ang laging mananaig.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
import app.services.trading.momentum_neural.live_runner as lr
import app.services.trading.momentum_neural.risk_policy as rp
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    adaptive_risk_capture_provider_installed,
    adaptive_risk_source_provider,
)


def test_escape_inactive_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", False)
    assert lr._legacy_alpaca_timeshare_sizing_active() is False


def test_escape_active_when_flag_on_and_no_provider(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    assert adaptive_risk_capture_provider_installed() is False
    assert lr._legacy_alpaca_timeshare_sizing_active() is True


def test_escape_yields_to_installed_provider(monkeypatch):
    """Kapag may provider (sealed/replay), ang adaptive path ang mananaig."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    with adaptive_risk_source_provider(lambda **kw: None):
        assert adaptive_risk_capture_provider_installed() is True
        assert lr._legacy_alpaca_timeshare_sizing_active() is False
    assert lr._legacy_alpaca_timeshare_sizing_active() is True


def _le_long():
    return {"position_side": "long", "structural_stop_price": 7.5}


def test_builder_returns_none_triple_under_escape(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    sess = SimpleNamespace(id=1, correlation_id="c", symbol="IPST",
                           execution_family="alpaca_spot",
                           risk_snapshot_json={})
    built, place_n, cid = lr._build_adaptive_alpaca_primary_before_legacy_sizing(
        sess, _le_long(), execution_family="alpaca_spot", bid=7.9, ask=8.0,
    )
    assert built is None and place_n is None and cid is None


def test_builder_still_raises_without_escape(monkeypatch):
    """Flag OFF → ang orihinal na hard requirement ay buo (walang provider → raise)."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", False)
    sess = SimpleNamespace(id=1, correlation_id="c", symbol="IPST",
                           execution_family="alpaca_spot")
    try:
        lr._build_adaptive_alpaca_primary_before_legacy_sizing(
            sess, _le_long(), execution_family="alpaca_spot", bid=7.9, ask=8.0,
        )
    except AdaptiveRiskBuilderError as exc:
        assert getattr(exc, "reason", str(exc.args[0])) == "builder_missing_capture_binding"
    else:
        raise AssertionError("dapat nag-raise nang walang escape")


def test_builder_short_still_raises_under_escape(monkeypatch):
    """Ang escape ay long-alpaca_spot lang — ang short ay bawal pa rin."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    sess = SimpleNamespace(id=1, correlation_id="c", symbol="IPST",
                           execution_family="alpaca_short")
    try:
        lr._build_adaptive_alpaca_primary_before_legacy_sizing(
            sess, {"position_side": "short"}, execution_family="alpaca_short",
            bid=7.9, ask=8.0,
        )
    except AdaptiveRiskBuilderError:
        pass
    else:
        raise AssertionError("dapat nag-raise para sa short")


def test_hard_loss_cap_none_on_adaptive_path(monkeypatch):
    """Walang escape → None pa rin (adaptive parity ang may-ari ng sizing)."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", False)
    assert rp.alpaca_paper_hard_loss_cap_usd("alpaca_spot") is None


def test_hard_loss_cap_real_under_escape(monkeypatch):
    """Ang cap ay dapat dumaan sa equity x fraction na may POSITIBONG fixed
    fallback — ang 0.0 fallback ay 'operator disable' short-circuit sa
    _equity_relative_cap (ang bug na itinago ng dating naka-patch na test)."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    seen: dict = {}

    def _spy(fixed_fallback_usd, execution_family=None):
        seen["fallback"] = float(fixed_fallback_usd)
        return 1234.5

    with patch.object(rp, "equity_relative_loss_cap", side_effect=_spy):
        cap = rp.alpaca_paper_hard_loss_cap_usd("alpaca_spot")
    assert cap == 1234.5
    assert seen["fallback"] > 0.0, "0.0 fallback = disable short-circuit = walang cap"


def test_hard_loss_cap_real_through_real_internals(monkeypatch):
    """End-to-end sa totoong equity_relative_loss_cap: patched LANG ang equity
    read — dapat lumabas ang equity x loss_fraction, hindi 0/None."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    with patch.object(rp, "_account_equity_usd", return_value=100_000.0):
        cap = rp.alpaca_paper_hard_loss_cap_usd("alpaca_spot")
    frac = float(getattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01))
    assert cap == round(100_000.0 * frac, 2)


def test_hard_loss_cap_fail_open_to_none_on_zero(monkeypatch):
    """Kapag hindi mabasa ang equity (cap 0/None) → None (walang pekeng ceiling)."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    with patch.object(rp, "equity_relative_loss_cap", return_value=0.0):
        assert rp.alpaca_paper_hard_loss_cap_usd("alpaca_spot") is None


def test_hard_loss_cap_non_alpaca_none(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    assert rp.alpaca_paper_hard_loss_cap_usd("coinbase_spot") is None


# ---- session-aware escape (sealed-lane exclusion) ----

def test_escape_refuses_captured_paper_marked_sessions(monkeypatch):
    """BLOCKER fix: sealed pending-owner/final-owner sessions ay hindi kailanman
    dadaan sa escape kahit ON ang flag (protektado ang sealed generation)."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    pending = SimpleNamespace(
        risk_snapshot_json={"captured_paper_session_pending_owner": {"x": 1}}
    )
    final = SimpleNamespace(
        risk_snapshot_json={"captured_paper_session_owner": {"x": 1}}
    )
    clean = SimpleNamespace(risk_snapshot_json={})
    unreadable = SimpleNamespace(risk_snapshot_json="hindi-dict")
    assert lr._legacy_alpaca_timeshare_escape(pending) is False
    assert lr._legacy_alpaca_timeshare_escape(final) is False
    assert lr._legacy_alpaca_timeshare_escape(unreadable) is False  # fail-closed
    assert lr._legacy_alpaca_timeshare_escape(clean) is True


def test_escape_inert_inside_sealed_replay(monkeypatch):
    """Replay determinism: sa loob ng replay_ortex_selection_provider (ang
    balot ng BAWAT replay_v3 decision tick) ang escape ay laging OFF."""
    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    clean = SimpleNamespace(risk_snapshot_json={})
    assert lr._legacy_alpaca_timeshare_escape(clean) is True
    token = lr._REPLAY_ORTEX_DECISION_STATE.set(object())
    try:
        assert lr._legacy_alpaca_timeshare_sizing_active() is False
        assert lr._legacy_alpaca_timeshare_escape(clean) is False
    finally:
        lr._REPLAY_ORTEX_DECISION_STATE.reset(token)


# ---- ANG kritikal na seam: reservation commit sa escape mode ----

def _seed_owner(db, symbol: str):
    import uuid as _uuid

    from app import models
    from tests.test_alpaca_account_risk_reservations import (
        _entry_order_request,
        _session,
        _variant,
    )

    user = models.User(name=f"esc-{_uuid.uuid4().hex[:10]}")
    db.add(user)
    db.flush()
    variant = _variant(db, f"esc_{_uuid.uuid4().hex[:10]}")
    owner = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol=symbol,
        family="alpaca_spot",
        state="live_pending_entry",
        live_execution={"side_long": True},
    )
    db.commit()
    return int(owner.id), _entry_order_request


def test_escape_reservation_admits_marked_pairless_commit(db, monkeypatch):
    """ANG blocker na hindi nahuli ng unang tests: ang reserve seam ay dapat
    TANGGAPIN ang marked, pair-less na legacy commit (dating hard-reject)."""
    import uuid as _uuid

    from app.services.trading.momentum_neural.alpaca_orphan_claims import (
        reserve_alpaca_entry_risk_committed,
    )

    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    owner_id, _req = _seed_owner(db, "ESCA")
    cid = f"cid-esc-{_uuid.uuid4().hex[:10]}"
    result = reserve_alpaca_entry_risk_committed(
        symbol="ESCA",
        claim_token=f"esc-{_uuid.uuid4().hex[:10]}",
        owner_session_id=owner_id,
        client_order_id=cid,
        post_bind_token=f"binder-{cid}",
        order_request=_req("ESCA", cid, qty="10"),
        order_role="primary",
        reserved_risk_usd=50.0,
        account_equity_usd=100_000.0,
        account_scope="alpaca:paper",
        role_metadata={"legacy_timeshare_sizing": True},
        per_symbol_cap_usd=100.0,
    )
    assert result.get("ok") is True, result
    assert result["reserved_risk_usd"] == 50.0
    assert result["symbol_cap_usd"] == 100.0


def test_escape_reservation_enforces_per_symbol_cap(db, monkeypatch):
    """Ang restored per-symbol hard cap ay TUNAY na veto sa reservation (dating
    dead parameter na symbol_cap=inf)."""
    import uuid as _uuid

    from app.services.trading.momentum_neural.alpaca_orphan_claims import (
        reserve_alpaca_entry_risk_committed,
    )

    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    owner_id, _req = _seed_owner(db, "ESCB")
    cid = f"cid-esc-{_uuid.uuid4().hex[:10]}"
    result = reserve_alpaca_entry_risk_committed(
        symbol="ESCB",
        claim_token=f"esc-{_uuid.uuid4().hex[:10]}",
        owner_session_id=owner_id,
        client_order_id=cid,
        post_bind_token=f"binder-{cid}",
        order_request=_req("ESCB", cid, qty="10"),
        order_role="primary",
        reserved_risk_usd=150.0,
        account_equity_usd=100_000.0,
        account_scope="alpaca:paper",
        role_metadata={"legacy_timeshare_sizing": True},
        per_symbol_cap_usd=100.0,
    )
    assert result.get("ok") is False
    assert result.get("reason") == "symbol_risk_cap_exceeded"
    assert result.get("symbol_cap_usd") == 100.0


def test_reservation_still_rejects_unmarked_pairless_commit(db, monkeypatch):
    """Walang marker → ang orihinal na adaptive hard requirement ay BUO."""
    import uuid as _uuid

    from app.services.trading.momentum_neural.alpaca_orphan_claims import (
        reserve_alpaca_entry_risk_committed,
    )

    monkeypatch.setattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", True)
    owner_id, _req = _seed_owner(db, "ESCC")
    cid = f"cid-esc-{_uuid.uuid4().hex[:10]}"
    result = reserve_alpaca_entry_risk_committed(
        symbol="ESCC",
        claim_token=f"esc-{_uuid.uuid4().hex[:10]}",
        owner_session_id=owner_id,
        client_order_id=cid,
        post_bind_token=f"binder-{cid}",
        order_request=_req("ESCC", cid, qty="10"),
        order_role="primary",
        reserved_risk_usd=50.0,
        account_equity_usd=100_000.0,
        account_scope="alpaca:paper",
        per_symbol_cap_usd=100.0,
    )
    assert result.get("ok") is False
    assert result.get("reason") == "adaptive_risk_request_packet_claim_required"
