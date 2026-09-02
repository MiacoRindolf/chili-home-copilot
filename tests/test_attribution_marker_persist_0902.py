"""The attribution skip/backoff markers must SURVIVE every other detail write.

MEASURED ON LIVE DATA (2026-09-02 20:2xZ, lookback 2 days, 118 live Alpaca
outcomes of which 115 are `unreconciled_no_fills` with NO entry evidence):
consecutive reconcile passes did not converge, they CYCLED —

    skipped_no_broker_read_needed: 0 → 20 → 40 → 60 → 0 → 20 → 40
    broker_reads:                 20 → 20 → 20 → 20 → 20 → 20 → 20
    "LOSS-GUARD ROWS STARVED BY THE READ BUDGET: 95 row(s)"  every single pass

`broker_read_plan` deliberately spends ONE proof read on a row with no entry
evidence (an envelope that shows nothing is exactly what an entry whose adoption
write was LOST looks like — CANF 19471 cycle 2), and only a readable listing that
owned nothing earns the permanent skip, recorded as
`detail["attribution_no_entry_evidence_proven_empty"]`. That design is correct and
is preserved here. What failed is PERSISTENCE: those markers are top-level keys of
`broker_recon_detail_json`, and EVERY writer of that column rebuilds the dict from
scratch — `reconcile_one_outcome` starts from `{"reconciled_at_utc": ...}` and
`alpaca_reconcile._apply_cancelled_pre_entry_orphan_truth` writes `{**truth, ...}` — so one write
from any path that does not hand-carry them erases the whole skip state and the
next pass re-reads the broker for rows a readable listing had already proved empty.

THE FIX: `outcome_reconcile.stamp_recon_detail` is THE single write site and
MERGES `STICKY_RECON_DETAIL_KEYS` forward from whatever is already on the row; a
pass that genuinely retires one says so explicitly (`cleared=`). Plus the
starvation gauge now counts only rows the loss guard cannot SKIP.

Runnable: pytest tests/test_attribution_marker_persist_0902.py -v  (DB-free)
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import outcome_reconcile as orc

SID = 19471
_APP_ROOT = pathlib.Path(orc.__file__).resolve().parents[4]


# ── stand-ins (no DB, no HTTP) ───────────────────────────────────────────────
class _Res:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _FakeDb:
    """Empty ledger + empty collision probe. Any other SQL is a test failure."""

    def __init__(self):
        self.sql = []

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.sql.append(sql)
        if "FROM momentum_fill_outcomes" in sql:
            return _Res([])
        if "momentum_automation_outcomes" in sql:
            return _Res([])
        if "FROM trading_trades" in sql:
            return _Res([(0,)])
        raise AssertionError(f"unexpected SQL in DB-free test: {sql}")

    def commit(self):
        pass

    def rollback(self):
        pass


def _pair(
    *,
    sid=SID,
    detail=None,
    recon_status=None,
    outcome_class="cancelled_pre_entry",
    le=None,
    terminal_at=datetime(2026, 9, 2, 13, 41, 8),
):
    """The LIVE shape of the 115 rows: alpaca_spot, terminal, NO entry evidence."""
    envelope = {"trade_cycles": 0} if le is None else le
    sess = SimpleNamespace(
        id=sid, symbol="CANF", execution_family="alpaca_spot", mode="live",
        state="live_finished", started_at=datetime(2026, 9, 2, 11, 3, 22),
        ended_at=datetime(2026, 9, 2, 13, 41, 8),
        risk_snapshot_json={"momentum_live_execution": envelope},
    )
    outcome = SimpleNamespace(
        id=200000 + sid, session_id=sid, symbol="CANF", mode="live",
        execution_family="alpaca_spot", outcome_class=outcome_class,
        terminal_at=terminal_at, realized_pnl_usd=None, return_bps=None,
        broker_recon_status=recon_status, broker_realized_pnl_usd=None,
        broker_return_bps=None, broker_notional_basis_usd=None, broker_win=None,
        broker_divergence_usd=None, broker_reconciled_at=None,
        broker_recon_detail_json=detail,
    )
    return outcome, sess


def _readable_empty(symbol, after, until):
    return {"readable": True, "orders": [], "truncated": False}


def _unreadable(symbol, after, until):
    return {"readable": False, "orders": [], "error": "credentials rotated"}


def _batch_db(rows, *, entry_event_sids=(), events_readable=True):
    """`entry_event_sids` stands in for `trading_automation_events` rows of type
    live_entry_filled / live_exit_filled / live_partial_exit_filled — the half of
    `_loss_history_entry_classification` that is not derivable from the ORM objects
    in hand. `events_readable=False` makes that probe raise, which must be
    fail-closed."""

    class _Q:
        def __init__(self, result):
            self._result = result

        def join(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def distinct(self, *a, **k):
            return self

        def all(self):
            if self._result is None:
                raise AssertionError("events table unavailable")
            return list(self._result)

    class _Db(_FakeDb):
        def query(self, *a, **k):
            # The batch query selects (outcome, session); the entry-event probe
            # selects a single column.
            if len(a) >= 2:
                return _Q(rows)
            return _Q([(int(s),) for s in entry_event_sids] if events_readable else None)

    return _Db()


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(orc.settings, "chili_momentum_broker_truth_reconciliation_enabled", True, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_enabled", True, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_grace_seconds", 900, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 20, raising=False)


# ═════════════════════════════════════════════════════════════════════════════
# (1) THE ONE WRITE SITE — no other writer may replace the markers away
# ═════════════════════════════════════════════════════════════════════════════
def test_a_whole_dict_replacing_write_cannot_erase_the_markers():
    """`_apply_cancelled_pre_entry_orphan_truth` writes `{**truth, "status": ...}` — a WHOLE-dict
    replace. Through the one write site it keeps the skip/backoff state."""
    o, _s = _pair(detail={
        "status": "unreconciled_no_fills",
        "attribution_no_entry_evidence_proven_empty": True,
        "attribution_terminal_at": "2026-09-02T13:41:08",
        "attribution_next_retry_utc": "2026-09-02T21:00:00",
        "attribution_attempts": 3,
        "attribution_retry_blocking": False,
        "attribution_ledger_release_done": True,
        "divergence_event_emitted": True,
    })
    written = orc.stamp_recon_detail(o, {"broker_truth": {"qty": 165}, "status": "fee_unconfirmed"})
    assert written is o.broker_recon_detail_json
    assert written["status"] == "fee_unconfirmed"          # the new write still wins
    for key in orc.STICKY_RECON_DETAIL_KEYS:
        assert key in written, f"{key} was erased by a whole-dict replace"
    assert written["attribution_attempts"] == 3


def test_the_new_write_always_wins_where_it_sets_a_key():
    prior = {"attribution_next_retry_utc": "2026-09-02T21:00:00", "attribution_attempts": 3}
    new = orc.merge_recon_detail(prior, {"attribution_attempts": 4})
    assert new["attribution_attempts"] == 4
    assert new["attribution_next_retry_utc"] == "2026-09-02T21:00:00"


def test_an_explicitly_cleared_marker_is_never_resurrected():
    """A CONVERGED read drops its retry horizon on purpose. A merge that brought
    it back would hold a settled row out of a later re-touch — the exact hazard
    the `cleared` set exists for."""
    prior = {"attribution_next_retry_utc": "2026-09-02T21:00:00", "attribution_attempts": 3}
    new = orc.merge_recon_detail(prior, {"status": "reconciled"},
                                 cleared={"attribution_next_retry_utc", "attribution_attempts"})
    assert "attribution_next_retry_utc" not in new
    assert "attribution_attempts" not in new


def test_attribution_version_is_never_sticky():
    """Carrying `attribution_version` across a pass that attributed NOTHING makes a
    terminal status immutable off a ledger-only label — the CANF-19471 −78.13
    instead of −186.98 undercount. It must stay out of the sticky set."""
    assert "attribution_version" not in orc.STICKY_RECON_DETAIL_KEYS
    assert "broker_attribution" not in orc.STICKY_RECON_DETAIL_KEYS
    new = orc.merge_recon_detail({"attribution_version": 2, "broker_attribution": {"attr_status": "flat"}},
                                 {"status": "unreconciled_no_fills"})
    assert "attribution_version" not in new
    assert "broker_attribution" not in new


def test_every_writer_of_the_detail_column_goes_through_the_one_write_site():
    """NEGATIVE CONTROL (verified against the unfixed tree: this fails on
    app/services/trading/momentum_neural/alpaca_reconcile.py:3768, which assigns
    `outcome.broker_recon_detail_json = {**truth, ...}` directly).

    AST, not regex: an assignment anywhere in app/ that does not route through
    `stamp_recon_detail` can silently erase the markers again.

    Attribute assignment is NOT the only way to write the column, so the walk also
    catches the four shapes a backfill or repair script naturally reaches for —
    `setattr(row, "broker_recon_detail_json", ...)`, a bulk ORM
    `.update({...broker_recon_detail_json: ...})`, in-place mutation
    (`row.broker_recon_detail_json.update(...)` / `.pop(...)` / `.clear()`, which
    also never marks the JSONB column dirty), and raw
    `UPDATE ... SET broker_recon_detail_json = ...` SQL — and it scans `scripts/`
    as well as `app/`, because the operator's recon drivers live there."""
    _COL = "broker_recon_detail_json"
    _MUTATORS = {"update", "pop", "clear", "setdefault", "popitem"}
    offenders = []
    sql_offenders = []
    roots = [p for p in (_APP_ROOT / "app", _APP_ROOT / "scripts") if p.exists()]
    for root in roots:
        for path in root.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
                tree = ast.parse(text)
            except (SyntaxError, UnicodeDecodeError, OSError):  # pragma: no cover - not our files
                continue
            rel = path.relative_to(_APP_ROOT)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        if isinstance(t, ast.Attribute) and t.attr == _COL:
                            offenders.append(f"{rel}:{node.lineno}")
                    continue
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                # setattr(row, "broker_recon_detail_json", ...)
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "setattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == _COL
                ):
                    offenders.append(f"{rel}:{node.lineno}")
                    continue
                if not isinstance(fn, ast.Attribute):
                    continue
                # row.broker_recon_detail_json.update(...) / .pop(...) / .clear()
                if (
                    fn.attr in _MUTATORS
                    and isinstance(fn.value, ast.Attribute)
                    and fn.value.attr == _COL
                ):
                    offenders.append(f"{rel}:{node.lineno}")
                    continue
                # q.update({Model.broker_recon_detail_json: ...}) / {"...": ...}
                if fn.attr == "update":
                    for arg in list(node.args) + [kw.value for kw in node.keywords]:
                        if not isinstance(arg, ast.Dict):
                            continue
                        for k in arg.keys:
                            if (isinstance(k, ast.Attribute) and k.attr == _COL) or (
                                isinstance(k, ast.Constant) and k.value == _COL
                            ):
                                offenders.append(f"{rel}:{node.lineno}")
            # Raw SQL, which no AST walk can see inside a string literal.
            for lineno, line in enumerate(text.splitlines(), start=1):
                low = line.lower()
                if "update" in low and f"set {_COL}" in low.replace('"', "").replace("'", ""):
                    sql_offenders.append(f"{rel}:{lineno}")
    assert not sql_offenders, (
        "raw SQL writes the detail column, bypassing the sticky merge entirely: "
        f"{sorted(set(sql_offenders))}"
    )
    # The ONLY permitted assignment is the one inside `stamp_recon_detail` itself.
    src = ast.parse(pathlib.Path(orc.__file__).read_text(encoding="utf-8"))
    allowed = set()
    for node in ast.walk(src):
        if isinstance(node, ast.FunctionDef) and node.name == "stamp_recon_detail":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Attribute) and t.attr == "broker_recon_detail_json":
                            allowed.add(
                                f"{pathlib.Path(orc.__file__).resolve().relative_to(_APP_ROOT)}:{sub.lineno}"
                            )
    assert allowed, "stamp_recon_detail must own the assignment"
    assert set(offenders) == allowed, (
        "these writers replace broker_recon_detail_json without merging the sticky "
        f"attribution markers: {sorted(set(offenders) - allowed)}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# (2) CONVERGENCE — the measured cycle must become a monotone climb
# ═════════════════════════════════════════════════════════════════════════════
def _no_evidence_population(n):
    return [_pair(sid=19000 + i,
                  terminal_at=datetime(2026, 9, 2, 13, 0, 0) + timedelta(seconds=i))
            for i in range(n)]


def test_consecutive_passes_converge_instead_of_cycling(monkeypatch):
    """THE HEADLINE. 115 no-entry-evidence rows, budget 20, a readable listing that
    owns nothing. `skipped_no_broker_read_needed` must climb monotonically to 115
    and `broker_reads` must fall to 0 — and an interleaved whole-dict-replacing
    write (the orphan-repair shape) must not reset the series."""
    monkeypatch.setattr(orc, "_default_alpaca_orders_reader", _readable_empty, raising=False)
    rows = _no_evidence_population(115)
    db = _batch_db(rows)
    skipped, reads, starved = [], [], []
    for i in range(9):
        out = orc.reconcile_momentum_outcomes_to_broker_truth(db, lookback_days=2.0, day_net_advisory=False)
        skipped.append(out["skipped_no_broker_read_needed"])
        reads.append(out["broker_reads"])
        starved.append(out["loss_guard_blind_starved"])
        # Another writer replaces the whole detail json on one row every pass.
        orc.stamp_recon_detail(rows[i][0], {"source": "trade_row", "status": "unreconciled_no_fills"})

    assert skipped == sorted(skipped), f"the skip count must never fall back: {skipped}"
    assert skipped[0] == 0 and skipped[-1] == 115, skipped
    assert reads[0] == 20 and reads[-1] == 0, reads
    assert all(s == 0 for s in starved), starved


def test_the_proof_read_is_spent_exactly_once_per_row(monkeypatch):
    """The one-proof-read design is preserved: 115 rows, 115 proof reads TOTAL
    across the whole convergence, never 20 per pass forever."""
    calls = []

    def _reader(symbol, after, until):
        calls.append(symbol)
        return _readable_empty(symbol, after, until)

    monkeypatch.setattr(orc, "_default_alpaca_orders_reader", _reader, raising=False)
    db = _batch_db(_no_evidence_population(115))
    for _ in range(9):
        orc.reconcile_momentum_outcomes_to_broker_truth(db, lookback_days=2.0, day_net_advisory=False)
    assert len(calls) == 115, len(calls)


# ═════════════════════════════════════════════════════════════════════════════
# (3) THE ONE-PROOF-READ DESIGN IS NOT WEAKENED
# ═════════════════════════════════════════════════════════════════════════════
def test_no_entry_evidence_alone_never_earns_the_permanent_skip():
    """An envelope that shows nothing is what a LOST adoption write looks like.
    Before any read, the plan must still spend the proof read."""
    o, s = _pair()
    plan = orc.broker_read_plan(o, s)
    assert plan["read"] is True and plan["reason"] == "no_entry_evidence_proof_read"


def test_an_unreadable_proof_read_never_stamps_proven_empty():
    o, s = _pair()
    orc.reconcile_one_outcome(_FakeDb(), o, s, read_plan=orc.broker_read_plan(o, s),
                              broker_orders_reader=_unreadable)
    assert "attribution_no_entry_evidence_proven_empty" not in o.broker_recon_detail_json


def test_a_readable_empty_listing_earns_it_and_the_next_pass_skips():
    o, s = _pair()
    orc.reconcile_one_outcome(_FakeDb(), o, s, read_plan=orc.broker_read_plan(o, s),
                              broker_orders_reader=_readable_empty)
    assert o.broker_recon_detail_json["attribution_no_entry_evidence_proven_empty"] is True
    plan = orc.broker_read_plan(o, s)
    assert plan["read"] is False and plan["reason"] == orc.ATTR_SKIPPED_NO_ENTRY_EVIDENCE


def test_a_spent_read_that_comes_back_unreadable_retires_an_inherited_proof():
    """A changed `terminal_at` forces a re-read of a proven-empty row. If THAT read
    is unreadable, the merge must not carry the old proof forward onto the new
    terminal_at — that would be a permanent skip no readable listing justified."""
    o, s = _pair()
    orc.reconcile_one_outcome(_FakeDb(), o, s, read_plan=orc.broker_read_plan(o, s),
                              broker_orders_reader=_readable_empty)
    assert o.broker_recon_detail_json["attribution_no_entry_evidence_proven_empty"] is True
    o.terminal_at = datetime(2026, 9, 2, 14, 10, 0)          # the row re-terminalized
    plan = orc.broker_read_plan(o, s)
    assert plan["read"] is True and plan["reason"] == "terminal_at_changed"
    orc.reconcile_one_outcome(_FakeDb(), o, s, read_plan=plan, broker_orders_reader=_unreadable)
    assert "attribution_no_entry_evidence_proven_empty" not in o.broker_recon_detail_json
    assert orc.broker_read_plan(o, s, now=datetime(2027, 1, 1))["read"] is True


def test_a_converged_read_still_drops_its_retry_horizon():
    """The `cleared` path end-to-end: an armed horizon must NOT survive a FLAT
    verdict just because the merge carries markers forward."""
    entry = SimpleNamespace(order_id="E1", client_order_id=f"chili_ml_e_{SID}_a", side="buy",
                            status="filled", filled_size=100.0, average_filled_price=4.0,
                            product_id="CANF", raw={"filled_at": "2026-09-02T11:10:19+00:00"})
    exit_ = SimpleNamespace(order_id="X1", client_order_id=f"chili_ml_s_{SID}_a", side="sell",
                            status="filled", filled_size=100.0, average_filled_price=3.5,
                            product_id="CANF", raw={"filled_at": "2026-09-02T11:20:19+00:00"})
    o, s = _pair(
        le={"entry_order_id": "E1", "trade_cycles": 1},
        outcome_class="stop_loss",
        detail={"attribution_next_retry_utc": "2026-09-02T21:00:00", "attribution_attempts": 3,
                "attribution_retry_blocking": False},
    )
    o.terminal_at = datetime(2026, 9, 2, 13, 41, 8)
    orc.reconcile_one_outcome(
        _FakeDb(), o, s, read_plan={"read": True, "reason": "attribute"},
        broker_orders_reader=lambda *a: {"readable": True, "orders": [entry, exit_], "truncated": False},
    )
    assert o.broker_recon_status == orc.STATUS_RECONCILED
    for key in ("attribution_next_retry_utc", "attribution_attempts", "attribution_retry_blocking"):
        assert key not in o.broker_recon_detail_json, key


# ═════════════════════════════════════════════════════════════════════════════
# (4) THE STARVATION ALARM MUST MEAN WHAT IT SAYS
# ═════════════════════════════════════════════════════════════════════════════
def test_the_alarm_ignores_rows_the_loss_guard_skips_outright(monkeypatch):
    """95 budget-deferred `cancelled_pre_entry` rows fired
    "LOSS-GUARD ROWS STARVED BY THE READ BUDGET" on EVERY pass. The guard
    (`_loss_history_entry_classification` → `not_entered`) skips exactly that
    class, so none of them can gap the day or disarm the lane — counting them
    trains the operator to ignore the one alarm that matters."""
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 1, raising=False)
    monkeypatch.setattr(orc, "_default_alpaca_orders_reader", _readable_empty, raising=False)
    rows = _no_evidence_population(4)
    out = orc.reconcile_momentum_outcomes_to_broker_truth(_batch_db(rows), lookback_days=2.0,
                                                          day_net_advisory=False)
    assert out["skipped_broker_budget"] == 3
    assert out["loss_guard_blind_starved"] == 0
    assert out["skipped_budget_guard_skips_row"] == 3


def test_a_genuinely_blind_row_still_raises_the_alarm(monkeypatch):
    """Fail-closed: an entered row the guard cannot use and cannot skip is exactly
    the 85-minute arming outage, and must still be counted."""
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 1, raising=False)
    monkeypatch.setattr(orc, "_default_alpaca_orders_reader", _readable_empty, raising=False)
    blind = _pair(sid=19471, outcome_class="stop_loss", le={"entry_order_id": "E1", "trade_cycles": 1},
                  terminal_at=datetime(2026, 9, 2, 13, 0, 0))
    blind[0].realized_pnl_usd = -48.97
    other = _pair(sid=19472, outcome_class="stop_loss", le={"entry_order_id": "E2", "trade_cycles": 1},
                  terminal_at=datetime(2026, 9, 2, 12, 0, 0))
    other[0].realized_pnl_usd = -12.0
    out = orc.reconcile_momentum_outcomes_to_broker_truth(_batch_db([blind, other]), lookback_days=2.0,
                                                          day_net_advisory=False)
    assert out["skipped_broker_budget"] == 1
    assert out["loss_guard_blind_starved"] == 1
    assert out["skipped_budget_guard_skips_row"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# (5) THE ALARM GATE MUST *BE* THE LOSS GUARD'S RULE, NOT A COPY OF IT
# ═════════════════════════════════════════════════════════════════════════════
# A hand-written mirror of `_loss_history_entry_classification` was wrong in three
# ways, ALL of them silencing the alarm on a row that genuinely disarms the
# account. `_loss_guard_can_block` now delegates to the classifier itself; these
# tests pin that equivalence so it cannot drift back.
_GUARD_SHAPES = [
    # (label, outcome attrs, envelope, entry_event_seen)
    ("control: truly empty never-entered row", {}, {"trade_cycles": 0}, False),
    ("entry EVENT seen, envelope empty", {}, {"trade_cycles": 0}, True),
    ("snapshot proof: le realized_pnl_usd", {}, {"realized_pnl_usd": 0.0}, False),
    ("snapshot proof: le last_exit_entry_price", {}, {"last_exit_entry_price": 4.0}, False),
    ("broker notional == 0.0", {"broker_notional_basis_usd": 0.0}, {"trade_cycles": 0}, False),
    ("broker notional == -3.0", {"broker_notional_basis_usd": -3.0}, {"trade_cycles": 0}, False),
    ("broker notional == 500.0", {"broker_notional_basis_usd": 500.0}, {"trade_cycles": 0}, False),
    ("nonzero economics", {"realized_pnl_usd": -48.97}, {"trade_cycles": 0}, False),
    ("entered class", {}, {"entry_order_id": "E1"}, False),
]


@pytest.mark.parametrize("label,attrs,envelope,seen", _GUARD_SHAPES, ids=[s[0] for s in _GUARD_SHAPES])
def test_the_alarm_gate_never_disagrees_with_the_loss_guard_classifier(label, attrs, envelope, seen):
    """THE regression. Every shape below where the guard says `conflict` GAPS the ET
    day and reports account-wide `loss_guard_history_unavailable` — the 85-minute
    arming outage of 2026-09-02 — while the old hand-written mirror returned False
    and the "LOSS-GUARD ROWS STARVED" warning stayed silent. Divergence in EITHER
    direction is a failure; divergence toward silence is the dangerous one."""
    from app.services.trading.momentum_neural import risk_policy as rp

    outcome_class = "stop_loss" if label == "entered class" else "cancelled_pre_entry"
    o, s = _pair(outcome_class=outcome_class, le=envelope)
    for k, v in attrs.items():
        setattr(o, k, v)
    authority = rp._loss_history_entry_classification(o, s, entry_event_seen=seen)
    mirror = orc._loss_guard_can_block(o, s, entry_event_seen=seen)
    assert mirror is (authority != "not_entered"), (
        f"{label}: guard says {authority!r} (blocks={authority != 'not_entered'}) "
        f"but the alarm gate says can_block={mirror}"
    )


def test_a_lost_adoption_entry_event_still_raises_the_alarm(monkeypatch):
    """CANF-19471 exactly: the adoption write is LOST so the envelope is empty and
    the outcome lands `cancelled_pre_entry`, but a `live_entry_filled` event exists.
    The guard calls that `conflict` and disarms the whole account; the row sorts
    into the LOWEST priority class, so it is the FIRST thing the budget drops. It
    must be counted as starved, not filed under "the guard skips this anyway"."""
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 1, raising=False)
    monkeypatch.setattr(orc, "_default_alpaca_orders_reader", _readable_empty, raising=False)
    rows = _no_evidence_population(3)
    lost = rows[1][0].session_id
    out = orc.reconcile_momentum_outcomes_to_broker_truth(
        _batch_db(rows, entry_event_sids=[lost]), lookback_days=2.0, day_net_advisory=False,
    )
    assert out["skipped_broker_budget"] == 2
    assert out["loss_guard_blind_starved"] == 1, out
    assert out["skipped_budget_guard_skips_row"] == 1, out


def test_a_nonpositive_broker_notional_still_raises_the_alarm(monkeypatch):
    """`_loss_history_notional_state` -> `nonpositive` is a `conflict`, and that check
    runs BEFORE the never-entered branch. The old gate only treated `> 0.0` as
    blocking, so a 0.0 basis fell through to the silent bucket."""
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 1, raising=False)
    monkeypatch.setattr(orc, "_default_alpaca_orders_reader", _readable_empty, raising=False)
    rows = _no_evidence_population(2)
    rows[0][0].broker_notional_basis_usd = 0.0   # oldest → sorted last → budget-deferred
    out = orc.reconcile_momentum_outcomes_to_broker_truth(_batch_db(rows), lookback_days=2.0,
                                                          day_net_advisory=False)
    assert out["skipped_broker_budget"] == 1, out
    assert out["loss_guard_blind_starved"] == 1, out
    assert out["skipped_budget_guard_skips_row"] == 0, out


def test_an_unreadable_entry_event_probe_is_fail_closed(monkeypatch):
    """Not knowing whether an entry event exists is not the same as knowing there is
    none. A failed probe must count every deferred row as blocking."""
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 1, raising=False)
    monkeypatch.setattr(orc, "_default_alpaca_orders_reader", _readable_empty, raising=False)
    rows = _no_evidence_population(3)
    out = orc.reconcile_momentum_outcomes_to_broker_truth(
        _batch_db(rows, events_readable=False), lookback_days=2.0, day_net_advisory=False,
    )
    assert out["loss_guard_blind_starved"] == 2, out
    assert out["skipped_budget_guard_skips_row"] == 0, out


# ═════════════════════════════════════════════════════════════════════════════
# (6) THE `ledger_settled_terminal` RELEASE MUST ACTUALLY RELEASE
# ═════════════════════════════════════════════════════════════════════════════
class _SettledLedgerDb(_FakeDb):
    """The ledger settled into a CLOSED round-trip while the row was skipped."""

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        # ONLY the `_aggregate_ledger` projection — the ledger is also queried for
        # its owned ORDER IDS, and answering that with these rows would fabricate
        # `listing_incomplete` anchors the session never recorded.
        if "SELECT side, leg_seq" in sql:
            return _Res([
                ("entry", 1, "broker_confirmed", 4.0, 100.0, 0.0, None, None, None, None),
                ("exit", 1, "broker_confirmed", 3.5, 100.0, 0.0, -50.0, 0.0, -50.0, 4.0),
            ])
        return super().execute(stmt, params)


def test_the_proof_short_circuits_the_horizon_so_popping_it_alone_is_a_no_op():
    """WHY the release has to retire the PROOF, not just the horizon:
    `broker_read_plan` tests `attribution_no_entry_evidence_proven_empty` BEFORE it
    ever looks at `attribution_next_retry_utc`."""
    o, s = _pair(detail={"attribution_no_entry_evidence_proven_empty": True,
                         "attribution_terminal_at": "2026-09-02T13:41:08"})
    assert orc.broker_read_plan(o, s, now=datetime(2030, 1, 1))["read"] is False
    o.broker_recon_detail_json.pop("attribution_no_entry_evidence_proven_empty")
    assert orc.broker_read_plan(o, s, now=datetime(2030, 1, 1))["read"] is True


def test_a_settled_ledger_releases_the_proof_and_the_row_reads_again():
    """The CANF-19471 cycle-2 shape. The row earned the permanent skip off a bounded
    listing that had not yet seen the late fill; then the FSM ledger settled into a
    closed round-trip, which CONTRADICTS "the broker owned nothing". The row must be
    re-checked against broker truth — carrying the proof through the release made
    that whole branch a no-op and pinned the ledger-only label forever."""
    o, s = _pair(detail={
        "attribution_no_entry_evidence_proven_empty": True,
        "attribution_terminal_at": "2026-09-02T13:41:08",
        "attribution_next_retry_utc": "2026-09-02T21:00:00",
        "attribution_attempts": 4,
    })
    plan = orc.broker_read_plan(o, s)
    assert plan["read"] is False and plan["reason"] == orc.ATTR_SKIPPED_NO_ENTRY_EVIDENCE
    detail = orc.reconcile_one_outcome(_SettledLedgerDb(), o, s, read_plan=plan,
                                       broker_orders_reader=_readable_empty)
    assert detail.get("attribution_backoff_released") == "ledger_settled_terminal"
    assert "attribution_no_entry_evidence_proven_empty" not in o.broker_recon_detail_json
    assert "attribution_next_retry_utc" not in o.broker_recon_detail_json
    assert "attribution_attempts" not in o.broker_recon_detail_json     # clean slate
    assert orc.broker_read_plan(o, s)["read"] is True, "the release must actually read"


def test_the_settled_ledger_release_fires_exactly_once():
    """Bounded: a re-read that comes back `no_owned_fills` re-stamps the proof, and
    without a durable one-shot latch the next skip would release AGAIN — a broker
    read every other pass forever, the churn the permanent skip exists to prevent."""
    o, s = _pair(detail={"attribution_no_entry_evidence_proven_empty": True,
                         "attribution_terminal_at": "2026-09-02T13:41:08"})
    db = _SettledLedgerDb()
    orc.reconcile_one_outcome(db, o, s, read_plan=orc.broker_read_plan(o, s),
                              broker_orders_reader=_readable_empty)
    assert o.broker_recon_detail_json["attribution_ledger_release_done"] is True
    # The released read is spent and the broker still owns nothing -> proof re-earned.
    plan = orc.broker_read_plan(o, s)
    assert plan["read"] is True
    orc.reconcile_one_outcome(db, o, s, read_plan=plan, broker_orders_reader=_readable_empty)
    assert o.broker_recon_detail_json["attribution_no_entry_evidence_proven_empty"] is True
    # ...and the latch survives, so the row never oscillates.
    detail = orc.reconcile_one_outcome(db, o, s, read_plan=orc.broker_read_plan(o, s),
                                       broker_orders_reader=_readable_empty)
    assert detail.get("attribution_backoff_released") != "ledger_settled_terminal"
    assert orc.broker_read_plan(o, s)["read"] is False


# ═════════════════════════════════════════════════════════════════════════════
# (7) THE WRITE SITE MUST BE SAFE FOR THE OBVIOUS READ-MODIFY-WRITE SHAPE
# ═════════════════════════════════════════════════════════════════════════════
def test_the_write_site_always_assigns_a_new_object():
    """`broker_recon_detail_json` is a plain `Column(JSONB)` with no `MutableDict`
    wrapper, so SQLAlchemy flags it dirty ONLY on assignment of a new object. A
    writer following the documented contract (`d = row.detail; d[k] = v;
    stamp_recon_detail(row, d)`) must not self-assign: that emits no UPDATE and the
    change is lost with no error, while the caller's own read-back still sees it."""
    o, _s = _pair(detail={"status": "unreconciled_no_fills",
                          "attribution_no_entry_evidence_proven_empty": True})
    before = o.broker_recon_detail_json
    d = o.broker_recon_detail_json
    d["fees_status"] = "settled"
    written = orc.stamp_recon_detail(o, d)
    assert written is not before, "self-assignment writes nothing to the DB"
    assert written["fees_status"] == "settled"
    assert written["attribution_no_entry_evidence_proven_empty"] is True
    # ...and `merge_recon_detail` is genuinely pure: the caller's dict is untouched.
    caller = {"status": "x"}
    merged = orc.merge_recon_detail({"attribution_attempts": 2}, caller)
    assert caller == {"status": "x"}
    assert merged["attribution_attempts"] == 2
