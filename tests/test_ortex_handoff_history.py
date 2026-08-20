from __future__ import annotations

import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import engine
from app.models.trading import BrainGraphNode, BrainNodeState
from app.services.trading.momentum_neural import pipeline
from app.services.trading.momentum_neural.ortex_handoff_history import (
    ORTEX_BATCH_STATUS_KEY,
    ORTEX_HANDOFF_HISTORY_KEY,
    ORTEX_HANDOFF_MAX_CANONICAL_BYTES,
    ORTEX_HANDOFF_RETENTION_SECONDS,
    ORTEX_HANDOFF_STRICT_CONTEXT_MAX_AGE_SECONDS,
    OrtexHandoffHistoryError,
    OrtexHandoffReason,
    inspect_ortex_handoff_history,
    lookup_ortex_handoff_manifest,
    note_ortex_handoff_lookup,
    ortex_handoff_runtime_metrics,
    preserve_ortex_handoff_state,
    stage_ortex_handoff_publication,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 19, 16, 0, tzinfo=UTC)


def _manifest(digit: str, *, decision_at: datetime | None = None) -> dict:
    return {
        "schema_version": "chili.ortex.squeeze-fuel-batch.v1",
        "decision_at": (decision_at or NOW).isoformat(),
        "complete": True,
        "quota_policy_sha256": "e" * 64,
        "selected_symbols": [],
        "members": [],
        "members_sha256": "f" * 64,
        "batch_sha256": digit * 64,
    }


def test_rotated_manifest_is_retained_by_exact_hash_and_deep_copied() -> None:
    first = _manifest("a")
    second = _manifest("b")
    initial = stage_ortex_handoff_publication(
        {"unrelated": {"keep": True}},
        manifest=first,
        displaced_at=NOW,
    )
    rotated = stage_ortex_handoff_publication(
        initial.local_state,
        manifest=second,
        displaced_at=NOW + timedelta(seconds=10),
    )

    assert rotated.local_state["unrelated"] == {"keep": True}
    assert rotated.local_state[ORTEX_BATCH_STATUS_KEY] == second
    assert rotated.metrics.entry_count == 1
    assert rotated.metrics.canonical_bytes > 0
    current = lookup_ortex_handoff_manifest(
        rotated.local_state,
        batch_sha256=second["batch_sha256"],
        observed_at=NOW + timedelta(seconds=10),
    )
    retained = lookup_ortex_handoff_manifest(
        rotated.local_state,
        batch_sha256=first["batch_sha256"],
        observed_at=NOW + timedelta(seconds=10),
    )
    assert current.reason is OrtexHandoffReason.CURRENT_HIT
    assert retained.reason is OrtexHandoffReason.HISTORY_HIT
    assert retained.manifest == first
    assert retained.manifest is not first
    retained.manifest["complete"] = False  # type: ignore[index]
    assert (
        rotated.local_state[ORTEX_HANDOFF_HISTORY_KEY]["entries"]
        [first["batch_sha256"]]["manifest"]["complete"]
        is True
    )


def test_same_hash_chunk_publication_is_byte_idempotent() -> None:
    first = _manifest("a")
    second = _manifest("b")
    state = stage_ortex_handoff_publication(
        {}, manifest=first, displaced_at=NOW
    ).local_state
    state = stage_ortex_handoff_publication(
        state,
        manifest=second,
        displaced_at=NOW + timedelta(seconds=10),
    ).local_state
    before = json.dumps(state, sort_keys=True, separators=(",", ":"))

    repeated = stage_ortex_handoff_publication(
        state,
        manifest=copy.deepcopy(second),
        displaced_at=NOW + timedelta(seconds=30),
    )

    assert repeated.changed is False
    assert json.dumps(
        repeated.local_state, sort_keys=True, separators=(",", ":")
    ) == before


def test_pruning_uses_displacement_clock_never_manifest_decision_at() -> None:
    ancient = _manifest("a", decision_at=NOW - timedelta(days=30))
    second = _manifest("b")
    third = _manifest("c")
    state = stage_ortex_handoff_publication(
        {}, manifest=ancient, displaced_at=NOW
    ).local_state
    state = stage_ortex_handoff_publication(
        state,
        manifest=second,
        displaced_at=NOW + timedelta(seconds=1),
    ).local_state

    at_boundary = stage_ortex_handoff_publication(
        state,
        manifest=third,
        displaced_at=(
            NOW + timedelta(seconds=1 + ORTEX_HANDOFF_RETENTION_SECONDS)
        ),
    )
    assert ancient["batch_sha256"] in (
        at_boundary.local_state[ORTEX_HANDOFF_HISTORY_KEY]["entries"]
    )

    fourth = _manifest("d")
    expired = stage_ortex_handoff_publication(
        at_boundary.local_state,
        manifest=fourth,
        displaced_at=(
            NOW
            + timedelta(seconds=2 + ORTEX_HANDOFF_RETENTION_SECONDS)
        ),
    )
    assert ancient["batch_sha256"] not in (
        expired.local_state[ORTEX_HANDOFF_HISTORY_KEY]["entries"]
    )
    assert expired.metrics.pruned_expired_total == 1


def test_restart_reader_expires_without_a_writer_after_exact_boundary() -> None:
    first = _manifest("a")
    second = _manifest("b")
    displaced_at = NOW + timedelta(seconds=1)
    state = stage_ortex_handoff_publication(
        {}, manifest=first, displaced_at=NOW
    ).local_state
    state = stage_ortex_handoff_publication(
        state,
        manifest=second,
        displaced_at=displaced_at,
    ).local_state
    restarted = json.loads(json.dumps(state))
    before = copy.deepcopy(restarted)

    boundary = lookup_ortex_handoff_manifest(
        restarted,
        batch_sha256=first["batch_sha256"],
        observed_at=(
            displaced_at + timedelta(seconds=ORTEX_HANDOFF_RETENTION_SECONDS)
        ),
    )
    expired = lookup_ortex_handoff_manifest(
        restarted,
        batch_sha256=first["batch_sha256"],
        observed_at=(
            displaced_at
            + timedelta(seconds=ORTEX_HANDOFF_RETENTION_SECONDS + 1)
        ),
    )

    assert boundary.reason is OrtexHandoffReason.HISTORY_HIT
    assert boundary.manifest == first
    assert expired.reason is OrtexHandoffReason.HISTORY_EXPIRED
    assert expired.manifest is None
    assert restarted == before


def test_unexpired_count_cap_rejects_without_mutating_input() -> None:
    first = _manifest("a")
    second = _manifest("b")
    third = _manifest("c")
    state = stage_ortex_handoff_publication(
        {}, manifest=first, displaced_at=NOW, max_entries=1
    ).local_state
    state = stage_ortex_handoff_publication(
        state,
        manifest=second,
        displaced_at=NOW + timedelta(seconds=1),
        max_entries=1,
    ).local_state
    before = copy.deepcopy(state)

    with pytest.raises(OrtexHandoffHistoryError) as caught:
        stage_ortex_handoff_publication(
            state,
            manifest=third,
            displaced_at=NOW + timedelta(seconds=2),
            max_entries=1,
        )

    assert caught.value.reason is OrtexHandoffReason.COUNT_CAP_EXCEEDED
    assert caught.value.metrics is not None
    assert caught.value.metrics.entry_count == 2
    assert state == before


def test_unexpired_canonical_byte_cap_rejects_without_eviction() -> None:
    first = _manifest("a")
    first["padding"] = "x" * 1024
    second = _manifest("b")
    state = stage_ortex_handoff_publication(
        {},
        manifest=first,
        displaced_at=NOW,
        max_canonical_bytes=128,
    ).local_state
    before = copy.deepcopy(state)

    with pytest.raises(OrtexHandoffHistoryError) as caught:
        stage_ortex_handoff_publication(
            state,
            manifest=second,
            displaced_at=NOW + timedelta(seconds=1),
            max_canonical_bytes=128,
        )

    assert (
        caught.value.reason
        is OrtexHandoffReason.CANONICAL_BYTE_CAP_EXCEEDED
    )
    assert state == before


def test_history_metrics_and_manifest_tamper_fail_closed() -> None:
    first = _manifest("a")
    second = _manifest("b")
    state = stage_ortex_handoff_publication(
        {}, manifest=first, displaced_at=NOW
    ).local_state
    state = stage_ortex_handoff_publication(
        state,
        manifest=second,
        displaced_at=NOW + timedelta(seconds=1),
    ).local_state

    for mutate in ("metrics", "manifest"):
        tampered = copy.deepcopy(state)
        history = tampered[ORTEX_HANDOFF_HISTORY_KEY]
        if mutate == "metrics":
            history["metrics"]["canonical_bytes"] += 1
        else:
            history["entries"][first["batch_sha256"]]["manifest"][
                "batch_sha256"
            ] = "c" * 64
        lookup = lookup_ortex_handoff_manifest(
            tampered,
            batch_sha256=first["batch_sha256"],
            observed_at=NOW + timedelta(seconds=1),
        )
        assert lookup.manifest is None
        assert lookup.reason is OrtexHandoffReason.HISTORY_INVALID
        with pytest.raises(OrtexHandoffHistoryError) as caught:
            inspect_ortex_handoff_history(tampered)
        assert caught.value.reason is OrtexHandoffReason.HISTORY_INVALID


def test_future_displacement_clock_fails_closed_for_reader_and_writer() -> None:
    first = _manifest("a")
    second = _manifest("b")
    third = _manifest("c")
    state = stage_ortex_handoff_publication(
        {}, manifest=first, displaced_at=NOW
    ).local_state
    state = stage_ortex_handoff_publication(
        state,
        manifest=second,
        displaced_at=NOW + timedelta(seconds=10),
    ).local_state

    lookup = lookup_ortex_handoff_manifest(
        state,
        batch_sha256=first["batch_sha256"],
        observed_at=NOW + timedelta(seconds=5),
    )
    assert lookup.manifest is None
    assert lookup.reason is OrtexHandoffReason.HISTORY_INVALID
    with pytest.raises(OrtexHandoffHistoryError) as caught:
        stage_ortex_handoff_publication(
            state,
            manifest=third,
            displaced_at=NOW + timedelta(seconds=5),
        )
    assert caught.value.reason is OrtexHandoffReason.HISTORY_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retention_seconds", ORTEX_HANDOFF_RETENTION_SECONDS + 1),
        ("retention_seconds", ORTEX_HANDOFF_RETENTION_SECONDS - 1),
        ("max_entries", 33),
        ("max_entries", 31),
        (
            "max_canonical_bytes",
            ORTEX_HANDOFF_MAX_CANONICAL_BYTES + 1,
        ),
        (
            "max_canonical_bytes",
            ORTEX_HANDOFF_MAX_CANONICAL_BYTES - 1,
        ),
    ],
)
def test_persisted_policy_cannot_self_authorize_different_limits(
    field: str,
    value: int,
) -> None:
    first = _manifest("a")
    second = _manifest("b")
    state = stage_ortex_handoff_publication(
        {}, manifest=first, displaced_at=NOW
    ).local_state
    state = stage_ortex_handoff_publication(
        state,
        manifest=second,
        displaced_at=NOW + timedelta(seconds=1),
    ).local_state
    tampered = copy.deepcopy(state)
    tampered[ORTEX_HANDOFF_HISTORY_KEY][field] = value

    lookup = lookup_ortex_handoff_manifest(
        tampered,
        batch_sha256=first["batch_sha256"],
        observed_at=NOW + timedelta(seconds=1),
    )

    assert lookup.reason is OrtexHandoffReason.HISTORY_INVALID
    assert lookup.manifest is None
    with pytest.raises(OrtexHandoffHistoryError) as caught:
        inspect_ortex_handoff_history(tampered)
    assert caught.value.reason is OrtexHandoffReason.HISTORY_INVALID


def test_restart_json_round_trip_preserves_exact_history_lookup() -> None:
    first = _manifest("a")
    second = _manifest("b")
    state = stage_ortex_handoff_publication(
        {}, manifest=first, displaced_at=NOW
    ).local_state
    state = stage_ortex_handoff_publication(
        state,
        manifest=second,
        displaced_at=NOW + timedelta(seconds=1),
    ).local_state
    restarted = json.loads(json.dumps(state))

    lookup = lookup_ortex_handoff_manifest(
        restarted,
        batch_sha256=first["batch_sha256"],
        observed_at=NOW + timedelta(seconds=1),
    )

    assert lookup.reason is OrtexHandoffReason.HISTORY_HIT
    assert lookup.manifest == first
    metrics = inspect_ortex_handoff_history(restarted)
    assert metrics.entry_count == 1
    assert metrics.retention_seconds >= (
        ORTEX_HANDOFF_STRICT_CONTEXT_MAX_AGE_SECONDS
    )
    assert metrics.max_canonical_bytes <= ORTEX_HANDOFF_MAX_CANONICAL_BYTES


def test_non_ortex_hub_rewrite_preserves_current_and_history() -> None:
    first = _manifest("a")
    second = _manifest("b")
    source = stage_ortex_handoff_publication(
        {}, manifest=first, displaced_at=NOW
    ).local_state
    source = stage_ortex_handoff_publication(
        source,
        manifest=second,
        displaced_at=NOW + timedelta(seconds=1),
    ).local_state

    rewritten = preserve_ortex_handoff_state(
        source,
        {"last_tick_utc": (NOW + timedelta(seconds=2)).isoformat()},
    )

    assert rewritten[ORTEX_BATCH_STATUS_KEY] == source[ORTEX_BATCH_STATUS_KEY]
    assert rewritten[ORTEX_HANDOFF_HISTORY_KEY] == source[
        ORTEX_HANDOFF_HISTORY_KEY
    ]
    assert rewritten[ORTEX_HANDOFF_HISTORY_KEY] is not source[
        ORTEX_HANDOFF_HISTORY_KEY
    ]


def test_runtime_observability_has_fixed_typed_hit_miss_counters() -> None:
    before = ortex_handoff_runtime_metrics()

    note_ortex_handoff_lookup(OrtexHandoffReason.CURRENT_HIT)
    note_ortex_handoff_lookup(OrtexHandoffReason.HISTORY_HIT)
    note_ortex_handoff_lookup(OrtexHandoffReason.MISSING)
    note_ortex_handoff_lookup(OrtexHandoffReason.HISTORY_EXPIRED)
    note_ortex_handoff_lookup(OrtexHandoffReason.HISTORY_INVALID)

    after = ortex_handoff_runtime_metrics()
    assert after.current_hits == before.current_hits + 1
    assert after.history_hits == before.history_hits + 1
    assert after.misses == before.misses + 2
    assert after.invalid_history == before.invalid_history + 1


def test_postgres_hub_lock_serializes_and_rollback_preserves_history(
    db,
) -> None:
    first = _manifest("a")
    initial = stage_ortex_handoff_publication(
        {}, manifest=first, displaced_at=NOW
    ).local_state
    db.execute(
        pg_insert(BrainGraphNode)
        .values(
            id=pipeline.HUB_NODE_ID,
            domain="trading",
            graph_version=1,
            node_type="momentum_intel",
            layer=1,
            label="Ortex handoff concurrency test hub",
            enabled=True,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    db.execute(
        pg_insert(BrainNodeState)
        .values(
            node_id=pipeline.HUB_NODE_ID,
            activation_score=0.9,
            confidence=0.9,
            local_state=initial,
            updated_at=NOW.replace(tzinfo=None),
        )
        .on_conflict_do_update(
            index_elements=["node_id"],
            set_={
                "local_state": initial,
                "updated_at": NOW.replace(tzinfo=None),
            },
        )
    )
    db.commit()

    second = _manifest("b")
    third = _manifest("c")
    first_locked = threading.Event()
    release_first = threading.Event()

    def write(manifest: dict, offset: int, *, hold: bool) -> None:
        with Session(engine) as session:
            hub = pipeline._get_or_create_hub_state_for_update(session)
            publication = stage_ortex_handoff_publication(
                hub.local_state,
                manifest=manifest,
                displaced_at=NOW + timedelta(seconds=offset),
            )
            hub.local_state = publication.local_state
            if hold:
                first_locked.set()
                assert release_first.wait(timeout=5.0)
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(write, second, 1, hold=True)
        assert first_locked.wait(timeout=5.0)
        second_future = pool.submit(write, third, 2, hold=False)
        time.sleep(0.1)
        assert second_future.done() is False
        release_first.set()
        first_future.result(timeout=5.0)
        second_future.result(timeout=5.0)

    db.expire_all()
    final = db.get(BrainNodeState, pipeline.HUB_NODE_ID)
    assert final is not None
    state = dict(final.local_state or {})
    assert state[ORTEX_BATCH_STATUS_KEY] == third
    assert set(state[ORTEX_HANDOFF_HISTORY_KEY]["entries"]) == {
        first["batch_sha256"],
        second["batch_sha256"],
    }
    committed = copy.deepcopy(state)
    db.rollback()

    fourth = _manifest("d")
    with Session(engine) as session:
        locked = pipeline._get_or_create_hub_state_for_update(session)
        staged = stage_ortex_handoff_publication(
            locked.local_state,
            manifest=fourth,
            displaced_at=NOW + timedelta(seconds=3),
        )
        locked.local_state = staged.local_state
        session.flush()
        session.rollback()
    with Session(engine) as verifier:
        persisted = verifier.get(BrainNodeState, pipeline.HUB_NODE_ID)
        assert persisted is not None
        assert persisted.local_state == committed
