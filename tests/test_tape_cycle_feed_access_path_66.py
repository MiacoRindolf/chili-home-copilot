"""[66] Ang tape-cycle feed ay hindi dapat mabulag ng planner sa isang mabigat na tape.

Live 2026-09-11: sa cold start (o sa bagong sesyon ng mabigat na pangalan sa hapon), pinili ng
Postgres ang Bitmap Heap Scan para sa `_FEED_SQL` — binabasa ang BAWAT hilera ng saklaw bago
mag-Sort. BDRX (321,303 print): kinansela sa 20 s, kaya sa 2 s na fence ay `read_failed` sa
UNANG pagbasa, hindi gumagalaw ang cursor, at bumabagsak ang parehong pagbasa sa bawat tick.
9 na sesyon / 6 na simbolo ang natapos nang walang ledger; ang 2 fill ng BDRX ay na-size nang
`no_tape_state`. Naka-off ang bitmap sa loob ng fence: Index Scan Backward, 28.9 ms.

Sinusuri dito ang MEKANISMO sa tunay na Postgres (ang GUC sa loob ng fence at ang planong
walang Bitmap), hindi ang oras — hindi kayang gayahin ng maliit na test DB ang tantya ng planner.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

from app.services.trading.momentum_neural.tape_cycles import (
    CYCLE_FEED_STATEMENT_TIMEOUT_MS,
    _FEED_SQL,
    PullbackCycleScanner,
    _apply_feed_statement_timeout,
    _reset_feed_statement_timeout,
    feed_scanner_from_db,
)


def _show(db, name: str) -> str:
    return str(db.execute(text(f"SHOW {name}")).scalar())


def test_the_fence_pins_the_access_path_and_gives_it_back_on_real_postgres(db):
    before_bitmap = _show(db, "enable_bitmapscan")
    before_timeout = _show(db, "statement_timeout")
    applied = _apply_feed_statement_timeout(db)
    assert applied is True
    assert _show(db, "enable_bitmapscan") == "off"
    assert _show(db, "statement_timeout") == f"{CYCLE_FEED_STATEMENT_TIMEOUT_MS // 1000}s"
    _reset_feed_statement_timeout(db, applied)
    # Ang natitirang bahagi ng tick ay tumatakbo sa DATING mga halaga — walang tumatagas.
    assert _show(db, "enable_bitmapscan") == before_bitmap
    assert _show(db, "statement_timeout") == before_timeout


def test_the_feed_plan_under_the_fence_has_no_bitmap_scan(db):
    base = datetime(2026, 9, 11, 10, 19, 0)
    for i in range(50):
        db.execute(
            text(
                "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask) "
                "VALUES ('TBMP', :t, :p, 100.0, :b, :a)"
            ),
            {"t": base + timedelta(seconds=i), "p": 1.30 + (i % 5) / 100.0,
             "b": 1.29 + (i % 5) / 100.0, "a": 1.30 + (i % 5) / 100.0},
        )
    db.flush()
    params = {
        "s": "TBMP",
        "last_at": datetime(2026, 9, 11, 8, 0, 0),
        "last_id": 0,
        "as_of": datetime(2026, 9, 11, 17, 55, 0),
        "n": 5000,
    }
    applied = _apply_feed_statement_timeout(db)
    try:
        plan = "\n".join(
            str(r[0]) for r in db.execute(text("EXPLAIN " + _FEED_SQL), params).fetchall()
        )
    finally:
        _reset_feed_statement_timeout(db, applied)
    assert "Bitmap" not in plan, plan
    # ...at ang feed mismo ay dumadaan pa rin sa buong tape nang eksaktong isang beses.
    sc = PullbackCycleScanner(0.5)
    out = feed_scanner_from_db(
        sc, "TBMP", db=db, session_start=params["last_at"], max_prints=20, as_of=params["as_of"],
    )
    assert out["fed"] == 50
    assert out["caught_up"] is True
    assert _show(db, "enable_bitmapscan") == "on"


class _Canceled(Exception):
    pass


class _QueryCanceled(Exception):
    pass


class _FailingDB:
    """Postgres ang dialect; ang feed read ay bumabagsak na parang SQLAlchemy OperationalError
    na may `.orig` na psycopg2 QueryCanceled — ang eksaktong hugis ng BDRX."""

    def __init__(self, *, fail_guc: bool = False):
        self.gucs: list[str] = []
        self.fail_guc = fail_guc

    def get_bind(self):
        class _D:
            name = "postgresql"

        class _B:
            dialect = _D()

        return _B()

    def execute(self, statement, params=None):
        sql = str(statement)
        if sql.lstrip().upper().startswith("SET LOCAL"):
            if self.fail_guc and "enable_bitmapscan" in sql and "off" in sql:
                raise RuntimeError("guc refused")
            self.gucs.append(sql)
            return None
        err = _Canceled("canceling statement due to statement timeout")
        err.orig = _QueryCanceled()
        raise err


def test_a_failed_read_names_its_cause_in_the_receipt():
    db = _FailingDB()
    out = feed_scanner_from_db(
        PullbackCycleScanner(0.5), "BDRX", db=db,
        session_start=datetime(2026, 9, 11, 8, 0, 0), max_prints=5000,
        as_of=datetime(2026, 9, 11, 17, 55, 0),
    )
    assert out["reason"] == "read_failed"
    assert out["error"] == "_QueryCanceled"
    assert out["fed"] == 0 and out["caught_up"] is False
    # Kahit bumagsak ang pagbasa, ibinalik ang parehong GUC.
    assert [g for g in db.gucs if "DEFAULT" in g] == [
        "SET LOCAL statement_timeout = DEFAULT",
        "SET LOCAL enable_bitmapscan = DEFAULT",
    ]


def test_a_refused_access_path_set_keeps_the_timeout_fence():
    db = _FailingDB(fail_guc=True)
    applied = _apply_feed_statement_timeout(db)
    assert applied is True
    assert any("statement_timeout" in g and "ms'" in g for g in db.gucs)
    _reset_feed_statement_timeout(db, applied)
    assert "SET LOCAL statement_timeout = DEFAULT" in db.gucs
