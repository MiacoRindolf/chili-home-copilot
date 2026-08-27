"""Ang basurang-lapad na direct quote ay pumasa sa execution_bbo_ok.

ANG BUG (nasukat 2026-08-27, BRNX). Sa 15:28:30Z ang tunay na tape ng BRNX ay
nagpi-print nang makitid sa 7.76-7.79 na may tunay na volume. Ang direct IEX na
quote na pinaniwalaan ni CHILI: ``bid 7.14 / ask 8.93`` = **2,227 bps** ang
lapad, 13.8s ang edad -- at pumasa ito sa ``execution_bbo_ok`` dahil EDAD LANG
ang tine-testing ng ``_final_entry_bbo``; ang ``spread_bps`` ay kinukwenta at
inilalagay sa snapshot pero hindi kailanman ginagamit sa gate. Ang basurang ask
ang nagpasya ng ``above_planned_limit`` na defer -- 14% sa itaas ng tunay na
market -- at hindi nag-entry si CHILI.

Parehong araw: XPON 3,344 bps, RDIB ~2,700 bps na basura, habang ang mga TUNAY
na quote ay 100-161 bps (DAIC 6.14/6.24, MSS 1.97/1.99). Ang 1,000 bps na
ceiling ay naghahati nang malinis na may malaking margin sa magkabilang panig.

ANG LUNAS: sa adapter mismo -- kapag lampas sa junk ceiling ang direct at
``allow_stand_in`` (ang entry seam), lumipat sa SIP -> IQFeed depth tiers, na
siyang tunay na larawan ng pinagsama-samang market. Kapag WALANG maibigay ang
mga tier, ibalik ang direct nang buo: byte-identical sa dati, hindi kailanman
mas mahigpit.

Runnable: pytest tests/test_execution_bbo_junk_spread_standin.py -v
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


class _Tick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask
        self.mid = (bid + ask) / 2.0


def _adapter(monkeypatch, *, direct, sip=None, depth=None):
    a = AlpacaSpotAdapter()
    monkeypatch.setattr(a, "_alpaca_latest_quote", lambda pid: (direct, object()))
    monkeypatch.setattr(
        a, "_execution_bbo_from_direct",
        lambda tick, meta, cap: ((tick, meta) if tick is not None else None))
    monkeypatch.setattr(
        a, "_massive_sip_execution_bbo", lambda pid, cap: sip)
    monkeypatch.setattr(
        a, "_iqfeed_depth_execution_bbo", lambda pid, cap: depth)
    return a


# ── Ang pangunahing kaso: ang eksaktong BRNX na numero ──────────────────────


def test_a_2227bps_direct_escalates_to_the_sip_standin(monkeypatch):
    """ANG KASO NG BRNX. bid 7.14 / ask 8.93 ay hindi dapat maging awtoridad ng
    submit kapag may SIP na tunay na larawan."""
    junk = _Tick(7.14, 8.93)          # 2,227 bps
    real = (_Tick(7.78, 7.80), object())
    a = _adapter(monkeypatch, direct=junk, sip=real)
    out = a.get_execution_bbo("BRNX", max_age_seconds=30.0, allow_stand_in=True)
    assert out is real, "dapat ang SIP stand-in ang awtoridad"


def test_the_depth_tier_is_the_second_fallback(monkeypatch):
    real = (_Tick(7.77, 7.79), object())
    a = _adapter(monkeypatch, direct=_Tick(7.14, 8.93), sip=None, depth=real)
    out = a.get_execution_bbo("BRNX", max_age_seconds=30.0, allow_stand_in=True)
    assert out is real


def test_no_standin_available_returns_the_junk_direct_UNCHANGED(monkeypatch):
    """⚠️ ANG DIREKSYON NG KALIGTASAN. Kapag walang maibigay ang bawat tier,
    ang gawi ay BYTE-IDENTICAL sa dati -- hindi kailanman mas mahigpit. Ang
    downstream na entry-quality gate pa rin ang huhusga."""
    junk = _Tick(7.14, 8.93)
    a = _adapter(monkeypatch, direct=junk, sip=None, depth=None)
    out = a.get_execution_bbo("BRNX", max_age_seconds=30.0, allow_stand_in=True)
    assert isinstance(out, tuple) and out[0] is junk


def test_a_real_tight_quote_is_untouched(monkeypatch):
    """Ang DAIC 6.14/6.24 (161 bps) ay direct pa rin — walang escalation."""
    tight = _Tick(6.14, 6.24)
    sip_should_not_be_used = (_Tick(6.15, 6.16), object())
    a = _adapter(monkeypatch, direct=tight, sip=sip_should_not_be_used)
    out = a.get_execution_bbo("DAIC", max_age_seconds=30.0, allow_stand_in=True)
    assert isinstance(out, tuple) and out[0] is tight


def test_without_allow_stand_in_the_junk_direct_is_returned(monkeypatch):
    """⚠️ Ang exit seam ay HINDI nag-o-opt in — ang isang cross-source stand-in
    ay sistematikong mapagbigay sa maling direksyon para sa exit. Ang junk
    check ay hindi dapat magbago ng anuman doon."""
    junk = _Tick(7.14, 8.93)
    a = _adapter(monkeypatch, direct=junk, sip=(_Tick(7.78, 7.80), object()))
    out = a.get_execution_bbo("BRNX", max_age_seconds=2.0, allow_stand_in=False)
    assert isinstance(out, tuple) and out[0] is junk


def test_zero_disables_the_check(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_alpaca_execution_bbo_junk_spread_bps", 0.0, raising=False)
    junk = _Tick(7.14, 8.93)
    a = _adapter(monkeypatch, direct=junk, sip=(_Tick(7.78, 7.80), object()))
    out = a.get_execution_bbo("BRNX", max_age_seconds=30.0, allow_stand_in=True)
    assert isinstance(out, tuple) and out[0] is junk


def test_the_measured_separation_is_recorded():
    """Ang ceiling ay hindi hula: basura 2,200-3,300 bps, tunay 100-161 bps."""
    fields = type(settings).model_fields
    name = "chili_alpaca_execution_bbo_junk_spread_bps"
    assert float(getattr(settings, name)) == 1000.0
    desc = str(fields[name].description or "")
    assert "BRNX" in desc and "2,227" in desc
