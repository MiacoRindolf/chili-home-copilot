"""[48] BUILD B — ang HELD decision tick ay nagbabasa ng IQFeed L1 muna, strict IEX
pangalawa, at HINDI KAILANMAN ng isang Massive row.

ANG PANGYAYARI (PCLA 21592, 2026-09-10 13:41:05.80Z). Ang bailout bid 8.96 ay galing
sa SIP-clocked massive_ws row 235009576 -- provider 13:40:59.221, received
13:41:04.236, 6.58 s na luma -- na pumasa sa 10-s SIP ceiling dahil ang SIP tier
ang UNANG tinatanong ng stand-in ladder; ang fenced IQFeed L1 row (8.88/9.00) ay
1.06 s LANG ang edad sa parehong sandali. Ang exit ay ipinresyo 13:41:12.128 off
sa own-clock row 234991296 na 33.875 s ang edad (cap 900) -> fill 8.88.

Sinusubok dito (fakes, walang DB maliban sa dalawang wiring test sa dulo):
  * source order at "snapshot-never" (§4): L1 muna, IEX pangalawa, WALANG tier 3
  * delayed-L1 (kaso a: stamp; kaso b: SIP witness)
  * ang `l1_unchanged_book` na tuntunin (heartbeat + contradicting print)
  * ang adapter read (`_iqfeed_l1_read`) at ang wrapper (`_iqfeed_l1_quote`)
  * ang resibo (§7): bawat exit/hold receipt ay may bbo_source / bbo_age_s /
    bbo_fallback_engaged (AST pin, pattern ng test_stand_in_basis_allowlist_parity)
  * ang hinangong hangganan (§5): runtime vs tagged fallback, n_min, TTL cache
  * wiring: blocked tick -> halt streak; exit pricing -> selector BAGO ang 900-s ladder

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
    derivation={"l1_fresh_bound_s": {"value": 4.917, "source": "test"}},
)


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
) -> tuple[HeldBboReads, dict[str, list]]:
    calls: dict[str, list] = {"l1": [], "hb": [], "print": [], "witness": [], "asof": [], "direct": []}

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

    return HeldBboReads(
        l1_read=_l1_read, heartbeat_age_s=_hb, contradicting_print=_print,
        sip_witness=_witness, l1_asof=_asof, direct=_direct,
    ), calls


def _select(reads, **kw):
    return select_held_bbo(object(), "PCLA", now=NOW, bounds=BOUNDS, reads=reads, **kw)


# ---------------------------------------------------------------------------
# (1)-(4) source order
# ---------------------------------------------------------------------------
def test_1_fresh_l1_answers_and_iex_is_never_asked():
    reads, calls = _reads(l1=_l1(1.06), direct=_iex_tick())
    dec = _select(reads)
    assert dec.tick is not None and dec.tick.bid == 8.88
    assert calls["direct"] == [], "hindi dapat tanungin ang IEX kapag sumagot ang L1"
    env = dec.envelope
    assert env["bbo_fallback_engaged"] is False
    assert env["bbo_quote_authority"] == AUTHORITY_L1_FENCED
    assert env["bbo_validity_rule"] == "l1_fresh"
    assert env["bbo_max_age_s"] == pytest.approx(BOUNDS.l1_fresh_bound_s)
    assert env["bbo_source"] == "iqfeed_l1"
    assert env["bbo_age_s"] == pytest.approx(1.06)
    assert env["bbo_selector_version"] == "held_bbo_v1"
    assert env["bbo_bounds"] == BOUNDS.derivation
    assert dec.snapshot["ok"] is True and dec.snapshot["reason"] == "execution_bbo_ok"
    assert dec.snapshot["quote_authority"] == AUTHORITY_L1_FENCED
    assert dec.tick.freshness.max_age_seconds == pytest.approx(BOUNDS.l1_fresh_bound_s)
    assert [e["tier"] for e in env["bbo_fallback_chain"]] == ["iqfeed_l1"]
    assert env["bbo_fallback_chain"][0]["outcome"] == "answered"
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
    reads, calls = _reads(l1=L1Read(reason="no_row"), direct=_iex_tick(0.3))
    dec = _select(reads)
    assert dec.tick is not None and dec.tick.bid == 8.90
    assert len(calls["direct"]) == 1
    assert calls["direct"][0][1] == pytest.approx(2.0)
    chain = dec.envelope["bbo_fallback_chain"]
    assert [(e["tier"], e["outcome"], e["reason"]) for e in chain] == [
        ("iqfeed_l1", "refused", "l1_no_row"),
        ("alpaca_iex", "answered", "iex_direct"),
    ]
    assert dec.envelope["bbo_fallback_engaged"] is True
    assert dec.envelope["bbo_quote_authority"] == AUTHORITY_ALPACA_DIRECT
    assert dec.envelope["bbo_validity_rule"] == "iex_direct"
    assert dec.envelope["bbo_l1_entitlement_state"] == "unknown_no_l1_row"


def test_4_both_refused_means_no_tick_and_the_deadman_is_the_floor():
    reads, _ = _reads(l1=L1Read(reason="no_row"), heartbeat=None)
    dec = _select(reads)
    assert dec.tick is None
    assert dec.snapshot["ok"] is False
    assert dec.snapshot["reason"] == "held_bbo_unavailable"
    chain = dec.envelope["bbo_fallback_chain"]
    assert [e["outcome"] for e in chain] == ["refused", "refused"]
    assert chain[0]["reason"] == "l1_no_row"
    assert chain[1]["reason"] == "iex_execution_bbo_unavailable"
    assert dec.envelope["bbo_fallback_engaged"] is True
    assert dec.envelope["bbo_source"] is None
    assert "l1_no_row" in dec.snapshot["unavailable_kind"]


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
# (5)-(6) snapshot-never
# ---------------------------------------------------------------------------
class _TapeAnsweringAdapter:
    """Bawat tape tier ay sumasagot; ang get_execution_bbo ay nagre-record ng kwargs."""

    def __init__(self) -> None:
        self.execution_bbo_kwargs: list[dict] = []
        self.tier_calls: list[str] = []

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

    def get_execution_bbo(self, product_id, **kwargs):
        self.execution_bbo_kwargs.append(dict(kwargs))
        return None, FreshnessMeta(retrieved_at_utc=NOW, provider_time_utc=None, max_age_seconds=2.0)


def test_5_a_tape_row_is_never_a_stand_in_for_a_held_decision(monkeypatch):
    adapter = _TapeAnsweringAdapter()
    dec = select_held_bbo(adapter, "PCLA", now=NOW, bounds=BOUNDS)
    assert dec.tick is None, "walang tape tier ang dapat sumagot para sa HELD tick"
    assert len(adapter.execution_bbo_kwargs) == 1
    assert "allow_stand_in" not in adapter.execution_bbo_kwargs[0]
    assert "stand_in_max_age_seconds" not in adapter.execution_bbo_kwargs[0]
    assert adapter.execution_bbo_kwargs[0]["max_age_seconds"] == pytest.approx(2.0)
    assert adapter.tier_calls == []
    assert dec.envelope["bbo_fallback_chain"][0]["reason"] == "l1_no_row"
    assert dec.counts_toward_halt is True  # heartbeat 0.1 s: tahimik ang pangalan, hindi ang feed


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
    called = {n.func.id for n in ast.walk(ast.Module(body=held.body, type_ignores=[]))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "select_held_bbo" in called
    assert "_final_entry_bbo" not in called
    assert src.count("chili_momentum_held_stand_in_max_age_seconds") == 0
    # walang numeric literal sa HELD branch (ang 15.0 ay nabanggit lang sa komento)
    consts = {n.value for n in ast.walk(ast.Module(body=held.body, type_ignores=[]))
              if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)}
    assert consts == set(), consts


# ---------------------------------------------------------------------------
# (7)-(10) delayed L1
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


def test_8_sip_disagreement_beyond_the_band_refuses_the_row():
    # As-of L1 bid 15 min old vs SIP: 700 bps off -> re-stamped delayed data.
    reads, _ = _reads(l1=_l1(1.0), witness=_witness(bid=8.88),
                      asof={"bid": 8.88 * (1 + 0.07), "ask": 9.6, "tape_row_id": 1})
    dec = _select(reads)
    assert dec.tick is None
    chain = dec.envelope["bbo_fallback_chain"]
    assert chain[0]["reason"] == "l1_rejected_sip_disagreement"
    assert chain[0]["witness"]["diff_bps"] == pytest.approx(700.0)
    assert dec.envelope["bbo_l1_entitlement_state"] == "suspect_by_sip_disagreement"
    assert dec.envelope["bbo_sip_witness"]["tape_row_id"] == 235009576
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
# (11)-(14) unchanged-book
# ---------------------------------------------------------------------------
def test_11_unchanged_book_is_accepted_up_to_the_gap_ceiling():
    reads, calls = _reads(l1=_l1(BOUNDS.l1_fresh_bound_s + 1.0), heartbeat=0.1)
    dec = _select(reads)
    assert dec.tick is not None
    assert dec.envelope["bbo_validity_rule"] == "l1_unchanged_book"
    assert dec.envelope["bbo_max_age_s"] == pytest.approx(BOUNDS.l1_gap_ceiling_s)
    assert dec.envelope["bbo_l1_heartbeat_age_s"] == pytest.approx(0.1)
    assert len(calls["print"]) == 1
    assert calls["print"][0][1] == NOW - timedelta(seconds=BOUNDS.l1_fresh_bound_s + 1.0)


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
    assert LR._held_bbo_receipt_fields({}) == {
        "bbo_source": None, "bbo_age_s": None, "bbo_fallback_engaged": None,
        "bbo_receipt": "no_held_bbo_envelope",
    }
    assert LR._held_bbo_receipt_fields(None)["bbo_receipt"] == "no_held_bbo_envelope"
    # ang lumang (pre-build-B) envelope na walang selector version ay "walang envelope"
    assert LR._held_bbo_receipt_fields({"last_held_execution_bbo": {"ok": True}})["bbo_receipt"] == "no_held_bbo_envelope"


_RECEIPTS_WITH_HELD_BID = (
    "live_bailout",
    "live_opinion_exit_armed",
    "live_momentum_break_exit",
    "live_burst_window_exit",
    "live_tape_accel_reversal_exit",
    "stop_breach_pending_confirm",
    "live_partial_exit",
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


# ---------------------------------------------------------------------------
# (20)-(22) derived bounds
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


def test_20_runtime_derivation_binds_the_measured_percentiles():
    fake = _FakeBoundsSession([(2000, 2.917), (5000, 18.832), (1356, 66.22)])
    b = derive_held_bbo_bounds(lambda: fake, now=NOW)
    assert b.l1_fresh_bound_s == pytest.approx(4.917)
    assert b.l1_gap_ceiling_s == pytest.approx(18.832)
    assert b.sip_disagree_band_bps == pytest.approx(66.22)
    assert b.heartbeat_bound_s == pytest.approx(4.917)
    assert b.delay_stamp_threshold_s == 2.0
    d = b.derivation
    assert d["l1_fresh_bound_s"]["source"] == "runtime"
    assert d["l1_fresh_bound_s"]["n"] == 2000 and d["l1_fresh_bound_s"]["n_min"] == 1000
    assert d["l1_fresh_bound_s"]["percentile"] == 0.999
    assert "available_at - provider_trade_reference_at" in d["l1_fresh_bound_s"]["distribution"]
    assert d["l1_gap_ceiling_s"]["n"] == 5000 and d["l1_gap_ceiling_s"]["n_min"] == 100
    assert d["l1_gap_ceiling_s"]["percentile"] == 0.99 and d["l1_gap_ceiling_s"]["window_min"] == 10
    assert d["sip_disagree_band_bps"]["n"] == 1356 and d["sip_disagree_band_bps"]["window_min"] == 5
    assert d["delay_stamp_threshold_s"]["value"] == 2.0
    assert d["l1_fresh_bound_s"]["measured_at_utc"] == NOW.isoformat()
    assert fake.sql[0].startswith("SET LOCAL statement_timeout = 4920")
    assert "percentile_cont(0.999)" in fake.sql[1] and L1_BASIS_FENCED in fake.sql[1]
    assert "LAG(observed_at)" in fake.sql[2] and "percentile_cont(0.99)" in fake.sql[2]
    assert "LATERAL" in fake.sql[3] and "interval '5 minutes'" in fake.sql[3] and "LIMIT 3000" in fake.sql[3]
    assert fake.rolled_back


def test_21_below_n_min_or_a_raising_session_falls_back_to_the_tagged_constants():
    fake = _FakeBoundsSession([(999, 2.0), (5000, 18.832), (99, 10.0)])
    b = derive_held_bbo_bounds(lambda: fake, now=NOW)
    assert b.l1_fresh_bound_s == pytest.approx(4.917)
    assert b.derivation["l1_fresh_bound_s"]["source"] == "measured_fallback_20260910T1740Z"
    assert b.derivation["l1_fresh_bound_s"]["n"] == 999
    assert b.l1_gap_ceiling_s == pytest.approx(18.832)
    assert b.derivation["l1_gap_ceiling_s"]["source"] == "runtime"
    assert b.sip_disagree_band_bps == pytest.approx(66.22)
    assert b.derivation["sip_disagree_band_bps"]["source"] == "measured_fallback_20260910T1740Z"

    # isang query ang sumabog (statement timeout) -> ang bound na iyon lang ang fallback
    fake = _FakeBoundsSession([(2000, 2.5), (5000, 17.0), (1356, 50.0)], raise_on=1)
    b = derive_held_bbo_bounds(lambda: fake, now=NOW)
    assert b.derivation["l1_gap_ceiling_s"]["source"] == "measured_fallback_20260910T1740Z"
    assert b.l1_gap_ceiling_s == pytest.approx(18.832)
    assert b.derivation["l1_fresh_bound_s"]["source"] == "runtime"

    # walang DB man lang
    b = derive_held_bbo_bounds(lambda: _FakeBoundsSession([], raise_all=True), now=NOW)
    assert all(v["source"] == "measured_fallback_20260910T1740Z"
               for k, v in b.derivation.items() if k != "delay_stamp_threshold_s")


def test_22_current_bounds_caches_for_60s_and_never_raises(monkeypatch):
    HB.reset_bounds_cache()
    calls = []

    def _derive(now):
        calls.append(now)
        return HB.fallback_bounds(now=NOW)

    monkeypatch.setattr(HB, "_derive_with_session_local", _derive)
    a = HB.current_bounds()
    b = HB.current_bounds()
    assert a is b and len(calls) == 1

    # lumipas ang TTL: ibinabalik agad ang naka-cache, background refresh lang
    spawned = []
    monkeypatch.setattr(HB, "_start_background_refresh", lambda: spawned.append(1))
    with HB._BOUNDS_LOCK:
        HB._BOUNDS_CACHE["at_monotonic"] -= HB._HELD_BBO_BOUNDS_TTL_S + 1
    c = HB.current_bounds()
    assert c is a and spawned == [1] and len(calls) == 1

    # ang derivation ay sumabog -> fallback, walang exception
    HB.reset_bounds_cache()
    monkeypatch.setattr(HB, "_derive_with_session_local", lambda now: (_ for _ in ()).throw(RuntimeError("boom")))
    d = HB.current_bounds()
    assert d.l1_fresh_bound_s == pytest.approx(4.917)
    assert d.derivation["l1_fresh_bound_s"]["source"] == "measured_fallback_20260910T1740Z"
    HB.reset_bounds_cache()


# ---------------------------------------------------------------------------
# (24)-(25) wiring — DB (chili_psv_test)
# ---------------------------------------------------------------------------
def _envelope(counts: bool) -> dict:
    reads, _ = _reads(l1=L1Read(reason="no_row"), heartbeat=(0.1 if counts else None))
    dec = _select(reads)
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
    assert blocked[0]["counts_toward_halt"] is counts
    assert "bbo_bounds" in blocked[0]
    db.refresh(sess)
    assert sess.risk_snapshot_json["momentum_live_execution"]["last_held_execution_bbo"]["bbo_selector_version"] == "held_bbo_v1"


def test_25_exit_pricing_asks_the_selector_before_the_900s_ladder(db, monkeypatch, _wired):
    from tests.test_momentum_emergency_exit_recovery import (
        _ScriptedAlpaca, _ensure_retained_entry_owner, _seed_session,
    )

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
    adapter = _ScriptedAlpaca(positions=[10.0])
    order: list[str] = []
    feb_calls: list[dict] = []

    def _fake_final_entry_bbo(_adapter, _pid, **kw):
        feb_calls.append(dict(kw))
        order.append("ladder" if kw.get("allow_stand_in") else "strict")
        if kw.get("allow_stand_in"):
            return _iex_tick()
        return None, {"ok": False, "reason": "execution_bbo_unavailable"}

    reads, _ = _reads(l1=_l1(1.06))
    real_select = HB.select_held_bbo

    def _fake_select(adapter_, pid, **kw):
        order.append("selector")
        assert kw.get("tiers") == ("iqfeed_l1",)
        return real_select(adapter_, pid, now=NOW, bounds=BOUNDS, reads=reads, tiers=kw["tiers"])

    monkeypatch.setattr(LR, "_final_entry_bbo", _fake_final_entry_bbo)
    monkeypatch.setattr(LR, "select_held_bbo", _fake_select)
    monkeypatch.setattr(LR, "current_bounds", lambda **_k: BOUNDS)
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
    assert pricing[0]["bbo_fallback_engaged"] is True
    assert pricing[0]["bbo_fallback_tier"] == "iqfeed_l1"
    assert pricing[0]["execution_bbo"]["pricing_tier"] == "held_selector_iqfeed_l1"
    assert pricing[0]["execution_bbo"]["quote_authority"] == AUTHORITY_L1_FENCED
    assert pricing[0]["execution_bbo"]["bbo_validity_rule"] == "l1_fresh"
    assert le["exit_final_bbo"]["bid"] == 8.88


def test_25b_when_l1_refuses_the_legacy_ladder_runs_and_is_receipted(db, monkeypatch, _wired):
    from tests.test_momentum_emergency_exit_recovery import (
        _ScriptedAlpaca, _ensure_retained_entry_owner, _seed_session,
    )

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
    adapter = _ScriptedAlpaca(positions=[10.0])
    order: list[str] = []

    def _fake_final_entry_bbo(_adapter, _pid, **kw):
        order.append("ladder" if kw.get("allow_stand_in") else "strict")
        if kw.get("allow_stand_in"):
            assert kw.get("stand_in_max_age_seconds") == pytest.approx(900.0)
            return _iex_tick()
        return None, {"ok": False, "reason": "execution_bbo_unavailable"}

    reads, _ = _reads(l1=L1Read(reason="no_row"), heartbeat=0.1)
    real_select = HB.select_held_bbo
    monkeypatch.setattr(LR, "_final_entry_bbo", _fake_final_entry_bbo)
    monkeypatch.setattr(
        LR, "select_held_bbo",
        lambda a, pid, **kw: order.append("selector") or real_select(a, pid, now=NOW, bounds=BOUNDS, reads=reads, tiers=kw["tiers"]),
    )
    monkeypatch.setattr(LR, "current_bounds", lambda **_k: BOUNDS)
    LR._submit_live_market_exit(
        db, sess, adapter, le=le, product_id=symbol, quantity=10.0,
        client_order_id="held-l1-exit-cid-2", reason="stop",
        bid=9.95, ask=9.97, mid=9.96,
    )
    assert order == ["strict", "selector", "ladder"], order
    pricing = [p for et, p in _wired if et == "live_exit_stand_in_pricing"]
    assert len(pricing) == 1
    assert pricing[0]["bbo_fallback_tier"] == "legacy_stand_in_ladder_900s"
    assert pricing[0]["held_selector_chain"][0]["reason"] == "l1_no_row"
