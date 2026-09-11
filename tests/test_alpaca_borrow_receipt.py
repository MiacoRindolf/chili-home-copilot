"""[63] BORROW RECEIPT — ang broker ang nagsasabi kung shortable ba ang pangalan.

Ang ``alpaca_spot.get_product().raw`` ay naglalabas na ng ``shortable`` at ``easy_to_borrow``
(alpaca_spot.py:2721-2722) simula pa noong isinulat ang komentong "so the short-entry gate can
fail-closed on a not-shortable / hard-to-borrow name" — pero WALANG bumabasa nito. Ang resibong
ito ang unang mambabasa, at INIUULAT LAMANG ito: walang desisyon, walang order kwarg, at
naka-quarantine pa rin ang buong short execution.

Sinusukat din dito ang AYOS sa cache ng listing probe: ang dating cache ay panghabambuhay ng
proseso, kaya ang isang lumilipas na error ay HABAMBUHAY na nagbabawal ng twin sa pangalang iyon.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.auto_arm as aa
from app.services.trading.venue import alpaca_spot as ap


class _Prod(SimpleNamespace):
    pass


def _prod(*, disabled=False, raw=None):
    return _Prod(trading_disabled=disabled, raw=raw)


@pytest.fixture(autouse=True)
def _clean_cache():
    aa._ALPACA_LISTED_CACHE.clear()
    yield
    aa._ALPACA_LISTED_CACHE.clear()


def _patch_adapter(monkeypatch, fn, counter=None):
    class _FakeAdapter:
        def get_product(self, sym):
            if counter is not None:
                counter["n"] += 1
            return fn(sym)

    monkeypatch.setattr(ap, "AlpacaSpotAdapter", _FakeAdapter)


# ── ang resibo mismo ─────────────────────────────────────────────────────────────────────


def test_receipt_surfaces_broker_flags(monkeypatch):
    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": True, "easy_to_borrow": True}), None),
    )
    r = aa.alpaca_borrow_receipt("dlth")
    assert r["symbol"] == "DLTH"
    assert r["listed"] is True
    assert r["shortable"] is True
    assert r["easy_to_borrow"] is True
    assert r["source"] == "alpaca_asset"
    assert r["age_s"] is not None and r["age_s"] >= 0.0


def test_receipt_surfaces_not_shortable_as_false_not_unknown(monkeypatch):
    """32 sa 34 na pangalan ng populasyon ng [62] ay ganito — HINDI shortable, at ALAM natin."""
    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": False, "easy_to_borrow": False}), None),
    )
    r = aa.alpaca_borrow_receipt("TNON")
    assert r["shortable"] is False
    assert r["easy_to_borrow"] is False
    assert r["listed"] is True


def test_missing_flags_are_named_unknown_never_false(monkeypatch):
    """Ang None ng adapter ay "HINDI ALAM" — kailangang PANGALANAN, hindi ituring na False."""
    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": None, "easy_to_borrow": None}), None),
    )
    r = aa.alpaca_borrow_receipt("MOBX")
    assert r["shortable"] == "unknown"
    assert r["easy_to_borrow"] == "unknown"
    assert r["shortable"] is not False
    assert r["listed"] is True


def test_raw_without_borrow_keys_is_unknown(monkeypatch):
    _patch_adapter(monkeypatch, lambda s: (_prod(raw={}), None))
    r = aa.alpaca_borrow_receipt("SKYQ")
    assert r["shortable"] == "unknown"
    assert r["easy_to_borrow"] == "unknown"
    assert r["source"] == "alpaca_asset"


def test_non_bool_flag_is_unknown(monkeypatch):
    """Ang string na "true" mula sa isang bagong SDK ay HINDI ebidensya ng borrow."""
    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": "true", "easy_to_borrow": 1}), None),
    )
    r = aa.alpaca_borrow_receipt("WYHG")
    assert r["shortable"] == "unknown"
    assert r["easy_to_borrow"] == "unknown"


def test_probe_error_fails_closed_and_says_so(monkeypatch):
    def _boom(_s):
        raise RuntimeError("network")

    _patch_adapter(monkeypatch, _boom)
    r = aa.alpaca_borrow_receipt("AHMA")
    assert r["listed"] is False
    assert r["source"] == "probe_error"
    assert r["shortable"] == "unknown"
    assert r["easy_to_borrow"] == "unknown"


def test_missing_asset_is_named_asset_missing(monkeypatch):
    _patch_adapter(monkeypatch, lambda s: (None, None))
    r = aa.alpaca_borrow_receipt("NOSUCH")
    assert r["listed"] is False
    assert r["source"] == "asset_missing"
    assert r["shortable"] == "unknown"


def test_blank_symbol_is_a_receipt_not_a_crash():
    r = aa.alpaca_borrow_receipt("   ")
    assert r["listed"] is False
    assert r["source"] == "no_symbol"
    assert r["shortable"] == "unknown"


# ── walang epekto sa anumang desisyon ────────────────────────────────────────────────────


def test_borrow_flags_never_change_the_listing_verdict(monkeypatch):
    """Ang HINDI-shortable na pangalan ay naka-LIST pa rin — ang long twin ay hindi apektado."""
    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": False, "easy_to_borrow": False}), None),
    )
    assert aa._alpaca_lists_symbol("TNON") is True
    aa._ALPACA_LISTED_CACHE.clear()
    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": True, "easy_to_borrow": True}), None),
    )
    assert aa._alpaca_lists_symbol("TNON") is True


def test_short_execution_is_still_quarantined():
    """Ang resibo ay HINDI nagbubukas ng short lane — ang seam ay patay pa rin sa broker boundary."""
    from app.services.trading.momentum_neural.live_runner import (
        _alpaca_execution_quarantine_reason,
    )

    sess = SimpleNamespace(
        execution_family="alpaca_short",
        symbol="DLTH",
        risk_snapshot_json={},
    )
    assert _alpaca_execution_quarantine_reason(sess) == "alpaca_short_execution_not_certified"


# ── ang cache: TTL + hard cap ────────────────────────────────────────────────────────────


def test_probe_is_cached_within_ttl(monkeypatch):
    n = {"n": 0}
    _patch_adapter(monkeypatch, lambda s: (_prod(raw={"shortable": True}), None), n)
    assert aa._alpaca_lists_symbol("LIDR") is True
    assert aa._alpaca_lists_symbol("LIDR") is True
    assert aa.alpaca_borrow_receipt("LIDR")["shortable"] is True
    assert n["n"] == 1


def test_transient_error_no_longer_bars_the_name_forever(monkeypatch):
    """ANG AYOS: dati ay panghabambuhay ng proseso ang `listed=False` ng isang network blip."""
    state = {"fail": True}

    def _flaky(_s):
        if state["fail"]:
            raise RuntimeError("blip")
        return (_prod(raw={"shortable": False, "easy_to_borrow": False}), None)

    _patch_adapter(monkeypatch, _flaky)
    assert aa._alpaca_lists_symbol("BIAF") is False
    state["fail"] = False
    # Sa loob pa ng TTL: naka-cache pa rin ang pagkabigo (walang order-path churn).
    assert aa._alpaca_lists_symbol("BIAF") is False
    # Lampas sa TTL: muling sinisilip — at gumagaling.
    aa._ALPACA_LISTED_CACHE["BIAF"]["observed_at"] = datetime.now(timezone.utc) - timedelta(
        seconds=aa._ALPACA_ASSET_TTL_S + 1.0
    )
    assert aa._alpaca_lists_symbol("BIAF") is True
    assert aa.alpaca_borrow_receipt("BIAF")["source"] == "alpaca_asset"


def test_stale_borrow_flag_is_reprobed_after_ttl(monkeypatch):
    """Ang easy_to_borrow ay ARAW-ARAW na listahan; ang naka-freeze na kopya ay resibo ng kahapon."""
    state = {"etb": True}
    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": True, "easy_to_borrow": state["etb"]}), None),
    )
    assert aa.alpaca_borrow_receipt("DLTH")["easy_to_borrow"] is True
    state["etb"] = False
    assert aa.alpaca_borrow_receipt("DLTH")["easy_to_borrow"] is True  # cached
    aa._ALPACA_LISTED_CACHE["DLTH"]["observed_at"] = datetime.now(timezone.utc) - timedelta(
        seconds=aa._ALPACA_ASSET_TTL_S + 1.0
    )
    assert aa.alpaca_borrow_receipt("DLTH")["easy_to_borrow"] is False


def test_cache_is_bounded(monkeypatch):
    _patch_adapter(monkeypatch, lambda s: (_prod(raw={"shortable": False}), None))
    for i in range(aa._ALPACA_ASSET_CACHE_MAX * 2 + 5):
        aa._alpaca_lists_symbol(f"SYM{i}")
    assert len(aa._ALPACA_LISTED_CACHE) <= aa._ALPACA_ASSET_CACHE_MAX
    # Ang pinakahuling pangalan ay laging nandiyan (hindi natin siya pinapaalis).
    assert f"SYM{aa._ALPACA_ASSET_CACHE_MAX * 2 + 4}" in aa._ALPACA_LISTED_CACHE


def test_expired_entries_are_evicted_first(monkeypatch):
    _patch_adapter(monkeypatch, lambda s: (_prod(raw={"shortable": False}), None))
    for i in range(aa._ALPACA_ASSET_CACHE_MAX):
        aa._alpaca_lists_symbol(f"OLD{i}")
    old = datetime.now(timezone.utc) - timedelta(seconds=aa._ALPACA_ASSET_TTL_S + 5.0)
    for i in range(0, aa._ALPACA_ASSET_CACHE_MAX, 2):
        aa._ALPACA_LISTED_CACHE[f"OLD{i}"]["observed_at"] = old
    aa._alpaca_lists_symbol("FRESH")
    assert len(aa._ALPACA_LISTED_CACHE) <= aa._ALPACA_ASSET_CACHE_MAX
    assert "FRESH" in aa._ALPACA_LISTED_CACHE
    assert "OLD0" not in aa._ALPACA_LISTED_CACHE
    assert "OLD1" in aa._ALPACA_LISTED_CACHE


def test_receipt_payload_is_json_safe(monkeypatch):
    """Ang resibo ay isinusulat sa payload_json ng isang event — kailangang JSON-serializable."""
    import json

    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": True, "easy_to_borrow": None}), None),
    )
    r = aa.alpaca_borrow_receipt("DLTH")
    assert json.loads(json.dumps(r)) == r
