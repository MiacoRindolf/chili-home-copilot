from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.services.trading.momentum_neural import short_mechanics as sm


UTC = timezone.utc
NOW = datetime(2026, 7, 26, 15, 30, tzinfo=UTC)
EXPECTED_KINDS = {
    "SUCCESS",
    "AUTHORITATIVE_EMPTY",
    "NOT_APPLICABLE",
    "DISABLED",
    "CREDENTIAL_UNAVAILABLE",
    "UNSUPPORTED_SYMBOL_OR_EXCHANGE",
    "AUTH_REJECTED",
    "RATE_LIMITED",
    "TRANSIENT_UNAVAILABLE",
    "PERMANENT_REJECTED",
    "MALFORMED_RESPONSE",
    "MONTHLY_QUOTA_EXHAUSTED",
    "BACKOFF_ACTIVE",
    "QUOTA_AUTHORITY_UNAVAILABLE",
    "CONTRACT_MISMATCH",
}


@dataclass(frozen=True)
class _Permit:
    attempt_id: UUID
    bundle_sha256: str
    owner_token: UUID
    request_sha256: str
    endpoint: str
    reserved_at: datetime
    lease_expires_at: datetime
    month_start: datetime
    quota_used_after: int


class _FakeQuota:
    def __init__(self, *, reserve_kind: str = "GRANTED") -> None:
        self.reserve_kind = reserve_kind
        self.calls: list[tuple[str, object]] = []
        self.reserve_metadata: list[tuple[str, object]] = []
        self.used = 0

    def reserve_bundle(self, *, bundle_sha256, owner_token, requests, lease_seconds=30):
        requests = tuple(requests)
        self.calls.append(("reserve", requests))
        self.reserve_metadata.append((bundle_sha256, owner_token))
        if self.reserve_kind != "GRANTED":
            retry_at = NOW + timedelta(seconds=17)
            return SimpleNamespace(
                kind=self.reserve_kind,
                permits=(),
                retry_at=retry_at,
                quota_used=self.used,
                quota_limit=1000,
                reason=self.reserve_kind.lower(),
                may_call_transport=False,
            )
        permits = []
        for request in requests:
            self.used += 1
            permits.append(
                _Permit(
                    attempt_id=uuid4(),
                    bundle_sha256=bundle_sha256,
                    owner_token=owner_token,
                    request_sha256=request.request_sha256,
                    endpoint=request.endpoint,
                    reserved_at=NOW,
                    lease_expires_at=NOW + timedelta(seconds=lease_seconds),
                    month_start=datetime(2026, 7, 1, tzinfo=UTC),
                    quota_used_after=self.used,
                )
            )
        return SimpleNamespace(
            kind="GRANTED",
            permits=tuple(permits),
            retry_at=None,
            quota_used=self.used,
            quota_limit=1000,
            reason="reserved",
            may_call_transport=False,
        )

    def begin_transport(self, permit):
        self.calls.append(("begin_transport", permit.attempt_id))
        return SimpleNamespace(
            kind="GRANTED",
            permits=(permit,),
            retry_at=None,
            quota_used=self.used,
            quota_limit=1000,
            reason="transport_committed",
            may_call_transport=True,
        )

    def complete_attempt(self, permit, **kwargs):
        self.calls.append(("complete_attempt", kwargs))
        return SimpleNamespace(
            kind="COMPLETED",
            permits=(permit,),
            retry_at=kwargs.get("backoff_until"),
            quota_used=self.used,
            quota_limit=1000,
            reason="completed",
            may_call_transport=False,
        )

    def refund_unstarted(self, permit):
        self.calls.append(("refund_unstarted", permit.attempt_id))
        raise AssertionError("a transport-committed attempt must never be refunded")


class _DurableFakeQuota(_FakeQuota):
    def __init__(self) -> None:
        super().__init__()
        self.records: dict[str, object] = {}

    def read_recent_completed(self, request_sha256, max_age_seconds):
        self.calls.append(("read_recent_completed", request_sha256))
        assert max_age_seconds == 24 * 60 * 60
        record = self.records.get(request_sha256)
        return SimpleNamespace(
            kind="COMPLETED" if record is not None else "NOT_FOUND",
            attempt=record,
        )

    def complete_attempt(self, permit, **kwargs):
        decision = super().complete_attempt(permit, **kwargs)
        segments = permit.endpoint.strip("/").split("/")
        self.records[permit.request_sha256] = SimpleNamespace(
            attempt_id=permit.attempt_id,
            plan_scope="ortex:trader-1000:v1",
            month_start=permit.month_start.date(),
            bundle_sha256=permit.bundle_sha256,
            bundle_index=0,
            owner_token=permit.owner_token,
            request_sha256=permit.request_sha256,
            provider="ortex",
            endpoint=permit.endpoint,
            symbol=segments[4],
            status="completed",
            monthly_limit=1000,
            quota_used_after=permit.quota_used_after,
            reserved_at=permit.reserved_at,
            lease_expires_at=permit.lease_expires_at,
            provider_outcome=kwargs["outcome"],
            http_status=kwargs.get("http_status"),
            backoff_until=kwargs.get("backoff_until"),
            raw_response_body_b64=kwargs.get("raw_response_body_b64"),
            response_sha256=kwargs.get("response_sha256"),
            provider_event_at=kwargs.get("provider_event_at"),
            effective_at=kwargs.get("effective_at"),
            received_at=kwargs.get("received_at"),
            available_at=kwargs.get("available_at"),
        )
        return SimpleNamespace(
            **decision.__dict__,
            attempt=self.records[permit.request_sha256],
        )


class _Response:
    def __init__(
        self,
        payload: object,
        *,
        status: int = 200,
        url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._raw = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, separators=(",", ":"), allow_nan=True).encode()
        )
        self._url = url
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._raw if size < 0 else self._raw[:size]

    def geturl(self) -> str:
        return self._url or ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _isolated_provider(monkeypatch):
    monkeypatch.setattr(
        sm.settings, "chili_momentum_squeeze_fuel_tilt_enabled", True
    )
    monkeypatch.setattr(sm.settings, "chili_ortex_api_key", "sekret-test-key")
    monkeypatch.setattr(sm, "_utcnow", lambda: NOW)
    monkeypatch.setattr(sm, "_monotonic", lambda: 10.0)
    monkeypatch.setattr(sm, "_sleep", lambda _seconds: None)
    sm._clear_cache_for_tests()
    yield
    sm._clear_cache_for_tests()


def _payload(field: str, values: list[tuple[str, float]]) -> dict:
    return {
        "rows": [
            {"date": event_date, field: value}
            for event_date, value in values
        ]
    }


def _install_http(monkeypatch, payloads):
    calls: list[str] = []

    def _open(request, timeout):
        del timeout
        url = request.full_url
        calls.append(url)
        for needle, response in payloads:
            if needle in url:
                if isinstance(response, BaseException):
                    raise response
                if response._url is None:
                    response._url = url
                return response
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(sm, "_open_no_redirect", _open)
    return calls


def test_outcome_vocabulary_is_closed_and_exact():
    assert {member.value for member in sm.OrtexOutcomeKind} == EXPECTED_KINDS
    assert sm._policy_capture()["success_cache_ttl_seconds"] == 24 * 60 * 60
    assert sm._policy_capture()["auth_rejection_backoff_seconds"] == 3600.0
    assert sm._policy_capture()["month_boundary_guard_seconds"] == 30.0
    assert (
        sm._policy_capture()["month_boundary_guard_seconds"]
        >= sm._policy_capture()["request_timeout_seconds"]
    )
    assert (
        sm._policy_capture()["month_boundary_guard_seconds"]
        >= sm._policy_capture()["quota_lease_seconds"]
    )
    assert len(sm._policy_sha256()) == 64


def test_public_policy_uses_exact_validated_runtime_settings(monkeypatch):
    baseline = sm.ortex_public_policy_sha256()
    monkeypatch.setattr(sm.settings, "chili_ortex_request_interval_seconds", 1.75)
    monkeypatch.setattr(sm.settings, "chili_ortex_reservation_lease_seconds", 45.0)
    monkeypatch.setattr(
        sm.settings, "chili_ortex_transient_backoff_base_seconds", 3.0
    )
    monkeypatch.setattr(
        sm.settings, "chili_ortex_transient_backoff_max_seconds", 240.0
    )
    monkeypatch.setattr(sm.settings, "chili_ortex_response_max_bytes", 262144)
    policy = sm.ortex_public_policy()
    assert policy["request_interval_seconds"] == 1.75
    assert policy["quota_lease_seconds"] == 45.0
    assert policy["month_boundary_guard_seconds"] == 45.0
    assert policy["backoff_base_seconds"] == 3.0
    assert policy["backoff_max_seconds"] == 240.0
    assert policy["max_response_bytes"] == 262144
    assert sm.ortex_public_policy_sha256() != baseline


def test_retry_after_accepts_delta_seconds_and_rfc_http_date_with_cap():
    assert sm._retry_after(
        {"Retry-After": "17"},
        observed_at=NOW,
        maximum_seconds=300,
    ) == NOW + timedelta(seconds=17)
    assert sm._retry_after(
        {"Retry-After": "Sun, 26 Jul 2026 15:34:00 GMT"},
        observed_at=NOW,
        maximum_seconds=300,
    ) == NOW + timedelta(minutes=4)
    assert sm._retry_after(
        {"Retry-After": "Sun, 26 Jul 2026 16:30:00 GMT"},
        observed_at=NOW,
        maximum_seconds=300,
    ) == NOW + timedelta(minutes=5)
    assert (
        sm._retry_after(
            {"Retry-After": "not-a-date"},
            observed_at=NOW,
            maximum_seconds=300,
        )
        is None
    )


def test_auth_latch_is_credential_and_sealed_policy_scoped():
    policy_sha256 = sm.ortex_public_policy_sha256()
    sm._install_credential_auth_latch(
        "credential-generation-a",
        policy_sha256,
        ttl_seconds=3600,
    )

    assert sm._credential_auth_latch_active(
        "credential-generation-a",
        policy_sha256,
    )
    assert not sm._credential_auth_latch_active(
        "credential-generation-b",
        policy_sha256,
    )
    assert not sm._credential_auth_latch_active(
        "credential-generation-a",
        hashlib.sha256(b"changed-sealed-policy").hexdigest(),
    )
    assert "credential-generation-a" not in repr(
        sm._credential_auth_latches
    )


def test_not_applicable_factory_is_deterministic_and_does_no_io(monkeypatch):
    monkeypatch.setattr(
        sm, "_open_no_redirect", lambda *_a, **_k: pytest.fail("network fallback")
    )
    outcome = sm.OrtexShortMechanicsOutcome.not_applicable(
        symbol="VEEE",
        reason="outside_top_n",
        observed_at=NOW,
        policy_sha256=sm._policy_sha256(),
    )
    assert outcome.kind is sm.OrtexOutcomeKind.NOT_APPLICABLE
    assert outcome.detail_code == "outside_top_n"
    assert outcome.endpoints == ()
    assert (
        sm.OrtexShortMechanicsOutcome.from_capture_dict(
            outcome.to_capture_dict()
        )
        == outcome
    )
    non_equity = sm.OrtexShortMechanicsOutcome.not_applicable(
        symbol="BTC-USD",
        reason="non_equity",
        observed_at=NOW,
        policy_sha256=sm._policy_sha256(),
    )
    assert non_equity.symbol == "BTC-USD"
    assert non_equity.kind is sm.OrtexOutcomeKind.NOT_APPLICABLE


def test_without_durable_quota_authority_no_network_is_possible(monkeypatch):
    monkeypatch.setattr(
        sm, "_open_no_redirect", lambda *_a, **_k: pytest.fail("network fallback")
    )
    outcome = sm.get_short_mechanics_outcome("VEEE", exchange_hint="nasdaq")
    assert outcome.kind is sm.OrtexOutcomeKind.QUOTA_AUTHORITY_UNAVAILABLE
    assert outcome.endpoints == ()
    assert sm.get_short_mechanics("VEEE", exchange_hint="nasdaq") is None


def test_quota_runtime_policy_mismatch_blocks_before_network(monkeypatch):
    quota = _FakeQuota()
    quota.monthly_limit = 999
    monkeypatch.setattr(
        sm, "_open_no_redirect", lambda *_a, **_k: pytest.fail("network fallback")
    )
    with sm.ortex_quota_authority(quota):
        outcome = sm.get_short_mechanics_outcome(
            "VEEE", exchange_hint="nasdaq"
        )
    assert outcome.kind is sm.OrtexOutcomeKind.CONTRACT_MISMATCH
    assert outcome.detail_code == "quota_policy_contract_mismatch"
    assert quota.calls == []


def test_success_is_deterministic_capture_round_trippable_and_cache_bound(
    monkeypatch,
):
    quota = _FakeQuota()
    calls = _install_http(
        monkeypatch,
        [
            (
                "/short_interest",
                _Response(
                    _payload(
                        "shortInterestPcFreeFloat",
                        [("2026-07-25", 0.21), ("2026-07-24", 0.18)],
                    )
                ),
            ),
            (
                "/ctb/all",
                _Response(
                    _payload(
                        "costToBorrowAll",
                        [("2026-07-24", 4.0), ("2026-07-25", 12.5)],
                    )
                ),
            ),
        ],
    )
    with sm.ortex_quota_authority(quota):
        first = sm.get_short_mechanics_outcome("veee", exchange_hint="nasdaq")
        second = sm.get_short_mechanics_outcome("VEEE", exchange_hint="nasdaq")

    assert first.kind is sm.OrtexOutcomeKind.SUCCESS
    assert first.short_interest_pct == pytest.approx(0.21)
    assert first.cost_to_borrow == pytest.approx(12.5)
    assert first.resolved_exchange == "nasdaq"
    assert len(first.endpoints) == 2
    assert len(calls) == 2
    assert [name for name, _ in quota.calls] == [
        "reserve",
        "begin_transport",
        "complete_attempt",
        "begin_transport",
        "complete_attempt",
    ]
    assert len(quota.calls[0][1]) == 2
    assert second.kind is sm.OrtexOutcomeKind.SUCCESS
    assert second.cache_origin_sha256 == first.capture_sha256
    assert second.cache_origin_received_at == first.received_at
    assert len(calls) == 2

    captured = first.to_capture_dict()
    encoded = json.dumps(captured, sort_keys=True, separators=(",", ":"))
    assert "sekret-test-key" not in encoded
    assert sm.OrtexShortMechanicsOutcome.from_capture_dict(captured) == first
    assert len(first.capture_sha256) == 64
    assert all(len(endpoint.raw_response_sha256 or "") == 64 for endpoint in first.endpoints)

    captured["endpoints"][0]["raw_response_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        sm.OrtexShortMechanicsOutcome.from_capture_dict(captured)

    forged = first.to_capture_dict()
    endpoint = forged["endpoints"][0]
    raw = json.loads(
        base64.b64decode(endpoint["raw_response_b64"]).decode("utf-8")
    )
    raw["rows"][0]["shortInterestPcFreeFloat"] = 0.99
    changed = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    endpoint["raw_response_b64"] = base64.b64encode(changed).decode("ascii")
    endpoint["raw_response_sha256"] = hashlib.sha256(changed).hexdigest()
    endpoint["capture_sha256"] = sm._sha256_json(
        {key: value for key, value in endpoint.items() if key != "capture_sha256"}
    )
    forged["capture_sha256"] = sm._sha256_json(
        {key: value for key, value in forged.items() if key != "capture_sha256"}
    )
    with pytest.raises(ValueError, match="semantics"):
        sm.OrtexShortMechanicsOutcome.from_capture_dict(forged)


def test_integer_endpoint_value_is_canonicalized_before_capture_hash(
    monkeypatch,
):
    quota = _FakeQuota()
    _install_http(
        monkeypatch,
        [
            (
                "/short_interest",
                _Response(
                    _payload(
                        "shortInterestPcFreeFloat",
                        [("2026-07-25", 0.21)],
                    )
                ),
            ),
            (
                "/ctb/all",
                _Response(
                    _payload("costToBorrowAll", [("2026-07-25", 20)])
                ),
            ),
        ],
    )
    with sm.ortex_quota_authority(quota):
        outcome = sm.get_short_mechanics_outcome(
            "VEEE", exchange_hint="nasdaq"
        )

    ctb = next(
        endpoint
        for endpoint in outcome.endpoints
        if endpoint.dataset == "cost_to_borrow"
    )
    assert type(ctb.value) is float
    assert ctb.value == 20.0
    assert (
        sm.OrtexShortMechanicsOutcome.from_capture_dict(
            outcome.to_capture_dict()
        )
        == outcome
    )


def test_durable_cache_and_pure_row_reconstruction_preserve_source_receipts(
    monkeypatch,
):
    quota = _DurableFakeQuota()
    calls = _install_http(
        monkeypatch,
        [
            (
                "/short_interest",
                _Response(
                    _payload(
                        "shortInterestPcFreeFloat",
                        [("2026-07-25", 0.37)],
                    )
                ),
            ),
            (
                "/ctb/all",
                _Response(
                    _payload("costToBorrowAll", [("2026-07-25", 19.0)])
                ),
            ),
        ],
    )
    with sm.ortex_quota_authority(quota):
        live = sm.get_short_mechanics_outcome("SOBR", exchange_hint="nasdaq")
    assert live.kind is sm.OrtexOutcomeKind.SUCCESS
    assert len(calls) == 2
    reconstructed = sm.ortex_outcome_from_completed_attempts(
        symbol="SOBR",
        requested_exchange="nasdaq",
        records=tuple(quota.records.values()),
        policy=sm.ortex_public_policy(),
        observed_at=NOW,
    )
    assert reconstructed == live

    sm._clear_cache_for_tests()
    monkeypatch.setattr(
        sm, "_open_no_redirect", lambda *_a, **_k: pytest.fail("live fallback")
    )
    with sm.ortex_quota_authority(quota):
        cached = sm.get_short_mechanics_outcome("SOBR", exchange_hint="nasdaq")
    assert cached.kind is sm.OrtexOutcomeKind.SUCCESS
    assert cached.detail_code == "durable_cache"
    assert cached.cache_origin_received_at == max(
        endpoint.received_at for endpoint in live.endpoints
    )
    assert [endpoint.capture_sha256 for endpoint in cached.endpoints] == [
        endpoint.capture_sha256 for endpoint in live.endpoints
    ]
    assert len(calls) == 2


def test_transient_attempt_is_charged_not_retried_or_cached(monkeypatch):
    quota = _FakeQuota()
    calls = _install_http(
        monkeypatch,
        [
            ("/short_interest", TimeoutError("ambiguous timeout")),
            ("/ctb/all", TimeoutError("ambiguous timeout")),
        ],
    )
    with sm.ortex_quota_authority(quota):
        first = sm.get_short_mechanics_outcome("SILO", exchange_hint="nasdaq")
        second = sm.get_short_mechanics_outcome("SILO", exchange_hint="nasdaq")

    assert first.kind is sm.OrtexOutcomeKind.TRANSIENT_UNAVAILABLE
    assert second.kind is sm.OrtexOutcomeKind.TRANSIENT_UNAVAILABLE
    assert len(calls) == 4
    assert [name for name, _ in quota.calls].count("begin_transport") == 4
    assert [name for name, _ in quota.calls].count("complete_attempt") == 4
    assert not any(name == "refund_unstarted" for name, _ in quota.calls)
    assert len({bundle for bundle, _owner in quota.reserve_metadata}) == 2
    assert len({str(owner) for _bundle, owner in quota.reserve_metadata}) == 2


@pytest.mark.parametrize(
    ("ctb_response", "expected"),
    [
        (TimeoutError("ctb timeout"), "TRANSIENT_UNAVAILABLE"),
        (
            sm.urllib.error.HTTPError(
                "https://api.ortex.com/api/v1/stock/nasdaq/SILO/ctb/all",
                401,
                "unauthorized",
                {},
                None,
            ),
            "AUTH_REJECTED",
        ),
        (
            _Response(
                _payload("costToBorrowAll", [("2026-07-25", float("nan"))])
            ),
            "MALFORMED_RESPONSE",
        ),
    ],
)
def test_partial_endpoint_never_exposes_economic_scalars(
    monkeypatch, ctb_response, expected
):
    quota = _FakeQuota()
    _install_http(
        monkeypatch,
        [
            (
                "/short_interest",
                _Response(
                    _payload(
                        "shortInterestPcFreeFloat",
                        [("2026-07-25", 0.42)],
                    )
                ),
            ),
            ("/ctb/all", ctb_response),
        ],
    )
    with sm.ortex_quota_authority(quota):
        outcome = sm.get_short_mechanics_outcome(
            "SILO", exchange_hint="nasdaq"
        )
    assert outcome.kind.value == expected
    assert outcome.short_interest_pct is None
    assert outcome.cost_to_borrow is None
    assert outcome.is_easy_to_borrow is None


def test_required_ctb_not_found_is_unavailable_not_authoritative_neutral(
    monkeypatch,
):
    quota = _FakeQuota()
    _install_http(
        monkeypatch,
        [
            (
                "/short_interest",
                _Response(
                    _payload(
                        "shortInterestPcFreeFloat",
                        [("2026-07-25", 0.42)],
                    )
                ),
            ),
            (
                "/ctb/all",
                sm.urllib.error.HTTPError(
                    "https://api.ortex.com/api/v1/stock/nasdaq/SILO/ctb/all",
                    404,
                    "not found",
                    {},
                    None,
                ),
            ),
        ],
    )
    with sm.ortex_quota_authority(quota):
        outcome = sm.get_short_mechanics_outcome("SILO")

    assert outcome.kind is sm.OrtexOutcomeKind.CONTRACT_MISMATCH
    assert outcome.detail_code == "required_endpoint_not_found"
    assert outcome.short_interest_pct is None
    assert outcome.cost_to_borrow is None
    assert outcome.is_easy_to_borrow is None


def test_all_exchange_discovery_not_found_remains_authoritative_neutral(
    monkeypatch,
):
    quota = _FakeQuota()
    calls: list[str] = []

    def _open(request, timeout):
        del timeout
        calls.append(request.full_url)
        raise sm.urllib.error.HTTPError(
            request.full_url,
            404,
            "not found",
            {},
            None,
        )

    monkeypatch.setattr(sm, "_open_no_redirect", _open)
    with sm.ortex_quota_authority(quota):
        outcome = sm.get_short_mechanics_outcome("CLRO")
        cached = sm.get_short_mechanics_outcome("CLRO")

    assert outcome.kind is sm.OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE
    assert outcome.detail_code == "symbol_not_found_on_supported_exchanges"
    assert outcome.short_interest_pct is None
    assert outcome.cost_to_borrow is None
    assert all(
        endpoint.kind is sm.OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE
        for endpoint in outcome.endpoints
    )
    assert len(calls) == 3
    assert cached.kind is sm.OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE
    assert cached.detail_code == "process_cache"
    assert cached.cache_origin_sha256 == outcome.capture_sha256


@pytest.mark.parametrize(
    ("status", "extra", "secret"),
    [
        (200, {"message": "sekret-test-key"}, "sekret-test-key"),
        (200, {"api_key": "different-secret"}, "different-secret"),
        (401, {"message": "sekret-test-key"}, "sekret-test-key"),
    ],
)
def test_credential_material_and_nonauthoritative_bodies_are_never_persisted(
    monkeypatch, status, extra, secret
):
    quota = _FakeQuota()
    payload = {
        "rows": [
            {
                "date": "2026-07-25",
                "shortInterestPcFreeFloat": 0.42,
            }
        ],
    }
    payload.update(extra)
    _install_http(
        monkeypatch,
        [
            ("/short_interest", _Response(payload, status=status)),
            ("/ctb/all", _Response(payload, status=status)),
        ],
    )
    with sm.ortex_quota_authority(quota):
        outcome = sm.get_short_mechanics_outcome(
            "SILO", exchange_hint="nasdaq"
        )

    serialized = json.dumps(
        outcome.to_capture_dict(), sort_keys=True, separators=(",", ":")
    )
    assert secret not in serialized
    assert all(endpoint.raw_response_b64 is None for endpoint in outcome.endpoints)
    assert all(
        endpoint.raw_response_sha256 is None for endpoint in outcome.endpoints
    )
    assert all(
        kwargs["raw_response_body_b64"] is None
        and kwargs["response_sha256"] is None
        for name, kwargs in quota.calls
        if name == "complete_attempt"
    )


def test_durable_then_process_cache_preserves_origin_and_only_remaining_ttl(
    monkeypatch,
):
    quota = _FakeQuota()
    wall = {"value": NOW - timedelta(hours=23, minutes=59)}
    monotonic = {"value": 10.0}
    monkeypatch.setattr(sm, "_utcnow", lambda: wall["value"])
    monkeypatch.setattr(sm, "_monotonic", lambda: monotonic["value"])
    _install_http(
        monkeypatch,
        [
            (
                "/short_interest",
                _Response(
                    _payload(
                        "shortInterestPcFreeFloat",
                        [("2026-07-25", 0.37)],
                    )
                ),
            ),
            (
                "/ctb/all",
                _Response(
                    _payload("costToBorrowAll", [("2026-07-25", 19.0)])
                ),
            ),
        ],
    )
    with sm.ortex_quota_authority(quota):
        original = sm.get_short_mechanics_outcome(
            "SOBR", exchange_hint="nasdaq"
        )
    sm._clear_cache_for_tests()

    wall["value"] = NOW
    durable = sm._durable_cache_provenance(original, observed_at=NOW)
    sm._cache_set_v2(
        "SOBR",
        "nasdaq",
        policy_sha256=sm._policy_sha256(),
        outcome=durable,
        ttl_seconds=24 * 60 * 60,
    )

    wall["value"] = NOW + timedelta(seconds=30)
    monotonic["value"] += 30.0
    cached = sm._cache_get_v2(
        "SOBR",
        "nasdaq",
        policy_sha256=sm._policy_sha256(),
        observed_at=wall["value"],
    )
    assert cached is not None
    assert cached.cache_origin_sha256 == durable.cache_origin_sha256
    assert (
        cached.cache_origin_received_at
        == durable.cache_origin_received_at
    )
    assert (
        cached.cache_origin_available_at
        == durable.cache_origin_available_at
    )

    wall["value"] = NOW + timedelta(seconds=61)
    monotonic["value"] += 31.0
    assert (
        sm._cache_get_v2(
            "SOBR",
            "nasdaq",
            policy_sha256=sm._policy_sha256(),
            observed_at=wall["value"],
        )
        is None
    )


@pytest.mark.parametrize(
    "row",
    [
        {
            "date": "2026-07-25",
            "updatedAt": (NOW + timedelta(seconds=1)).isoformat(),
            "shortInterestPcFreeFloat": 0.42,
        },
        {
            "date": (NOW + timedelta(days=1)).date().isoformat(),
            "shortInterestPcFreeFloat": 0.42,
        },
    ],
)
def test_future_provider_or_effective_clock_is_rejected(monkeypatch, row):
    quota = _FakeQuota()
    _install_http(
        monkeypatch,
        [
            ("/short_interest", _Response({"rows": [row]})),
            ("/ctb/all", _Response({"rows": []})),
        ],
    )
    with sm.ortex_quota_authority(quota):
        outcome = sm.get_short_mechanics_outcome(
            "SILO", exchange_hint="nasdaq"
        )

    assert outcome.kind is sm.OrtexOutcomeKind.MALFORMED_RESPONSE
    assert outcome.short_interest_pct is None
    assert outcome.cost_to_borrow is None
    malformed_endpoint = next(
        endpoint
        for endpoint in outcome.endpoints
        if endpoint.dataset == "short_interest"
    )
    assert malformed_endpoint.kind is sm.OrtexOutcomeKind.MALFORMED_RESPONSE
    assert malformed_endpoint.raw_response_b64 is None
    assert malformed_endpoint.raw_response_sha256 is None


def test_exchange_discovery_only_advances_on_typed_not_found(monkeypatch):
    quota = _FakeQuota()
    seen: list[str] = []

    def _open(request, timeout):
        del timeout
        seen.append(request.full_url)
        if "/nasdaq/" in request.full_url:
            raise sm.urllib.error.HTTPError(
                request.full_url, 404, "not found", {}, None
            )
        if "/nyse/" in request.full_url and "/short_interest" in request.full_url:
            return _Response(
                _payload("shortInterestPcFreeFloat", [("2026-07-25", 0.31)]),
                url=request.full_url,
            )
        if "/nyse/" in request.full_url and "/ctb/all" in request.full_url:
            return _Response(
                _payload("costToBorrowAll", [("2026-07-25", 8.0)]),
                url=request.full_url,
            )
        raise AssertionError(request.full_url)

    monkeypatch.setattr(sm, "_open_no_redirect", _open)
    with sm.ortex_quota_authority(quota):
        outcome = sm.get_short_mechanics_outcome("CLRO")
        cached = sm.get_short_mechanics_outcome("CLRO")

    assert outcome.kind is sm.OrtexOutcomeKind.SUCCESS
    assert outcome.resolved_exchange == "nyse"
    assert len(seen) == 3
    assert "/nasdaq/" in seen[0]
    assert "/nyse/" in seen[1] and "/nyse/" in seen[2]
    assert len(outcome.endpoints) == 3
    assert [len(value) for name, value in quota.calls if name == "reserve"] == [
        1,
        1,
        1,
    ]
    assert cached.kind is sm.OrtexOutcomeKind.SUCCESS
    assert cached.detail_code == "process_cache"
    assert len(seen) == 3


@pytest.mark.parametrize(
    ("reserve_kind", "expected"),
    [
        ("QUOTA_EXHAUSTED", "MONTHLY_QUOTA_EXHAUSTED"),
        ("BACKOFF_ACTIVE", "BACKOFF_ACTIVE"),
        ("PACING_ACTIVE", "BACKOFF_ACTIVE"),
        ("MONTH_BOUNDARY_GUARD", "BACKOFF_ACTIVE"),
        ("INDETERMINATE_ACTIVE", "BACKOFF_ACTIVE"),
        ("DB_UNAVAILABLE", "QUOTA_AUTHORITY_UNAVAILABLE"),
    ],
)
def test_durable_quota_decision_blocks_before_transport(
    monkeypatch, reserve_kind, expected
):
    quota = _FakeQuota(reserve_kind=reserve_kind)
    monkeypatch.setattr(
        sm, "_open_no_redirect", lambda *_a, **_k: pytest.fail("transport called")
    )
    with sm.ortex_quota_authority(quota):
        outcome = sm.get_short_mechanics_outcome("TRNR", exchange_hint="nasdaq")

    assert outcome.kind.value == expected
    assert [name for name, _ in quota.calls] == ["reserve"]


def test_bound_capture_provider_is_strict_and_has_zero_live_fallback(monkeypatch):
    quota = _FakeQuota()
    _install_http(
        monkeypatch,
        [
            (
                "/short_interest",
                _Response(
                    _payload("shortInterestPcFreeFloat", [("2026-07-25", 0.44)])
                ),
            ),
            (
                "/ctb/all",
                _Response(_payload("costToBorrowAll", [("2026-07-25", 21.0)])),
            ),
        ],
    )
    with sm.ortex_quota_authority(quota):
        recorded = sm.get_short_mechanics_outcome("SOBR", exchange_hint="nasdaq")

    class _Provider:
        network_fallback_allowed = False

        def get_short_mechanics_outcome(self, *, symbol, exchange_hint):
            assert symbol == "SOBR"
            assert exchange_hint == "nasdaq"
            return sm.OrtexShortMechanicsOutcome.from_capture_dict(
                recorded.to_capture_dict()
            )

    monkeypatch.setattr(
        sm, "_open_no_redirect", lambda *_a, **_k: pytest.fail("live fallback")
    )
    with sm.ortex_outcome_provider(_Provider()):
        replayed = sm.get_short_mechanics_outcome("SOBR", exchange_hint="nasdaq")
    assert replayed == recorded

    with pytest.raises(ValueError, match="network fallback"):
        with sm.ortex_outcome_provider(
            SimpleNamespace(
                network_fallback_allowed=True,
                get_short_mechanics_outcome=lambda **_kwargs: recorded,
            )
        ):
            pass


def test_redirect_host_drift_and_nonfinite_rows_are_contract_failures(monkeypatch):
    quota = _FakeQuota()
    calls = _install_http(
        monkeypatch,
        [
            (
                "/short_interest",
                _Response(
                    _payload("shortInterestPcFreeFloat", [("2026-07-25", 0.2)]),
                    url="https://evil.example/api/v1/stock/nasdaq/VEEE/short_interest",
                ),
            )
        ],
    )
    with sm.ortex_quota_authority(quota):
        drift = sm.get_short_mechanics_outcome("VEEE", exchange_hint="nasdaq")
    assert calls
    assert drift.kind is sm.OrtexOutcomeKind.CONTRACT_MISMATCH

    sm._clear_cache_for_tests()
    quota2 = _FakeQuota()
    _install_http(
        monkeypatch,
        [
            (
                "/short_interest",
                _Response(
                    _payload(
                        "shortInterestPcFreeFloat",
                        [("2026-07-25", float("nan"))],
                    )
                ),
            ),
            ("/ctb/all", _Response({"rows": []})),
        ],
    )
    with sm.ortex_quota_authority(quota2):
        malformed = sm.get_short_mechanics_outcome(
            "VEEE", exchange_hint="nasdaq"
        )
    assert malformed.kind is sm.OrtexOutcomeKind.MALFORMED_RESPONSE
