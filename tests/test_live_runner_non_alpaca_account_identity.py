from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner


class _Db:
    def __init__(self):
        self.flushes = 0

    def flush(self):
        self.flushes += 1


class _Query:
    def __init__(self, session):
        self.session = session

    def filter(self, *_args, **_kwargs):
        return self

    def with_for_update(self, **_kwargs):
        return self

    def one_or_none(self):
        return self.session


class _TickDb(_Db):
    def __init__(self, session):
        super().__init__()
        self.session = session

    def query(self, _model):
        return _Query(self.session)


class _RotatingAdapter:
    def __init__(self, identities: list[str]):
        self.identities = list(identities)
        self.identity_reads = 0
        self.place_calls = 0
        self.cancel_calls = 0

    def is_enabled(self):
        return True

    def get_account_identity_truth(self):
        self.identity_reads += 1
        identity = self.identities.pop(0) if len(self.identities) > 1 else self.identities[0]
        return {"readable": True, "identity": identity}

    def place_limit_order_gtc(self, **_kwargs):
        self.place_calls += 1
        return {"ok": True, "order_id": f"order-{self.place_calls}"}

    def cancel_order(self, _order_id):
        self.cancel_calls += 1
        return {"ok": True}


def _session(*, family: str = "coinbase_spot", frozen: str | None = "account-a"):
    snapshot = {}
    if frozen is not None:
        snapshot["non_alpaca_account_identity"] = frozen
    return SimpleNamespace(
        id=71,
        state=live_runner.STATE_LIVE_PENDING_ENTRY,
        execution_family=family,
        risk_snapshot_json=snapshot,
        correlation_id="account-fence-test",
    )


def _capture_events(monkeypatch: pytest.MonkeyPatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner,
        "_emit",
        lambda _db, _sess, event, payload: events.append((event, dict(payload))),
    )
    return events


def test_rotation_after_tick_start_blocks_literal_place_and_pauses(
    monkeypatch: pytest.MonkeyPatch,
):
    events = _capture_events(monkeypatch)
    db = _Db()
    sess = _session()
    adapter = _RotatingAdapter(["account-a", "account-b"])

    assert live_runner._non_alpaca_account_identity_fence(
        db,
        sess,
        adapter,
        phase="tick_start",
    ) is None
    guarded = live_runner._NonAlpacaAccountFencedAdapter(
        adapter,
        db=db,
        sess=sess,
    )
    result = guarded.place_limit_order_gtc(
        product_id="ACTU",
        side="buy",
        base_size="10",
        limit_price="2.50",
        client_order_id="entry-cid",
    )

    assert result["ok"] is False
    assert result["deferred"] is True
    assert result["pre_place_blocked"] is True
    assert result["broker_mutations"] == 0
    assert result["reason"] == "non_alpaca_account_identity_mismatch"
    assert adapter.place_calls == 0
    assert adapter.identity_reads == 2
    pause = sess.risk_snapshot_json["operator_pause"]
    assert pause["active"] is True
    marker = sess.risk_snapshot_json[
        "non_alpaca_account_identity_quarantined"
    ]
    assert marker["first_phase"] == "before_order_place"
    assert marker["last_phase"] == "before_order_place"
    assert marker["fingerprint"]["frozen_identity"] == "account-a"
    assert marker["fingerprint"]["current_identity"] == "account-b"
    assert len(events) == 1


def test_tick_start_mismatch_returns_deferred_quarantine_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
):
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_live_runner_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        live_runner,
        "_venue_broker_connected",
        lambda _family: True,
    )
    sess = _session()
    sess.mode = "live"
    sess.symbol = "ACTU"
    adapter = _RotatingAdapter(["account-b"])
    db = _TickDb(sess)

    result = live_runner.tick_live_session(
        db,
        sess.id,
        adapter_factory=lambda: adapter,
    )

    assert result["ok"] is True
    assert result["skipped"] == "non_alpaca_account_identity_quarantined"
    assert result["deferred"] is True
    assert result["broker_mutations"] == 0
    assert result["reason"] == "non_alpaca_account_identity_mismatch"
    assert adapter.place_calls == adapter.cancel_calls == 0
    assert adapter.identity_reads == 1
    assert sess.risk_snapshot_json["operator_pause"]["active"] is True
    assert len(events) == 1


def test_rotation_after_tick_start_blocks_literal_cancel(
    monkeypatch: pytest.MonkeyPatch,
):
    events = _capture_events(monkeypatch)
    db = _Db()
    sess = _session(family="robinhood_spot")
    adapter = _RotatingAdapter(["account-a", "account-b"])

    assert live_runner._non_alpaca_account_identity_fence(
        db,
        sess,
        adapter,
        phase="tick_start",
    ) is None
    guarded = live_runner._NonAlpacaAccountFencedAdapter(
        adapter,
        db=db,
        sess=sess,
    )
    result = guarded.cancel_order("working-order")

    assert result["ok"] is False
    assert result["deferred"] is True
    assert result["pre_cancel_blocked"] is True
    assert result["broker_mutations"] == 0
    assert result["reason"] == "non_alpaca_account_identity_mismatch"
    assert adapter.cancel_calls == 0
    assert adapter.identity_reads == 2
    assert events[0][1]["phase"] == "before_order_cancel"


def test_quarantine_is_idempotent_and_preserves_first_timestamps(
    monkeypatch: pytest.MonkeyPatch,
):
    events = _capture_events(monkeypatch)
    db = _Db()
    sess = _session()
    truth = {
        "ok": False,
        "applicable": True,
        "reason": "non_alpaca_account_identity_mismatch",
        "frozen_identity": "account-a",
        "current_identity": "account-b",
    }

    first = live_runner._quarantine_non_alpaca_account_identity(
        db,
        sess,
        phase="before_order_place",
        truth=truth,
    )
    first_marker = dict(
        sess.risk_snapshot_json["non_alpaca_account_identity_quarantined"]
    )
    first_pause = dict(sess.risk_snapshot_json["operator_pause"])
    second = live_runner._quarantine_non_alpaca_account_identity(
        db,
        sess,
        phase="before_order_cancel",
        truth=truth,
    )
    second_marker = sess.risk_snapshot_json[
        "non_alpaca_account_identity_quarantined"
    ]
    second_pause = sess.risk_snapshot_json["operator_pause"]

    assert first["reason"] == second["reason"]
    assert second_marker["first_quarantined_at_utc"] == first_marker[
        "first_quarantined_at_utc"
    ]
    assert second_marker["last_changed_at_utc"] == first_marker[
        "last_changed_at_utc"
    ]
    assert second_marker["first_phase"] == "before_order_place"
    assert second_marker["last_phase"] == "before_order_cancel"
    assert "phase" not in second_marker["fingerprint"]
    assert second_pause["paused_at_utc"] == first_pause["paused_at_utc"]
    assert len(events) == 1

    changed_truth = {**truth, "current_identity": "account-c"}
    live_runner._quarantine_non_alpaca_account_identity(
        db,
        sess,
        phase="before_order_place",
        truth=changed_truth,
    )
    changed_marker = sess.risk_snapshot_json[
        "non_alpaca_account_identity_quarantined"
    ]
    assert changed_marker["first_quarantined_at_utc"] == first_marker[
        "first_quarantined_at_utc"
    ]
    assert changed_marker["fingerprint"]["current_identity"] == "account-c"
    assert len(events) == 2


def test_missing_legacy_identity_blocks_without_adapter_read(
    monkeypatch: pytest.MonkeyPatch,
):
    _capture_events(monkeypatch)
    db = _Db()
    sess = _session(frozen=None)
    adapter = _RotatingAdapter(["must-not-be-read"])

    result = live_runner._non_alpaca_account_identity_fence(
        db,
        sess,
        adapter,
        phase="tick_start",
    )

    assert result is not None
    assert result["reason"] == "non_alpaca_account_identity_unfrozen"
    assert result["broker_mutations"] == 0
    assert adapter.identity_reads == 0


def test_alpaca_is_not_wrapped_or_read_by_non_alpaca_fence():
    db = _Db()
    sess = _session(family="alpaca_spot", frozen=None)
    adapter = _RotatingAdapter(["must-not-be-read"])

    assert live_runner._non_alpaca_account_identity_fence(
        db,
        sess,
        adapter,
        phase="tick_start",
    ) is None
    assert adapter.identity_reads == 0
