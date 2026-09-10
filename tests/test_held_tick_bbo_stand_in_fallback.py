"""Ang HELD-tick BBO ay dumadaan sa HELD selector — IQFeed L1 muna, strict IEX
pangalawa, WALANG stand-in ladder ([48] build B, 2026-09-10).

ANG KASAYSAYAN NG FILE NA ITO. 2026-08-25 (BDRX, session 15344): walang premarket
book ang Alpaca, kaya 1,625 sunod-sunod na tick ang naharang at bulag ang lane sa
sariling posisyon (nanatiling 1.51 ang HWM habang 1.71 ang bid). Ang lunas noon ay
ang stand-in ladder ng `get_execution_bbo` sa ilalim ng literal na 15.0 s -- at ang
SIP-clocked massive_ws tier ang UNANG tinatanong doon.

2026-09-10 (PCLA 21592): ang bailout bid 8.96 ay galing sa massive_ws row na 6.58 s
na luma habang ang fenced IQFeed L1 row (8.88/9.00) ay 1.06 s lang ang edad. Ang
desisyon ay nagbasa ng lumang libro. Kaya ngayon: `_live_tick_bbo` HELD ->
`select_held_bbo` (L1 sa sariling event clock laban sa mga HINANGONG hangganan,
tapos strict IEX, tapos -- LAMANG kapag hindi makakaputok ang broker deadman, i.e.
labas ng regular session -- ang SIP-clocked floor sa ilalim ng SARILING kontrata).
Kapag lahat ay tumanggi ay `tick=None`; sa RTH ang nakapahingang broker deadman
ang sahig -- hindi isang lumang hilera. Ang BDRX na kaso ay sagot ng L1 tier (may
premarket na L1 ang IQFeed).

Ang pre-entry at non-Alpaca na mga landas ay HINDI binago (hiwalay na PR).

Runnable: pytest tests/test_held_tick_bbo_stand_in_fallback.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.held_bbo import HeldBboBounds, HeldBboDecision


class _Tick:
    def __init__(self, bid, freshness="fresh"):
        self.bid = bid
        self.freshness = freshness


_BOUNDS = HeldBboBounds(
    l1_fresh_bound_s=4.917, l1_gap_ceiling_s=18.832, heartbeat_bound_s=4.917,
    sip_disagree_band_bps=66.22, delay_stamp_threshold_s=2.0,
    derivation={"l1_fresh_bound_s": {"value": 4.917, "source": "test"}},
)


@pytest.fixture
def selector(monkeypatch):
    """Kunin ang bawat tawag sa select_held_bbo kasama ang kwargs nito."""
    seen = []
    feb = []

    def _fake(adapter, product_id, **kw):
        seen.append((product_id, kw))
        return _fake.result

    _fake.result = None
    monkeypatch.setattr(LR, "select_held_bbo", _fake)
    monkeypatch.setattr(LR, "current_bounds", lambda **_k: _BOUNDS)
    monkeypatch.setattr(LR, "_final_entry_bbo", lambda *a, **k: feb.append(k) or (None, {}))
    return seen, feb, _fake


def test_held_tick_asks_the_selector_once_with_no_stand_in(selector):
    """⚠️ ANG PANGUNAHING BANTAY. Ang HELD tick ay tumatawag ng selector nang ISANG
    beses, hindi ng `_final_entry_bbo` (walang allow_stand_in kahit saan), at
    ibinabalik ang tick/snapshot/envelope nito nang buo."""
    seen, feb, fake = selector
    fake.result = HeldBboDecision(
        tick=_Tick(8.88),
        snapshot={"ok": True, "reason": "execution_bbo_ok", "bid": 8.88},
        envelope={"bbo_source": "iqfeed_l1", "bbo_age_s": 1.06, "bbo_fallback_engaged": False},
        counts_toward_halt=False,
    )
    tick, freshness, snap = LR._live_tick_bbo(
        object(), "PCLA", execution_family="alpaca_spot", state="live_entered")
    assert tick is not None and tick.bid == 8.88 and freshness == "fresh"
    assert len(seen) == 1 and seen[0][0] == "PCLA"
    assert seen[0][1]["bounds"] is _BOUNDS
    assert isinstance(seen[0][1]["now"], datetime) and seen[0][1]["now"].tzinfo is timezone.utc
    assert "tiers" not in seen[0][1], "default tiers: L1, IEX, tapos ang floor kapag inert ang deadman"
    # review fix: ang gate ng floor (makakaputok ba ang deadman?) ay ipinapasa kasama
    # ang ebidensya nito -- ang selector ang nagpapasya kung tatanungin ang floor.
    assert isinstance(seen[0][1]["resting_floor_live"], bool)
    assert seen[0][1]["floor_gate"]["source"] == "_deadman_protection_is_live"
    assert seen[0][1]["floor_gate"]["resting_floor_live"] is seen[0][1]["resting_floor_live"]
    assert feb == [], "hindi dapat tawagin ang _final_entry_bbo mula sa HELD branch"
    assert snap["bbo_source"] == "iqfeed_l1" and snap["bbo_age_s"] == 1.06
    assert snap["bbo_fallback_engaged"] is False
    assert snap["counts_toward_halt"] is False
    assert snap["ok"] is True and snap["bid"] == 8.88


@pytest.mark.parametrize("state", ["live_entered", "live_scaling_out", "live_trailing", "live_bailout"])
def test_every_held_state_routes_through_the_selector(selector, state):
    seen, _feb, fake = selector
    fake.result = HeldBboDecision(tick=None, snapshot={"ok": False, "reason": "held_bbo_unavailable"},
                                  envelope={"bbo_fallback_chain": []}, counts_toward_halt=True)
    tick, _f, snap = LR._live_tick_bbo(object(), "PCLA", execution_family="alpaca_spot", state=state)
    assert tick is None and len(seen) == 1
    assert snap["reason"] == "held_bbo_unavailable"
    assert snap["counts_toward_halt"] is True


def test_a_refusing_selector_fails_toward_the_deadman_not_a_stand_in(selector):
    """⚠️ Kapag tumanggi ang selector (L1, IEX, at -- sa labas ng RTH -- ang
    SIP-clocked floor sa ilalim ng sariling kontrata) ay None ang tick at WALANG
    ibang tanong mula sa `_live_tick_bbo`: hindi ito nag-iimbento ng quote at hindi
    tumatawag sa 900-s ladder. Sa RTH ang deadman ang sahig; sa labas nito ang
    floor tier ng selector mismo ang sahig (review fix)."""
    seen, feb, fake = selector
    fake.result = HeldBboDecision(tick=None, snapshot={"ok": False, "reason": "held_bbo_unavailable"},
                                  envelope={"bbo_fallback_engaged": True}, counts_toward_halt=False)
    tick, _f, snap = LR._live_tick_bbo(object(), "BDRX", execution_family="alpaca_spot", state="live_entered")
    assert tick is None
    assert len(seen) == 1 and feb == []
    assert snap["bbo_fallback_engaged"] is True


@pytest.mark.parametrize("family,state", [
    ("coinbase_spot", "live_entered"),
    ("robinhood_spot", "watching_live"),
    ("", "watching_live"),
])
def test_non_alpaca_paths_are_untouched(monkeypatch, family, state):
    """Ang crypto at ang ibang pamilya ay may sariling quote path at sariling
    kontrata. Hindi sila dapat dumaan sa execution-BBO contract kahit kailan."""
    called = []
    monkeypatch.setattr(LR, "_final_entry_bbo",
                        lambda *a, **k: called.append(k) or (None, {}))
    monkeypatch.setattr(LR, "select_held_bbo",
                        lambda *a, **k: called.append(("selector", k)) or None)

    class _A:
        def get_best_bid_ask(self, pid):
            return _Tick(5.0), "ordinary"

    tick, freshness, snap = LR._live_tick_bbo(
        _A(), "X", execution_family=family, state=state)
    assert tick.bid == 5.0 and freshness == "ordinary" and snap is None
    assert called == [], "hindi dapat naabot ang execution-BBO contract"


def test_alpaca_PRE_ENTRY_is_now_routed_too(monkeypatch):
    """⚠️ ITINATAMA ANG INVARIANT NA ITO (2026-08-26).

    Ang orihinal na anyo ng testong ito ay nagsasabing ang alpaca PRE-ENTRY ay
    "hindi dapat dumaan sa execution-BBO contract kahit kailan". Iyon ay tumpak
    noong 2026-08-25, nang ang HELD na landas lamang ang niruruta.

    Ito ay hindi na totoo, at ang pagiging hindi-totoo nito ang buong punto ng
    PR #1177. Ang pre-entry na landas ay bumabagsak dati sa hilaw na
    `get_best_bid_ask`, na sa premarket ay nagbabalik ng saradong libro ng
    KAHAPON bilang buhay -- nasukat: RDIB bid 7.31 / ask 20.75 na may
    `provider_time` 2026-08-25 20:00, 15 ORAS ang tanda, 9,579 bps. Ang quote na
    iyon ang kumansela ng isang buhay na order 9 segundo matapos itong ipadala.

    ⚠️ ANG MAS MAHALAGANG ARAL: hindi nahuli ng regression run ng #1177 ang
    testong ito, dahil pumili ako ng listahan ng file sa kamay sa halip na
    patakbuhin ang lahat ng tumutukoy sa `_live_tick_bbo`. Ang pulang test na
    nag-eengkoda ng maling invariant ay mas masama kaysa sa walang test.

    [48] build B: ang PRE-ENTRY ay HINDI binago (direct 10 s, tapos SIP-first
    stand-in 15 s) -- parehong klase ng depekto, hiwalay na PR; ang selector
    ay HINDI dapat tawagin dito.
    """
    called = []
    monkeypatch.setattr(LR, "_final_entry_bbo",
                        lambda *a, **k: called.append(k) or (None, {}))
    monkeypatch.setattr(LR, "select_held_bbo",
                        lambda *a, **k: called.append(("selector", k)) or None)

    class _A:
        def get_best_bid_ask(self, pid):
            raise AssertionError("hindi dapat maabot ang lasong landas")

    tick, freshness, snap = LR._live_tick_bbo(
        _A(), "X", execution_family="alpaca_spot", state="watching_live")
    assert tick is None
    assert len(called) == 2, "mahigpit muna, tapos stand-in"
    assert called[0].get("allow_stand_in") in (False, None)
    assert called[1].get("allow_stand_in") is True
    assert not any(isinstance(c, tuple) for c in called), "walang selector sa pre-entry"
