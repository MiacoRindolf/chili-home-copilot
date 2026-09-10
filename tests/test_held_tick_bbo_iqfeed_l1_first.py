"""[48] BUILD B — ang HELD decision tick ay nagbabasa ng IQFeed L1 muna, strict IEX
pangalawa, at ang SIP-clocked floor (sa ilalim ng SARILING kontrata) LAMANG habang
hindi makakaputok ang nakapahingang broker deadman. Hindi kailanman ang 900-s ladder.

ANG PANGYAYARI (PCLA 21592, 2026-09-10 13:41:05.80Z). Ang bailout bid 8.96 ay galing
sa SIP-clocked massive_ws row 235009576 -- provider 13:40:59.221, received
13:41:04.236, 6.58 s na luma -- na pumasa sa 10-s SIP ceiling dahil ang SIP tier
ang UNANG tinatanong ng stand-in ladder; ang fenced IQFeed L1 row (8.88/9.00) ay
1.06 s LANG ang edad sa parehong sandali. Ang exit ay ipinresyo 13:41:12.128 off
sa own-clock row 234991296 na 33.875 s ang edad (cap 900) -> fill 8.88.

ANG REVIEW (parehong gabi). Ang unang anyo ng build B ay fail-closed nang WALANG
sahig sa labas ng regular session (ang deadman stop ay `status=new` hanggang open),
nagve-veto ng sariwang L1 row sa salita ng isang 5-8-s na huling saksi, tinatanggap
ang own-clock row sa fenced na bound, nagbibilang ng halt sa tick na sumagot, at
naiwan ang ikaapat na protective pricing site sa SIP-first ladder. Bawat isa ay may
test dito.

Sinusubok dito (fakes, walang DB maliban sa mga wiring test sa dulo):
  * source order (§4): L1 muna, IEX pangalawa, ang floor LAMANG kapag inert ang deadman
  * demotion (hindi veto): SIP witness at unchanged-book ay sumasagot sa likod ng IEX
  * delayed-L1 (kaso a: stamp; kaso b: SIP witness -> label, hindi veto)
  * ang adapter read (`_iqfeed_l1_read`), ang wrapper (`_iqfeed_l1_quote`), ang floor read
  * ang resibo (§7): bawat exit/hold receipt ay may bbo_source / bbo_age_s /
    bbo_fallback_engaged (AST pin); ang flatten receipt ay nagsasabi ng binding nito
  * ang hinangong hangganan (§5): runtime vs tagged fallback, n_min, A2 own-clock,
    mga konstanteng dala; `current_bounds` ay hindi kailanman inline sa tick path
  * wiring: blocked tick -> halt streak; exit pricing -> selector (L1, floor) BAGO ang
    900-s ladder sa LAHAT ng apat na site; isang kahulugan lang ang bbo_fallback_engaged

Runnable: pytest tests/test_held_tick_bbo_iqfeed_l1_first.py -v
"""
from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import app.db as db_mod
from app.config import settings
from app.services.trading.momentum_neural import held_bbo as HB
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.held_bbo import (
    AUTHORITY_ALPACA_DIRECT,
    AUTHORITY_L1_FENCED,
    AUTHORITY_L1_OWN_CLOCK,
    EXIT_PRICING_TIERS,
    HELD_BBO_RECEIPT_KEYS,
    L1_BASIS_FENCED,
    L1_BASIS_OWN_CLOCK,
    HeldBboBounds,
    HeldBboReads,
    L1Read,
    derive_held_bbo_bounds,
    select_held_bbo,
)
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedTicker

_LIVE_RUNNER_SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"
)
_HELD_BBO_SRC = _LIVE_RUNNER_SRC.with_name("held_bbo.py")

NOW = datetime(2026, 9, 10, 17, 40, 0, tzinfo=timezone.utc)
PIN = "iqfeed-l1-exact-print-provenance-v3+sha256:2aa8d76326865c4c"
RUN_ID = "12553525-2da8-4b22-a69f-d3034871e90c"
BOUNDS = HeldBboBounds(
    l1_fresh_bound_s=4.917,
    l1_gap_ceiling_s=18.832,
    heartbeat_bound_s=4.917,
    sip_disagree_band_bps=66.22,
    delay_stamp_threshold_s=2.0,
    l1_own_clock_fresh_bound_s=4.337,
    derivation={"l1_fresh_bound_s": {"value": 4.917, "source": "test"}},
)
FLOOR_AUTHORITY = "stand_in_massive_sip"   # ang label na isinusuot ng adapter, hindi ng held_bbo


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
def _l1(
    age_s: float,
    *,
    basis: str = L1_BASIS_FENCED,
    reason: str | None = None,
    delay_s: float = 0.5,
    bid: float = 8.88,
    ask: float = 9.00,
    row_id: int = 234991000,
) -> L1Read:
    ref = NOW - timedelta(seconds=age_s)
    recv = ref + timedelta(seconds=delay_s)
    meta = FreshnessMeta(retrieved_at_utc=recv, provider_time_utc=ref, max_age_seconds=18.832)
    mid = (bid + ask) / 2.0
    tick = NormalizedTicker(
        product_id="PCLA", bid=bid, ask=ask, mid=mid,
        spread_bps=(ask - bid) / mid * 1e4, freshness=meta,
        raw={"feed": "iqfeed_l1", "timestamp_basis": basis, "tape_row_id": row_id},
    )
    return L1Read(
        ticker=tick, meta=meta, reason=reason, basis=basis,
        event_reference_at=ref, received_at=recv,
        available_at=recv + timedelta(seconds=0.5), tape_row_id=row_id,
        bridge_version=PIN, delay_signature_s=delay_s,
    )


def _iex_tick(age_s: float = 0.3) -> tuple[NormalizedTicker, dict]:
    prov = NOW - timedelta(seconds=age_s)
    meta = FreshnessMeta(retrieved_at_utc=NOW, provider_time_utc=prov, max_age_seconds=2.0)
    tick = NormalizedTicker(
        product_id="PCLA", bid=8.90, ask=8.95, mid=8.925, spread_bps=56.0, freshness=meta,
        raw={"feed": "iex", "timestamp_basis": "provider_event_at"},
    )
    snap = {
        "ok": True, "reason": "execution_bbo_ok", "symbol": "PCLA", "source": "iex",
        "tape_row_id": None, "provider_event_at_utc": prov.isoformat(),
        "received_at_utc": NOW.isoformat(), "available_at_utc": None,
        "timestamp_basis": "provider_event_at", "quote_authority": "alpaca_direct",
        "age_seconds": age_s, "max_age_seconds": 2.0, "bid": 8.90, "ask": 8.95,
        "mid": 8.925, "spread_bps": 56.0,
    }
    return tick, snap


def _floor_tick(age_s: float = 5.2, bid: float = 8.84, ask: float = 8.90) -> tuple[NormalizedTicker, dict]:
    """Ang sagot ng adapter `_sip_clocked_floor_quote`: (tick, payload) -- ang payload
    ang nagpapangalan ng source / basis / authority / kontrata."""
    prov = NOW - timedelta(seconds=age_s)
    recv = prov + timedelta(seconds=5.0)
    meta = FreshnessMeta(retrieved_at_utc=recv, provider_time_utc=prov, max_age_seconds=10.0)
    mid = (bid + ask) / 2.0
    tick = NormalizedTicker(
        product_id="PCLA", bid=bid, ask=ask, mid=mid, spread_bps=(ask - bid) / mid * 1e4,
        freshness=meta,
        raw={"feed": "massive_ws", "timestamp_basis": "massive_sip_unix_ms", "tape_row_id": 235009576,
             "provider_event_at_utc": prov.isoformat(), "received_at_utc": recv.isoformat()},
    )
    payload = {
        "ok": True, "reason": "execution_bbo_ok", "symbol": "PCLA", "source": "massive_ws",
        "timestamp_basis": "massive_sip_unix_ms", "bridge_version": "massive_ws_v2_sip_clock",
        "quote_authority": FLOOR_AUTHORITY, "max_age_seconds": 10.0, "age_seconds": age_s,
        "tape_row_id": 235009576, "provider_event_at_utc": prov.isoformat(),
        "received_at_utc": recv.isoformat(), "available_at_utc": None,
        "bid": bid, "ask": ask, "mid": mid, "spread_bps": (ask - bid) / mid * 1e4,
    }
    return tick, payload


FLOOR_REFUSED = (None, {"ok": False, "reason": "no_row_within_contract", "source": "massive_ws",
                        "max_age_seconds": 10.0, "age_seconds": None})

IEX_UNAVAILABLE = (None, {"ok": False, "reason": "execution_bbo_unavailable",
                          "unavailable_kind": "absent_quote", "source": "iex"})


def _reads(
    *,
    l1: L1Read,
    heartbeat: float | None = 0.1,
    contradicting_print: dict | None = None,
    witness: dict | None = None,
    asof: dict | None = None,
    direct: tuple = IEX_UNAVAILABLE,
    floor: tuple | None = None,
) -> tuple[HeldBboReads, dict[str, list]]:
    calls: dict[str, list] = {"l1": [], "hb": [], "print": [], "witness": [], "asof": [], "direct": [], "floor": []}

    def _l1_read(sym, max_age_s):
        calls["l1"].append((sym, max_age_s))
        return l1

    def _hb():
        calls["hb"].append(1)
        return heartbeat

    def _print(sym, *, since_utc, bid, ask):
        calls["print"].append((sym, since_utc, bid, ask))
        return contradicting_print

    def _witness(sym):
        calls["witness"].append(sym)
        return witness

    def _asof(sym, at):
        calls["asof"].append((sym, at))
        return asof

    def _direct(pid, max_age_s):
        calls["direct"].append((pid, max_age_s))
        return direct

    def _floor(sym):
        calls["floor"].append(sym)
        return floor

    return HeldBboReads(
        l1_read=_l1_read, heartbeat_age_s=_hb, contradicting_print=_print,
        sip_witness=_witness, l1_asof=_asof, direct=_direct,
        sip_clocked_floor=(_floor if floor is not None else None),
    ), calls


def _select(reads, **kw):
    return select_held_bbo(object(), "PCLA", now=NOW, bounds=BOUNDS, reads=reads, **kw)


def _tiers(dec) -> list[tuple]:
    return [(e["tier"], e["outcome"], e["reason"]) for e in dec.envelope["bbo_fallback_chain"]]


# ---------------------------------------------------------------------------
# (1)-(4) source order
# ---------------------------------------------------------------------------
def test_1_fresh_l1_answers_and_iex_is_never_asked():
    reads, calls = _reads(l1=_l1(1.06), direct=_iex_tick(), floor=_floor_tick())
    dec = _select(reads)
    assert dec.tick is not None and dec.tick.bid == 8.88
    assert calls["direct"] == [], "hindi dapat tanungin ang IEX kapag sumagot ang L1"
    assert calls["floor"] == [], "hindi dapat tanungin ang floor kapag sumagot ang L1"
    env = dec.envelope
    assert env["bbo_fallback_engaged"] is False
    assert env["bbo_quote_authority"] == AUTHORITY_L1_FENCED
    assert env["bbo_validity_rule"] == "l1_fresh"
    assert env["bbo_answering_tier"] == "iqfeed_l1"
    assert env["bbo_max_age_s"] == pytest.approx(BOUNDS.l1_fresh_bound_s)
    assert env["bbo_source"] == "iqfeed_l1"
    assert env["bbo_age_s"] == pytest.approx(1.06)
    assert env["bbo_selector_version"] == "held_bbo_v1"
    assert env["bbo_bounds"] == BOUNDS.derivation
    assert dec.snapshot["ok"] is True and dec.snapshot["reason"] == "execution_bbo_ok"
    assert dec.snapshot["quote_authority"] == AUTHORITY_L1_FENCED
    assert dec.tick.freshness.max_age_seconds == pytest.approx(BOUNDS.l1_fresh_bound_s)
    assert _tiers(dec) == [("iqfeed_l1", "answered", "l1_fresh")]
    assert dec.counts_toward_halt is False


def test_2_own_clock_row_carries_the_existing_cross_source_label():
    reads, _ = _reads(l1=_l1(0.8, basis=L1_BASIS_OWN_CLOCK))
    dec = _select(reads)
    assert dec.tick is not None
    assert dec.envelope["bbo_quote_authority"] == AUTHORITY_L1_OWN_CLOCK
    assert dec.envelope["bbo_timestamp_basis"] == L1_BASIS_OWN_CLOCK
    # Hindi kailanman alpaca_direct ang L1 -- ang #1233/#1249/#1268 na aral.
    assert dec.envelope["bbo_quote_authority"] != AUTHORITY_ALPACA_DIRECT


def test_3_no_l1_row_falls_to_iex_and_the_chain_says_so():
    reads, calls = _reads(l1=L1Read(reason="no_row"), direct=_iex_tick(0.3), floor=_floor_tick())
    dec = _select(reads)
    assert dec.tick is not None and dec.tick.bid == 8.90
    assert len(calls["direct"]) == 1
    assert calls["direct"][0][1] == pytest.approx(2.0)
    assert calls["floor"] == [], "hindi dapat tanungin ang floor kapag sumagot ang IEX"
    assert _tiers(dec) == [
        ("iqfeed_l1", "refused", "l1_no_row"),
        ("alpaca_iex", "answered", "iex_direct"),
    ]
    assert dec.envelope["bbo_fallback_engaged"] is True
    assert dec.envelope["bbo_quote_authority"] == AUTHORITY_ALPACA_DIRECT
    assert dec.envelope["bbo_validity_rule"] == "iex_direct"
    assert dec.envelope["bbo_answering_tier"] == "alpaca_iex"
    assert dec.envelope["bbo_l1_entitlement_state"] == "unknown_no_l1_row"


def test_4_everything_refused_means_no_tick_and_every_tier_is_in_the_chain():
    reads, _ = _reads(l1=L1Read(reason="no_row"), heartbeat=None)
    dec = _select(reads)
    assert dec.tick is None
    assert dec.snapshot["ok"] is False
    assert dec.snapshot["reason"] == "held_bbo_unavailable"
    assert _tiers(dec) == [
        ("iqfeed_l1", "refused", "l1_no_row"),
        ("alpaca_iex", "refused", "iex_execution_bbo_unavailable"),
        ("sip_clocked_floor", "refused", "floor_reader_missing"),
    ]
    assert dec.envelope["bbo_fallback_engaged"] is True
    assert dec.envelope["bbo_source"] is None
    assert dec.envelope["bbo_answering_tier"] is None
    assert dec.snapshot["unavailable_kind"] == "l1_no_row|iex_execution_bbo_unavailable|floor_reader_missing"


def test_iex_stale_still_counts_toward_the_halt_streak():
    """Ang lumang mapping (`execution_bbo_stale` -> register) ay dala pa rin
    kahit INFRA ang dahilan ng L1 (na hindi nagbibilang)."""
    reads, _ = _reads(
        l1=L1Read(reason="bridge_build_mismatch"),
        direct=(None, {"ok": False, "reason": "execution_bbo_stale", "age_seconds": 5.0}),
    )
    dec = _select(reads)
    assert dec.tick is None
    assert dec.counts_toward_halt is True


# ---------------------------------------------------------------------------
# (5) the floor: SIP-clocked row, own contract, ONLY while the deadman is inert
# ---------------------------------------------------------------------------
class _TapeAnsweringAdapter:
    """Bawat tape tier ay sumasagot; ang get_execution_bbo ay nagre-record ng kwargs."""

    def __init__(self, *, floor: tuple | None = None) -> None:
        self.execution_bbo_kwargs: list[dict] = []
        self.tier_calls: list[str] = []
        self.floor_calls: list[str] = []
        self._floor = floor

    def _iqfeed_l1_read(self, sym, *, max_age_seconds):
        return L1Read(reason="no_row")

    def _l1_feed_heartbeat_age_s(self):
        return 0.1

    def _tape(self, name):
        self.tier_calls.append(name)
        tick, meta = _iex_tick()
        return tick, meta

    def _massive_sip_execution_bbo(self, pid, max_age):
        return self._tape("massive_sip")

    def _iqfeed_l1_own_clock_execution_bbo(self, pid, max_age):
        return self._tape("l1_own_clock")

    def _iqfeed_trade_embedded_execution_bbo(self, pid, max_age):
        return self._tape("trade_embedded")

    def _iqfeed_depth_execution_bbo(self, pid, max_age):
        return self._tape("depth")

    def _sip_clocked_floor_quote(self, sym):
        self.floor_calls.append(sym)
        return self._floor if self._floor is not None else FLOOR_REFUSED

    def get_execution_bbo(self, product_id, **kwargs):
        self.execution_bbo_kwargs.append(dict(kwargs))
        return None, FreshnessMeta(retrieved_at_utc=NOW, provider_time_utc=None, max_age_seconds=2.0)


def test_5_in_the_regular_session_no_tape_row_is_a_stand_in_for_a_held_decision():
    """RTH: buhay ang deadman => ang floor ay HINDI tinatanong kahit sasagot ito."""
    adapter = _TapeAnsweringAdapter(floor=_floor_tick())
    dec = select_held_bbo(adapter, "PCLA", now=NOW, bounds=BOUNDS, resting_floor_live=True)
    assert dec.tick is None, "walang tape tier ang dapat sumagot habang buhay ang deadman"
    assert adapter.floor_calls == [], "hindi dapat tanungin ang floor sa RTH"
    assert len(adapter.execution_bbo_kwargs) == 1
    assert "allow_stand_in" not in adapter.execution_bbo_kwargs[0]
    assert "stand_in_max_age_seconds" not in adapter.execution_bbo_kwargs[0]
    assert adapter.execution_bbo_kwargs[0]["max_age_seconds"] == pytest.approx(2.0)
    assert adapter.tier_calls == [], "ang 900-s ladder ay hindi kailanman tinatanong ng HELD tick"
    assert _tiers(dec)[-1] == ("sip_clocked_floor", "skipped", "resting_deadman_live")
    assert dec.envelope["bbo_floor_gate"] == {"resting_floor_live": True, "source": "caller"}
    assert dec.counts_toward_halt is True  # heartbeat 0.1 s, walang sumagot: tahimik ang pangalan


def test_5b_outside_the_regular_session_the_floor_answers_labelled_under_its_own_contract():
    """Premarket/after-hours: inert ang deadman (`live_deadman_stop_inert_until_rth`),
    ang runner ang TANGING proteksyon -- kaya ang SIP-clocked row ang sahig, sa
    ilalim ng SARILING kontrata (10 s), suot ang SARILING label mula sa adapter."""
    adapter = _TapeAnsweringAdapter(floor=_floor_tick(age_s=5.2))
    gate = {"resting_floor_live": False, "market_session": "pre", "sole_protection": "runner_software_stop"}
    dec = select_held_bbo(adapter, "PCLA", now=NOW, bounds=BOUNDS, resting_floor_live=False, floor_gate=gate)
    assert dec.tick is not None and dec.tick.bid == 8.84
    assert adapter.floor_calls == ["PCLA"]
    assert adapter.tier_calls == [], "ang floor ay ang adapter floor read, hindi ang ladder"
    env = dec.envelope
    assert env["bbo_answering_tier"] == "sip_clocked_floor"
    assert env["bbo_validity_rule"] == "sip_clocked_floor"
    assert env["bbo_source"] == "massive_ws"
    assert env["bbo_quote_authority"] == FLOOR_AUTHORITY
    assert env["bbo_age_s"] == pytest.approx(5.2)
    assert env["bbo_max_age_s"] == pytest.approx(10.0)
    assert env["bbo_fallback_engaged"] is True
    assert env["bbo_floor_gate"] == gate
    assert env["bbo_tape_row_id"] == 235009576
    assert _tiers(dec) == [
        ("iqfeed_l1", "refused", "l1_no_row"),
        ("alpaca_iex", "refused", "iex_execution_bbo_unavailable"),
        ("sip_clocked_floor", "answered", "sip_clocked_floor"),
    ]
    assert dec.snapshot["quote_authority"] == FLOOR_AUTHORITY
    assert dec.snapshot["validity_rule"] == "sip_clocked_floor"
    assert dec.counts_toward_halt is False, "sumagot ang tick: hindi ito halt signal"


def test_5c_the_floor_refusing_leaves_the_position_blocked_and_receipted():
    adapter = _TapeAnsweringAdapter(floor=FLOOR_REFUSED)
    dec = select_held_bbo(adapter, "PCLA", now=NOW, bounds=BOUNDS, resting_floor_live=False)
    assert dec.tick is None
    assert _tiers(dec)[-1] == ("sip_clocked_floor", "refused", "floor_no_row_within_contract")
    assert dec.envelope["bbo_fallback_chain"][-1]["max_age_s"] == pytest.approx(10.0)
    assert dec.snapshot["floor_refusal"] == "floor_no_row_within_contract"


def test_5d_unknown_gate_is_fail_suspicious_like_deadman_protection_is_live():
    """`resting_floor_live=None` (hindi malaman) => pinapayagan ang floor, gaya ng
    `_deadman_protection_is_live` na nag-uulat ng HINDI buhay kapag hindi maiuri."""
    reads, calls = _reads(l1=L1Read(reason="no_row"), floor=_floor_tick())
    dec = _select(reads)
    assert dec.tick is not None and dec.envelope["bbo_answering_tier"] == "sip_clocked_floor"
    assert calls["floor"] == ["PCLA"]


_FORBIDDEN_TOKENS = ("massive", "trade_embedded", "depth", "massive_snapshot", "iqfeed_depth")


def _identifiers_and_strings(node: ast.AST) -> tuple[set[str], set[str]]:
    idents: set[str] = set()
    strings: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            idents.add(n.id)
        elif isinstance(n, ast.Attribute):
            idents.add(n.attr)
        elif isinstance(n, ast.arg):
            idents.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            idents.add(n.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            strings.add(n.value)
    return idents, strings


def test_6a_held_bbo_module_never_names_a_tape_tier_outside_the_witness():
    """Ang floor tier ay HINDI exception: ang source / basis / authority nito ay
    galing sa payload ng adapter (`_sip_clocked_floor_quote`), hindi sa module."""
    tree = ast.parse(_HELD_BBO_SRC.read_text(encoding="utf-8"))
    # Tanggalin ang mga function na may 'witness' sa pangalan (ang SIP ay saksi lang).
    kept = ast.Module(body=[], type_ignores=[])
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and "witness" in node.name:
            continue
        kept.body.append(node)
    idents, strings = _identifiers_and_strings(kept)
    for tok in _FORBIDDEN_TOKENS:
        bad_i = sorted(i for i in idents if tok in i)
        bad_s = sorted(s[:60] for s in strings if tok in s)
        assert not bad_i, f"identifier na may {tok!r}: {bad_i}"
        assert not bad_s, f"string na may {tok!r}: {bad_s}"
    # `snapshot` ay ang mandated na field ng HeldBboDecision lamang -- hindi bahagi
    # ng mas mahabang pangalan (massive_snapshot / depth_snapshot) at wala sa strings.
    assert all(i == "snapshot" for i in idents if "snapshot" in i)
    assert not [s for s in strings if "snapshot" in s]
    assert FLOOR_AUTHORITY not in strings, "ang label ng floor ay sa adapter nakatira"


def _live_tick_bbo_held_branch(tree: ast.AST) -> ast.If:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_live_tick_bbo":
            for stmt in node.body:
                if isinstance(stmt, ast.If) and "_HELD_LIVE_STATES" in ast.dump(stmt.test):
                    return stmt
    pytest.fail("hindi mahanap ang HELD branch ng _live_tick_bbo")


def test_6b_the_held_branch_has_no_stand_in_and_the_literal_setting_is_gone():
    src = _LIVE_RUNNER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    held = _live_tick_bbo_held_branch(tree)
    kw = {k.arg for n in ast.walk(ast.Module(body=held.body, type_ignores=[]))
          if isinstance(n, ast.Call) for k in n.keywords}
    assert "allow_stand_in" not in kw
    assert "stand_in_max_age_seconds" not in kw
    assert "resting_floor_live" in kw and "floor_gate" in kw, "ang gate ng floor ay dapat ipasa"
    called = {n.func.id for n in ast.walk(ast.Module(body=held.body, type_ignores=[]))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "select_held_bbo" in called
    assert "_held_tick_floor_gate" in called
    assert "_final_entry_bbo" not in called
    assert src.count("chili_momentum_held_stand_in_max_age_seconds") == 0
    # walang numeric literal sa HELD branch (ang 15.0 ay nabanggit lang sa komento)
    consts = {n.value for n in ast.walk(ast.Module(body=held.body, type_ignores=[]))
              if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)}
    assert consts == set(), consts


@pytest.mark.parametrize("session,expect_live", [("pre", False), ("post", False), ("regular", True), ("unknown", False)])
def test_6c_the_held_tick_gates_the_floor_on_deadman_protection_is_live(monkeypatch, session, expect_live):
    """Ang `_live_tick_bbo` ang nagtatanong kung makakaputok ang deadman (session ==
    regular, fail-suspicious) at ipinapasa iyon sa selector kasama ang ebidensya."""
    seen: list[dict] = []
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda _symbol, **_k: session,
    )
    monkeypatch.setattr(LR, "current_bounds", lambda **_k: BOUNDS)
    monkeypatch.setattr(LR, "select_held_bbo", lambda a, pid, **kw: seen.append(kw) or HB.HeldBboDecision(
        tick=None, snapshot={"ok": False, "reason": "held_bbo_unavailable"}, envelope={}, counts_toward_halt=False))
    LR._live_tick_bbo(object(), "PCLA", execution_family="alpaca_spot", state="live_entered")
    assert len(seen) == 1
    assert seen[0]["resting_floor_live"] is expect_live
    gate = seen[0]["floor_gate"]
    assert gate["resting_floor_live"] is expect_live
    assert gate["market_session"] == session
    assert gate["source"] == "_deadman_protection_is_live"
    assert gate["sole_protection"] == ("broker_stop" if expect_live else "runner_software_stop")


# ---------------------------------------------------------------------------
# (7)-(10) delayed L1 -- the witness downgrades the label, it does not veto
# ---------------------------------------------------------------------------
def test_7_delayed_stamp_is_refused_and_iex_is_consulted():
    reads, calls = _reads(l1=_l1(900.3, reason="delayed_stamp", delay_s=900.3), direct=_iex_tick())
    dec = _select(reads)
    assert dec.tick is not None and dec.envelope["bbo_source"] == "iex"
    chain = dec.envelope["bbo_fallback_chain"]
    assert chain[0]["reason"] == "l1_rejected_delayed_stamp"
    assert chain[0]["delay_signature_s"] == pytest.approx(900.3)
    assert dec.envelope["bbo_l1_entitlement_state"] == "delayed_by_stamp"
    assert dec.envelope["bbo_l1_delay_signature_s"] == pytest.approx(900.3)
    assert dec.counts_toward_halt is False
    assert len(calls["direct"]) == 1


def _witness(bid=8.88, at=NOW - timedelta(seconds=6.5)):
    return {"bid": bid, "ask": bid + 0.04, "provider_event_at": at,
            "received_at": at + timedelta(seconds=5.0), "tape_row_id": 235009576,
            "source": "massive_ws"}


def test_8_sip_disagreement_demotes_the_row_behind_iex_in_rth():
    """RTH: pinagdudahan ng saksi ang L1 (700 bps off as-of) -> ang strict IEX ang
    sumasagot, at ang L1 ay `demoted`, hindi `refused`."""
    reads, calls = _reads(l1=_l1(1.0), witness=_witness(bid=8.88),
                          asof={"bid": 8.88 * (1 + 0.07), "ask": 9.6, "tape_row_id": 1},
                          direct=_iex_tick())
    dec = _select(reads, resting_floor_live=True)
    assert dec.tick is not None and dec.tick.bid == 8.90
    assert _tiers(dec) == [
        ("iqfeed_l1", "demoted", "l1_sip_witness_disagreement"),
        ("alpaca_iex", "answered", "iex_direct"),
    ]
    chain = dec.envelope["bbo_fallback_chain"]
    assert chain[0]["witness"]["diff_bps"] == pytest.approx(700.0)
    assert chain[0]["demoted_behind"] == ["alpaca_iex"]
    assert dec.envelope["bbo_l1_entitlement_state"] == "suspect_by_sip_disagreement"
    assert dec.envelope["bbo_sip_witness"]["tape_row_id"] == 235009576
    assert dec.envelope["bbo_answering_tier"] == "alpaca_iex"
    assert len(calls["direct"]) == 1


def test_8b_sip_disagreement_does_not_blind_the_position_when_nothing_fresher_answers():
    """⚠️ ANG REVIEW FINDING. Premarket, gap-down: ang sariwang fenced L1 (1 s) ay
    nagpapakita ng breach; ang saksi ay 5 s na huli at pre-gap ang bid; ang as-of L1
    ay post-gap ang content (observed_at = trade reference, content hanggang 2 s
    mas bago) -> > 66 bps -> DATI: veto -> tick=None -> walang stop ladder, at ang
    deadman ay inert hanggang RTH. NGAYON: ang pinagdudahang row ay sumasagot,
    nakalabel, engaged=True, at tumatakbo ang stop ladder."""
    reads, calls = _reads(l1=_l1(1.0, bid=8.60, ask=8.66), witness=_witness(bid=8.88),
                          asof={"bid": 8.60, "ask": 8.66, "tape_row_id": 1}, floor=_floor_tick())
    dec = _select(reads, resting_floor_live=False)
    assert dec.tick is not None and dec.tick.bid == 8.60, "ang sariwang L1 ang sumagot"
    assert _tiers(dec) == [
        ("iqfeed_l1", "demoted", "l1_sip_witness_disagreement"),
        ("alpaca_iex", "refused", "iex_execution_bbo_unavailable"),
        ("iqfeed_l1", "answered", "l1_suspect_by_sip_witness"),
    ]
    assert calls["floor"] == [], "mas pinipili ang pinagdudahang L1 (1 s) kaysa sa 5-s na huling floor"
    env = dec.envelope
    assert env["bbo_validity_rule"] == "l1_suspect_by_sip_witness"
    assert env["bbo_l1_entitlement_state"] == "suspect_by_sip_disagreement"
    assert env["bbo_fallback_engaged"] is True
    assert env["bbo_answering_tier"] == "iqfeed_l1"
    assert env["bbo_quote_authority"] == AUTHORITY_L1_FENCED
    assert dec.envelope["bbo_fallback_chain"][-1]["demotions"] == ["sip_witness_disagreement"]
    assert dec.envelope["bbo_fallback_chain"][-1]["after"] == ["alpaca_iex"]
    assert dec.counts_toward_halt is False


def test_9_witness_within_band_upgrades_the_entitlement():
    reads, calls = _reads(l1=_l1(1.0), witness=_witness(bid=8.88), asof={"bid": 8.90, "ask": 8.95})
    dec = _select(reads)
    assert dec.tick is not None
    assert dec.envelope["bbo_l1_entitlement_state"] == "realtime_by_stamp_and_sip_witness"
    assert dec.envelope["bbo_sip_witness"]["diff_bps"] == pytest.approx(22.52, abs=0.05)
    assert calls["asof"][0][1] == _witness()["provider_event_at"]


def test_10_no_witness_means_realtime_by_stamp():
    reads, _ = _reads(l1=_l1(1.0), witness=None)
    dec = _select(reads)
    assert dec.tick is not None
    assert dec.envelope["bbo_l1_entitlement_state"] == "realtime_by_stamp"
    assert dec.envelope["bbo_sip_witness"] is None


# ---------------------------------------------------------------------------
# (11)-(14) unchanged-book -- accepted, but behind a live IEX tick
# ---------------------------------------------------------------------------
def test_11_unchanged_book_answers_when_iex_refuses_up_to_the_gap_ceiling():
    reads, calls = _reads(l1=_l1(BOUNDS.l1_fresh_bound_s + 1.0), heartbeat=0.1)
    dec = _select(reads)
    assert dec.tick is not None and dec.tick.bid == 8.88
    assert dec.envelope["bbo_validity_rule"] == "l1_unchanged_book"
    assert dec.envelope["bbo_max_age_s"] == pytest.approx(BOUNDS.l1_gap_ceiling_s)
    assert dec.envelope["bbo_l1_heartbeat_age_s"] == pytest.approx(0.1)
    assert dec.envelope["bbo_fallback_engaged"] is True, "hindi ang unang pili (sariwang L1)"
    assert len(calls["print"]) == 1
    assert calls["print"][0][1] == NOW - timedelta(seconds=BOUNDS.l1_fresh_bound_s + 1.0)
    assert _tiers(dec) == [
        ("iqfeed_l1", "demoted", "l1_unchanged_book"),
        ("alpaca_iex", "refused", "iex_execution_bbo_unavailable"),
        ("iqfeed_l1", "answered", "l1_unchanged_book"),
    ]


def test_11b_a_live_iex_tick_is_the_witness_the_dead_watch_cannot_be():
    """⚠️ REVIEW FINDING: kapag namatay ang per-symbol na L1 watch (bridge silent-hang)
    habang buhay ang global heartbeat, ang quote stream AT ang contradicting-print
    guard ay iisang patay na pinagmulan -- kaya ang isang buhay na IEX tick (<= 2 s)
    ang nananalo laban sa tahimik na libro na hanggang 18.8 s ang edad."""
    reads, calls = _reads(l1=_l1(BOUNDS.l1_fresh_bound_s + 1.0, bid=8.88), heartbeat=0.1, direct=_iex_tick())
    dec = _select(reads)
    assert dec.tick is not None and dec.tick.bid == 8.90, "IEX ang sumagot, hindi ang tahimik na libro"
    assert _tiers(dec) == [
        ("iqfeed_l1", "demoted", "l1_unchanged_book"),
        ("alpaca_iex", "answered", "iex_direct"),
    ]
    assert dec.envelope["bbo_answering_tier"] == "alpaca_iex"
    assert len(calls["print"]) == 1, "ang print guard ay tumakbo pa rin (receipted)"


def test_11c_both_demotions_compose_into_one_rule():
    reads, _ = _reads(l1=_l1(BOUNDS.l1_fresh_bound_s + 1.0), heartbeat=0.1,
                      witness=_witness(bid=8.88), asof={"bid": 9.50, "ask": 9.60})
    dec = _select(reads, resting_floor_live=True)
    assert dec.tick is not None
    assert dec.envelope["bbo_validity_rule"] == "l1_unchanged_book_suspect_by_sip_witness"
    assert _tiers(dec)[0] == ("iqfeed_l1", "demoted", "l1_unchanged_book|l1_sip_witness_disagreement")


def test_12_a_stalled_feed_refuses_the_unchanged_book_without_counting_a_halt():
    reads, _ = _reads(l1=_l1(BOUNDS.l1_fresh_bound_s + 1.0), heartbeat=9.0)
    dec = _select(reads)
    assert dec.tick is None
    assert dec.envelope["bbo_fallback_chain"][0]["reason"] == "l1_feed_stalled"
    assert dec.counts_toward_halt is False


def test_13_a_print_outside_the_book_refuses_it_and_the_print_is_in_the_receipt():
    cp = {"tick_id": 77, "price": 8.70, "size": 300, "observed_at_utc": NOW.isoformat()}
    reads, _ = _reads(l1=_l1(BOUNDS.l1_fresh_bound_s + 1.0), heartbeat=0.1, contradicting_print=cp)
    dec = _select(reads)
    assert dec.tick is None
    assert dec.envelope["bbo_fallback_chain"][0]["reason"] == "l1_contradicted_by_print"
    assert dec.envelope["bbo_contradicting_print"]["price"] == 8.70
    assert dec.counts_toward_halt is False


@pytest.mark.parametrize("heartbeat,expected", [(0.1, True), (None, False), (30.0, False)])
def test_14_beyond_the_gap_ceiling_counts_toward_halt_only_when_the_feed_is_alive(heartbeat, expected):
    reads, _ = _reads(l1=_l1(BOUNDS.l1_gap_ceiling_s + 1.0, reason="stale"), heartbeat=heartbeat)
    dec = _select(reads)
    assert dec.tick is None
    entry = dec.envelope["bbo_fallback_chain"][0]
    assert entry["reason"] == "l1_gap_ceiling_exceeded"
    assert entry["age_s"] == pytest.approx(BOUNDS.l1_gap_ceiling_s + 1.0)
    assert dec.counts_toward_halt is expected


@pytest.mark.parametrize("l1", [L1Read(reason="no_row"), _l1(BOUNDS.l1_gap_ceiling_s + 1.0, reason="stale")])
def test_14b_a_tick_that_answered_never_counts_toward_the_halt_streak(l1):
    """⚠️ REVIEW FINDING: L1 tahimik (no_row / lampas sa gap ceiling) na may buhay
    na heartbeat, pero SUMAGOT ang IEX -- ang bawat spliced na resibo ay nagdadala
    noon ng `counts_toward_halt: True` para sa tick na sumagot. Ngayon: False."""
    reads, _ = _reads(l1=l1, heartbeat=0.1, direct=_iex_tick())
    dec = _select(reads)
    assert dec.tick is not None
    assert dec.counts_toward_halt is False
    assert dec.envelope["counts_toward_halt"] is False
    # at ang parehong harang na WALANG sumagot ay nagbibilang pa rin
    reads, _ = _reads(l1=l1, heartbeat=0.1)
    dec = _select(reads, resting_floor_live=True)
    assert dec.tick is None and dec.counts_toward_halt is True


# ---------------------------------------------------------------------------
# (15)-(16) adapter read
# ---------------------------------------------------------------------------
def _row(**overrides):
    now = datetime.now(timezone.utc)
    reference = now - timedelta(milliseconds=250)
    received = now - timedelta(milliseconds=100)
    values = {
        "id": 42, "bid": 8.88, "ask": 9.00, "mid": 8.94, "spread_bps": 134.2,
        "observed_at": reference.replace(tzinfo=None), "source": "iqfeed_l1",
        "provider_event_at": None, "received_at": received,
        "timestamp_basis": L1_BASIS_FENCED, "bridge_version": PIN,
        "provider_trade_reference_at": reference, "message_type": "Q",
        "bridge_run_id": RUN_ID, "connection_generation": 3,
        "available_at": received + timedelta(milliseconds=550),
    }
    values.update(overrides)
    return tuple(values[k] for k in (
        "id", "bid", "ask", "mid", "spread_bps", "observed_at", "source",
        "provider_event_at", "received_at", "timestamp_basis", "bridge_version",
        "provider_trade_reference_at", "message_type", "bridge_run_id",
        "connection_generation", "available_at",
    ))


def _own_clock_row(**overrides):
    now = datetime.now(timezone.utc)
    prov = now - timedelta(milliseconds=300)
    return _row(
        timestamp_basis=L1_BASIS_OWN_CLOCK, provider_event_at=prov,
        provider_trade_reference_at=None, observed_at=prov.replace(tzinfo=None),
        **overrides,
    )


def _install_row(monkeypatch, row, captured=None, *, pin=PIN):
    captured = captured if captured is not None else {}

    class _Result:
        def fetchone(self):
            return row

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

        def execute(self, stmt, params=None):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return _Result()

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", pin, raising=False)
    return captured


def test_15_iqfeed_l1_read_accepts_both_bases_with_the_bounded_basis_filtered_query(monkeypatch):
    captured = _install_row(monkeypatch, _own_clock_row())
    r = AlpacaSpotAdapter()._iqfeed_l1_read("PCLA", max_age_seconds=18.832)
    assert r.reason is None, r
    assert r.basis == L1_BASIS_OWN_CLOCK
    assert r.event_reference_at == _aware(r.ticker.raw["provider_event_at_utc"])
    assert r.received_at is not None and r.available_at is not None
    assert "timestamp_basis IN" in captured["sql"]
    assert "interval '10 minutes'" in captured["sql"]
    assert "message_type = 'Q'" in captured["sql"]
    assert captured["params"]["s"] == "PCLA"
    assert set(captured["params"]["bases"]) == {L1_BASIS_FENCED, L1_BASIS_OWN_CLOCK}
    assert r.meta.provider_time_utc == r.event_reference_at
    assert r.ticker.raw["event_reference_at_utc"] == r.event_reference_at.isoformat()

    _install_row(monkeypatch, _row())
    r = AlpacaSpotAdapter()._iqfeed_l1_read("PCLA", max_age_seconds=18.832)
    assert r.reason is None and r.basis == L1_BASIS_FENCED
    assert r.event_reference_at == _aware(r.ticker.raw["provider_trade_reference_at_utc"])
    assert r.delay_signature_s == pytest.approx(0.15, abs=0.01)

    _install_row(monkeypatch, _row(bridge_version="iqfeed-l1-exact-print-provenance-v3+sha256:ffffffffffffffff"))
    assert AlpacaSpotAdapter()._iqfeed_l1_read("PCLA", max_age_seconds=18.832).reason == "bridge_build_mismatch"

    _install_row(monkeypatch, _row(), pin="")
    assert AlpacaSpotAdapter()._iqfeed_l1_read("PCLA", max_age_seconds=18.832).reason == "bridge_build_unpinned"
    # own-clock ay hindi nangangailangan ng pin: receipted na degradation
    _install_row(monkeypatch, _own_clock_row(), pin="")
    assert AlpacaSpotAdapter()._iqfeed_l1_read("PCLA", max_age_seconds=18.832).reason is None


def _aware(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


@pytest.mark.parametrize("overrides,reason", [
    ({"bid": 9.10}, "invalid_book"),                                             # ask < bid
    ({"received_at": datetime.now(timezone.utc) + timedelta(seconds=5)}, "delayed_stamp"),
    ({"received_at": datetime.now(timezone.utc) - timedelta(seconds=3)}, "clock_impossible"),
    ({"message_type": "P"}, "provenance_rejected"),
    ({"connection_generation": 0}, "provenance_rejected"),
    ({"provider_event_at": datetime.now(timezone.utc)}, "provenance_rejected"),
])
def test_15b_fenced_row_reasons(monkeypatch, overrides, reason):
    _install_row(monkeypatch, _row(**overrides))
    assert AlpacaSpotAdapter()._iqfeed_l1_read("PCLA", max_age_seconds=18.832).reason == reason


def test_15c_a_stale_row_still_returns_the_ticker_so_the_age_can_be_reported(monkeypatch):
    now = datetime.now(timezone.utc)
    ref = now - timedelta(seconds=25)
    _install_row(monkeypatch, _row(
        observed_at=ref.replace(tzinfo=None), provider_trade_reference_at=ref,
        received_at=ref + timedelta(milliseconds=100),
    ))
    r = AlpacaSpotAdapter()._iqfeed_l1_read("PCLA", max_age_seconds=18.832)
    assert r.reason == "stale"
    assert r.ticker is not None and r.event_reference_at == ref


def test_15d_a_delayed_nyse_row_carries_the_900s_signature(monkeypatch):
    """Kaso (a): IQFeed ay nagpapasa ng exchange stamp ng 15-min delayed data."""
    now = datetime.now(timezone.utc)
    ref = now - timedelta(seconds=900.3)
    _install_row(monkeypatch, _row(
        observed_at=ref.replace(tzinfo=None), provider_trade_reference_at=ref,
        received_at=now - timedelta(milliseconds=100),
    ))
    r = AlpacaSpotAdapter()._iqfeed_l1_read("PCLA", max_age_seconds=18.832)
    assert r.reason == "delayed_stamp"
    assert r.delay_signature_s == pytest.approx(900.2, abs=0.01)


def test_16_the_quote_wrapper_stays_fenced_only_with_no_provider_time(monkeypatch):
    captured = _install_row(monkeypatch, _own_clock_row())
    assert AlpacaSpotAdapter()._iqfeed_l1_quote("PCLA", max_age_seconds=2.0) is None
    assert captured["params"]["bases"] == [L1_BASIS_FENCED]
    _install_row(monkeypatch, _row())
    tick, meta = AlpacaSpotAdapter()._iqfeed_l1_quote("PCLA", max_age_seconds=2.0)
    assert meta.provider_time_utc is None
    assert tick.freshness.provider_time_utc is None
    assert meta.max_age_seconds == pytest.approx(2.0)
    assert tick.raw["provider_event_at_utc"] is None


def test_16b_the_floor_read_wraps_the_sip_quote_under_the_configured_contract(monkeypatch):
    """Ang adapter floor read: ang configured SIP ceiling ang kontrata (hindi 900),
    ang label ay `stand_in_massive_sip`, at ang bawat pagtanggi ay may dahilan."""
    adapter = AlpacaSpotAdapter()
    seen: list[dict] = []
    monkeypatch.setattr(settings, "chili_alpaca_execution_bbo_massive_sip_max_age_seconds", 10.0, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_execution_bbo_massive_sip_fallback_enabled", True, raising=False)

    def _quote(sym, *, max_age_seconds):
        seen.append({"sym": sym, "max_age_seconds": max_age_seconds})
        tick, _payload = _floor_tick()
        # ang adapter ay sumusukat ng edad sa TUNAY na orasan (`_now()`), hindi sa NOW
        real_now = datetime.now(timezone.utc)
        meta = FreshnessMeta(retrieved_at_utc=real_now, provider_time_utc=real_now - timedelta(seconds=5.2),
                             max_age_seconds=max_age_seconds)
        return tick, meta

    monkeypatch.setattr(adapter, "_massive_sip_quote", _quote)
    tick, payload = adapter._sip_clocked_floor_quote("pcla")
    assert tick is not None and payload["ok"] is True
    assert seen == [{"sym": "PCLA", "max_age_seconds": 10.0}]
    assert payload["quote_authority"] == FLOOR_AUTHORITY
    assert payload["source"] == "massive_ws" and payload["timestamp_basis"] == "massive_sip_unix_ms"
    assert payload["max_age_seconds"] == pytest.approx(10.0)
    assert payload["age_seconds"] == pytest.approx(5.2, abs=1.0)
    assert payload["tape_row_id"] == 235009576 and payload["bid"] == 8.84

    monkeypatch.setattr(adapter, "_massive_sip_quote", lambda sym, *, max_age_seconds: None)
    tick, payload = adapter._sip_clocked_floor_quote("PCLA")
    assert tick is None and payload["reason"] == "no_row_within_contract"
    assert payload["max_age_seconds"] == pytest.approx(10.0)

    monkeypatch.setattr(settings, "chili_alpaca_execution_bbo_massive_sip_max_age_seconds", 0.0, raising=False)
    assert adapter._sip_clocked_floor_quote("PCLA")[1]["reason"] == "contract_disabled"
    assert adapter._sip_clocked_floor_quote("BTC-USD")[1]["reason"] == "not_equity"


def test_the_shared_constants_are_one_value():
    from app.services.trading.momentum_neural import live_runner_loop as loop
    from app.services.trading.venue import alpaca_spot as ad

    assert HB.EVENT_TICK_MIN_SPACING_S == loop._EVENT_TICK_MIN_SPACING_S
    assert ad._IQFEED_AUTHORITY_MAX_AGE_S == HB.IQFEED_L1_RECEIVE_REFERENCE_FENCE_S == 2.0
    assert ad._IQFEED_FUTURE_TOLERANCE_S == HB.IQFEED_L1_FUTURE_TOLERANCE_S == 1.0
    assert ad._IQFEED_AUTHORITY_BASIS == L1_BASIS_FENCED


# ---------------------------------------------------------------------------
# (17)-(19) receipts
# ---------------------------------------------------------------------------
def test_17_receipt_helper_returns_every_key_and_the_missing_envelope_shape():
    reads, _ = _reads(l1=_l1(1.0))
    dec = _select(reads)
    le = {"last_held_execution_bbo": {**dec.snapshot, **dec.envelope}}
    out = LR._held_bbo_receipt_fields(le)
    assert set(out) == set(HELD_BBO_RECEIPT_KEYS)
    assert out["bbo_source"] == "iqfeed_l1" and out["bbo_age_s"] == pytest.approx(1.0)
    assert out["bbo_fallback_engaged"] is False
    assert out["bbo_answering_tier"] == "iqfeed_l1"
    assert LR._held_bbo_receipt_fields({}) == {
        "bbo_source": None, "bbo_age_s": None, "bbo_fallback_engaged": None,
        "bbo_receipt": "no_held_bbo_envelope",
    }
    assert LR._held_bbo_receipt_fields(None)["bbo_receipt"] == "no_held_bbo_envelope"
    # ang lumang (pre-build-B) envelope na walang selector version ay "walang envelope"
    assert LR._held_bbo_receipt_fields({"last_held_execution_bbo": {"ok": True}})["bbo_receipt"] == "no_held_bbo_envelope"


def test_17b_a_receipt_emitted_before_this_ticks_read_says_which_envelope_it_binds():
    """⚠️ REVIEW FINDING: ang quote-independent flatten ay nag-e-emit ng
    `live_exit_submitted` BAGO ang `_live_tick_bbo` ng tick, kaya ang envelope ay
    mula sa NAKARAANG tick. Ang resibo ay nagsasabi niyon (`bbo_binding`) at kung
    gaano katanda ang envelope (`bbo_envelope_age_s`)."""
    reads, _ = _reads(l1=_l1(1.0))
    dec = _select(reads)
    le = {"last_held_execution_bbo": {**dec.snapshot, **dec.envelope}}
    out = LR._held_bbo_receipt_fields(le, binding="last_held_tick_envelope", now=NOW + timedelta(seconds=2.4))
    assert out["bbo_binding"] == "last_held_tick_envelope"
    assert out["bbo_envelope_age_s"] == pytest.approx(2.4)
    assert set(out) == set(HELD_BBO_RECEIPT_KEYS) | {"bbo_binding", "bbo_envelope_age_s"}
    # walang envelope: dala pa rin ang binding, None ang edad
    out = LR._held_bbo_receipt_fields({}, binding="last_held_tick_envelope", now=NOW)
    assert out["bbo_receipt"] == "no_held_bbo_envelope" and out["bbo_binding"] == "last_held_tick_envelope"
    assert out["bbo_envelope_age_s"] is None
    # ang site mismo ay pumapasa ng binding
    tree = ast.parse(_LIVE_RUNNER_SRC.read_text(encoding="utf-8"))
    for c in _emit_calls(tree, "live_exit_submitted"):
        payload = c.args[3]
        keys = [k.value if isinstance(k, ast.Constant) else None for k in payload.keys]
        val = payload.values[keys.index("decision_bbo")]
        assert isinstance(val, ast.Call) and val.func.id == "_held_bbo_receipt_fields"
        assert {k.arg for k in val.keywords} >= {"binding", "now"}, "ang flatten receipt ay dapat magsabi ng binding nito"
        assert "quote_independent" in keys


_RECEIPTS_WITH_HELD_BID = (
    "live_bailout",
    "live_opinion_exit_armed",
    "live_momentum_break_exit",
    "live_burst_window_exit",
    "live_tape_accel_reversal_exit",
    "stop_breach_pending_confirm",
    "live_partial_exit",
    # review fix: ang mga HELD-state hold/ratchet receipt mula sa parehong bid
    "live_trail_ratchet",
    "live_measured_move_exit",
    "live_ofi_exhaustion_lock",
    "live_sell_into_strength",
    "live_trailing_armed",
)


def _emit_calls(tree: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_emit"
            and len(node.args) >= 4
            and isinstance(node.args[2], ast.Constant)
            and node.args[2].value == name
        ):
            out.append(node)
    return out


def _has_receipt_splice(payload: ast.AST) -> bool:
    if not isinstance(payload, ast.Dict):
        return False
    for k, v in zip(payload.keys, payload.values):
        if k is None and isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
                and v.func.id == "_held_bbo_receipt_fields":
            return True
    return False


def test_18_every_receipt_that_reads_the_held_bid_splices_the_bbo_fields():
    tree = ast.parse(_LIVE_RUNNER_SRC.read_text(encoding="utf-8"))
    for name in _RECEIPTS_WITH_HELD_BID:
        calls = _emit_calls(tree, name)
        assert calls, f"walang _emit para sa {name}"
        missing = [c.lineno for c in calls if not _has_receipt_splice(c.args[3])]
        assert not missing, f"{name}: walang **_held_bbo_receipt_fields(le) sa linya {missing}"
    assert len(_emit_calls(tree, "live_bailout")) == 9, "bagong bailout site na walang bbo fields?"
    assert len(_emit_calls(tree, "live_trailing_armed")) == 2
    submitted = _emit_calls(tree, "live_exit_submitted")
    assert submitted
    for c in submitted:
        payload = c.args[3]
        assert isinstance(payload, ast.Dict)
        keys = {k.value for k in payload.keys if isinstance(k, ast.Constant)}
        assert "decision_bbo" in keys
        val = payload.values[[k.value if isinstance(k, ast.Constant) else None for k in payload.keys].index("decision_bbo")]
        assert isinstance(val, ast.Call) and val.func.id == "_held_bbo_receipt_fields"
    # ang blocked receipt ay nagsa-spread ng envelope
    blocked = _emit_calls(tree, "live_held_execution_bbo_blocked")
    assert blocked and any(k is None for k in blocked[0].args[3].keys)


def test_18b_bbo_fallback_engaged_has_one_meaning_at_the_pricing_sites():
    """⚠️ REVIEW FINDING: ang `execution_bbo` ng pricing receipt ay may
    `bbo_fallback_engaged` ng selector (False kapag sariwang L1) habang ang
    top-level ay True -- dalawang halaga sa iisang susi. Ngayon: ang top-level na
    tanong ('umalis ba ang PRESYO sa strict IEX read') ay may sariling susi."""
    src = _LIVE_RUNNER_SRC.read_text(encoding="utf-8")
    assert src.count('"bbo_fallback_engaged": True') == 0, "walang site ang nagsasapawan ng halaga ng selector"
    tree = ast.parse(src)
    sites = _emit_calls(tree, "live_exit_stand_in_pricing") + _emit_calls(tree, "live_emergency_exit_stand_in_pricing")
    assert len(sites) == 4, "dalawang site x (selector, ladder)"
    for c in sites:
        keys = {k.value for k in c.args[3].keys if isinstance(k, ast.Constant)}
        assert "exit_pricing_stand_in_engaged" in keys and "bbo_fallback_tier" in keys
        assert "bbo_fallback_engaged" not in keys


def test_18c_all_four_protective_pricing_sites_ask_the_selector_before_the_ladder():
    """⚠️ REVIEW FINDING: ang captured-paper literal marketability check
    (`_literal_exit_bbo_failure`) ay naiwan sa SIP-first 900-s ladder kahit ang
    docstring ay nagsasabing APAT ang protective site at ang two-phase dispatcher
    ang buhay na driver. Bawat isa sa apat ay tumatawag ng
    `_held_selector_exit_pricing` BAGO ang `_final_entry_bbo(allow_stand_in=True)`."""
    src = _LIVE_RUNNER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert src.count("_held_selector_exit_pricing(adapter,") == 4, "apat na protective pricing site"
    assert src.count("_held_selector_l1_only") == 0

    def _calls_in(fn: ast.FunctionDef) -> list[tuple[int, str]]:
        out = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in ("_held_selector_exit_pricing", "_final_entry_bbo"):
                stand_in = any(k.arg == "allow_stand_in" for k in n.keywords)
                if n.func.id == "_final_entry_bbo" and not stand_in:
                    continue
                out.append((n.lineno, n.func.id))
        return sorted(out)

    literal = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_literal_exit_bbo_failure"]
    assert len(literal) == 1
    calls = _calls_in(literal[0])
    assert [c[1] for c in calls] == ["_held_selector_exit_pricing", "_final_entry_bbo"], calls
    # ang selector na tawag ay naka-gate sa stop-class (> 2.0 s), gaya ng ladder
    start = src.index("def _literal_exit_bbo_failure")
    seg = src[start:src.index("_held_selector_exit_pricing(adapter, request.symbol)", start)]
    assert "if _req_max_age > 2.0:" in seg


def test_19_the_accept_snapshot_is_a_superset_of_final_entry_bbo_keys():
    tick, _snap = _iex_tick()

    class _Stub:
        def get_execution_bbo(self, pid, **kw):
            return tick, tick.freshness

    with LR.replay_clock(NOW.replace(tzinfo=None)):
        _t, direct_snapshot = LR._final_entry_bbo(_Stub(), "PCLA", max_age_seconds=2.0)
    assert direct_snapshot["ok"] is True, direct_snapshot
    reads, _ = _reads(l1=_l1(1.0))
    dec = _select(reads)
    assert set(direct_snapshot) <= set(dec.snapshot), set(direct_snapshot) - set(dec.snapshot)
    # ang floor snapshot ay parehong hugis
    reads, _ = _reads(l1=L1Read(reason="no_row"), floor=_floor_tick())
    dec = _select(reads, resting_floor_live=False)
    assert dec.envelope["bbo_answering_tier"] == "sip_clocked_floor"
    assert set(direct_snapshot) <= set(dec.snapshot), set(direct_snapshot) - set(dec.snapshot)


# ---------------------------------------------------------------------------
# (20)-(23) derived bounds
# ---------------------------------------------------------------------------
class _FakeBoundsSession:
    def __init__(self, rows: list, *, raise_on: int | None = None, raise_all: bool = False):
        self.rows = list(rows)
        self.raise_on = raise_on
        self.raise_all = raise_all
        self.sql: list[str] = []
        self.rolled_back = False
        self._i = 0

    def __enter__(self):
        if self.raise_all:
            raise RuntimeError("no db")
        return self

    def __exit__(self, *_a):
        return None

    def execute(self, stmt, params=None):
        text = str(stmt)
        self.sql.append(text)
        if text.startswith("SET LOCAL"):
            return SimpleNamespace(fetchone=lambda: None)
        idx = self._i
        self._i += 1
        if self.raise_on is not None and idx == self.raise_on:
            raise RuntimeError("statement timeout")
        row = self.rows[idx]
        return SimpleNamespace(fetchone=lambda: row)

    def rollback(self):
        self.rolled_back = True


# A, B, D, A2 -- ang pagkakasunod ng `_BOUND_QUERIES`
_RUNTIME_ROWS = [(2000, 2.917), (5000, 18.832), (1356, 66.22), (30937, 2.337)]


def test_20_runtime_derivation_binds_the_measured_percentiles():
    fake = _FakeBoundsSession(_RUNTIME_ROWS)
    b = derive_held_bbo_bounds(lambda: fake, now=NOW)
    assert b.l1_fresh_bound_s == pytest.approx(4.917)
    assert b.l1_own_clock_fresh_bound_s == pytest.approx(4.337)
    assert b.l1_gap_ceiling_s == pytest.approx(18.832)
    assert b.sip_disagree_band_bps == pytest.approx(66.22)
    assert b.heartbeat_bound_s == pytest.approx(4.917)
    assert b.delay_stamp_threshold_s == 2.0
    d = b.derivation
    assert d["l1_fresh_bound_s"]["source"] == "runtime"
    assert d["l1_fresh_bound_s"]["n"] == 2000 and d["l1_fresh_bound_s"]["n_min"] == 1000
    assert d["l1_fresh_bound_s"]["percentile"] == 0.999
    assert "available_at - provider_trade_reference_at" in d["l1_fresh_bound_s"]["distribution"]
    assert d["l1_own_clock_fresh_bound_s"]["source"] == "runtime"
    assert d["l1_own_clock_fresh_bound_s"]["n"] == 30937 and d["l1_own_clock_fresh_bound_s"]["n_min"] == 1000
    assert "available_at - provider_event_at" in d["l1_own_clock_fresh_bound_s"]["distribution"]
    assert d["l1_gap_ceiling_s"]["n"] == 5000 and d["l1_gap_ceiling_s"]["n_min"] == 100
    assert d["l1_gap_ceiling_s"]["percentile"] == 0.99 and d["l1_gap_ceiling_s"]["window_min"] == 10
    assert d["sip_disagree_band_bps"]["n"] == 1356 and d["sip_disagree_band_bps"]["window_min"] == 5
    assert d["delay_stamp_threshold_s"]["value"] == 2.0
    assert d["l1_fresh_bound_s"]["measured_at_utc"] == NOW.isoformat()
    assert fake.sql[0].startswith("SET LOCAL statement_timeout = 4920")
    assert "percentile_cont(0.999)" in fake.sql[1] and L1_BASIS_FENCED in fake.sql[1]
    assert "LAG(observed_at)" in fake.sql[2] and "percentile_cont(0.99)" in fake.sql[2]
    assert "LATERAL" in fake.sql[3] and "interval '5 minutes'" in fake.sql[3] and "LIMIT 3000" in fake.sql[3]
    assert "percentile_cont(0.999)" in fake.sql[4] and L1_BASIS_OWN_CLOCK in fake.sql[4]
    assert "available_at - provider_event_at" in fake.sql[4]
    assert fake.rolled_back


def test_20b_carried_constants_are_in_the_bounds_receipt():
    """⚠️ REVIEW FINDING: IQFEED_L1_FUTURE_TOLERANCE_S at ang strict IEX ceiling ay
    ginagamit ng selector pero wala sa `bbo_bounds`. Ngayon nakatatak."""
    b = HB.fallback_bounds(now=NOW)
    d = b.derivation
    assert d["l1_future_tolerance_s"] == {
        **d["l1_future_tolerance_s"], "value": 1.0, "source": "constant_bridge_fence", "n": None, "percentile": None,
    }
    assert d["iex_direct_max_age_s"]["value"] == pytest.approx(HB._iex_direct_max_age_s())
    assert d["iex_direct_max_age_s"]["source"] == "constant_strict_iex_ceiling"
    assert "chili_momentum_entry_bbo_max_age_seconds" in d["iex_direct_max_age_s"]["distribution"]
    assert d["l1_own_clock_fresh_bound_s"]["value"] == pytest.approx(4.337)
    assert d["l1_own_clock_fresh_bound_s"]["source"] == "measured_fallback_20260910T1913Z"
    assert b.l1_own_clock_fresh_bound_s == pytest.approx(4.337)


def test_20c_an_own_clock_row_is_judged_on_its_own_fresh_bound():
    """⚠️ REVIEW FINDING: ang fenced bound (2.917 + 2.0) ay inilalapat noon sa own-clock
    row na may sariling lag (A2 p99.9 2.337) -- tinatanggap bilang 'fresh' nang mas
    matagal kaysa sa sarili nitong distribusyon. Ngayon: 4.5 s ay fresh sa fenced,
    unchanged-book (demoted) sa own-clock."""
    reads, _ = _reads(l1=_l1(4.5, basis=L1_BASIS_FENCED), heartbeat=0.1)
    dec = _select(reads)
    assert dec.envelope["bbo_validity_rule"] == "l1_fresh"
    assert dec.envelope["bbo_fallback_chain"][0]["fresh_bound_s"] == pytest.approx(4.917)
    reads, calls = _reads(l1=_l1(4.5, basis=L1_BASIS_OWN_CLOCK), heartbeat=0.1)
    dec = _select(reads)
    assert dec.envelope["bbo_validity_rule"] == "l1_unchanged_book"
    assert dec.envelope["bbo_fallback_chain"][0]["fresh_bound_s"] == pytest.approx(4.337)
    assert len(calls["print"]) == 1, "ang unchanged-book na tuntunin ang tumakbo (print guard)"
    # walang sariling sukat (lumang caller): ang fenced bound ang binding, hindi sumasabog
    old = HeldBboBounds(l1_fresh_bound_s=4.917, l1_gap_ceiling_s=18.832, heartbeat_bound_s=4.917,
                        sip_disagree_band_bps=66.22, delay_stamp_threshold_s=2.0)
    reads, _ = _reads(l1=_l1(4.5, basis=L1_BASIS_OWN_CLOCK), heartbeat=0.1)
    dec = select_held_bbo(object(), "PCLA", now=NOW, bounds=old, reads=reads)
    assert dec.envelope["bbo_fallback_chain"][0]["fresh_bound_s"] == pytest.approx(4.917)


def test_21_below_n_min_or_a_raising_session_falls_back_to_the_tagged_constants():
    fake = _FakeBoundsSession([(999, 2.0), (5000, 18.832), (99, 10.0), (500, 2.0)])
    b = derive_held_bbo_bounds(lambda: fake, now=NOW)
    assert b.l1_fresh_bound_s == pytest.approx(4.917)
    assert b.derivation["l1_fresh_bound_s"]["source"] == "measured_fallback_20260910T1740Z"
    assert b.derivation["l1_fresh_bound_s"]["n"] == 999
    assert b.l1_gap_ceiling_s == pytest.approx(18.832)
    assert b.derivation["l1_gap_ceiling_s"]["source"] == "runtime"
    assert b.sip_disagree_band_bps == pytest.approx(66.22)
    assert b.derivation["sip_disagree_band_bps"]["source"] == "measured_fallback_20260910T1740Z"
    assert b.l1_own_clock_fresh_bound_s == pytest.approx(4.337)
    assert b.derivation["l1_own_clock_fresh_bound_s"]["source"] == "measured_fallback_20260910T1913Z"
    assert b.derivation["l1_own_clock_fresh_bound_s"]["n"] == 500

    # isang query ang sumabog (statement timeout) -> ang bound na iyon lang ang fallback
    fake = _FakeBoundsSession([(2000, 2.5), (5000, 17.0), (1356, 50.0), (30937, 2.0)], raise_on=1)
    b = derive_held_bbo_bounds(lambda: fake, now=NOW)
    assert b.derivation["l1_gap_ceiling_s"]["source"] == "measured_fallback_20260910T1740Z"
    assert b.l1_gap_ceiling_s == pytest.approx(18.832)
    assert b.derivation["l1_fresh_bound_s"]["source"] == "runtime"

    # walang DB man lang
    b = derive_held_bbo_bounds(lambda: _FakeBoundsSession([], raise_all=True), now=NOW)
    assert all(v["source"].startswith("measured_fallback_")
               for k, v in b.derivation.items() if not v["source"].startswith("constant_"))


def test_22_current_bounds_never_derives_inline_and_caches_for_60s(monkeypatch):
    """⚠️ REVIEW FINDING: ang unang `current_bounds()` ng proseso ay nagde-derive
    inline (hanggang 4.92 s) -- at sa `tick_live_session` ang unang tumatawag
    pagkatapos ng restart ay maaaring ang EMERGENCY flatten. Ngayon: fallback agad,
    background thread ang nagde-derive; `allow_derive=False` = walang thread man lang."""
    HB.reset_bounds_cache()
    derived = []
    spawned = []
    monkeypatch.setattr(HB, "_derive_with_session_local", lambda now: derived.append(now) or HB.fallback_bounds(now=NOW))
    monkeypatch.setattr(HB, "_start_background_refresh", lambda: spawned.append(1))
    try:
        # walang cache: fallback agad + isang background spawn, WALANG inline derive
        a = HB.current_bounds()
        assert a.derivation["l1_fresh_bound_s"]["source"] == "measured_fallback_20260910T1740Z"
        assert spawned == [1] and derived == []
        # ikalawang tawag habang nagre-refresh: walang bagong spawn
        HB.current_bounds()
        assert spawned == [1]
        # natapos ang background refresh -> naka-cache; ang susunod ay ang cache
        HB._refresh_in_background()
        assert derived == [None]
        b = HB.current_bounds()
        c = HB.current_bounds()
        assert b is c and b is HB._BOUNDS_CACHE["bounds"]
        assert spawned == [1]
        # lumipas ang TTL: ibinabalik agad ang naka-cache, background refresh lang
        with HB._BOUNDS_LOCK:
            HB._BOUNDS_CACHE["at_monotonic"] -= HB._HELD_BBO_BOUNDS_TTL_S + 1
        d = HB.current_bounds()
        assert d is b and spawned == [1, 1] and derived == [None]
        # allow_derive=False sa walang cache: fallback, walang spawn
        HB.reset_bounds_cache()
        e = HB.current_bounds(allow_derive=False)
        assert e.derivation["l1_fresh_bound_s"]["source"] == "measured_fallback_20260910T1740Z"
        assert spawned == [1, 1] and derived == [None]
        # ang derivation ay sumabog sa thread -> walang exception, fallback pa rin
        HB.reset_bounds_cache()
        monkeypatch.setattr(HB, "_derive_with_session_local", lambda now: (_ for _ in ()).throw(RuntimeError("boom")))
        HB.current_bounds()
        HB._refresh_in_background()
        assert HB._BOUNDS_CACHE["bounds"] is None and HB._BOUNDS_CACHE["refreshing"] is False
        f = HB.current_bounds()
        assert f.l1_fresh_bound_s == pytest.approx(4.917)
    finally:
        HB.reset_bounds_cache()


def test_22b_the_tick_path_does_not_derive_for_an_adapter_without_an_l1_reader(monkeypatch):
    """Replay `MockBrokerAdapter` / bench fakes: walang `_iqfeed_l1_read`, kaya walang
    DB read at walang daemon thread ang HELD tick -- `allow_derive=False`."""
    from app.services.trading.momentum_neural.replay_mock_broker import MockBrokerAdapter

    assert HB.bounds_derivable(object()) is False
    assert HB.bounds_derivable(AlpacaSpotAdapter()) is True
    assert not callable(getattr(MockBrokerAdapter, "_iqfeed_l1_read", None))
    seen: list[dict] = []
    monkeypatch.setattr(LR, "current_bounds", lambda **kw: seen.append(kw) or BOUNDS)
    monkeypatch.setattr(LR, "select_held_bbo", lambda a, pid, **kw: HB.HeldBboDecision(
        tick=None, snapshot={"ok": False, "reason": "held_bbo_unavailable"}, envelope={}, counts_toward_halt=False))
    LR._live_tick_bbo(object(), "PCLA", execution_family="alpaca_spot", state="live_entered", resting_floor_live=True)
    assert seen == [{"allow_derive": False}]
    seen.clear()
    LR._held_selector_exit_pricing(object(), "PCLA")
    assert seen == [{"allow_derive": False}]


# ---------------------------------------------------------------------------
# (24)-(25) wiring — DB (chili_psv_test)
# ---------------------------------------------------------------------------
def _envelope(counts: bool) -> dict:
    reads, _ = _reads(l1=L1Read(reason="no_row"), heartbeat=(0.1 if counts else None))
    dec = _select(reads, resting_floor_live=True)
    assert dec.tick is None and dec.counts_toward_halt is counts
    return {**dec.snapshot, **dec.envelope, "counts_toward_halt": dec.counts_toward_halt}


@pytest.fixture
def _wired(monkeypatch):
    from tests.test_momentum_emergency_exit_recovery import TEST_ALPACA_ACCOUNT_ID

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID, raising=False)
    monkeypatch.setattr(LR, "_venue_broker_connected", lambda _family: True)
    monkeypatch.setattr(LR, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(LR, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(LR, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(LR, "_finalize_live_decision_after_exit", lambda *a, **k: None)
    monkeypatch.setattr(LR, "_record_live_exit_intent_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda _symbol, **_k: "regular",
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(LR, "_emit", lambda _db, _sess, et, payload: events.append((et, payload)))
    return events


@pytest.mark.parametrize("counts", [True, False])
def test_24_a_blocked_held_tick_feeds_the_halt_streak_only_when_the_selector_says_so(db, monkeypatch, _wired, counts):
    from tests.test_momentum_emergency_exit_recovery import (
        _ScriptedAlpaca, _ensure_retained_entry_owner, _seed_session,
    )

    sess = _seed_session(db, symbol="PCLA")
    _ensure_retained_entry_owner(db, sess)
    env = _envelope(counts)
    monkeypatch.setattr(LR, "_live_tick_bbo", lambda *a, **k: (None, None, dict(env)))
    registered = []
    monkeypatch.setattr(LR, "_register_stale_quote_tick", lambda *a, **k: registered.append(1))
    LR._reconcile_counters.pop(int(sess.id), None)
    out = LR.tick_live_session(db, int(sess.id), adapter_factory=lambda: _ScriptedAlpaca(positions=[100.0]))
    db.commit()
    assert out.get("ok") is not None
    assert len(registered) == (1 if counts else 0)
    blocked = [p for et, p in _wired if et == "live_held_execution_bbo_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["phase"] == "held"
    assert blocked[0]["reason"] == "held_bbo_unavailable"
    assert blocked[0]["bbo_fallback_chain"][0]["reason"] == "l1_no_row"
    assert blocked[0]["bbo_fallback_chain"][-1]["reason"] == "resting_deadman_live"
    assert blocked[0]["counts_toward_halt"] is counts
    assert "bbo_bounds" in blocked[0]
    db.refresh(sess)
    assert sess.risk_snapshot_json["momentum_live_execution"]["last_held_execution_bbo"]["bbo_selector_version"] == "held_bbo_v1"


def _pricing_session(db):
    from tests.test_momentum_emergency_exit_recovery import _ensure_retained_entry_owner, _seed_session

    symbol = "PCLA"
    sess = _seed_session(db, symbol=symbol, quantity=10.0, avg_entry_price=10.0)
    le = {"side_long": True, "position": {"product_id": symbol, "side": "long",
                                          "quantity": 10.0, "avg_entry_price": 10.0}}
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot["momentum_live_execution"] = le
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    _ensure_retained_entry_owner(db, sess)
    return sess, le, symbol


def _wire_pricing(monkeypatch, *, reads, order: list[str], ladder_answers: bool = True):
    feb_calls: list[dict] = []

    def _fake_final_entry_bbo(_adapter, _pid, **kw):
        feb_calls.append(dict(kw))
        order.append("ladder" if kw.get("allow_stand_in") else "strict")
        if kw.get("allow_stand_in"):
            assert kw.get("stand_in_max_age_seconds") == pytest.approx(900.0)
            return _iex_tick() if ladder_answers else (None, {"ok": False, "reason": "execution_bbo_unavailable"})
        return None, {"ok": False, "reason": "execution_bbo_unavailable"}

    real_select = HB.select_held_bbo

    def _fake_select(adapter_, pid, **kw):
        order.append("selector")
        assert kw.get("tiers") == EXIT_PRICING_TIERS == ("iqfeed_l1", "sip_clocked_floor")
        assert kw.get("resting_floor_live") is False, "ang pricing ng protective exit ay laging may floor"
        assert kw.get("floor_gate", {}).get("source") == "protective_exit_pricing"
        return real_select(adapter_, pid, now=NOW, bounds=BOUNDS, reads=reads, tiers=kw["tiers"],
                           resting_floor_live=kw["resting_floor_live"], floor_gate=kw.get("floor_gate"))

    monkeypatch.setattr(LR, "_final_entry_bbo", _fake_final_entry_bbo)
    monkeypatch.setattr(LR, "select_held_bbo", _fake_select)
    monkeypatch.setattr(LR, "current_bounds", lambda **_k: BOUNDS)
    return feb_calls


def test_25_exit_pricing_asks_the_selector_before_the_900s_ladder(db, monkeypatch, _wired):
    from tests.test_momentum_emergency_exit_recovery import _ScriptedAlpaca

    sess, le, symbol = _pricing_session(db)
    adapter = _ScriptedAlpaca(positions=[10.0])
    order: list[str] = []
    reads, _ = _reads(l1=_l1(1.06), floor=_floor_tick())
    feb_calls = _wire_pricing(monkeypatch, reads=reads, order=order)
    LR._submit_live_market_exit(
        db, sess, adapter, le=le, product_id=symbol, quantity=10.0,
        client_order_id="held-l1-exit-cid", reason="stop",
        bid=9.95, ask=9.97, mid=9.96,
    )
    assert order[:2] == ["strict", "selector"], order
    assert "ladder" not in order, "ang 900-s ladder ay hindi dapat tumakbo kapag sumagot ang L1"
    assert all("allow_stand_in" not in c for c in feb_calls)
    pricing = [p for et, p in _wired if et == "live_exit_stand_in_pricing"]
    assert len(pricing) == 1
    assert pricing[0]["exit_pricing_stand_in_engaged"] is True
    assert pricing[0]["bbo_fallback_tier"] == "iqfeed_l1"
    assert "bbo_fallback_engaged" not in pricing[0], "isang kahulugan lang: nasa execution_bbo"
    assert pricing[0]["execution_bbo"]["bbo_fallback_engaged"] is False
    assert pricing[0]["execution_bbo"]["pricing_tier"] == "held_selector_iqfeed_l1"
    assert pricing[0]["execution_bbo"]["quote_authority"] == AUTHORITY_L1_FENCED
    assert pricing[0]["execution_bbo"]["bbo_validity_rule"] == "l1_fresh"
    assert pricing[0]["execution_bbo"]["bbo_floor_gate"]["source"] == "protective_exit_pricing"
    assert le["exit_final_bbo"]["bid"] == 8.88


def test_25b_when_l1_and_the_floor_refuse_the_legacy_ladder_runs_and_is_receipted(db, monkeypatch, _wired):
    from tests.test_momentum_emergency_exit_recovery import _ScriptedAlpaca

    sess, le, symbol = _pricing_session(db)
    adapter = _ScriptedAlpaca(positions=[10.0])
    order: list[str] = []
    reads, _ = _reads(l1=L1Read(reason="no_row"), heartbeat=0.1, floor=FLOOR_REFUSED)
    _wire_pricing(monkeypatch, reads=reads, order=order)
    LR._submit_live_market_exit(
        db, sess, adapter, le=le, product_id=symbol, quantity=10.0,
        client_order_id="held-l1-exit-cid-2", reason="stop",
        bid=9.95, ask=9.97, mid=9.96,
    )
    assert order == ["strict", "selector", "ladder"], order
    pricing = [p for et, p in _wired if et == "live_exit_stand_in_pricing"]
    assert len(pricing) == 1
    assert pricing[0]["bbo_fallback_tier"] == "legacy_stand_in_ladder_900s"
    assert pricing[0]["exit_pricing_stand_in_engaged"] is True
    assert [(e["tier"], e["reason"]) for e in pricing[0]["held_selector_chain"]] == [
        ("iqfeed_l1", "l1_no_row"),
        ("sip_clocked_floor", "floor_no_row_within_contract"),
    ], "ang ladder ay umaabot LAMANG kapag L1 at ang SIP floor ay parehong tumanggi"


def test_25c_when_l1_refuses_the_floor_prices_the_exit_and_the_ladder_never_runs(db, monkeypatch, _wired):
    """⚠️ REVIEW FINDING: ang SIP-first 900-s ladder ay tumatakbo noon sa sandaling
    tumanggi ang L1. Ngayon ang SIP-clocked row sa ilalim ng SARILING kontrata (10 s)
    ang pumepresyo, naka-label, at ang ladder (SIP-first, 900 s) ay hindi tumatakbo."""
    from tests.test_momentum_emergency_exit_recovery import _ScriptedAlpaca

    sess, le, symbol = _pricing_session(db)
    adapter = _ScriptedAlpaca(positions=[10.0])
    order: list[str] = []
    reads, calls = _reads(l1=L1Read(reason="no_row"), heartbeat=0.1, floor=_floor_tick(age_s=6.58))
    _wire_pricing(monkeypatch, reads=reads, order=order)
    LR._submit_live_market_exit(
        db, sess, adapter, le=le, product_id=symbol, quantity=10.0,
        client_order_id="held-l1-exit-cid-3", reason="stop",
        bid=9.95, ask=9.97, mid=9.96,
    )
    assert order == ["strict", "selector"], order
    assert calls["floor"] == ["PCLA"]
    pricing = [p for et, p in _wired if et == "live_exit_stand_in_pricing"]
    assert len(pricing) == 1
    assert pricing[0]["bbo_fallback_tier"] == "sip_clocked_floor"
    xb = pricing[0]["execution_bbo"]
    assert xb["pricing_tier"] == "held_selector_sip_clocked_floor"
    assert xb["quote_authority"] == FLOOR_AUTHORITY
    assert xb["bbo_source"] == "massive_ws" and xb["bbo_age_s"] == pytest.approx(6.58)
    assert xb["bbo_max_age_s"] == pytest.approx(10.0), "sariling kontrata, hindi 900"
    assert xb["bbo_fallback_engaged"] is True
    assert le["exit_final_bbo"]["bid"] == 8.84
