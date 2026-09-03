"""LOCKED BOOK (`bid == ask`) — ang degenerado ay iniskor bilang perpekto.

Ang bawat spread gate sa `live_runner.py` ay MONOTONE sa spread. Ang isang locked
na libro ay nagbibigay ng 0.0 bps — ang MINIMUM ng domain — kaya ito ay dumadaan sa
bawat pagsusuri sa kalidad sa pamamagitan mismo ng pagiging pinaka-degenerado. Bago
ang guard na ito, walang ni isang linya sa 46,107 ng file na iyon ang bumabanggit ng
locked na libro; ang crossed (`ask < bid`) lamang ang napangalanan kahit saan.

ANG KATOTOHANAN SA VENUE ANG NAGPAPASYA, HINDI ANG P&L (mahalaga ito: ang 21-araw na
corpus ay walang panalong trade, kaya ang "tatanggihan sana nito ang taloang ito" ay
WALANG halaga bilang ebidensya — ang pagtanggi sa lahat ay nananalo sa populasyong
iyon). Ipinag-uutos ng Reg NMS Rule 610(d) na iwasan ang locked/crossed na display sa
REGULAR hours at wala itong bisa sa labas nito. Eksaktong hinahati ito ng sinukat na
tape (30-min symbol-bounded na bintana sa `momentum_nbbo_spread_tape`):

    SDOT 2026-08-21 14:45Z (REGULAR)   ->      1 /    213 rows locked =  0.47%
    GYGY 2026-09-01 08:41Z (PREMARKET) ->  3,284 / 13,926 rows locked = 23.58%
    AUUD 2026-09-01 11:10Z (PREMARKET) ->  9,724 / 37,614 rows locked = 25.85%
    RDHL 2026-08-31 12:42Z (PREMARKET) -> 13,715 / 46,281 rows locked = 29.63%

Kaya: RTH -> artifact -> TANGGIHAN. Extended -> tunay -> TANGGAPIN na may one-tick na
EPEKTIBONG spread (ang pinakamababang TUNAY na gastos sa pagtawid; nasukat sa loob ng
~2% ng sariling p50 ng bawat pangalan nang hindi hinahawakan ang kontaminadong tape).

Bawat kaso sa ibaba ay muling itinayo mula sa mga tunay na row/event na pinangalanan
sa docstring nito.

TAPAT NA PAGHAHATI LABAN SA origin/main @ 89cb0eb (26 na kaso; sinukat, hindi
inaangkin — tumakbo nang serial na may `-p no:randomly` sa isang sariwang
detached na worktree sa 89cb0eb):

  BUMABAGSAK SA GAWI (4) — ito ang mga demonstrasyon ng depekto, at ang bawat
  isa ay bumabagsak sa isang PAGKAKAIBA SA GAWI, hindi sa isang nawawalang key:
    test_sdot_locked_book_rejected_in_regular_hours          "dumaan bilang wasto"
    test_locked_book_cannot_defeat_a_deliberate_block_all_cap "tumalo sa block-all"
    test_auud_locked_book_is_not_priced_as_a_free_crossing    0.0 == 87.7193
    test_rdhl_locked_tape_does_not_manufacture_a_blown_out_spread  false veto

  BUMABAGSAK SA ISANG NAWAWALANG PANGALAN (2) — sinasabi nang tapat:
    test_locked_bbo_is_a_retryable_book_quality_reason  — ang reason string na
      `locked_bbo` ay wala pa sa origin/main, kaya wala ring maging retryable.
    test_guard_off_restores_byte_identical_behaviour — AttributeError sa
      monkeypatch: ang setting ay wala pa. Isa itong kill-switch na pagsusuri,
      hindi isang pahayag tungkol sa depekto.

  PARITY GUARDS NA PUMAPASA SA MAGKABILANG PANIG (7) — sinusukat nila ang GAWI na
  dapat HINDI magbago, kaya ang pagpasa sa main ang mismong punto:
    crossed pa rin invalid; dalawang-panig na libro hindi ginalaw (x2); ang
    locked na secondary ay nai-a-append pa rin sa `rescued_out`; ang crypto ay
    dumadaan sa dating landas; ang tunay na nabubulok na libro ay nagba-veto pa
    rin; at ang locked na filter ay hindi kailanman gumagawa ng veto.

  NILAKTAWAN SA MAIN (13) — mga helper na wala pa roon.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import market_profile, nbbo_tape
from app.services.trading.momentum_neural.live_runner import (
    _bid_prop_confirms_break,
    _entry_spread_risk_decision,
    _quote_quality_block,
)

# Sa origin/main ay WALA pa ang helper. Ini-import natin ito nang malambot para ang
# mga pagsusuri sa GAWI sa ibaba ay tumakbo pa rin doon at BUMAGSAK sa tamang dahilan
# (isang collection error ay teknikal na pagbagsak pero hindi ito nagpapatunay ng
# kahit ano tungkol sa depekto).
try:
    from app.services.trading.momentum_neural.live_runner import _locked_book_state
except ImportError:  # pragma: no cover - origin/main lamang
    _locked_book_state = None

try:
    from app.services.trading.momentum_neural.live_runner import (
        _decision_spread_bps_for_fill_log,
    )
except ImportError:  # pragma: no cover - origin/main lamang
    _decision_spread_bps_for_fill_log = None


def _tick(bid, ask, *, spread_bps=None, source="iqfeed_l1"):
    """Isang quote na walang freshness meta, kaya nilalaktawan ang staleness branch."""
    mid = (bid + ask) / 2.0
    if spread_bps is None:
        spread_bps = ((ask - bid) / mid) * 10_000.0 if mid else 0.0
    return SimpleNamespace(
        bid=bid, ask=ask, mid=mid, spread_bps=spread_bps,
        freshness=None, raw={"source": source},
    )


@pytest.fixture
def regular_hours(monkeypatch):
    monkeypatch.setattr(
        market_profile, "market_session_now", lambda *a, **k: "regular"
    )


@pytest.fixture
def premarket(monkeypatch):
    monkeypatch.setattr(
        market_profile, "market_session_now", lambda *a, **k: "premarket"
    )


# ───────────────── SHAPE A — ang validity test sa :24176 ─────────────────


def test_sdot_locked_book_rejected_in_regular_hours(regular_hours):
    """SDOT session 14825 @ 2026-08-21 14:45:47.667335Z — 17.29/17.29, 0.0 bps.

    Ang `live_entry_final_bbo` ay nag-ulat ng bid 17.29 / ask 17.29 / spread_bps 0.0
    (source massive_ws_universe) at ang gate ay iniskor ito bilang perpekto. Sa buong
    11.5-segundong hawak (14:45:59.383449 -> 14:46:10.867396) ay may 3,158 na tick sa
    `iqfeed_trade_ticks` at ZERO ang locked; ang pinakamahusay na bid kahit saan ay
    17.01 — 162 bps sa ILALIM ng "perpektong" libro. Ang lock ay HINDI ang merkado.
    """
    block = _quote_quality_block(
        _tick(17.29, 17.29), None, max_spread_bps=500.0, symbol="SDOT",
    )
    assert block is not None, "ang locked na libro sa RTH ay dumaan bilang wasto"
    assert block["reason"] == "locked_bbo"
    assert block["locked_book"] is True
    assert block["market_session"] == "regular"


def test_crossed_book_still_rejected_as_invalid(regular_hours):
    """PARITY: ang crossed ay `invalid_bbo` pa rin, hindi muling binansagan."""
    block = _quote_quality_block(
        _tick(17.30, 17.29, spread_bps=0.0), None,
        max_spread_bps=500.0, symbol="SDOT",
    )
    assert block is not None
    assert block["reason"] == "invalid_bbo"


def test_two_sided_book_unchanged_in_regular_hours(regular_hours):
    """PARITY: ang tunay na dalawang-panig na libro ay hindi hinahawakan."""
    assert _quote_quality_block(
        _tick(17.28, 17.29), None, max_spread_bps=500.0, symbol="SDOT",
    ) is None


# ────── SHAPE B — ang monotone na gate na gumagantimpala sa degenerasyon ──────


def test_locked_book_cannot_defeat_a_deliberate_block_all_cap(premarket):
    """Ang `max_spread = 0.0` ay SADYANG block-all (komento sa :24222-24223).

    Ang `0.0 > 0.0` ay False, kaya ang locked na libro ang TANGING libro na
    nakakatalo sa isang block-all na cap — sa pamamagitan ng pagiging degenerado.
    Ginagamit ang GYGY 1.42/1.42 (tape row 181529679 @ 2026-09-01 08:41:54.769168Z).
    """
    block = _quote_quality_block(
        _tick(1.42, 1.42), None, max_spread_bps=0.0, symbol="GYGY",
    )
    assert block is not None, "ang locked na libro ay tumalo sa block-all na cap"
    assert block["reason"] == "wide_bbo_spread"
    # Ang isang tick sa mid 1.42 = 70.4 bps ang iniskor, hindi 0.0.
    assert block["spread_bps"] == pytest.approx(70.4225, rel=1e-4)


def test_locked_secondary_still_rescues_so_the_held_bid_is_not_left_on_garbage(
    premarket, monkeypatch
):
    """REVIEW FIX — ang rescue admission ay HINDI dapat magbago dahil sa guard.

    Ang `rescued_out` ay isang OUT-CHANNEL, hindi telemetry. Kapag ang rescue ay
    tinanggap, `rescued_out.append(tick)`; ang tumatawag ay nagbi-bind ng
    `tick = _rescued_entry_ticks[-1]`, at ang `bid` mula roon ang IISANG `bid` na
    binabasa ng buong held-position na exit stack — stop breach, trail arm, at ang
    C1 per-trade max-loss FORCE LIQUIDATION. Ang unang draft ay nagpalit ng `_s2`
    BAGO ang admission test, na tumatanggi sa rescue at nag-iiwan sa hawak na
    posisyon na nakapresyo sa PUNIT NA PRIMARY (1.30 dito sa halip na 1.42) — ang
    mismong butas (2026-08-18 PFSA frozen mid) na pinanganak ng rescue para isara.

    Kaya: ang secondary ay dapat NAI-APPEND pa rin, at ang locked na estado ay
    NAKIKITA sa payload sa halip na tahimik.
    """
    monkeypatch.setattr(
        lr, "_refetch_bbo_secondary",
        lambda sym: (_tick(1.42, 1.42, source="massive_ws_universe"), "massive"),
    )
    rescued: list = []
    block = _quote_quality_block(
        _tick(1.30, 1.45, source="massive_ws_universe"), None,
        max_spread_bps=0.0, symbol="GYGY", rescued_out=rescued,
    )
    assert block is None, block
    assert rescued, "ang na-validate na secondary ay hindi naiabot pabalik"
    assert rescued[-1].bid == pytest.approx(1.42)


# ────────────────────── CRYPTO — walang monkeypatch dito ──────────────────────


def test_crypto_locked_book_is_never_treated_as_regular_hours():
    """`market_session_now` ay nagbabalik ng "regular" para sa BAWAT -USD sa BAWAT oras.

    market_profile.py:73-74 — crypto ay 24/7. Kung wala ang carve-out, ang
    mahigpit na RTH-reject ay PERMANENTENG naka-arm sa lane ng Coinbase, sa lakas
    ng Reg NMS 610(d) (na hindi namamahala sa Coinbase) at ng apat na sinukat na
    bintana (na pawang US equities). SADYANG WALANG `regular_hours` na fixture
    dito: ang buong punto ay ang TUNAY na `market_session_now`.
    """
    # Ang premise, na totoo sa magkabilang panig: crypto == "regular" 24/7.
    assert market_profile.market_session_now("BTC-USD") == "regular"
    # Ang GAWI — dating landas nang buo: walang pagtanggi AT walang pagpapalit.
    # Ito ay tumatakbo nang PAREHO sa origin/main at dito (isang parity guard, at
    # hindi isang demonstrasyon ng depekto — ang depekto ay ipinakikilala SANA ng
    # branch, at ang pagsusuring ito ang pumipigil doon).
    assert _quote_quality_block(
        _tick(1.42, 1.42), None, max_spread_bps=500.0, symbol="BTC-USD",
    ) is None
    _ok, _gate = _entry_spread_risk_decision(
        **{
            "bid": 1.14, "ask": 1.14, "quantity": 551.0, "stop_distance": 0.05,
            "max_fraction": 0.25, "expected_move_bps": 206.7,
            **({"symbol": "ETH-USD"} if _locked_book_state is not None else {}),
        }
    )
    assert _gate["gate_spread_bps"] == 0.0
    assert _ok is True
    # At ang mga helper mismo, kung nandito na sila.
    if _locked_book_state is not None:
        assert lr._locked_book_in_regular_hours("BTC-USD") is False
        assert lr._locked_book_guard_applies("BTC-USD") is False
        assert lr._locked_book_guard_applies("SDOT") is True


# ────────── SHAPE C — ang stand-in predicate sa :21397 (0.0 vs 88.1) ──────────


def _auud(**overrides):
    """AUUD session 19337 @ 2026-09-01 11:10:41.485652Z — tape row 182087598.

    bid 1.14 / ask 1.14, qty 551. Ang budget ay 31.0 bps (= 0.15 x 206.7 expected
    move). Ang `spread_cost_derate` sa PAREHONG session object ay may hawak na
    88.1 bps na may label na "derate", at ang sariling p50 ng AUUD ay 89.4 bps sa
    36,060 na sample — kaya ang 88.1 ang PANGKARANIWANG libro at ang 0.0 ang outlier.
    """
    kwargs = dict(
        bid=1.14, ask=1.14, quantity=551.0, stop_distance=0.05,
        max_fraction=0.25, expected_move_bps=206.7,
    )
    kwargs.update(overrides)
    return _entry_spread_risk_decision(**kwargs)


def test_auud_locked_book_is_not_priced_as_a_free_crossing():
    """Tatlong sukatan ang lahat 0.0 sa main: bps, USD cost, at parehong fraction."""
    ok, gate = _auud()
    # ⚠️ ANG PAGKAKASUNOD-SUNOD AY MAHALAGA (review fix). Ang GAWI muna, at ang
    # bagong telemetry key sa DULO: sa origin/main ang kasong ito ay dapat
    # bumagsak sa `gate_spread_bps 0.0 != 87.7193` — isang pahayag tungkol sa
    # depekto — at HINDI sa `KeyError: 'locked_book'`, na isang pahayag lamang
    # tungkol sa isang nawawalang key.
    # 1 tick sa mid 1.14 = 87.7 bps — nasa loob ng ~2% ng sariling p50 (89.4).
    assert gate["gate_spread_bps"] == pytest.approx(87.7193, rel=1e-4)
    assert gate["spread_cost_usd"] == pytest.approx(5.51, abs=0.01)
    assert gate["spread_fraction_of_expected_move"] > 0.0
    assert ok is False, gate
    assert gate["reason"] == "spread_exceeds_expected_move_budget"
    assert gate["locked_book"] is True


def test_two_sided_book_spread_gate_unchanged():
    """PARITY: ang tunay na libro ay dumadaan sa dating aritmetika."""
    ok, gate = _auud(bid=1.14, ask=1.1401)
    # `.get` para ang PARITY na ito ay tumakbo nang PAREHO sa origin/main at dito —
    # ang punto ay ang GAWI ay hindi nagbago, hindi ang bagong telemetry key.
    assert gate.get("locked_book", False) is False
    assert gate["gate_spread_bps"] == pytest.approx(0.8771, rel=1e-3)
    assert ok is True, gate


# ───── SHAPE D — ang median-based na degenerasyon sa :22724 (FALSE VETO) ─────


def test_rdhl_locked_tape_does_not_manufacture_a_blown_out_spread(monkeypatch):
    """RDHL session 19216, event 1421057 @ 2026-08-31 12:42:44.330506.

    Naitala: `{'samples': 8, 'bid_first': 1.43, 'bid_last': 1.42,
    'spread_last_bps': 70.18, 'spread_median_bps': 0.0, 'spread_blown_out': True,
    'bid_stepping_down': True, 'blocked_trigger': 'abcd_break_tick_ok'}`.

    Muling itinayo ang eksaktong 8-row na bintana sa cutoff 12:42:43.988432Z: PITO sa
    walo ay locked sa 1.43/1.43 = 0.0 bps at IISA ang tunay na 1.42/1.43 sa 70.1754
    bps. Sa median na 0.0 ang `spreads[-1] > median * 1.5 + 1e-9` ay bumabagsak sa
    "ang spread ay hindi eksaktong zero", at ang `bid_first` na 1.43 ay ang PUNIT NA
    ASK LEVEL ng mga naka-lock na row — kaya ang PAREHONG kalahati ng AND ay sira.
    Ang 70.18 bps ay 1.035x ng sariling p50 ng RDHL na 67.8 bps (173,206 na sample):
    isang ganap na pangkaraniwang libro ang nabansagang "blown out".
    """
    window = [(1.43, 0.0)] * 7 + [(1.42, 70.1754)]
    monkeypatch.setattr(
        nbbo_tape, "recent_bid_spread_tape", lambda *a, **k: list(window)
    )
    confirmed, dbg = _bid_prop_confirms_break(None, "RDHL", window_s=30.0)
    assert confirmed is True, dbg
    assert dbg["reason"] == "bid_prop_locked_book_tape_fail_open"
    assert dbg["unlocked_samples"] == 1


def test_genuinely_deteriorating_book_still_vetoes(monkeypatch):
    """PARITY: sa TUNAY na mga sample ang veto ay pumuputok pa rin.

    Walang locked na row; ang bid ay bumababa at ang huling spread ay lumalampas sa
    trailing median nito nang higit sa 1.5x. Ito ang kaso na SINADYANG haharangin.
    """
    window = [
        (1.50, 60.0), (1.49, 62.0), (1.48, 61.0),
        (1.47, 63.0), (1.44, 400.0),
    ]
    monkeypatch.setattr(
        nbbo_tape, "recent_bid_spread_tape", lambda *a, **k: list(window)
    )
    confirmed, dbg = _bid_prop_confirms_break(None, "RDHL", window_s=30.0)
    assert confirmed is False, dbg
    assert dbg["reason"] == "bid_prop_book_deteriorating"
    assert dbg.get("locked_rows_dropped", 0) == 0


def test_locked_filter_can_never_manufacture_a_veto(monkeypatch):
    """REVIEW FIX — ang guard ay ISANG-DIREKSYON.

    Ang unang draft ay basta pinalitan ang tape ng nasala nito. Kapag ang
    PINAKABAGONG row ang siyang locked, ang pagsala ay nagpapalit ng `spreads[-1]`
    AT ng `bids[-1]`, kaya kaya nitong GUMAWA ng veto na wala sa origin/main.
    Sinukat sa magkabilang worktree gamit ang eksaktong bintanang ito:

        main  -> confirmed=True   bid_last 1.52  median 61.0  blown_out False
        draft -> confirmed=False  bid_last 1.47  median 61.5  blown_out True
                 reason bid_prop_book_deteriorating, locked_rows_dropped 1

    Sa sinukat na 23.58-29.63% na locked na premarket tape, ang pinakabagong row
    ay locked nang humigit-kumulang isa sa bawat apat na tick, kaya ang bagong
    veto na iyon ay buhay sa loob mismo ng premarket na bintana. KONTRATA: ang
    guard ay may pahintulot na MAG-ALIS ng veto at hindi kailanman makakadagdag.
    """
    window = [
        (1.50, 60.0), (1.49, 62.0), (1.48, 61.0),
        (1.47, 400.0), (1.52, 0.0),
    ]
    monkeypatch.setattr(
        nbbo_tape, "recent_bid_spread_tape", lambda *a, **k: list(window)
    )
    confirmed, dbg = _bid_prop_confirms_break(None, "RDHL", window_s=30.0)
    assert confirmed is True, dbg
    assert dbg.get("reason") != "bid_prop_book_deteriorating"
    # Ang hilaw na tape ang nagpasya, kaya walang row ang naibaba.
    assert dbg.get("locked_rows_dropped", 0) == 0
    assert dbg["bid_last"] == pytest.approx(1.52)


# ───────────── ang punch-window hold — ang `locked_bbo` ay lumilipas ─────────────


def test_locked_bbo_is_a_retryable_book_quality_reason():
    """Ang locked ay ang PINAKA-LUMILIPAS na estado ng libro sa file na ito.

    Ang `_PUNCH_RETRYABLE_QUOTE_REASONS` (idinagdag ng 2026-08-21 flush_dip_buy
    audit) ang pumipigil sa isang one-shot na BOOK-quality veto na mag-demote ng
    sariwang dip-family candidate pabalik sa WATCHING. Ang wide/stale/unstable/
    INVALID ay nasa loob; kung ang `locked_bbo` ay maiiwan sa labas, ito ang
    magiging TANGING book reason na one-shot na pumapatay ng kandidato — ang
    eksaktong regression na idinagdag ang hold para pigilan.
    """
    assert "locked_bbo" in lr._PUNCH_RETRYABLE_QUOTE_REASONS
    # PARITY: ang set ay hindi lumawak lampas sa isang idinagdag.
    assert lr._PUNCH_RETRYABLE_QUOTE_REASONS == frozenset(
        {"wide_bbo_spread", "stale_bbo", "unstable_spread", "invalid_bbo", "locked_bbo"}
    )


# ───────── ang write site ng `spread_bps_at_decision` (mig308 fill log) ─────────


@pytest.mark.skipif(
    _decision_spread_bps_for_fill_log is None,
    reason="ang helper ay wala pa sa origin/main",
)
@pytest.mark.parametrize(
    "symbol, bid, ask, expect_bps, expect_locked",
    [
        # Ang tatlong sinukat na session na may 0.0 na NAKA-IMBAK NA sa
        # momentum_fill_outcomes.spread_bps_at_decision.
        ("SDOT", 17.29, 17.29, 5.7837, True),
        ("GYGY", 1.42, 1.42, 70.4225, True),
        ("AUUD", 1.14, 1.14, 87.7193, True),
        # PARITY: ang tunay na dalawang-panig na libro ay hilaw na aritmetika.
        ("AUUD", 1.14, 1.1401, 0.8771, False),
        # CRYPTO: dating landas nang buo — 0.0 pa rin, walang equity tick.
        ("BTC-USD", 1.42, 1.42, 0.0, False),
    ],
)
def test_fill_log_decision_spread_no_longer_writes_a_fabricated_zero(
    symbol, bid, ask, expect_bps, expect_locked
):
    """Ang mga LUMANG row ay isang hiwalay na migration (CLAUDE.md hard rule 3).

    Ang WRITE SITE ay hindi: kung hindi ito aayusin, ang branch na ito ay
    magpapatuloy sa paggawa ng mismong 0.0 na sinulat nito para alisin — kasama
    sa mga extended-hours na admission na SADYA nating pinapayagan (GYGY: isang
    tick = 70.4 bps laban sa 100.6 bps na budget, kaya pumapasok pa rin iyon).
    """
    _mid = (bid + ask) / 2.0
    _bps, _locked = _decision_spread_bps_for_fill_log(bid, ask, _mid, symbol)
    assert _locked is expect_locked
    assert _bps == pytest.approx(expect_bps, rel=1e-4, abs=1e-9)
    if expect_locked:
        assert _bps > 0.0, "isang bagong ginawang 0.0 sa fill log"


# ───────────────────────── ang helper mismo ─────────────────────────


@pytest.mark.parametrize(
    "bid, ask, locked, bps",
    [
        (1.14, 1.14, True, 87.7193),     # AUUD  — sariling p50 89.4
        (1.43, 1.43, True, 69.9301),     # RDHL  — sariling p50 67.8
        (1.42, 1.42, True, 70.4225),     # GYGY
        (17.29, 17.29, True, 5.7837),    # SDOT
        (0.50, 0.50, True, 2.0),         # sub-$1: apat na decimal na tick
        (1.42, 1.43, False, 0.0),        # tunay na dalawang-panig
        (1.43, 1.42, False, 0.0),        # CROSSED ay hindi locked
        (0.0, 0.0, False, 0.0),          # walang libro
    ],
)
@pytest.mark.skipif(
    _locked_book_state is None, reason="ang helper ay wala pa sa origin/main"
)
def test_locked_book_state(bid, ask, locked, bps):
    is_locked, eff_bps, _ = _locked_book_state(bid, ask)
    assert is_locked is locked
    assert eff_bps == pytest.approx(bps, rel=1e-4)


# ─────────────────────────── ang kill switch ───────────────────────────


def test_guard_off_restores_byte_identical_behaviour(regular_hours, monkeypatch):
    """OFF => ang apat na site ay bumabalik sa dating gawi nang eksakto."""
    monkeypatch.setattr(
        settings, "chili_momentum_locked_book_guard_enabled", False
    )
    assert _quote_quality_block(
        _tick(17.29, 17.29), None, max_spread_bps=500.0, symbol="SDOT",
    ) is None
    ok, gate = _auud()
    assert ok is True and gate["gate_spread_bps"] == 0.0

    window = [(1.43, 0.0)] * 7 + [(1.42, 70.1754)]
    monkeypatch.setattr(
        nbbo_tape, "recent_bid_spread_tape", lambda *a, **k: list(window)
    )
    confirmed, dbg = _bid_prop_confirms_break(None, "RDHL", window_s=30.0)
    assert confirmed is False
    assert dbg["spread_median_bps"] == 0.0
