"""[63] BORROW RECEIPT — ang broker ang nagsasabi kung shortable ba ang pangalan.

Ang ``alpaca_spot.get_product().raw`` ay naglalabas na ng ``shortable`` at ``easy_to_borrow``
simula pa noong isinulat ang komentong "so the short-entry gate can fail-closed on a
not-shortable / hard-to-borrow name" — pero WALANG bumabasa nito. Ang resibong ito ang unang
mambabasa, at INIUULAT LAMANG ito: walang desisyon, walang order kwarg, at naka-quarantine pa
rin ang buong short execution.

Sinusukat din dito ang AYOS sa cache ng asset probe (ISANG kopya na ngayon, sa venue module,
ginagamit ng ROUTING path at ng ARM path nang sabay):
  * TTL (ang ``easy_to_borrow`` ay ARAW-ARAW na listahan ng broker),
  * ang error ay HINDI nag-o-overwrite ng sagot na sinagot na ng broker,
  * hard cap + lock (dalawang thread ang pumapasok sa landas na ito),
  * at ang paghahati ng "SUMAGOT ang broker: walang ganitong asset" (404 => ``asset_missing``)
    sa "HINDI TAYO NAKATANONG" (network/auth/5xx => ``probe_error``), na dati ay iisa lamang
    dahil ang ``get_product`` ay lumululon ng LAHAT ng exception.
"""
from __future__ import annotations

import json
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.auto_arm as aa
from app.services.trading.venue import alpaca_spot as ap


class _Prod(SimpleNamespace):
    pass


def _prod(*, disabled=False, raw=None):
    return _Prod(trading_disabled=disabled, raw=raw)


class _Boom(Exception):
    """SDK-shaped exception na may HTTP status (o wala)."""

    def __init__(self, status=None, msg="boom"):
        super().__init__(msg)
        self.status_code = status


@pytest.fixture(autouse=True)
def _clean_cache():
    ap._LISTED_CACHE.clear()
    ap._ASSET_PROBE_INFLIGHT.clear()
    yield
    ap._LISTED_CACHE.clear()
    ap._ASSET_PROBE_INFLIGHT.clear()


def _patch_adapter(monkeypatch, fn, counter=None):
    """Palitan ang adapter ng peke na sumasagot sa BAGONG seam (``get_product_probe``).

    ``fn(sym)`` ay nagbabalik ng ``(prod, err)`` — ang ``err`` ay None kapag SUMAGOT ang
    broker (kahit ``prod`` ay None: iyon ang 404), at isang string kapag hindi tayo
    nakatanong.
    """

    class _FakeAdapter:
        broker_environment = "paper"

        def get_product_probe(self, sym):
            if counter is not None:
                counter["n"] += 1
            prod, err = fn(sym)
            return prod, None, err

        def get_product(self, sym):
            prod, _meta, _err = self.get_product_probe(sym)
            return prod, None

    monkeypatch.setattr(ap, "AlpacaSpotAdapter", _FakeAdapter)


def _age_out(sym, extra=1.0):
    ap._LISTED_CACHE[sym]["observed_at"] = ap._now() - timedelta(
        seconds=ap._ALPACA_ASSET_TTL_S + extra
    )


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
    assert r["stale"] is False


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
    _patch_adapter(monkeypatch, lambda s: (None, "ConnectionError:no_http_status"))
    r = aa.alpaca_borrow_receipt("AHMA")
    assert r["listed"] is False
    assert r["source"] == "probe_error"
    assert r["shortable"] == "unknown"
    assert r["easy_to_borrow"] == "unknown"
    assert r["last_error"] == "ConnectionError:no_http_status"


def test_missing_asset_is_named_asset_missing(monkeypatch):
    _patch_adapter(monkeypatch, lambda s: (None, None))
    r = aa.alpaca_borrow_receipt("NOSUCH")
    assert r["listed"] is False
    assert r["source"] == "asset_missing"
    assert r["shortable"] == "unknown"
    assert r["last_error"] is None


def test_blank_symbol_is_a_receipt_not_a_crash():
    r = aa.alpaca_borrow_receipt("   ")
    assert r["listed"] is False
    assert r["source"] == "no_symbol"
    assert r["shortable"] == "unknown"


def test_receipt_names_the_broker_generation_that_answered(monkeypatch):
    """Ang shortable/easy_to_borrow ay ACCOUNT-SCOPED — magkaiba ang paper at live.

    Ang isang hilerang naitala sa ilalim ng paper at binasa pagkatapos ng paglipat ng
    posture ay dapat MAPAGKAKILALA; kaya ang resibo ay nagpapangalan ng broker
    environment at ng account scope/identity ng arm na nagdala nito.
    """
    _patch_adapter(monkeypatch, lambda s: (_prod(raw={"shortable": True}), None))
    r = aa.alpaca_borrow_receipt(
        "DLTH", account_scope="alpaca:paper", account_identity="paper-account"
    )
    import hashlib

    assert r["broker_environment"] == "paper"
    assert r["account_scope"] == "alpaca:paper"
    # sha256 lamang, hindi ang hubad na UUID (review ng [63]) — kapareho ng loss_guard_policy.
    assert r["account_identity_sha256"] == hashlib.sha256(b"paper-account").hexdigest()


# ── walang epekto sa anumang desisyon ────────────────────────────────────────────────────


def test_borrow_flags_never_change_the_listing_verdict(monkeypatch):
    """Ang HINDI-shortable na pangalan ay naka-LIST pa rin — ang long arm ay hindi apektado."""
    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": False, "easy_to_borrow": False}), None),
    )
    assert aa._alpaca_lists_symbol("TNON") is True
    ap._LISTED_CACHE.clear()
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


# ── ANG ADAPTER: 404 ay SAGOT, ang iba ay HINDI TAYO NAKATANONG ──────────────────────────
#
# Ito ang depektong hinuli ng review: ang `get_product` ay nagbabalot ng BUONG katawan nito
# sa try/except at nagbabalik ng (None, meta) sa KAHIT ANONG exception, kaya ang isang 500
# ng Alpaca ay lumalabas sa resibo bilang `asset_missing` — isang PAG-AANGKIN na sinagot ng
# broker na wala siyang ganitong asset. Ang `probe_error` ay patay na code sa produksyon.


class _FakeClient:
    def __init__(self, fn):
        self._fn = fn

    def get_asset(self, sym):
        return self._fn(sym)


def _adapter_with(monkeypatch, fn):
    ad = ap.AlpacaSpotAdapter()
    monkeypatch.setattr(ad, "_account_client", lambda: _FakeClient(fn))
    return ad


def test_adapter_probe_names_a_transport_failure(monkeypatch):
    def _boom(_s):
        raise _Boom(status=None, msg="read timed out")

    ad = _adapter_with(monkeypatch, _boom)
    prod, _meta, err = ad.get_product_probe("TNON")
    assert prod is None
    assert err is not None and "no_http_status" in err


def test_adapter_probe_names_a_broker_5xx(monkeypatch):
    def _boom(_s):
        raise _Boom(status=500)

    ad = _adapter_with(monkeypatch, _boom)
    prod, _meta, err = ad.get_product_probe("TNON")
    assert prod is None
    assert err is not None and err.endswith(":500")


def test_adapter_probe_treats_404_as_the_broker_answering(monkeypatch):
    def _boom(_s):
        raise _Boom(status=404)

    ad = _adapter_with(monkeypatch, _boom)
    prod, _meta, err = ad.get_product_probe("NOSUCH")
    assert prod is None
    assert err is None  # SUMAGOT ang broker: wala siyang ganitong asset.


def test_adapter_get_product_keeps_its_legacy_two_tuple(monkeypatch):
    def _boom(_s):
        raise _Boom(status=500)

    ad = _adapter_with(monkeypatch, _boom)
    out = ad.get_product("TNON")
    assert isinstance(out, tuple) and len(out) == 2
    assert out[0] is None


def test_broker_500_is_probe_error_not_asset_missing(monkeypatch):
    """ANG AYOS: dati, ang bawat 500/timeout ay nag-uulat ng `asset_missing`."""

    def _boom(_s):
        raise _Boom(status=503)

    class _FakeAdapter:
        broker_environment = "paper"

        def get_product_probe(self, sym):
            return _adapter_with(monkeypatch, _boom).get_product_probe(sym)

    monkeypatch.setattr(ap, "AlpacaSpotAdapter", _FakeAdapter)
    r = aa.alpaca_borrow_receipt("TNON")
    assert r["source"] == "probe_error"
    assert r["source"] != "asset_missing"
    assert r["listed"] is False


# ── ang cache: TTL + hard cap + huling-alam ──────────────────────────────────────────────


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
            return (None, "ConnectionError:no_http_status")
        return (_prod(raw={"shortable": False, "easy_to_borrow": False}), None)

    _patch_adapter(monkeypatch, _flaky)
    assert aa._alpaca_lists_symbol("BIAF") is False
    state["fail"] = False
    # Sa loob pa ng TTL: naka-cache pa rin ang pagkabigo (walang order-path churn).
    assert aa._alpaca_lists_symbol("BIAF") is False
    # Lampas sa TTL: muling sinisilip — at gumagaling.
    _age_out("BIAF")
    ap._LISTED_CACHE["BIAF"]["last_error_at"] = None
    assert aa._alpaca_lists_symbol("BIAF") is True
    assert aa.alpaca_borrow_receipt("BIAF")["source"] == "alpaca_asset"


def test_error_never_overwrites_an_answer_the_broker_already_gave(monkeypatch):
    """ANG AYOS (review): dati, ang isang blip sa 11:01 ay nagde-demote ng PATUNAY NA listing
    at nagbabawal sa pangalan sa buong susunod na oras."""
    state = {"fail": False}

    def _flaky(_s):
        if state["fail"]:
            return (None, "ReadTimeout:no_http_status")
        return (_prod(raw={"shortable": True, "easy_to_borrow": True}), None)

    _patch_adapter(monkeypatch, _flaky)
    assert aa._alpaca_lists_symbol("DLTH") is True
    state["fail"] = True
    _age_out("DLTH")  # oras na para mag-refresh...
    r = aa.alpaca_borrow_receipt("DLTH")
    # ...pero bumigo ang probe: NANANATILI ang sagot ng broker, at pinangalanan ang pagkabigo.
    assert r["listed"] is True
    assert r["shortable"] is True
    assert r["source"] == "alpaca_asset"
    assert r["stale"] is True
    assert r["last_error"] == "ReadTimeout:no_http_status"
    assert aa._alpaca_lists_symbol("DLTH") is True


def test_failed_probe_backs_off_for_one_request_deadline(monkeypatch):
    """Hindi tayo nagtatanong kada pass sa isang patay na network: ang backoff ay ang
    SARILING bounded HTTP deadline ng adapter (``chili_alpaca_http_timeout_seconds``)."""
    n = {"n": 0}

    def _always_fail(_s):
        return (None, "ConnectionError:no_http_status")

    _patch_adapter(monkeypatch, _always_fail, n)
    aa._alpaca_lists_symbol("AHMA")
    assert n["n"] == 1
    aa._alpaca_lists_symbol("AHMA")
    assert n["n"] == 1  # nasa loob pa ng backoff
    ap._LISTED_CACHE["AHMA"]["last_error_at"] = ap._now() - timedelta(
        seconds=ap._asset_error_backoff_s() + 1.0
    )
    _age_out("AHMA")
    aa._alpaca_lists_symbol("AHMA")
    assert n["n"] == 2


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
    _age_out("DLTH")
    assert aa.alpaca_borrow_receipt("DLTH")["easy_to_borrow"] is False


def test_cache_is_bounded(monkeypatch):
    _patch_adapter(monkeypatch, lambda s: (_prod(raw={"shortable": False}), None))
    for i in range(ap._ALPACA_ASSET_CACHE_MAX * 2 + 5):
        aa._alpaca_lists_symbol(f"SYM{i}")
    assert len(ap._LISTED_CACHE) <= ap._ALPACA_ASSET_CACHE_MAX
    # Ang pinakahuling pangalan ay laging nandiyan (hindi natin siya pinapaalis).
    assert f"SYM{ap._ALPACA_ASSET_CACHE_MAX * 2 + 4}" in ap._LISTED_CACHE


def test_expired_entries_are_evicted_first(monkeypatch):
    _patch_adapter(monkeypatch, lambda s: (_prod(raw={"shortable": False}), None))
    for i in range(ap._ALPACA_ASSET_CACHE_MAX):
        aa._alpaca_lists_symbol(f"OLD{i}")
    old = ap._now() - timedelta(seconds=ap._ALPACA_ASSET_TTL_S + 5.0)
    for i in range(0, ap._ALPACA_ASSET_CACHE_MAX, 2):
        ap._LISTED_CACHE[f"OLD{i}"]["observed_at"] = old
    aa._alpaca_lists_symbol("FRESH")
    assert len(ap._LISTED_CACHE) <= ap._ALPACA_ASSET_CACHE_MAX
    assert "FRESH" in ap._LISTED_CACHE
    assert "OLD0" not in ap._LISTED_CACHE
    assert "OLD1" in ap._LISTED_CACHE


def test_concurrent_probes_do_not_raise_dict_changed_size(monkeypatch):
    """Ang landas na ito ay pinapasok ng DALAWANG thread (scheduler job + ignition bridge).

    Ang eviction ay UMIIKOT sa cache; kung walang lock, ang isang insert ng kabilang thread
    sa gitna ng comprehension ay `RuntimeError: dictionary changed size during iteration` —
    na nilululon ng arm-path na `except` at TAHIMIK na lumalaktaw ng buong pass.
    """
    _patch_adapter(monkeypatch, lambda s: (_prod(raw={"shortable": False}), None))
    for i in range(ap._ALPACA_ASSET_CACHE_MAX):
        aa._alpaca_lists_symbol(f"SEED{i}")
    errors: list[BaseException] = []
    start = threading.Event()

    def _worker(base):
        start.wait()
        try:
            for i in range(120):
                aa._alpaca_lists_symbol(f"{base}{i}")
        except BaseException as exc:  # noqa: BLE001 — ito mismo ang sinusukat
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(b,)) for b in ("A", "B", "C", "D")]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(ap._LISTED_CACHE) <= ap._ALPACA_ASSET_CACHE_MAX


def test_routing_path_shares_the_same_fixed_cache(monkeypatch):
    """Ang kopya sa ROUTING path (`execution_family_registry` -> `alpaca_lists_symbol`) ay
    dating BYTE-FOR-BYTE na kapareho ng depekto: walang TTL, walang cap, at ang error ay
    HABAMBUHAY na nagruruta ng isang crypto major palayo sa Alpaca. Isang implementasyon
    na lamang ngayon."""
    state = {"fail": False}

    def _flaky(_s):
        if state["fail"]:
            return (None, "ConnectionError:no_http_status")
        return (_prod(raw={}), None)

    _patch_adapter(monkeypatch, _flaky)
    assert ap.alpaca_lists_symbol("BTC-USD") is True
    state["fail"] = True
    _age_out("BTC-USD")
    assert ap.alpaca_lists_symbol("BTC-USD") is True  # hindi na-demote ng blip
    assert ap._LISTED_CACHE["BTC-USD"]["last_error"] == "ConnectionError:no_http_status"


def test_reset_clients_for_tests_clears_the_asset_cache(monkeypatch):
    _patch_adapter(monkeypatch, lambda s: (_prod(raw={}), None))
    aa._alpaca_lists_symbol("DLTH")
    assert ap._LISTED_CACHE
    ap.reset_clients_for_tests()
    assert not ap._LISTED_CACHE


def test_receipt_payload_is_json_safe(monkeypatch):
    """Ang resibo ay isinusulat sa payload_json ng isang event — kailangang JSON-serializable."""
    _patch_adapter(
        monkeypatch,
        lambda s: (_prod(raw={"shortable": True, "easy_to_borrow": None}), None),
    )
    r = aa.alpaca_borrow_receipt("DLTH", account_scope="alpaca:paper")
    assert json.loads(json.dumps(r)) == r


# ── REVIEW NG [63] ───────────────────────────────────────────────────────────────────────


def test_first_ever_probe_error_expires_after_one_deadline_not_the_whole_ttl(monkeypatch):
    """ANG AYOS (review): dati ay UNA ang sangay ng TTL, at ang record ng error ay may
    ``observed_at`` na NGAYON — kaya ang isang 200 ms na blip sa UNANG probe ng isang
    bagong pangalan ay nagpi-pin ng ``listed=False`` nang BUONG ORAS (ang buong day-trade
    window ng pangalang iyon), kahit ang backoff ay 10 s lang. Ang freshness ng isang
    "hindi tayo nakatanong" ay ang BACKOFF, hindi ang TTL ng isang tunay na sagot."""
    state = {"fail": True}
    n = {"n": 0}

    def _flaky(_s):
        if state["fail"]:
            return (None, "ConnectionError:no_http_status")
        return (_prod(raw={"shortable": True, "easy_to_borrow": True}), None)

    _patch_adapter(monkeypatch, _flaky, n)

    assert aa._alpaca_lists_symbol("AHMA") is False
    assert n["n"] == 1
    assert aa._alpaca_lists_symbol("AHMA") is False  # backoff: hindi tayo nagtatanong ulit
    assert n["n"] == 1

    # ISANG request deadline lang ang lumipas — ang observed_at ay SARIWA PA sa loob ng TTL.
    rec = ap._LISTED_CACHE["AHMA"]
    assert (ap._now() - rec["observed_at"]).total_seconds() < ap._ALPACA_ASSET_TTL_S
    rec["last_error_at"] = ap._now() - timedelta(
        seconds=ap._asset_error_backoff_s() + 1.0
    )
    rec["observed_at"] = rec["last_error_at"]

    state["fail"] = False
    assert aa._alpaca_lists_symbol("AHMA") is True
    assert n["n"] == 2


def test_bounded_record_never_holds_the_arm_past_one_request_deadline(monkeypatch):
    """Ang probe ng arm path ay nakaupo SA LOOB ng bukas na transaksyon ng arm at sa loob
    ng ignition->arm lock. Ang alpaca-py ay nag-uulit ng HTTP 429, kaya ang isang
    rate-limited na probe ay kayang humawak ng admission nang sampu-sampung segundo.
    Ang resibo ay INIUULAT LAMANG — hindi ito puwedeng magkahalaga ng admission latency."""
    started = threading.Event()
    release = threading.Event()

    def _slow(_s):
        started.set()
        release.wait(5.0)
        return (_prod(raw={"shortable": True, "easy_to_borrow": True}), None)

    _patch_adapter(monkeypatch, _slow)
    t0 = ap._now()
    rec = ap.alpaca_asset_record_bounded("SLOW", deadline_s=0.1)
    elapsed = (ap._now() - t0).total_seconds()

    assert started.is_set()
    assert elapsed < 2.0, elapsed
    assert rec["source"] == "probe_deadline"
    assert rec["listed"] is False          # fail-CLOSED habang hindi pa sumasagot
    assert "SLOW" not in ap._LISTED_CACHE  # walang pekeng sagot na naiiwan sa cache
    release.set()


def test_bounded_record_serves_the_background_answer_on_the_next_pass(monkeypatch):
    """Ang probe ay nagpapatuloy pagkatapos ng deadline: ang SUSUNOD na pass ay may sagot."""
    release = threading.Event()

    def _slow(_s):
        release.wait(5.0)
        return (_prod(raw={"shortable": True, "easy_to_borrow": True}), None)

    _patch_adapter(monkeypatch, _slow)
    assert ap.alpaca_asset_record_bounded("SLOW", deadline_s=0.1)["source"] == (
        "probe_deadline"
    )
    release.set()
    for _ in range(100):
        if "SLOW" in ap._LISTED_CACHE:
            break
        threading.Event().wait(0.05)
    rec = ap.alpaca_asset_record_bounded("SLOW", deadline_s=0.1)
    assert rec["source"] == "alpaca_asset"
    assert rec["listed"] is True
    assert rec["shortable"] is True


def test_bounded_record_starts_one_probe_for_many_waiters(monkeypatch):
    """Isang in-flight probe kada simbolo — hindi nagsasalansan ang thread kada pass."""
    n = {"n": 0}
    release = threading.Event()

    def _slow(_s):
        release.wait(5.0)
        return (_prod(raw={"shortable": False}), None)

    _patch_adapter(monkeypatch, _slow, n)
    for _ in range(4):
        assert ap.alpaca_asset_record_bounded("SLOW", deadline_s=0.05)["source"] == (
            "probe_deadline"
        )
    release.set()
    for _ in range(100):
        if "SLOW" in ap._LISTED_CACHE:
            break
        threading.Event().wait(0.05)
    assert n["n"] == 1


def test_bounded_record_is_the_seam_the_arm_path_uses(monkeypatch):
    """Ang arm path ay dapat DUMAAN sa bounded na seam, hindi sa humaharang na tawag."""
    seen = {}

    def _fake_bounded(sym, *, deadline_s=None):
        seen["sym"] = sym
        return {
            "listed": True, "shortable": True, "easy_to_borrow": True,
            "source": "alpaca_asset", "observed_at": ap._now(), "last_error": None,
        }

    monkeypatch.setattr(ap, "alpaca_asset_record_bounded", _fake_bounded)
    monkeypatch.setattr(
        ap, "alpaca_asset_record", lambda *_a, **_k: pytest.fail("blocking call on arm path")
    )
    assert aa.alpaca_borrow_receipt("TNON")["shortable"] is True
    assert seen["sym"] == "TNON"


def test_receipt_hashes_the_account_identity_and_never_carries_it_bare(monkeypatch):
    """Ang resibo ay pumapasok sa pass summary at ang summary ay ini-log NANG BUO kada arm
    (trading_scheduler). Ang sha256 ang nagpapangalan sa broker generation, hindi ang UUID."""
    import hashlib

    _patch_adapter(monkeypatch, lambda s: (_prod(raw={"shortable": True}), None))
    r = aa.alpaca_borrow_receipt(
        "LIDR", account_scope="alpaca:paper", account_identity="acct-uuid-1234"
    )
    assert "account_identity" not in r
    assert r["account_identity_sha256"] == hashlib.sha256(b"acct-uuid-1234").hexdigest()
    assert r["account_scope"] == "alpaca:paper"
    assert "acct-uuid-1234" not in json.dumps(r, default=str)
