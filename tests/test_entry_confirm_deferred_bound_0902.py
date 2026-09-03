"""Item D ng 09-02: ang `entry_confirm_deferred` ay walang hangganan.

ANG INSIDENTE (CELU 17712, Alpaca paper, 2026-08-27). Ang entry-confirm poll ay
nagbalik ng WALANG order object (`no is None`), kaya ang tick ay nag-emit ng
`entry_confirm_deferred` at bumalik na PENDING. Tama iyon -- ang kapalaran ng
order ay HINDI ALAM, at ang malusog na nakaupong entry order ay hindi dapat
itulak sa LIVE_ERROR. Ang mali ay walang counter, walang cap at walang
escalation: ang emit ay walang kondisyon KADA TICK at ang postcondition ay
kapareho ng precondition.

SINUKAT SA LIVE DB (2026-09-02, buong talahanayan mula 2026-04-20):
  * `entry_confirm_deferred` = 267 event, 1 session, 1 araw.
  * CELU 17712: una 21:14:04.698944, huli 22:02:53.954898, saklaw 2,929.3s,
    isang natatanging order id (74a1992d-ad00-429c-8301-ad414211942f).
  * Agwat sa pagitan ng event: mean 11.01s, min 5.94s, max 106.02s -- KAYA ANG
    CAP AY DAPAT NAKABATAY SA ORAS, HINDI SA BILANG NG ATTEMPT. Sa 6 attempt
    ang parehong cap ay puputok saanman sa pagitan ng 36s at 10 minuto.
  * Ang session ay may `entry_submitted=true` at ang order id pa rin ngayon;
    WALANG `live_entry_filled`, WALANG `live_deadman_stop_placed`.
  * Ang tumapos nito ay TAO: `operator_state_repair_fill_adopted` 22:02:52.020644,
    dahilan `get_order_broken_by_1212_options_kwarg_fill_invisible`. Exposure
    mula `live_entry_submitted` 21:13:58.472814 = 2,933.5s sa isang na-fill na
    236-share na posisyon.
  * WALANG `operator_pause` ang session na ito -- ibang mekanismo ito sa item B,
    hindi pangalawang sintomas nito.

Runnable: pytest tests/test_entry_confirm_deferred_bound_0902.py -v -p no:randomly
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading import alerts as AL
from app.services.trading.momentum_neural import live_runner as LR


class _Sess:
    def __init__(self, sid=17712, symbol="CELU"):
        self.id = sid
        self.user_id = 1
        self.symbol = symbol
        self.state = "live_pending_entry"
        self.correlation_id = f"corr-{sid}"
        self.risk_snapshot_json = {}


class _Db:
    def flush(self):
        pass


@pytest.fixture
def bus(monkeypatch):
    """Captures emitted events and dispatched pages."""
    rec = SimpleNamespace(events=[], pages=[], deliver=True)

    def _emit(_db, sess, event_type, payload):
        rec.events.append((str(event_type), dict(payload)))
        return SimpleNamespace(id=len(rec.events))

    def _dispatch(**kw):
        rec.pages.append(kw)
        return rec.deliver

    monkeypatch.setattr(LR, "_emit", _emit)
    monkeypatch.setattr(AL, "dispatch_alert", _dispatch)
    # `settings` is a pydantic model, and forcing a field it does not DEFINE is a
    # double trap: the assignment raises ValueError, and `raising=False` makes it
    # WORSE — monkeypatch then records the attribute as absent and tries
    # `object.__delattr__` at teardown, which pydantic also rejects, turning a
    # clean assertion failure into a fixture ERROR. On origin/main these two
    # fields do not exist, and that is exactly how nine of these tests came to
    # never reach an assertion at all. So: only force what already exists. The
    # values are identical to the code's own defaults, so the tests exercise the
    # same numbers either way — forcing them guards a future default change, it
    # is not a precondition.
    for _name, _value in (
        ("chili_momentum_entry_confirm_deferred_emit_interval_s", 15.0),
        ("chili_momentum_entry_confirm_deferred_cap_seconds", 180.0),
    ):
        if hasattr(LR.settings, _name):
            monkeypatch.setattr(LR.settings, _name, _value)
    return rec


def _replay(bus, gaps_s, *, oid="74a1992d-ad00-429c-8301-ad414211942f", le=None):
    """Drive the bound over a sequence of inter-tick gaps; return (le, results)."""
    sess = _Sess()
    le = le if le is not None else {"entry_submitted": True, "entry_order_id": oid}
    t = datetime(2026, 8, 27, 21, 14, 4, 698944)
    results = []
    for gap in [0.0] + list(gaps_s):
        t = t + timedelta(seconds=gap)
        results.append(LR._note_entry_confirm_deferred(_Db(), sess, le, now=t))
    return le, results


def _routine(bus):
    return [e for e in bus.events if e[0] == "entry_confirm_deferred"]


def _alarm(bus):
    return [e for e in bus.events if e[0] == "entry_confirm_deferred_unbounded"]


# ── D1: the CELU replay converges instead of running forever ────────────────


def test_celu_replay_is_bounded_and_pages_once(bus):
    """267 na tick sa sinukat na 11.01s na kadensya. Sa main: 267 event, 0 page,
    tapos ng TAO pagkalipas ng 2,929s."""
    le, res = _replay(bus, [11.01] * 266)
    assert le["entry_confirm_deferred_attempts"] == 267

    # The routine event is throttled AND it STOPS once the alarm owns the loop.
    # Throttling alone would be a rate limit, not a bound: 2,929s at 15s is still
    # ~195 rows against main's 267. The invariants are (a) the spacing and (b) the
    # fact that nothing is written after the alarm, which caps the whole run at
    # ceil(cap/interval) + 1 = 13 regardless of how long the deferral lasts.
    import math

    stamps = [e[1]["deferred_for_s"] for e in _routine(bus)]
    assert all(b - a >= 15.0 for a, b in zip(stamps, stamps[1:])), stamps[:8]
    assert len(stamps) <= math.ceil(180.0 / 15.0) + 1, stamps
    assert max(stamps) <= 180.0 + 11.01, "walang routine event pagkatapos ng alarm"

    # Exactly ONE alarm and exactly ONE page for the whole run.
    assert len(_alarm(bus)) == 1
    assert len(bus.pages) == 1
    page = bus.pages[0]
    assert page["db"] is None, (
        "ang dispatch_alert ay nag-co-commit; hindi ito dapat makahawak sa "
        "transaction ng tick"
    )
    assert page["alert_type"] == AL.LIVE_ENTRY_ORDER_UNOBSERVED
    assert page["ticker"] == "CELU"
    assert page["skip_throttle"] is True
    assert page["content_signature"] == (
        "entry_confirm_deferred:17712:74a1992d-ad00-429c-8301-ad414211942f"
    )
    assert "74a1992d" in page["message"]

    # And it fired long before the 2,929s a human took.
    fired = next(r for r in res if r.get("escalated"))
    assert fired["deferred_for_s"] <= 200.0, fired
    assert fired["cap_seconds"] == 180.0


def test_a_short_deferral_never_alarms(bus):
    """Ang deferral ay LEHITIMO: governor defer o transient get_order. Ang
    maikling deferral ay hindi dapat mag-page."""
    le, res = _replay(bus, [2.0] * 20)  # 40s total, well inside the cap
    assert _alarm(bus) == []
    assert bus.pages == []
    assert all(r["escalated"] is False for r in res)
    # ...but it was still counted and still spoke at least once.
    assert le["entry_confirm_deferred_attempts"] == 21
    assert len(_routine(bus)) >= 1


def test_the_first_deferral_always_speaks_immediately(bus):
    _, res = _replay(bus, [])
    assert len(_routine(bus)) == 1
    assert res[0]["attempts"] == 1
    assert res[0]["emitted"] is True


# ── D2: a time cap, not an attempt cap (the measured 5.94s..106.02s spread) ──


@pytest.mark.parametrize("gap,ticks_to_alarm_max", [
    (5.94, 40),    # the fastest observed cadence
    (106.02, 4),   # the slowest observed cadence
])
def test_cap_is_time_based_across_the_measured_gap_spread(bus, gap, ticks_to_alarm_max):
    le, res = _replay(bus, [gap] * 60)
    fired = [i for i, r in enumerate(res) if r.get("escalated")]
    assert len(fired) == 1, "isang escalation lang kada order"
    assert res[fired[0]]["deferred_for_s"] >= 180.0
    # regardless of how many ticks that took, the naked TIME is bounded
    assert res[fired[0]]["deferred_for_s"] <= 180.0 + gap + 15.0
    assert fired[0] <= ticks_to_alarm_max


# ── D3: run identity — a new order id starts a new run ──────────────────────


def test_a_new_order_id_resets_the_run(bus):
    le, _ = _replay(bus, [30.0] * 10)          # 300s -> alarmed
    assert len(bus.pages) == 1
    le["entry_order_id"] = "552efe43-0000-0000-0000-000000000000"
    sess = _Sess()
    out = LR._note_entry_confirm_deferred(
        _Db(), sess, le, now=datetime(2026, 8, 27, 22, 0, 0),
    )
    assert out["attempts"] == 1
    assert out["escalated"] is False
    assert out["order_id"] == "552efe43-0000-0000-0000-000000000000"
    assert len(bus.pages) == 1, "walang instant na page mula sa minanang edad"


def test_an_undelivered_page_never_becomes_a_durable_row_storm(bus):
    """THE HOLE THIS CLOSES. `dispatch_alert` returns False when the channel is
    merely TURNED OFF (`alerts_enabled=False`, or a Telegram TIER_A preference
    cell that is off) — a normal configuration state, not a fault — and it writes
    an AlertHistory row plus a shadow decision packet on EVERY call regardless of
    the send, then commits. Retrying on a False return once per `_due` tick was
    therefore an unbounded durable-write loop on the live-runner hot path: on the
    CELU shape, ~183 rows, ~183 mid-tick commits and up to ~183 ten-second HTTP
    POSTs. The ALARM latches unconditionally; only the SEND retries, and it is
    bounded."""
    bus.deliver = False
    # 300 ticks, 30s apart = 9,000s — three times the CELU deferral.
    le, res = _replay(bus, [30.0] * 300)

    assert len(_alarm(bus)) == 1, "ang durable na alarm ay isa, hindi kada tick"
    assert len(bus.pages) == LR._ENTRY_CONFIRM_PAGE_MAX_ATTEMPTS, bus.pages
    assert le["entry_confirm_deferred_page_attempts"] == (
        LR._ENTRY_CONFIRM_PAGE_MAX_ATTEMPTS
    )
    assert "entry_confirm_deferred_escalated_utc" not in le
    # ...and the routine stream stopped when the alarm took over: 300 ticks over
    # 9,000s produced a handful of rows, all of them inside the 180s cap.
    assert len(_routine(bus)) <= 13, len(_routine(bus))
    assert max(e[1]["deferred_for_s"] for e in _routine(bus)) <= 180.0 + 30.0
    # Every attempt was spaced by the retry budget, not by the tick.
    assert all(p["db"] is None for p in bus.pages)


def test_the_page_latches_on_confirmed_delivery_and_then_stops(bus):
    bus.deliver = True
    le, _ = _replay(bus, [30.0] * 40)
    assert len(bus.pages) == 1
    assert le["entry_confirm_deferred_escalated_utc"]
    assert le["entry_confirm_deferred_page_attempts"] == 1


def test_a_page_failure_never_breaks_the_tick(bus, monkeypatch, caplog):
    def _boom(**_kw):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(AL, "dispatch_alert", _boom)
    with caplog.at_level("CRITICAL", logger=LR._log.name):
        le, res = _replay(bus, [30.0] * 10)
    # the durable record was written anyway
    assert len(_alarm(bus)) == 1
    assert any("ENTRY ORDER UNOBSERVED" in r.getMessage() for r in caplog.records)


# ── D4: the poll is never throttled; only the emit is ───────────────────────


def test_the_confirm_poll_itself_is_not_throttled():
    """Ang throttle ay nasa EMIT lamang. Ang pag-throttle sa poll ay
    magpapabagal sa pagtuklas, hindi magpapaligtas."""
    src = inspect.getsource(LR.tick_live_session)
    i = src.index('pending": "entry_confirm_deferred"')
    window = src[max(0, i - 1500):i]
    assert "_note_entry_confirm_deferred(" in window
    assert "_commit_le(sess, le)" in window
    # the throttle lives in the helper, not around the poll
    helper = inspect.getsource(LR._note_entry_confirm_deferred)
    assert "_entry_confirm_deferred_emit_interval_s" in helper
    assert "_entry_confirm_deferred_cap_seconds" in helper


def test_counters_persist_into_the_session_snapshot(bus):
    """Kailangang mabuhay ang run sa mga tick: ang counter ay nasa `le`, na
    isinusulat ng `_commit_le`, hindi sa in-memory na diksyunaryo."""
    le, _ = _replay(bus, [5.0] * 5)
    for k in (
        "entry_confirm_deferred_attempts",
        "entry_confirm_deferred_first_utc",
        "entry_confirm_deferred_order_id",
        "entry_confirm_deferred_last_emit_utc",
    ):
        assert k in le, k


# ── D5: the page is TIER_A and does not arm the sleep ladder ────────────────


def test_alert_type_is_tier_a_individual_and_not_stop_critical():
    """Ang membership sa `_STOP_CRITICAL_TYPES` ay nag-aarma ng 2s/8s na
    `time.sleep` ladder sa THREAD ng tumatawag -- at ang tumatawag dito ay isang
    live-runner tick na may bukas na transaction."""
    assert AL.classify_alert_tier(AL.LIVE_ENTRY_ORDER_UNOBSERVED) == AL.TIER_A
    assert AL.LIVE_ENTRY_ORDER_UNOBSERVED in AL._INDIVIDUAL_MSG_TYPES
    assert AL.LIVE_ENTRY_ORDER_UNOBSERVED not in AL._STOP_CRITICAL_TYPES
