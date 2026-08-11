"""A momentary pressure stall must not permanently retire a capture lifecycle.

2026-08-11, r165: `48 attempt, 0 admitted`, largest bucket
`capture_resource_pressure_sample_stale=25`. `submit()` returning False latches
`_submission_failure`, which is write-once with NO reset path anywhere in the
tree, so one stale sampler read made the whole lifecycle permanently
noncertifiable and every later submit raised "capture lifecycle is already
noncertifiable: ingress_rejected_sequence_N".

The pressure sampler goes stale for a moment and comes back --
`_retained_rejection_is_retryable` says so, and the retained path already honours
it. `_submit` did not.

The fix is a PRE-CHECK, not a retry: re-submitting would consume the event and
record a coverage gap each time (deliberate: "bounded, aggregated overflow
evidence"), so retries would burn sequences and `seal_run` would refuse on
`submitted != sequence`. Asking the controller first keeps that contract intact.
"""

from datetime import timedelta

import pytest

from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    CaptureClocks,
    CaptureProducerLifecycleRuntime,
    CaptureStream,
)

from tests.test_replay_capture_producer_lifecycle import (
    BASE,
    _identity,
    _producer,
    _resource_binding,
)


class _StallingController:
    """Reports a retryable rejection for the first N reads, then clears."""

    def __init__(self, binding, stalls, reason="capture_resource_pressure_sample_stale"):
        self.binding = binding
        self._left = stalls
        self._reason = reason
        self.reads = 0

    @property
    def rejection_reason(self):
        self.reads += 1
        if self._left > 0:
            self._left -= 1
            return self._reason
        return None


def _runtime(controller=None):
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(identity, binding, streams=(CaptureStream.NBBO_QUOTE,))
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    if controller is not None:
        ingress.pressure_controller = controller
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=600.0,
    )
    runtime.open()
    runtime.register(producer.producer_id)
    return runtime, producer, binding


def _submit(runtime, producer, ms):
    at = BASE + timedelta(milliseconds=ms)
    return runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(provider_event_at=at, received_at=at, available_at=at),
        payload={"bid": 4.99, "ask": 5.00, "n": ms},
    )


def test_a_transient_stall_is_waited_out_not_latched():
    """THE regression: the sampler stalls, then clears, and the run survives."""
    _, binding, = None, None
    runtime, producer, binding = _runtime()
    controller = _StallingController(binding, stalls=3)
    runtime.ingress.pressure_controller = controller

    result = _submit(runtime, producer, 2)

    assert result is not None
    assert runtime._submission_failure is None, (
        f"a momentary stall latched: {runtime._submission_failure}"
    )
    assert controller.reads > 1, "the pre-check never consulted the controller"


def test_the_wait_is_bounded_by_the_samplers_own_freshness_window():
    """A permanent stall must still fail closed, and must not hang.

    The bound is the sampler's own max age -- no magic number, and it cannot
    exceed the age that made the sample stale in the first place.
    """
    import time

    runtime, producer, binding = _runtime()
    runtime.ingress.pressure_controller = _StallingController(binding, stalls=10**9)

    budget = binding.policy.pressure_sample_max_age_seconds
    t0 = time.monotonic()
    runtime._await_transient_ingress_pressure()
    waited = time.monotonic() - t0

    assert waited <= budget * 2.5, f"waited {waited:.2f}s against a {budget}s budget"


def test_a_clock_integrity_fault_is_not_waited_on():
    """Non-retryable reasons must return immediately and stay fail-closed."""
    import time

    runtime, producer, binding = _runtime()
    runtime.ingress.pressure_controller = _StallingController(
        binding, stalls=10**9, reason="capture_resource_pressure_sample_clock_invalid"
    )

    t0 = time.monotonic()
    runtime._await_transient_ingress_pressure()
    assert time.monotonic() - t0 < 0.25, "waited on an integrity fault"


def test_no_controller_is_a_no_op():
    """Runtimes without a pressure controller must be untouched."""
    runtime, producer, _ = _runtime()
    runtime.ingress.pressure_controller = None

    runtime._await_transient_ingress_pressure()
    assert _submit(runtime, producer, 2) is not None
    assert runtime._submission_failure is None


def test_a_broken_controller_never_breaks_the_pre_check():
    """The pre-check runs on every submit; it must never be why one fails.

    Scoped deliberately to the pre-check. A controller that raises will still
    break `submit()` itself, because the ingress reads `rejection_reason`
    directly -- that is pre-existing behaviour on a path this change does not
    own, and asserting otherwise here would be claiming a guarantee this fix does
    not provide.
    """

    class _Exploding:
        binding = None

        @property
        def rejection_reason(self):
            raise RuntimeError("controller exploded")

    runtime, producer, _ = _runtime()
    runtime.ingress.pressure_controller = _Exploding()

    runtime._await_transient_ingress_pressure()  # must not raise
    assert runtime._submission_failure is None


def test_the_latch_still_names_the_reason_when_one_is_known():
    """r165's latches carried only a sequence number, so the branch was lost."""
    runtime, producer, binding = _runtime()

    runtime._latch_failure("ingress_rejected_sequence_3_capture_resource_pressure_sample_stale")
    assert runtime._submission_failure.startswith("ingress_rejected_sequence_")
    assert "sample_stale" in runtime._submission_failure


def test_the_prefix_survives_for_the_abort_path():
    """The captured-paper abort path string-matches this prefix."""
    runtime, producer, binding = _runtime()
    runtime._latch_failure("ingress_rejected_sequence_9_capture_queue_overflow")
    assert runtime._submission_failure.startswith("ingress_rejected_sequence_")
