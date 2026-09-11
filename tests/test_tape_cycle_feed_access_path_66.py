"""[66] Ang tape-cycle feed ay hindi dapat mabulag ng planner sa isang mabigat na tape.

Live 2026-09-11: pinili ng Postgres ang Bitmap Heap Scan para sa `_FEED_SQL` — binabasa ang
BAWAT hilera ng saklaw bago mag-Sort. Ang sanhi ay ang tantya ng planner (table-wide na dalas
ng simbolo × selectivity ng saklaw, magkahiwalay na column): para sa pangalang mabigat ngayong
araw pero bihira sa table, ang tantya ay MAS MABABA sa LIMIT, anuman ang kinalalagyan ng cursor
(BDRX 22264 bumagsak sa 14:43 matapos ang 80,000 print). 9 na sesyon / 6 na simbolo ang natapos
nang walang ledger; ang 2 fill ng BDRX ay na-size nang `no_tape_state`.

REVIEW FIXES (2026-09-11) — bawat test dito ay BUMABAGSAK nang wala ang ayos nito:
  * ang plan test ay may PRECONDITION: ang parehong query, walang fence, ay Bitmap Heap Scan na
    bumabasa ng BUONG saklaw (sa isang temp table na kamukha ng produksyon);
  * ang tinanggihang pin ay sinusubok sa TUNAY na Postgres (ang fake ay hindi nag-a-abort ng
    transaksyon, kaya ang dating test ay nagpapatunay ng ugaling wala sa Postgres);
  * ang resibo ng bahagyang pagkabigo ay nagsasabi ng TOTOONG nakain (fed/to/ms/reads);
  * ang budget ay predictive; ang bagong sesyon ay nagmamana ng ledger ng symbol-day;
  * ang resibo ng fill ay nagsasabi kung BAKIT bulag ang ledger.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

import app.services.trading.momentum_neural.live_runner as lr
import app.services.trading.momentum_neural.optional_db_read as odr
import app.services.trading.momentum_neural.tape_cycles as tc
from app import models
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.tape_cycles import (
    CYCLE_FEED_BUDGET_MS,
    CYCLE_FEED_STATEMENT_TIMEOUT_MS,
    CYCLE_LEDGER_MAX_CYCLES,
    _FEED_SQL,
    PullbackCycleScanner,
    _close_feed_fence,
    _open_feed_fence,
    feed_scanner_from_db,
)

DAY = datetime(2026, 9, 11)
SESSION_START = datetime(2026, 9, 11, 8, 0, 0)  # 04:00 EDT
AS_OF = datetime(2026, 9, 11, 17, 55, 0)


def _show(db, name: str) -> str:
    return str(db.execute(text(f"SHOW {name}")).scalar())


def _insert(db, sym: str, n: int, *, start: datetime, step_s: float = 1.0) -> None:
    for i in range(n):
        px = 1.30 + (i % 7) / 100.0 + (i // 7) / 50.0
        db.execute(
            text(
                "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask) "
                "VALUES (:s, :t, :p, 100.0, :b, :a)"
            ),
            {"s": sym, "t": start + timedelta(seconds=step_s * i), "p": px, "b": px - 0.01, "a": px},
        )
    db.flush()


def _spy_reads(monkeypatch, db) -> list[tuple[str, str]]:
    """Itinatala ang GUC na AKTWAL na nakikita ng BAWAT feed read (hindi ang hula ng test)."""
    seen: list[tuple[str, str]] = []
    real = odr.optional_fetchall

    def _spy(d, statement, params=None, **kw):
        seen.append((_show(d, "enable_bitmapscan"), _show(d, "statement_timeout")))
        return real(d, statement, params, **kw)

    monkeypatch.setattr(odr, "optional_fetchall", _spy)
    return seen


# ── ANG FENCE SA TUNAY NA POSTGRES ────────────────────────────────────────────
def test_every_read_runs_inside_the_fence_and_nothing_leaks_into_the_rest_of_the_tick(db, monkeypatch):
    _insert(db, "TFEN", 30, start=datetime(2026, 9, 11, 10, 19, 0))
    before = (_show(db, "enable_bitmapscan"), _show(db, "statement_timeout"))
    seen = _spy_reads(monkeypatch, db)
    out = feed_scanner_from_db(
        PullbackCycleScanner(0.5), "TFEN", db=db, session_start=SESSION_START, max_prints=10, as_of=AS_OF,
    )
    assert out["fed"] == 30 and out["caught_up"] is True and "reason" not in out
    assert len(seen) == out["reads"] == 4  # 10 + 10 + 10 + 0
    assert set(seen) == {("off", f"{CYCLE_FEED_STATEMENT_TIMEOUT_MS // 1000}s")}
    assert out["fence"] == {"timeout_ms": CYCLE_FEED_STATEMENT_TIMEOUT_MS, "pin": "enable_bitmapscan=off"}
    # ROLLBACK TO SAVEPOINT: ang natitirang tick ay tumatakbo sa DATING mga halaga.
    assert (_show(db, "enable_bitmapscan"), _show(db, "statement_timeout")) == before


def _scan_nodes(plan: dict[str, Any]) -> list[tuple[str, int]]:
    out, stack = [], [plan]
    while stack:
        node = stack.pop()
        if node.get("Relation Name") == "iqfeed_trade_ticks":
            out.append((str(node["Node Type"]), int(node.get("Actual Rows") or 0)))
        stack.extend(node.get("Plans") or [])
    return out


def _explain(db, params: dict[str, Any]) -> list[tuple[str, int]]:
    doc = db.execute(
        text("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF, FORMAT JSON) " + _FEED_SQL), params
    ).scalar()
    return _scan_nodes(doc[0]["Plan"])


def test_the_fence_turns_the_bitmap_plan_that_reads_the_whole_range_into_an_index_scan_that_stops(db):
    """PRECONDITION muna (ang dating test ay walang ganito kaya pumapasa kahit wala ang ayos):
    ang PAREHONG query sa PAREHONG datos, walang fence, ay Bitmap Heap Scan na bumabasa ng BUONG
    saklaw kahit LIMIT 100. Ang datos ay nasa TEMP table na may kaparehong kahulugan (LIKE ...
    INCLUDING ALL — kasama ang (symbol, observed_at DESC) index) at kamukha ng produksyon: ang
    simbolo ay nakakalat sa buong heap, at ang tantya ay MAS MABABA sa totoo (ang parehong
    maling-tantya ng planner). ``enable_seqscan = off`` sa PAREHONG braso: 30,000 hilera lang
    ito, kaya ang seq scan ay mura rito — sa 67M na hilera ng produksyon ay hindi ito kandidato."""
    db.execute(
        text(
            "CREATE TEMP TABLE iqfeed_trade_ticks "
            "(LIKE public.iqfeed_trade_ticks INCLUDING ALL) ON COMMIT DROP"
        )
    )
    db.execute(
        text(
            "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask) "
            "SELECT CASE WHEN g % 3 = 0 THEN 'TBMP' ELSE 'OTH' || (g % 97)::text END, "
            "timestamp '2026-09-01 10:19:00' + (g * interval '30 seconds'), "
            "1.30 + (g % 5) / 100.0, 100.0, 1.29, 1.30 FROM generate_series(1, 30000) g"
        )
    )
    db.execute(text("ANALYZE iqfeed_trade_ticks"))
    db.execute(text("SET LOCAL enable_seqscan = off"))
    in_range = int(
        db.execute(
            text(
                "SELECT count(*) FROM iqfeed_trade_ticks WHERE symbol = 'TBMP' "
                "AND observed_at >= :a AND observed_at <= :b"
            ),
            {"a": SESSION_START, "b": datetime(2026, 9, 11, 23, 55)},
        ).scalar()
    )
    limit = 100
    params = {
        "s": "TBMP", "last_at": SESSION_START, "last_id": 0,
        "as_of": datetime(2026, 9, 11, 23, 55), "n": limit,
    }
    assert in_range > limit
    unfenced = _explain(db, params)
    assert ("Bitmap Heap Scan", in_range) in unfenced, unfenced  # ang LIMIT ay walang silbi

    sp, fence = _open_feed_fence(db)
    try:
        fenced = _explain(db, params)
    finally:
        _close_feed_fence(sp)
    assert fence["pin"] == "enable_bitmapscan=off"
    assert not [n for n, _ in fenced if "Bitmap" in n], fenced
    assert fenced and all(rows <= limit + 1 for _, rows in fenced), fenced  # humihinto sa LIMIT
    assert _show(db, "enable_bitmapscan") == "on"

    # ...at ang feed mismo ay dumadaan pa rin sa buong saklaw nang eksaktong isang beses.
    sc = PullbackCycleScanner(0.5)
    out = feed_scanner_from_db(
        sc, "TBMP", db=db, session_start=SESSION_START, max_prints=limit, as_of=params["as_of"],
    )
    assert out["fed"] == in_range and out["caught_up"] is True


def test_a_refused_pin_is_contained_named_and_the_timeout_still_fences_every_read(db, monkeypatch):
    """TUNAY na pagtanggi ng Postgres (hindi kilalang GUC ⇒ UndefinedObject). Ang dating anyo ay
    nag-SET sa panlabas na transaksyon: ang pagtanggi ay nag-a-abort sa BUONG tick
    (InFailedSqlTransaction) — ang feed read, ang reset at lahat ng kasunod. Ngayon ay ang pin
    LAMANG ang nire-rollback: ang timeout fence ay nananatili sa bawat pagbasa, at ang
    pagtanggi ay nasa resibo."""
    monkeypatch.setattr(tc, "_FEED_ACCESS_PATH_GUC", "chili_no_such_planner_knob")
    _insert(db, "TREF", 12, start=datetime(2026, 9, 11, 10, 19, 0))
    before_timeout = _show(db, "statement_timeout")
    seen = _spy_reads(monkeypatch, db)
    out = feed_scanner_from_db(
        PullbackCycleScanner(0.5), "TREF", db=db, session_start=SESSION_START, max_prints=50, as_of=AS_OF,
    )
    assert "reason" not in out, out
    assert out["fed"] == 12 and out["caught_up"] is True
    assert out["fence"]["pin"] == "refused"
    assert out["fence"]["pin_error"] == "UndefinedObject"
    assert out["fence"]["timeout_ms"] == CYCLE_FEED_STATEMENT_TIMEOUT_MS
    assert [t for _, t in seen] == [f"{CYCLE_FEED_STATEMENT_TIMEOUT_MS // 1000}s"] * out["reads"]
    # Buhay ang transaksyon ng tick: nakakabasa at nakakasulat pa ang natitirang bahagi nito.
    assert db.execute(text("SELECT 1")).scalar() == 1
    _insert(db, "TREF", 1, start=datetime(2026, 9, 11, 11, 0, 0))
    assert _show(db, "statement_timeout") == before_timeout


# ── ANG RESIBO: PAGKABIGO, BAHAGYANG PAGKABIGO, BUDGET (control flow — fake DB) ──────────
class _SP:
    def __init__(self, owner: "_PgFake"):
        self.owner = owner
        owner.events.append("savepoint")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *exc):
        self.owner.events.append("rollback" if exc_type else "release")
        return False

    def commit(self):
        self.owner.events.append("release")

    def rollback(self):
        self.owner.events.append("rollback")


class _QueryCanceled(Exception):
    pass


class _Wrapped(Exception):
    """Ang hugis ng SQLAlchemy OperationalError: ang psycopg2 na klase ay nasa `.orig`."""


class _PgFake:
    """Postgres ang dialect, may `begin_nested` (gaya ng tunay na Session). Ang bawat feed read
    ay kumukuha ng susunod na kinalabasan: listahan ng hilera o exception."""

    def __init__(self, outcomes: list[Any], *, on_read=None):
        self.outcomes = list(outcomes)
        self.events: list[str] = []
        self.on_read = on_read

    def get_bind(self):
        class _D:
            name = "postgresql"

        class _B:
            dialect = _D()

        return _B()

    def begin_nested(self):
        return _SP(self)

    def execute(self, statement, params=None):
        sql = str(statement)
        if sql.lstrip().upper().startswith("SET LOCAL"):
            self.events.append(sql)

            class _R:
                def fetchall(self):
                    return []

            return _R()
        if self.on_read is not None:
            self.on_read()
        nxt = self.outcomes.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        rows = nxt

        class _Rows:
            def fetchall(self):
                return list(rows)

        return _Rows()


def _canceled() -> _Wrapped:
    err = _Wrapped("canceling statement due to statement timeout")
    err.orig = _QueryCanceled()
    return err


def _rows(n: int, *, start_id: int = 1, t0: datetime = datetime(2026, 9, 11, 10, 19, 0)):
    return [
        (t0 + timedelta(milliseconds=250 * (start_id + i)), start_id + i, 1.30 + ((start_id + i) % 5) / 100.0,
         100.0, 1.29, 1.30)
        for i in range(n)
    ]


def test_a_failed_first_read_names_its_cause_counts_the_attempt_and_the_fence_it_burned():
    db = _PgFake([_canceled()])
    out = feed_scanner_from_db(
        PullbackCycleScanner(0.5), "BDRX", db=db, session_start=SESSION_START, max_prints=5000, as_of=AS_OF,
    )
    assert out["reason"] == "read_failed"
    assert out["error"] == "_QueryCanceled"
    assert out["reads"] == 1  # ang bigong pagbasa ay pagbasa rin
    assert out["failed_read_ms"] >= 0.0
    assert out["fed"] == 0 and out["caught_up"] is False and out["to"] is None
    # Ang fence savepoint ay ROLLED BACK (iyon ang nagbabalik ng GUC) — walang `SET ... DEFAULT`.
    assert db.events[-1] == "rollback"
    assert not [e for e in db.events if "DEFAULT" in e]


def test_a_partial_failure_reports_what_the_ledger_already_consumed():
    """FTFT 22275: 6 × 5,000 na pagbasa ang pumasok (n_prints 30,000, gumalaw ang cursor) at
    ang ika-7 ay kinansela — ang resibo ay `fed 0, reads 6`, walang `to`, walang `ms`."""
    n = 10
    sc = PullbackCycleScanner(0.5)
    db = _PgFake([_rows(n, start_id=1), _rows(n, start_id=1 + n), _canceled()])
    out = feed_scanner_from_db(sc, "FTFT", db=db, session_start=SESSION_START, max_prints=n, as_of=AS_OF)
    assert out["reason"] == "read_failed" and out["error"] == "_QueryCanceled"
    assert out["fed"] == 2 * n == sc.n_prints
    assert out["reads"] == 3
    assert out["to"] == sc.last_observed_at is not None
    assert out["ms"] >= 0.0 and out["failed_read_ms"] >= 0.0
    assert "last_read_ms" in out  # ang gastos ng huling MATAGUMPAY na pagbasa


def test_the_budget_is_predictive_a_read_that_would_overrun_it_is_not_started(monkeypatch):
    """Malamig na pagbasa na 900 ms, budget 1,500: ang dating loop ay nagsisimula ng ika-2
    (0.9 < 1.5) at natatapos sa 1.8 s. Ang predictive: 0.9 + 0.9 > 1.5 ⇒ hindi na."""
    clock = {"t": 0.0}
    monkeypatch.setattr(tc.time, "monotonic", lambda: clock["t"])
    read_s = 0.9
    assert read_s < CYCLE_FEED_BUDGET_MS / 1000.0 < 2 * read_s

    def _cost():
        clock["t"] += read_s

    n = 5
    db = _PgFake([_rows(n, start_id=1 + k * n) for k in range(8)], on_read=_cost)
    out = feed_scanner_from_db(
        PullbackCycleScanner(0.5), "LBGJ", db=db, session_start=SESSION_START, max_prints=n, as_of=AS_OF,
    )
    assert out["reads"] == 1
    assert out["budget_hit"] is True and out["caught_up"] is False
    assert out["budget"] == {
        "elapsed_ms": pytest.approx(read_s * 1000.0),
        "last_read_ms": pytest.approx(read_s * 1000.0),
        "budget_ms": CYCLE_FEED_BUDGET_MS,
    }
    assert out["ms"] <= CYCLE_FEED_BUDGET_MS


# ── ANG LEDGER AY NG SYMBOL-DAY: ang bagong sesyon ay nagpapatuloy, hindi nagsisimula muli ──
def _session(db, user_id: int, variant_id: int, symbol: str, le: dict | None = None) -> TradingAutomationSession:
    s = TradingAutomationSession(
        user_id=user_id, symbol=symbol, mode="live", variant_id=variant_id,
        state=lr.STATE_WATCHING_LIVE, execution_family="alpaca_spot",
        risk_snapshot_json={"momentum_live_execution": dict(le or {})},
        updated_at=AS_OF,
    )
    db.add(s)
    db.flush()
    return s


def _owner(db) -> tuple[int, int]:
    u = models.User(name="tape-ledger-66")
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(family="tc66", variant_key="tc66_v", label="tc66", params_json={})
    db.add(v)
    db.flush()
    return u.id, v.id


def _ledger_only(st: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in st.items() if k not in ("feed", "day", "inherited_from")}


def test_a_new_session_continues_the_sibling_ledger_of_the_same_symbol_day(db, monkeypatch):
    """Ang BAWAT bagong sesyon ay dating nagsisimula sa 04:00 ET. Sa malamig na cache (p50
    1,231.7 ms kada 5,000 print) iyon ay ~70 tick ng `tape_not_caught_up`. Ngayon: minamana ang
    ledger ng kapatid at binabasa LAMANG ang pagkatapos ng cursor nito — at ang resulta ay
    EKSAKTONG kapareho ng ledger na binasa mula 04:00 ET."""
    uid, vid = _owner(db)
    _insert(db, "TINH", 60, start=datetime(2026, 9, 11, 10, 0, 0), step_s=300.0)  # 10:00–14:55Z
    monkeypatch.setattr(lr.settings, "chili_momentum_cycle_feed_max_prints", 20, raising=False)
    with lr.replay_clock(AS_OF):
        a = _session(db, uid, vid, "TINH")
        le_a: dict[str, Any] = {}
        st_a = lr._feed_tape_cycle_state(db, a, le_a, max_reads=1)
        assert st_a["n_prints"] == 20 and st_a["feed"]["caught_up"] is False
        a.risk_snapshot_json = {"momentum_live_execution": le_a}
        a.updated_at = AS_OF
        db.flush()

        monkeypatch.setattr(lr.settings, "chili_momentum_cycle_feed_max_prints", 5000, raising=False)
        b = _session(db, uid, vid, "TINH")
        le_b: dict[str, Any] = {}
        st_b = lr._feed_tape_cycle_state(db, b, le_b)

        ref = PullbackCycleScanner(0.5, max_cycles=CYCLE_LEDGER_MAX_CYCLES)
        feed_scanner_from_db(ref, "TINH", db=db, session_start=SESSION_START, max_prints=5000, as_of=AS_OF)
    assert st_b["inherited_from"] == {
        "session_id": a.id, "n_prints": 20, "to": st_a["last_observed_at"],
    }
    assert st_b["feed"]["sibling"]["adopted"]["session_id"] == a.id
    assert st_b["feed"]["fed"] == 40  # ang 20 ng kapatid ay HINDI binasa muli
    assert st_b["feed"]["caught_up"] is True
    assert st_b["n_prints"] == 60
    assert _ledger_only(st_b) == _ledger_only(ref.to_dict())  # eksaktong pagpapatuloy
    # Ang pinagmulan ay dala sa susunod na tick (hindi lang sa tick ng pagmana)...
    with lr.replay_clock(AS_OF):
        st_b2 = lr._feed_tape_cycle_state(db, b, le_b)
    assert st_b2["inherited_from"]["session_id"] == a.id
    assert "sibling" not in st_b2["feed"]  # ...at walang bagong paghahanap kapag may ledger na
    # ...at nasa matibay na resibo ng fill.
    _, rec = lr._cycle_exhaustion_conditioning(le_b, mid=1.5)
    assert rec["inherited_from"]["session_id"] == a.id


def test_a_sibling_ledger_is_never_inherited_past_the_as_of_or_across_frac_day_or_version(db, monkeypatch):
    """Sa FSM REPLAY ang ledger ng ibang sesyon ay maaaring nasa HINAHARAP ng sim clock:
    ang pagmana roon ay look-ahead. Ganoon din ang ibang praksyon / araw / bersyon — ibang
    pagbasa ng tape iyon, hindi prefix ng atin."""
    uid, vid = _owner(db)
    _insert(db, "TLOK", 60, start=datetime(2026, 9, 11, 10, 0, 0), step_s=300.0)
    monkeypatch.setattr(lr.settings, "chili_momentum_cycle_feed_max_prints", 20, raising=False)
    with lr.replay_clock(AS_OF):
        a = _session(db, uid, vid, "TLOK")
        le_a: dict[str, Any] = {}
        st_a = lr._feed_tape_cycle_state(db, a, le_a, max_reads=1)  # cursor 11:35Z
        a.risk_snapshot_json = {"momentum_live_execution": le_a}
        db.flush()
    early = datetime(2026, 9, 11, 11, 0, 0)  # BAGO ang cursor ng kapatid
    with lr.replay_clock(early):
        b = _session(db, uid, vid, "TLOK")
        le_b: dict[str, Any] = {}
        st_b = lr._feed_tape_cycle_state(db, b, le_b)
    assert st_b["feed"]["sibling"] == {"candidates": 1, "adopted": None}
    assert "inherited_from" not in st_b
    assert st_b["last_observed_at"] <= early.isoformat()  # hindi kailanman lampas sa as-of

    base = dict(st_a)
    ok = lr._tape_cycle_state_eligible(base, day_key="2026-09-11", frac=0.5,
                                       max_cycles=CYCLE_LEDGER_MAX_CYCLES, as_of=AS_OF)
    assert ok == (20, datetime.fromisoformat(st_a["last_observed_at"]))
    for bad in (
        dict(base, pullback_frac=0.25),
        dict(base, day="2026-09-10"),
        dict(base, v=2),
        dict(base, max_cycles=CYCLE_LEDGER_MAX_CYCLES * 4),
        dict(base, n_prints=0),
        dict(base, last_observed_at="not-a-stamp"),
    ):
        assert lr._tape_cycle_state_eligible(
            bad, day_key="2026-09-11", frac=0.5, max_cycles=CYCLE_LEDGER_MAX_CYCLES, as_of=AS_OF
        ) is None, bad


# ── ANG MATIBAY NA RESIBO NG FILL ────────────────────────────────────────────
def test_the_fill_receipt_says_why_the_ledger_was_blind():
    """BDRX 22277: bumagsak ang mga pagbasa, n_prints 0, at napuno ang sesyon sa 17:13Z. Ang
    payload ng `live_entry_filled` ay nagsabi lang ng `no_tape_state` — ang `QueryCanceled` ay
    nasa snapshot lamang, na pinapalitan bawat tick."""
    feed = {
        "fed": 0, "reads": 1, "caught_up": False, "reason": "read_failed", "error": "QueryCanceled",
        "failed_read_ms": 2001.4, "ms": 2003.2, "to": None,
        "fence": {"timeout_ms": CYCLE_FEED_STATEMENT_TIMEOUT_MS, "pin": "enable_bitmapscan=off"},
    }
    _, rec = lr._cycle_exhaustion_conditioning(
        {"tape_cycle_state": {"n_prints": 0, "day": "2026-09-11", "feed": feed}}, mid=1.0
    )
    assert rec["reason"] == "no_tape_state"
    assert rec["detail"] == {"reason": "no_tape_state", "feed_reason": "read_failed", "feed_error": "QueryCanceled"}
    assert rec["feed"]["failed_read_ms"] == 2001.4 and rec["feed"]["fence"]["pin"] == "enable_bitmapscan=off"

    # Bahagyang ledger (n_prints > 0, hindi pa naaabutan): ang WHY ay nasa detail din.
    sc = PullbackCycleScanner(0.5)
    sc.feed(_rows(30))
    st = dict(sc.to_dict(), day="2026-09-11",
              feed=dict(feed, fed=30, reads=4, to=sc.last_observed_at, budget_hit=False))
    _, rec2 = lr._cycle_exhaustion_conditioning({"tape_cycle_state": st}, mid=1.0)
    assert rec2["reason"] == "tape_not_caught_up"
    assert rec2["detail"]["feed_error"] == "QueryCanceled" and rec2["detail"]["reads"] == 4
    assert rec2["feed"]["to"] == sc.last_observed_at
    import json

    json.dumps(rec2)  # nasa event payload ito
