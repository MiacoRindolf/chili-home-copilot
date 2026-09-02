"""Bug A ng 09-02 CANF 19471: ang risk ledger ay nagtuturing sa PRE-TRANSPORT
pending row bilang "unknown exposure" at nagra-raise.

ANG INSIDENTE (2026-09-02, Alpaca PAPER, session 19471 CANF). 11:16:34Z
`live_entry_pending_place` @4.40 → 11:16:45–11:18:55Z 12× tick FAILED
`alpaca_risk_ledger_unavailable:pending_entry_evidence_missing:session=19471`
— ang session ay nag-raise sa SARILI NIYA (wala pa itong claim dahil hindi pa
ito nakakarating sa `_prepare_alpaca_place_claim`), kaya walang submit nang 2.6
minuto habang tumatakbo ang CANF 4.40→4.62. Saklaw sa lane log: 1,186 failures
sa 21 session; 1,650 ng 1,992 `alpaca_risk_ledger_unavailable` ay CROSS-session
(ang stuck na session X ay pumapatay sa tick ng session Y); 09-01 13:41–15:01Z
(RTH open) ang 19428/19430/19431 ay nagharang sa isa't isa.

ANG KATOTOHANAN: ang claim row ay COMMITTED sa sarili nitong maikling
transaction BAGO ang anumang POST (alpaca_orphan_claims
`acquire_action_claim_committed` / `mark_entry_transport_started`), at ang
claim na umabot sa transport ay hindi kailanman tahimik na nirerelease. Kaya
"pending + walang claim + hindi submitted" = HINDI pa tumawid sa broker =
$0 exposure, hindi unknown.

Ang held-state raises (`held_live_execution_missing`, `held_position_missing`,
`position_malformed`, qty/avg invalid, unstopped SHORT) ay nananatiling
byte-identical — fail-closed pa rin sa hindi mabilang na exposure.

Runnable: pytest tests/test_naked_position_0902_risk_ledger.py -v
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import risk_evaluator as RE


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, rows, log=None):
        self._rows = rows
        self._log = log

    def filter(self, *_a, **_k):
        return self

    def all(self):
        if self._log is not None:
            self._log.append("rows")
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows, log=None):
        self._rows = rows
        self.log = log

    def query(self, *_a, **_k):
        return _FakeQuery(self._rows, self.log)


def _sess(sid, *, state, le, family="alpaca_spot", symbol="CANF"):
    snap = {
        "alpaca_account_scope": "alpaca:paper",
        "momentum_live_execution": le,
    }
    return SimpleNamespace(
        id=sid, symbol=symbol, state=state, execution_family=family,
        risk_snapshot_json=snap, user_id=1,
    )


def _claim(owner, *, phase="submit_indeterminate", cid="chili-x", oid=None,
           reserved=45.99):
    return SimpleNamespace(
        owner_session_id=owner, phase=phase, client_order_id=cid,
        broker_order_id=oid,
        metadata_json={"reserved_risk_usd": reserved,
                       "order_request": {"product_id": "CANF"}},
    )


def _patch_claims(monkeypatch, claims, log=None):
    def _fn(db):
        if log is not None:
            log.append("claims")
        return list(claims)
    monkeypatch.setattr(RE, "_alpaca_unresolved_entry_claims", _fn)


def _readers(db):
    return {
        "aggregate": lambda: RE.aggregate_open_risk_usd(
            db, user_id=1, execution_family="alpaca_spot"),
        "count": lambda: RE.count_inflight_entry_orders(
            db, user_id=1, execution_family="alpaca_spot"),
        "sum": lambda: RE.sum_inflight_entry_risk_usd(
            db, user_id=1, execution_family="alpaca_spot",
            per_trade_fallback_usd=50.0),
    }


# ── 1. pre-transport pending is $0 with a note, never a raise ────────────────


@pytest.mark.parametrize("le", [{}, {"entry_submitted": False}, "not-a-dict"])
def test_pre_transport_pending_row_is_zero_with_note(monkeypatch, le):
    """ANG PANGUNAHING KASO — ang 19471 sa 11:16:45Z: pending, walang claim,
    hindi submitted. Bawat reader ay dapat $0 / 0, hindi raise."""
    _patch_claims(monkeypatch, [])
    db = _FakeDb([_sess(19471, state="live_pending_entry", le=le)])
    total, rows = RE.aggregate_open_risk_usd(
        db, user_id=1, execution_family="alpaca_spot")
    assert total == 0.0
    assert len(rows) == 1
    assert rows[0]["session_id"] == 19471
    assert rows[0]["at_risk_usd"] == 0.0
    assert rows[0]["note"] == "pre_transport_pending"
    assert RE.count_inflight_entry_orders(
        db, user_id=1, execution_family="alpaca_spot") == 0
    assert RE.sum_inflight_entry_risk_usd(
        db, user_id=1, execution_family="alpaca_spot",
        per_trade_fallback_usd=50.0) == 0.0


def test_pre_transport_row_never_blocks_a_sibling(monkeypatch):
    """Ang cross-session shape: ang stuck na X ay HINDI dapat pumatay sa tick
    ng Y. Dalawang pre-transport row ⇒ pareho $0."""
    _patch_claims(monkeypatch, [])
    db = _FakeDb([
        _sess(19428, state="live_pending_entry", le={}),
        _sess(19430, state="live_pending_entry", le={"entry_submitted": False}),
    ])
    total, rows = RE.aggregate_open_risk_usd(
        db, user_id=1, execution_family="alpaca_spot")
    assert total == 0.0
    assert {r["session_id"] for r in rows} == {19428, 19430}


# ── 2. a CLAIMED pending row is charged through the claim ledger ─────────────


def test_claimed_pending_row_is_charged_via_claim_ledger(monkeypatch):
    _patch_claims(monkeypatch, [_claim(19471, reserved=45.99)])
    db = _FakeDb([_sess(19471, state="live_pending_entry", le={})])
    assert RE.aggregate_open_risk_usd(
        db, user_id=1, execution_family="alpaca_spot") == (0.0, [])
    assert RE.sum_inflight_entry_risk_usd(
        db, user_id=1, execution_family="alpaca_spot",
        per_trade_fallback_usd=50.0) == pytest.approx(45.99)
    assert RE.count_inflight_entry_orders(
        db, user_id=1, execution_family="alpaca_spot") == 1


def test_submitted_pending_row_without_claim_is_still_charged(monkeypatch):
    """entry_submitted=True + walang claim (crash-after-HTTP na shape) ay
    HINDI pre-transport — binibilang pa rin sa in-flight ledger."""
    _patch_claims(monkeypatch, [])
    db = _FakeDb([_sess(19471, state="live_pending_entry",
                        le={"entry_submitted": True,
                            "entry_inflight_risk_usd": 33.0})])
    assert RE.count_inflight_entry_orders(
        db, user_id=1, execution_family="alpaca_spot") == 1
    assert RE.sum_inflight_entry_risk_usd(
        db, user_id=1, execution_family="alpaca_spot",
        per_trade_fallback_usd=50.0) == pytest.approx(33.0)
    # aggregate: pending + submitted + position None => not charged here (in-flight
    # ledger owns it), but NOT a note row either and never a raise.
    assert RE.aggregate_open_risk_usd(
        db, user_id=1, execution_family="alpaca_spot") == (0.0, [])


# ── 3. held rows stay fail-closed ────────────────────────────────────────────


@pytest.mark.parametrize("state", ["live_entered", "live_trailing",
                                   "live_scaling_out", "live_bailout"])
def test_held_rows_keep_fail_closed(monkeypatch, state):
    _patch_claims(monkeypatch, [])
    with pytest.raises(RuntimeError, match="held_live_execution_missing"):
        RE.aggregate_open_risk_usd(
            _FakeDb([_sess(1, state=state, le="bad")]),
            user_id=1, execution_family="alpaca_spot")
    with pytest.raises(RuntimeError, match="held_position_missing"):
        RE.aggregate_open_risk_usd(
            _FakeDb([_sess(1, state=state, le={"entry_submitted": True})]),
            user_id=1, execution_family="alpaca_spot")
    with pytest.raises(RuntimeError, match="position_malformed"):
        RE.aggregate_open_risk_usd(
            _FakeDb([_sess(1, state=state, le={"position": "bad"})]),
            user_id=1, execution_family="alpaca_spot")


@pytest.mark.parametrize("field", ["quantity", "avg_entry_price"])
@pytest.mark.parametrize("bad", [None, "bad", "nan", "inf", True, 0, -1])
def test_held_qty_or_avg_invalid_still_raises(monkeypatch, field, bad):
    _patch_claims(monkeypatch, [])
    pos = {"quantity": 10, "avg_entry_price": 10.0, "stop_price": 9.5}
    pos[field] = bad
    db = _FakeDb([_sess(1, state="live_entered",
                        le={"side_long": True, "position": pos})])
    with pytest.raises(RuntimeError, match="position_risk_fields_invalid"):
        RE.aggregate_open_risk_usd(db, user_id=1, execution_family="alpaca_spot")


def test_pending_row_with_malformed_position_still_raises(monkeypatch):
    _patch_claims(monkeypatch, [])
    db = _FakeDb([_sess(1, state="live_pending_entry", le={"position": "bad"})])
    with pytest.raises(RuntimeError, match="position_malformed"):
        RE.aggregate_open_risk_usd(db, user_id=1, execution_family="alpaca_spot")


# ── 4/5. unknown stop: LONG = full notional bound; SHORT = still raise ───────


def test_held_stop_unknown_long_is_full_notional(monkeypatch):
    """806× `position_risk_fields_invalid` sa lane log ay ang adopt-for-safety
    / kill-switch row na `stop_price=None`. Ang worst case ng LONG ay ang buong
    notional — alam, hindi unknown."""
    _patch_claims(monkeypatch, [])
    pos = {"quantity": 177, "avg_entry_price": 9.91, "stop_price": None}
    db = _FakeDb([_sess(1, state="live_entered",
                        le={"side_long": True, "position": pos})])
    total, rows = RE.aggregate_open_risk_usd(
        db, user_id=1, execution_family="alpaca_spot")
    assert total == pytest.approx(1754.07)
    assert rows[0]["note"] == "stop_unknown_full_notional_bound"
    assert rows[0]["at_risk_usd"] == pytest.approx(1754.07)
    pos["stop_price"] = 9.82
    total, rows = RE.aggregate_open_risk_usd(
        db, user_id=1, execution_family="alpaca_spot")
    assert total == pytest.approx(15.93)
    assert "note" not in rows[0]


def test_held_stop_unknown_short_is_bounded_not_raised(monkeypatch):
    """⚠️ Ang notional ay HINDI upper bound ng SHORT — kaya HINDI full notional
    kundi notional x ang dokumentadong multiple (2026-09-02 follow-up sa #1285:
    ang raise dito ay parehong account-wide landmine class; tests/
    test_short_unstopped_bound_0902.py ang buong contract)."""
    _patch_claims(monkeypatch, [])
    pos = {"quantity": 177, "avg_entry_price": 9.91, "stop_price": None}
    for family, le in (("alpaca_short", {"position": pos}),
                       ("alpaca_spot", {"side_long": False, "position": pos})):
        db = _FakeDb([_sess(1, state="live_entered", family=family, le=le)])
        total, rows = RE.aggregate_open_risk_usd(db, user_id=1, execution_family="alpaca_spot")
        assert total == pytest.approx(177 * 9.91 * 2.0), family
        assert rows[0]["note"] == "stop_unknown_short_notional_multiple_bound"
        assert total > 177 * 9.91, "never looser than the long's full notional"


@pytest.mark.parametrize("le_extra,pos_extra", [
    ({}, {"side": "short"}),
    ({}, {"side": "sell"}),
    ({}, {"side_long": False}),
    ({}, {"position_intent": "sell_to_open"}),
    ({}, {"intent": "buy_to_close"}),
    ({"side": "short"}, {}),
    ({"position_intent": "sell_to_open"}, {}),
])
def test_held_stop_unknown_short_shaped_position_is_not_the_long_bound(monkeypatch, le_extra, pos_extra):
    """Ang full-notional bound ay para LANG sa posisyong provably LONG sa bawat
    marker na binabasa ng runner (`_le_side_long`): `position.side`,
    `position_intent`/`intent`, nested `side_long`. Ang row na inampon sa
    ilalim ng operator repair ay may `position.side` at WALANG `side_long`
    key — hindi ito dapat singilin bilang long by omission: ang provably SHORT
    na row ay kumukuha ng short multiple bound."""
    _patch_claims(monkeypatch, [])
    pos = {"quantity": 100, "avg_entry_price": 5.0, "stop_price": None, **pos_extra}
    db = _FakeDb([_sess(1, state="live_entered", family="alpaca_spot",
                        le={**le_extra, "position": pos})])
    total, rows = RE.aggregate_open_risk_usd(db, user_id=1, execution_family="alpaca_spot")
    assert rows[0]["note"] == "stop_unknown_short_notional_multiple_bound"
    assert total == pytest.approx(1000.0)


@pytest.mark.parametrize("le_extra,pos_extra", [
    ({}, {"side": "sideways"}),          # unreadable marker => not certified
    ({"side_long": True}, {"side": "short"}),  # contradictory => not certified
])
def test_held_stop_unknown_uncertifiable_direction_still_raises(monkeypatch, le_extra, pos_extra):
    _patch_claims(monkeypatch, [])
    pos = {"quantity": 100, "avg_entry_price": 5.0, "stop_price": None, **pos_extra}
    db = _FakeDb([_sess(1, state="live_entered", family="alpaca_spot",
                        le={**le_extra, "position": pos})])
    with pytest.raises(RuntimeError, match="position_risk_fields_invalid"):
        RE.aggregate_open_risk_usd(db, user_id=1, execution_family="alpaca_spot")


@pytest.mark.parametrize("le_extra,pos_extra", [
    ({}, {}),
    ({}, {"side": "long"}),
    ({}, {"side": "buy", "position_intent": "buy_to_open"}),
    ({"side_long": True}, {"side_long": True, "intent": "sell_to_close"}),
])
def test_held_stop_unknown_long_shaped_position_is_full_notional(monkeypatch, le_extra, pos_extra):
    _patch_claims(monkeypatch, [])
    pos = {"quantity": 100, "avg_entry_price": 5.0, "stop_price": None, **pos_extra}
    db = _FakeDb([_sess(1, state="live_entered", family="alpaca_spot",
                        le={**le_extra, "position": pos})])
    total, rows = RE.aggregate_open_risk_usd(db, user_id=1, execution_family="alpaca_spot")
    assert total == pytest.approx(500.0)
    assert rows[0]["note"] == "stop_unknown_full_notional_bound"


def test_note_rows_are_never_a_displacement_target():
    """Ang note row ay bookkeeping, hindi posisyong maaaring i-tighten."""
    open_rows = [
        {"symbol": "A", "session_id": 1, "at_risk_usd": 500.0,
         "note": "stop_unknown_full_notional_bound"},
        {"symbol": "B", "session_id": 2, "at_risk_usd": 0.0,
         "note": "pre_transport_pending"},
    ]
    src = inspect.getsource(RE._enqueue_risk_envelope_displacement)
    assert 'not r.get("note")' in src
    # sanity: the filter expression drops both note rows
    kept = [r for r in open_rows if isinstance(r, dict) and not r.get("note")
            and float(r.get("at_risk_usd") or 0.0) > 0.0]
    assert kept == []


# ── 6. ledger read failure still propagates ─────────────────────────────────


def test_ledger_read_failure_still_raises(monkeypatch):
    def _boom(db):
        raise RuntimeError("claims_unreadable")
    monkeypatch.setattr(RE, "_alpaca_unresolved_entry_claims", _boom)
    db = _FakeDb([_sess(1, state="live_pending_entry", le={})])
    for name, reader in _readers(db).items():
        with pytest.raises(RuntimeError, match="claims_unreadable"):
            reader()


# ── 7. rows are read BEFORE claims (no advisory lock on Alpaca) ─────────────


def test_rows_read_before_claims_runtime(monkeypatch):
    """Ang sibling na ang claim ay nag-commit sa PAGITAN ng dalawang read ay
    dapat makitang claim-present, hindi pre-transport."""
    for name in ("aggregate", "count", "sum"):
        log: list[str] = []
        _patch_claims(monkeypatch, [], log=log)
        db = _FakeDb([_sess(1, state="live_pending_entry", le={})], log=log)
        _readers(db)[name]()
        assert "rows" in log and "claims" in log, (name, log)
        assert log.index("rows") < log.index("claims"), (name, log)


def _first_call_index(fn, *, attr=None, name=None) -> int:
    """Lineno of the FIRST matching call in SOURCE order (ast.walk is
    breadth-first, so the first-visited node is not the first-in-source one)."""
    tree = ast.parse(inspect.getsource(fn))
    linenos: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if attr is not None and isinstance(f, ast.Attribute) and f.attr == attr:
            linenos.append(node.lineno)
        if name is not None and isinstance(f, ast.Name) and f.id == name:
            linenos.append(node.lineno)
    if not linenos:
        raise AssertionError(f"no call attr={attr} name={name}")
    return min(linenos)


@pytest.mark.parametrize("fn", [
    RE.aggregate_open_risk_usd,
    RE.count_inflight_entry_orders,
    RE.sum_inflight_entry_risk_usd,
])
def test_rows_read_before_claims_ast(fn):
    i_all = _first_call_index(fn, attr="all")
    i_claims = _first_call_index(fn, name="_alpaca_unresolved_entry_claims")
    assert i_all < i_claims, (fn.__name__, i_all, i_claims)


# ── 8. AST guards ────────────────────────────────────────────────────────────


def test_pending_entry_evidence_missing_is_gone_from_all_readers():
    for fn in (RE.aggregate_open_risk_usd, RE.count_inflight_entry_orders,
               RE.sum_inflight_entry_risk_usd):
        assert "pending_entry_evidence_missing" not in inspect.getsource(fn), fn.__name__


def test_aggregate_keeps_every_held_and_malformed_raise():
    src = inspect.getsource(RE.aggregate_open_risk_usd)
    for reason in ("held_live_execution_missing", "held_position_missing",
                   "position_malformed", "position_risk_fields_invalid",
                   "position_risk_nonfinite"):
        assert f'"{reason}"' in src, reason
    assert src.count("_alpaca_pending_pre_transport(") >= 2
    assert "stop_unknown_full_notional_bound" in src
