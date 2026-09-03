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
    monkeypatch.setattr(
        LR.settings, "chili_momentum_entry_confirm_deferred_emit_interval_s", 15.0,
        raising=False,
    )
    monkeypatch.setattr(
        LR.settings, "chili_momentum_entry_confirm_deferred_cap_seconds", 180.0,
        raising=False,
    )
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

    # The routine event is throttled, not one per tick. The INVARIANT is the
    # spacing, not a magic count: no two emits closer than the interval. (At the
    # measured 11.01s cadence that lands on every other tick, 134 of 267.)
    stamps = [e[1]["deferred_for_s"] for e in _routine(bus)]
    assert len(stamps) < 267
    assert all(b - a >= 15.0 for a, b in zip(stamps, stamps[1:])), stamps[:8]
    assert len(stamps) == 134

    # Exactly ONE alarm and exactly ONE page for the whole run.
    assert len(_alarm(bus)) == 1
    assert len(bus.pages) == 1
    page = bus.pages[0]
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


def test_the_alarm_latches_only_on_confirmed_delivery(bus):
    bus.deliver = False
    le, res = _replay(bus, [30.0] * 20)
    # undelivered => no latch => it retries, but bounded by the emit interval,
    # never once per tick.
    assert len(bus.pages) > 1
    assert len(bus.pages) < 20
    assert "entry_confirm_deferred_escalated_utc" not in le

    bus.deliver = True
    sess = _Sess()
    LR._note_entry_confirm_deferred(
        _Db(), sess, le, now=datetime(2026, 8, 27, 22, 30, 0),
    )
    assert le["entry_confirm_deferred_escalated_utc"]


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
