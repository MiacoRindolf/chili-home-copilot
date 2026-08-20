"""SIP-CLOCKED STAND-IN — ang dahilan kung bakit ZERO ang premarket orders (2026-08-20).

Ang `get_execution_bbo` ay dating tumatanggap LAMANG ng direktang Alpaca quote, sa
palagay na WALANG tape row na may quote-event clock. Luma na ang palagay na iyon.

Sinukat sa buhay na premarket kaninang umaga:
    SGLY tick=None  provider_time_utc=None      <- WALANG quote
    AAPL tick=None  provider_time_utc=None      <- WALANG quote (!)
    BTCT tick=None  provider_time_utc=2026-08-19 20:58:46Z   <- kahapong close
IEX-only ang entitlement, at hindi nagbubukas ang IEX bago mag-08:00 ET — kaya
bawat `live_entry_final_bbo` ay `no_provider_timestamp` at ZERO ang order sa araw
na ang mga setup ay PREMARKET.

Samantala ang Massive WS recorder ay nagtatatak ng SIP event clock:
    massive_ws_universe  100% may provider_event_at, ~0.26s lag, ~1.8s cadence
Ang mga row na ITO lang ang puwedeng tumayo bilang execution authority. Ang IQFeed
ay HINDI kailanman — wala talaga itong quote-event clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db as db_mod
from app.config import settings
from app.services.trading.venue.alpaca_spot import (
    AlpacaSpotAdapter,
    FreshnessMeta,
    NormalizedTicker,
)

_BASIS = "massive_sip_unix_ms"
_BRIDGE = "massive_ws_v2_sip_clock"


def _sip_row(**overrides):
    """Ang tunay na hugis ng row na kinuha ko sa live tape para sa SGLY."""
    now = datetime.now(timezone.utc)
    values = {
        "id": 114300823,
        "bid": 7.07,
        "ask": 7.09,
        "mid": 7.08,
        "spread_bps": 28.2485,
        "source": "massive_ws_universe",
        "provider_event_at": now - timedelta(milliseconds=360),
        "received_at": now - timedelta(milliseconds=100),
        "timestamp_basis": _BASIS,
        "bridge_version": _BRIDGE,
        "message_type": "Q",
    }
    values.update(overrides)
    return tuple(values[k] for k in (
        "id", "bid", "ask", "mid", "spread_bps", "source", "provider_event_at",
        "received_at", "timestamp_basis", "bridge_version", "message_type",
    ))


def _install_row(monkeypatch, row, captured=None):
    captured = captured if captured is not None else {}

    class _Result:
        def fetchone(self):
            return row

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, stmt, params):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return _Result()

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _Session())
    return captured


def _quote(monkeypatch, row, *, max_age=5.0):
    _install_row(monkeypatch, row)
    return AlpacaSpotAdapter()._massive_sip_quote("SGLY", max_age_seconds=max_age)


# ─────────────────── ang sandali na dating nawawala ───────────────────


def test_live_sip_row_can_stand_in(monkeypatch):
    captured = _install_row(monkeypatch, _sip_row())
    tick, meta = AlpacaSpotAdapter()._massive_sip_quote("SGLY", max_age_seconds=5.0)

    assert tick.product_id == "SGLY"
    assert (tick.bid, tick.ask) == (7.07, 7.09)
    assert tick.raw["feed"] == "massive_ws_universe"
    assert tick.raw["timestamp_basis"] == _BASIS
    assert tick.raw["tape_row_id"] == 114300823
    # ANG BUONG PUNTO: may tunay na quote-event clock ito.
    assert isinstance(meta.provider_time_utc, datetime)
    assert meta.provider_time_utc.tzinfo is not None


def test_query_is_fenced_to_sip_rows(monkeypatch):
    captured = _install_row(monkeypatch, _sip_row())
    AlpacaSpotAdapter()._massive_sip_quote("sgly", max_age_seconds=5.0)

    assert "provider_event_at IS NOT NULL" in captured["sql"]
    assert "received_at IS NOT NULL" in captured["sql"]
    assert "source LIKE" in captured["sql"]
    assert "ORDER BY observed_at DESC, id DESC" in captured["sql"]
    assert captured["params"]["s"] == "SGLY"
    assert captured["params"]["src"] == "massive_ws%"


# ─────────────────── ang IQFeed ay HINDI puwede ───────────────────


def test_iqfeed_row_can_never_stand_in(monkeypatch):
    """ANG MAHALAGA: kahit dumaan ito sa SQL, tinatanggihan ng basis check.
    Walang quote-event clock ang IQFeed frame."""
    assert _quote(monkeypatch, _sip_row(
        source="iqfeed_l1",
        timestamp_basis="iqfeed_q_receive_trade_reference_fenced",
    )) is None


def test_local_receive_only_basis_is_rejected(monkeypatch):
    """Ang recorder ay nagtatatak ng 'local_receive_only' kapag WALANG SIP field —
    kaya tunay na discriminator ang basis, hindi dekorasyon."""
    assert _quote(monkeypatch, _sip_row(timestamp_basis="local_receive_only")) is None


@pytest.mark.parametrize("bad", ["", "massive_ws_v1", "massive_ws_v2_sip_clock_x"])
def test_unpinned_bridge_build_is_rejected(monkeypatch, bad):
    assert _quote(monkeypatch, _sip_row(bridge_version=bad)) is None


def test_non_quote_message_is_rejected(monkeypatch):
    assert _quote(monkeypatch, _sip_row(message_type="T")) is None


def test_foreign_source_is_rejected(monkeypatch):
    assert _quote(monkeypatch, _sip_row(source="coinbase_ws")) is None
    assert _quote(monkeypatch, _sip_row(source="massive_snapshot")) is None


# ─────────────────── mga bitag ng orasan ───────────────────


def test_stale_row_beyond_ceiling_is_rejected(monkeypatch):
    now = datetime.now(timezone.utc)
    assert _quote(monkeypatch, _sip_row(
        provider_event_at=now - timedelta(seconds=30),
        received_at=now - timedelta(seconds=30),
    )) is None


def test_yesterdays_close_is_rejected(monkeypatch):
    """Ang eksaktong bitag ng BTCT: hindi-null pero 14 oras nang luma."""
    now = datetime.now(timezone.utc)
    assert _quote(monkeypatch, _sip_row(
        provider_event_at=now - timedelta(hours=14),
        received_at=now - timedelta(hours=14),
    )) is None


def test_replayed_row_is_rejected(monkeypatch):
    """Sariwa ang receive pero matagal nang nakaraan ang SIP event = replay."""
    now = datetime.now(timezone.utc)
    assert _quote(monkeypatch, _sip_row(
        provider_event_at=now - timedelta(seconds=45),
        received_at=now - timedelta(milliseconds=50),
    )) is None


def test_receive_before_event_beyond_tolerance_is_rejected(monkeypatch):
    """Imposible sa pisika: natanggap bago pa mangyari = sirang orasan."""
    now = datetime.now(timezone.utc)
    assert _quote(monkeypatch, _sip_row(
        provider_event_at=now + timedelta(seconds=5),
        received_at=now,
    )) is None


def test_naive_timestamps_are_rejected(monkeypatch):
    now = datetime.now(timezone.utc)
    assert _quote(monkeypatch, _sip_row(
        provider_event_at=now.replace(tzinfo=None)
    )) is None
    assert _quote(monkeypatch, _sip_row(
        received_at=now.replace(tzinfo=None)
    )) is None


def test_null_provider_clock_is_rejected(monkeypatch):
    assert _quote(monkeypatch, _sip_row(provider_event_at=None)) is None


# ─────────────────── mga sirang market ───────────────────


@pytest.mark.parametrize("bad", [
    {"bid": 0.0}, {"ask": 0.0}, {"mid": 0.0},
    {"bid": 7.20, "ask": 7.10},           # crossed
    {"bid": None}, {"ask": float("nan")},
])
def test_insane_market_is_rejected(monkeypatch, bad):
    assert _quote(monkeypatch, _sip_row(**bad)) is None


def test_missing_spread_is_recomputed(monkeypatch):
    tick, _ = _quote(monkeypatch, _sip_row(spread_bps=None))
    assert tick.spread_bps == pytest.approx((7.09 - 7.07) / 7.08 * 10_000.0)


# ─────────────────── ang ceiling ay bumabagsak lang ───────────────────


def test_ceiling_only_tightens(monkeypatch):
    """Ang hiniling na max_age ay hindi kailanman nakakaluwag lampas sa ceiling."""
    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_massive_sip_max_age_seconds", 5.0,
        raising=False,
    )
    now = datetime.now(timezone.utc)
    row = _sip_row(
        provider_event_at=now - timedelta(seconds=8),
        received_at=now - timedelta(seconds=8),
    )
    # Humihingi ng 30s, pero 5s ang ceiling -> tinatanggihan pa rin ang 8s na row.
    assert _quote(monkeypatch, row, max_age=30.0) is None
    # ...at ang mas MAKIPOT na hiling ang nananalo laban sa ceiling.
    tick, meta = _quote(monkeypatch, _sip_row(), max_age=1.0)
    assert meta.max_age_seconds == 1.0


def test_zero_ceiling_disables_the_stand_in(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_massive_sip_max_age_seconds", 0.0,
        raising=False,
    )
    assert _quote(monkeypatch, _sip_row()) is None


# ─────────────────── komposisyon sa get_execution_bbo ───────────────────


def _direct_ok():
    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now,
        provider_time_utc=now - timedelta(milliseconds=200),
        max_age_seconds=2.0,
    )
    return NormalizedTicker(
        product_id="SGLY", bid=7.00, ask=7.02, mid=7.01, spread_bps=28.5,
        bid_size=None, ask_size=None, freshness=meta, raw={"feed": "alpaca"},
    ), meta


def test_usable_direct_quote_still_wins(monkeypatch):
    """WALANG REGRESSION: kapag may tunay na Alpaca quote, ito ang ginagamit at
    HINDI man lang kinakausap ang tape."""
    adapter = AlpacaSpotAdapter()
    monkeypatch.setattr(adapter, "_alpaca_latest_quote", lambda pid: _direct_ok())

    def _boom(*_a, **_k):
        raise AssertionError("hindi dapat kinausap ang stand-in")

    monkeypatch.setattr(adapter, "_massive_sip_quote", _boom)
    tick, meta = adapter.get_execution_bbo("SGLY", max_age_seconds=2.0)
    assert tick.raw["feed"] == "alpaca"
    assert tick.raw["timestamp_basis"] == "provider_event_at"


def test_stand_in_fires_when_direct_has_no_clock(monkeypatch):
    """Ang eksaktong sitwasyon ngayong umaga: walang ibinabalik ang Alpaca."""
    adapter = AlpacaSpotAdapter()
    monkeypatch.setattr(
        adapter, "_alpaca_latest_quote",
        lambda pid: (None, FreshnessMeta(
            retrieved_at_utc=datetime.now(timezone.utc),
            provider_time_utc=None, max_age_seconds=2.0)),
    )
    _install_row(monkeypatch, _sip_row())
    tick, meta = adapter.get_execution_bbo(
        "SGLY", max_age_seconds=5.0, allow_stand_in=True
    )
    assert tick is not None
    assert tick.raw["timestamp_basis"] == _BASIS
    assert isinstance(meta.provider_time_utc, datetime)


def test_default_is_direct_only_so_exit_paths_are_untouched(monkeypatch):
    """ANG PINAKAMAHALAGANG GUARD: shared primitive ang get_execution_bbo — ginagamit
    din ng exit-marketability refresh at ng extended-hours orphan close. Ang NBBO bid
    ay laging >= bid ng iisang venue, kaya ang stand-in ay magpapasyang marketable (o
    magpepresyo) ng EXIT nang mas mataas kaysa kayang abutin ng venue: 'pasok tapos
    ipit'. Kaya ang default ay DIRECT-ONLY, at ang entry lang ang puwedeng mag-opt-in."""
    adapter = AlpacaSpotAdapter()
    direct_meta = FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc),
        provider_time_utc=None, max_age_seconds=2.0,
    )
    monkeypatch.setattr(adapter, "_alpaca_latest_quote", lambda pid: (None, direct_meta))

    def _boom(*_a, **_k):
        raise AssertionError("hindi dapat kinakausap ang stand-in kapag walang opt-in")

    monkeypatch.setattr(adapter, "_massive_sip_quote", _boom)
    # WALANG allow_stand_in -> dating ugali, byte-identical.
    tick, meta = adapter.get_execution_bbo("SGLY", max_age_seconds=5.0)
    assert tick is None
    assert meta is direct_meta
    # ...at kahit tahasang False.
    tick2, _ = adapter.get_execution_bbo(
        "SGLY", max_age_seconds=5.0, allow_stand_in=False
    )
    assert tick2 is None


def test_flag_off_restores_direct_only_and_keeps_attribution(monkeypatch):
    """PARITY: naka-OFF ang flag -> walang stand-in, at ang IBINABALIK na meta ay
    ang TUNAY na resulta ng adapter para manatiling totoo ang unavailable_kind."""
    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_massive_sip_fallback_enabled", False,
        raising=False,
    )
    adapter = AlpacaSpotAdapter()
    direct_meta = FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc),
        provider_time_utc=None, max_age_seconds=2.0,
    )
    monkeypatch.setattr(adapter, "_alpaca_latest_quote", lambda pid: (None, direct_meta))
    _install_row(monkeypatch, _sip_row())
    tick, meta = adapter.get_execution_bbo(
        "SGLY", max_age_seconds=5.0, allow_stand_in=True
    )
    assert tick is None
    assert meta is direct_meta          # -> unavailable_kind='no_provider_timestamp'


def test_crypto_never_uses_the_stand_in(monkeypatch):
    adapter = AlpacaSpotAdapter()
    monkeypatch.setattr(
        adapter, "_alpaca_latest_quote",
        lambda pid: (None, FreshnessMeta(
            retrieved_at_utc=datetime.now(timezone.utc),
            provider_time_utc=None, max_age_seconds=2.0)),
    )

    def _boom(*_a, **_k):
        raise AssertionError("equities lang ang tape na ito")

    monkeypatch.setattr(adapter, "_massive_sip_quote", _boom)
    tick, _ = adapter.get_execution_bbo(
        "BTC-USD", max_age_seconds=5.0, allow_stand_in=True
    )
    assert tick is None


def test_no_row_at_all_returns_direct_meta(monkeypatch):
    adapter = AlpacaSpotAdapter()
    direct_meta = FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc),
        provider_time_utc=None, max_age_seconds=2.0,
    )
    monkeypatch.setattr(adapter, "_alpaca_latest_quote", lambda pid: (None, direct_meta))
    _install_row(monkeypatch, None)
    tick, meta = adapter.get_execution_bbo(
        "SGLY", max_age_seconds=5.0, allow_stand_in=True
    )
    assert tick is None
    assert meta is direct_meta


def test_settings_are_wired_and_bounded():
    assert getattr(
        settings, "chili_alpaca_execution_bbo_massive_sip_fallback_enabled", None
    ) is True
    v = float(getattr(
        settings, "chili_alpaca_execution_bbo_massive_sip_max_age_seconds", -1
    ))
    # Dapat saklawin ang tunay na ~1.8s cadence ng leader PLUS ang ~6s na
    # DB-visibility lag ng recorder (5s flush + 1s spacing) — sinukat live 08-20:
    # SNSC na-block sa 4.9s na row sa ilalim ng 5.0 ceiling.
    assert v >= 8.0
    # ...pero hindi kasing-luwag na tumanggap ng quote mula sa ibang market regime.
    assert v <= 15.0


# ─────────────── ang entry-only wiring sa live_runner ───────────────


class _StubAdapter:
    """Itinatala kung ANO ang ipinasa — dito nabubuhay o namamatay ang guard."""

    def __init__(self, tick=None, meta=None):
        self.calls = []
        self._tick = tick
        self._meta = meta

    def get_execution_bbo(self, product_id, **kwargs):
        self.calls.append(kwargs)
        return self._tick, self._meta


def _stand_in_tick(basis=_BASIS, feed="massive_ws_universe"):
    now = datetime.now(timezone.utc)
    meta = FreshnessMeta(
        retrieved_at_utc=now,
        provider_time_utc=now - timedelta(milliseconds=300),
        max_age_seconds=5.0,
    )
    return NormalizedTicker(
        product_id="SGLY", bid=7.07, ask=7.09, mid=7.08, spread_bps=28.2,
        bid_size=None, ask_size=None, freshness=meta,
        raw={"feed": feed, "timestamp_basis": basis,
             "provider_event_at_utc": meta.provider_time_utc.isoformat()},
    ), meta


def test_exit_callers_do_not_opt_in():
    """ANG BUONG PUNTO NG GUARD: ang default ay hindi humihingi ng stand-in, kaya
    ang exit-marketability at ang orphan close ay nananatiling direct-Alpaca."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo
    tick, meta = _stand_in_tick()
    ad = _StubAdapter(tick, meta)
    _final_entry_bbo(ad, "SGLY", max_age_seconds=2.0)
    assert ad.calls == [{"max_age_seconds": 2.0}]
    assert "allow_stand_in" not in ad.calls[0]


def test_entry_caller_opts_in():
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo
    tick, meta = _stand_in_tick()
    ad = _StubAdapter(tick, meta)
    _final_entry_bbo(ad, "SGLY", max_age_seconds=5.0, allow_stand_in=True)
    assert ad.calls[0]["allow_stand_in"] is True


def test_quote_authority_is_tagged_honestly():
    """Hindi dapat mabasa ang stand-in bilang Alpaca quote sa downstream."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo
    tick, meta = _stand_in_tick()
    _, snap = _final_entry_bbo(
        _StubAdapter(tick, meta), "SGLY", max_age_seconds=5.0, allow_stand_in=True
    )
    assert snap["quote_authority"] == "stand_in_massive_sip"
    assert snap["timestamp_basis"] == _BASIS

    direct_tick, direct_meta = _stand_in_tick(basis="provider_event_at", feed="alpaca")
    _, snap2 = _final_entry_bbo(
        _StubAdapter(direct_tick, direct_meta), "SGLY", max_age_seconds=5.0
    )
    assert snap2["quote_authority"] == "alpaca_direct"


def test_adapter_without_the_parameter_still_works():
    """Ang adapter na mas luma kaysa sa parameter ay hindi dapat sumabog kapag
    hindi humihingi ng opt-in ang caller."""
    from app.services.trading.momentum_neural.live_runner import _final_entry_bbo

    class _Legacy:
        def get_execution_bbo(self, product_id, *, max_age_seconds):
            return _stand_in_tick(basis="provider_event_at", feed="alpaca")

    tick, snap = _final_entry_bbo(_Legacy(), "SGLY", max_age_seconds=5.0)
    assert snap["quote_authority"] == "alpaca_direct"
