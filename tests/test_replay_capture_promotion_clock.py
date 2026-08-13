"""The promotion boundary must be checked with BOUNDS, not clock equality.

This file exists because the whole existing suite could not see the bug. Every
promotion test drives the runtime with `_ManualClock` -- a frozen, settable clock
-- so two consecutive reads return the identical value and an equality check
against a second read always passes. In production `wall_clock` is
`lambda: datetime.now(UTC)`, the two reads land microseconds apart, and the guard
could never be satisfied.

Measured on this box: 20,000 back-to-back `datetime.now(UTC)` reads yielded 18
distinct values, minimum non-zero delta 521us -- while the work between the two
reads (hot-symbol lease acquire, pretrigger ring scan with provenance hashing,
sha256 inventory hash over every event, full CaptureEvent construction) takes far
longer than that. So the lane latched `promotion_boundary_clock_mismatch` on every
promotion that carried data, and admitted 0 of ~30k attempts.

These tests therefore use an ADVANCING clock. A frozen clock cannot prove anything
here -- the last test pins exactly that.
"""

from datetime import timedelta

import pytest

from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    BoundedPreTriggerRing,
    CaptureClocks,
    CaptureContractError,
    CaptureProducerLifecycleRuntime,
    CaptureStream,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    resolve_capture_source_payload,
)

from tests.test_replay_capture_producer_lifecycle import (
    BASE,
    _identity,
    _producer,
    _resource_binding,
)


class _AdvancingClock:
    """A production-SHAPED clock: every read returns a strictly later instant.

    Deterministic rather than `datetime.now(UTC)` so the test cannot flake, but it
    reproduces the one property that matters and that `_ManualClock` hides: two
    reads are never equal.
    """

    def __init__(self, start, step=timedelta(microseconds=521)):
        # Start ahead of the fixture stamps: the trusted read must never
        # precede an input received_at, which is a separate guard.
        self._now = start
        self._step = step

    def __call__(self):
        self._now = self._now + self._step
        return self._now

    def peek(self):
        return self._now


def _promotion_fixture(clock, *, promoted_at=None, source_offsets=(2, 3)):
    """Runtime + a real pretrigger transfer, mirroring production's shape."""
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(identity, binding, streams=(CaptureStream.NBBO_QUOTE,))
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=BoundedCaptureIngress.from_resource_binding(binding),
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=600.0,
        wall_clock=clock,
    )
    # No explicit opened_at: `_trusted_recorded_at` carries the SAME
    # equality-against-a-second-read defect for its `requested` argument, so
    # passing one here would trip `caller_recorded_at_mismatch` before the
    # promotion guard under test is ever reached. Left for a separate change.
    runtime.open()
    runtime.register(producer.producer_id)

    ring = BoundedPreTriggerRing.from_resource_binding(
        binding, horizon=timedelta(minutes=3), per_symbol_max_events=16
    )
    for offset in source_offsets:
        source_at = BASE + timedelta(milliseconds=offset)
        retained, _ = ring.retain_observation(
            identity=identity,
            stream=CaptureStream.NBBO_QUOTE,
            provider="iqfeed",
            symbol="VEEE",
            clocks=CaptureClocks(
                provider_event_at=source_at,
                received_at=source_at,
                available_at=source_at,
            ),
            payload={"offset": offset},
        )
        assert retained is True

    transfer = ring.begin_promotion(
        "VEEE",
        promoted_at=promoted_at if promoted_at is not None else clock.peek(),
        source_identity=identity,
    )
    return runtime, producer, transfer


def _submit_promoted(runtime, producer, transfer):
    """Submit the first promoted frame.

    `recorded_at` is deliberately NOT passed -- production's
    `_submit_event_locked` does not pass it either, so the runtime takes its own
    trusted read. That second read is the whole point of these tests.
    """
    first = transfer.events[0]
    return runtime.submit_input(
        producer.producer_id,
        stream=first.stream,
        provider=first.provider,
        symbol=first.symbol,
        clocks=first.clocks,
        payload={
            **dict(first.payload),
            "_capture_promotion": {
                "promotion_id": transfer.promotion_id,
                "promoted_at": transfer.promoted_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "promotion_order": 1,
                "original_provisional_available_at": (
                    first.clocks.available_at.isoformat().replace("+00:00", "Z")
                ),
                "provisional_event_sha256": first.event_sha256,
                "source_identity_sha256": transfer.source_identity_sha256,
                "inventory_sha256": transfer.inventory_sha256,
            },
        },
        promotion_id=transfer.promotion_id,
        promoted_at=transfer.promoted_at,
        promotion_source_identity_sha256=transfer.source_identity_sha256,
        promotion_resource_binding_sha256=transfer.resource_binding_sha256,
        promotion_inventory_sha256=transfer.inventory_sha256,
        # The runtime demands the opaque transfer itself, not just its hashes --
        # `(promotion_id is None) != (promotion_transfer is None)` is a hard
        # symmetry check. Passing the provenance without the object is what the
        # earlier fixture got wrong.
        promotion_transfer=transfer,
    )


def test_promotion_microseconds_before_the_trusted_read_is_accepted():
    """THE regression.

    Production's shape exactly: the supervisor stamps `promoted_at`, real work
    happens, then the runtime takes its own trusted read. Under the old equality
    check this latched every single time -- and only when the ring was non-empty,
    i.e. only when there was data, which is why nothing could ever be admitted.
    """
    clock = _AdvancingClock(BASE + timedelta(seconds=1))
    runtime, producer, transfer = _promotion_fixture(clock)

    result = _submit_promoted(runtime, producer, transfer)

    assert result is not None
    assert runtime._submission_failure is None, (
        f"promotion latched: {runtime._submission_failure}"
    )


def test_advancing_clock_preserves_promotion_before_durable_availability():
    """A retained source remains readable when capture accepts it later."""

    clock = _AdvancingClock(BASE + timedelta(seconds=1))
    runtime, producer, transfer = _promotion_fixture(clock)

    result = _submit_promoted(runtime, producer, transfer)
    release = result.payload["_capture_release"]
    view = resolve_capture_source_payload(result)

    assert transfer.promoted_at < result.clocks.available_at
    assert release["promoted_at"] == transfer.promoted_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert release["released_available_at"] == (
        result.clocks.available_at.isoformat().replace("+00:00", "Z")
    )
    assert view.original_available_at == transfer.events[0].clocks.available_at
    assert view.promotion_id == transfer.promotion_id


def test_promotion_in_the_future_of_the_trusted_clock_is_rejected():
    """The property the guard actually exists for: no fabricated boundary."""
    clock = _AdvancingClock(BASE + timedelta(seconds=1))
    # Ahead of the trusted clock, but still inside the ring's 3-minute horizon --
    # an hour out would leave `begin_promotion` with zero events and test nothing.
    runtime, producer, transfer = _promotion_fixture(
        clock, promoted_at=BASE + timedelta(seconds=31)
    )

    with pytest.raises(CaptureContractError, match="later than the trusted wall clock"):
        _submit_promoted(runtime, producer, transfer)

    assert runtime._submission_failure == "promotion_boundary_after_trusted_clock"


def test_an_empty_ring_was_never_the_failing_case():
    """Pins the asymmetry that made this so hard to see from the outside.

    With nothing to promote there is no `promoted_at` on the path at all, so the
    guard is never reached and admission succeeds. The lane therefore looked
    healthy whenever it had no data and failed the instant it had some.
    """
    clock = _AdvancingClock(BASE + timedelta(seconds=1))
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(identity, binding, streams=(CaptureStream.NBBO_QUOTE,))
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=BoundedCaptureIngress.from_resource_binding(binding),
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=600.0,
        wall_clock=clock,
    )
    # No explicit opened_at: `_trusted_recorded_at` carries the SAME
    # equality-against-a-second-read defect for its `requested` argument, so
    # passing one here would trip `caller_recorded_at_mismatch` before the
    # promotion guard under test is ever reached. Left for a separate change.
    runtime.open()
    runtime.register(producer.producer_id)

    at = BASE + timedelta(milliseconds=2)
    result = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=at, received_at=at, available_at=at
        ),
        payload={"bid": 4.99, "ask": 5.00},
    )

    assert result is not None
    assert runtime._submission_failure is None


def test_a_frozen_clock_would_have_hidden_this():
    """Documents why the existing suite was blind, so it is not re-introduced.

    When every read returns the same value, an equality check and a bounds check
    are indistinguishable -- both pass. That is exactly the condition
    `_ManualClock` creates, and exactly why no existing test failed while
    production admitted nothing.
    """
    frozen = BASE + timedelta(milliseconds=5)

    class _Frozen:
        def __call__(self):
            return frozen

        def peek(self):
            return frozen

    runtime, producer, transfer = _promotion_fixture(_Frozen())
    result = _submit_promoted(runtime, producer, transfer)

    assert result is not None
    assert runtime._submission_failure is None
