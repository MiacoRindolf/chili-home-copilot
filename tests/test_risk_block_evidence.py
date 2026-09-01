"""Ang risk block ay nagdadala na ng DETALYE, hindi lamang ng mensahe (#1272).

NASUKAT 2026-09-01. Ang `live_blocked_by_risk` na payload ay
``{"severity", "errors"}`` lamang -- puro string. Ngunit ang `ev` na
kakakalkula pa lang sa parehong seam ay may buong ``checks`` na listahan na may
``detail`` sa bawat isa. Itinatapon ito sa emit.

Ang pinakamalaking halimbawa ngayong araw: **250 harang sa 22 sesyon** na
"Alpaca paper watcher resource headroom is unavailable." Ang
``risk_evaluator.py:2143-2155`` ay nagtatakda ng ``available: False`` sa
DALAWANG magkaibang daan:

    (a) tunay ngang punong kapasidad, at
    (b) SUMABOG ang kalkulasyon (``except Exception`` -> fail-closed; ang
        ``error_type`` lamang ang nagsasabi niyon).

Magkaiba silang problema at MAGKAPAREHO ang hitsura sa log. Ito ang ikatlong
beses ngayong araw na ang parehong pattern ang humaharang sa diagnosis
(#1269 no_bbo, #1270 drive-release, #1272 rito).

Runnable: pytest tests/test_risk_block_evidence.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import (
    _RISK_BLOCK_EVIDENCE_MAX_CHECKS,
    _RISK_BLOCK_EVIDENCE_MAX_KEYS,
    _risk_block_evidence,
)


def _check(name, ok, severity="block", detail=None):
    return {"name": name, "ok": ok, "severity": severity, "detail": detail}


# Ang eksaktong hugis ng 250-harang na bucket ngayong 2026-09-01.
CAPACITY_FULL = {
    "schema_version": "chili.alpaca-paper-arm-resource-capacity.v1",
    "account_scope": "alpaca:paper",
    "risk_usd": 0.0,
    "available": False,
    "provenance": {"authority": "resource_only_watch_fanout"},
}
CAPACITY_THREW = dict(CAPACITY_FULL, error_type="OperationalError")


def test_the_two_identical_looking_failures_become_distinguishable():
    """ANG PANGUNAHIN: puno ba talaga, o sumabog ang kalkulasyon?"""
    full = _risk_block_evidence({"checks": [
        _check("alpaca_paper_watch_resource_capacity", False, detail=CAPACITY_FULL),
    ]})
    threw = _risk_block_evidence({"checks": [
        _check("alpaca_paper_watch_resource_capacity", False, detail=CAPACITY_THREW),
    ]})
    cap_full = full["blocking_checks"]["alpaca_paper_watch_resource_capacity"]
    cap_threw = threw["blocking_checks"]["alpaca_paper_watch_resource_capacity"]
    assert "error_type" not in cap_full
    assert cap_threw["error_type"] == "OperationalError"
    assert cap_full["available"] is False and cap_threw["available"] is False, (
        "pareho silang available=False — ang error_type LAMANG ang naghihiwalay"
    )


def test_only_blocking_checks_are_carried():
    """Ang pumasa at ang warn ay hindi ingay sa payload."""
    out = _risk_block_evidence({"checks": [
        _check("ok_one", True, severity="ok", detail={"a": 1}),
        _check("warn_one", False, severity="warn", detail={"b": 2}),
        _check("block_one", False, detail={"c": 3}),
    ]})
    assert set(out["blocking_checks"]) == {"block_one"}
    assert out["blocking_check_names"] == ["block_one"]


def test_nested_detail_is_kept_one_level():
    out = _risk_block_evidence({"checks": [
        _check("x", False, detail={"top": 1, "nest": {"inner": "v"}}),
    ]})
    d = out["blocking_checks"]["x"]
    assert d["top"] == 1
    assert d["nest"]["inner"] == "v"


def test_deep_nesting_is_dropped_not_exploded():
    """Ang malalim na istruktura ay IBINABAGSAK, hindi iniimbak bilang None.

    Ang isang susi na ang halaga ay pinutol ay mas nakakalito kaysa sa isang
    susing wala — ang ``"b": null`` ay mukhang "sinukat namin at wala", samantalang
    ang katotohanan ay "masyadong malalim para dalhin". Kaya ibinababa ito.
    """
    out = _risk_block_evidence({"checks": [
        _check("x", False, detail={"a": {"b": {"c": {"d": "malalim"}}}}),
    ]})
    inner = out["blocking_checks"]["x"]["a"]
    assert inner == {}, "ang malalim na sanga ay ibinababa nang buo"
    # ...pero ang TUNAY na null ay nananatili, dahil iyon ay sinukat na katotohanan.
    out2 = _risk_block_evidence({"checks": [
        _check("y", False, detail={"error_type": None, "available": False}),
    ]})
    assert out2["blocking_checks"]["y"] == {"error_type": None, "available": False}


def test_checks_are_bounded():
    checks = [
        _check(f"blk_{i}", False, detail={"i": i})
        for i in range(_RISK_BLOCK_EVIDENCE_MAX_CHECKS + 3)
    ]
    out = _risk_block_evidence({"checks": checks})
    assert len(out["blocking_checks"]) == _RISK_BLOCK_EVIDENCE_MAX_CHECKS
    assert out["blocking_checks_truncated"] is True


def test_keys_are_bounded():
    big = {f"k{i}": i for i in range(_RISK_BLOCK_EVIDENCE_MAX_KEYS + 6)}
    out = _risk_block_evidence({"checks": [_check("x", False, detail=big)]})
    d = out["blocking_checks"]["x"]
    assert len(d) <= _RISK_BLOCK_EVIDENCE_MAX_KEYS + 1
    assert d["(pinutol)"] is True


def test_no_blocking_checks_adds_nothing():
    """Walang bumara ⇒ walang idinagdag na field (byte-identical na payload)."""
    assert _risk_block_evidence({"checks": [_check("a", True, severity="ok")]}) == {}
    assert _risk_block_evidence({"checks": []}) == {}


def test_never_raises_on_garbage():
    """Telemetry ay hindi kailanman dapat pumatay ng tick."""
    for bad in (None, {}, {"checks": "hindi-listahan"}, {"checks": [None, 7, "x"]}):
        assert _risk_block_evidence(bad) == {}, bad
