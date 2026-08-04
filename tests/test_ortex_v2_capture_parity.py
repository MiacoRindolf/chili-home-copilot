from __future__ import annotations

import base64
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

from app.services.trading.momentum_neural import short_mechanics as ortex_module
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
    CaptureOrtexSelectionSnapshot,
    bind_ortex_squeeze_fuel_batch_reference,
    sha256_json,
    validate_ortex_squeeze_fuel_batch_manifest,
    validate_ortex_squeeze_fuel_batch_reference,
    validate_ortex_selection_batch,
)
from app.services.trading.momentum_neural.ross_momentum import (
    squeeze_fuel_signal,
)
from app.services.trading.momentum_neural.short_mechanics import (
    OrtexShortMechanicsOutcome,
    ortex_outcome_from_completed_attempts,
    ortex_public_policy,
)


UTC = timezone.utc
OBSERVED_AT = datetime(2026, 7, 25, 15, 30, tzinfo=UTC)


def _success_outcome(
    symbol: str,
    *,
    preceding_not_found: bool = False,
) -> OrtexShortMechanicsOutcome:
    policy = ortex_public_policy()
    policy_sha256 = sha256_json(policy)
    effective_at = OBSERVED_AT - timedelta(days=1)
    records: list[SimpleNamespace] = []
    attempts = []
    if preceding_not_found:
        attempts.append(
            ("nasdaq", "short_interest", None, None, "not_found", 404)
        )
    resolved_exchange = "nyse" if preceding_not_found else "nasdaq"
    attempts.extend(
        (
            (
                resolved_exchange,
                "short_interest",
                "shortInterestPcFreeFloat",
                0.235,
                "success",
                200,
            ),
            (
                resolved_exchange,
                "cost_to_borrow",
                "costToBorrowAll",
                47.0,
                "success",
                200,
            ),
        )
    )
    for index, (
        exchange,
        dataset,
        field,
        value,
        provider_outcome,
        http_status,
    ) in enumerate(attempts):
        plan = ortex_module._request_plan(
            dataset=dataset,
            symbol=symbol,
            exchange=exchange,
            policy_sha256=policy_sha256,
        )
        received_at = (
            OBSERVED_AT
            - timedelta(seconds=3)
            + timedelta(milliseconds=100 * index)
        )
        provider_event_at = (
            None
            if field is None
            else effective_at + timedelta(hours=12)
        )
        raw = (
            None
            if field is None
            else json.dumps(
                {
                    "rows": [
                        {
                            "date": effective_at.date().isoformat(),
                            "updatedAt": provider_event_at.isoformat(),
                            field: value,
                        }
                    ]
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        bundle_sha256 = hashlib.sha256(
            (
                f"{symbol}:{exchange}:{dataset}:"
                f"{OBSERVED_AT.isoformat()}"
            ).encode()
        ).hexdigest()
        records.append(
            SimpleNamespace(
                attempt_id=uuid.uuid4(),
                month_start=OBSERVED_AT.date().replace(day=1),
                bundle_sha256=bundle_sha256,
                bundle_index=0,
                owner_token=f"owner-{symbol}-{index}",
                request_sha256=plan.request_sha256,
                endpoint=f"/api/v1{plan.path}",
                symbol=symbol,
                status="completed",
                monthly_limit=1_000,
                quota_used_after=10 + index,
                reserved_at=received_at - timedelta(seconds=2),
                lease_expires_at=received_at + timedelta(seconds=28),
                provider_outcome=provider_outcome,
                http_status=http_status,
                backoff_until=None,
                raw_response_body_b64=(
                    None
                    if raw is None
                    else base64.b64encode(raw).decode("ascii")
                ),
                response_sha256=(
                    None if raw is None else hashlib.sha256(raw).hexdigest()
                ),
                provider_event_at=provider_event_at,
                effective_at=(
                    None
                    if raw is None
                    else datetime(
                        effective_at.year,
                        effective_at.month,
                        effective_at.day,
                        tzinfo=UTC,
                    )
                ),
                received_at=received_at,
                available_at=received_at + timedelta(milliseconds=1),
            )
        )
    return ortex_outcome_from_completed_attempts(
        symbol=symbol,
        requested_exchange=None if preceding_not_found else "nasdaq",
        records=records,
        policy=policy,
        observed_at=OBSERVED_AT,
    )


def _batch(
    outcomes: tuple[OrtexShortMechanicsOutcome, ...],
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    scores: dict[str, float | None] = {}
    for outcome in outcomes:
        score = squeeze_fuel_signal(
            outcome.short_interest_pct,
            outcome.cost_to_borrow,
            utilization=outcome.utilization,
            is_easy_to_borrow=outcome.is_easy_to_borrow,
        ).squeeze_pct
        scores[outcome.symbol] = score
    score_values = sorted(float(score) for score in scores.values() if score is not None)
    for outcome in sorted(outcomes, key=lambda item: item.symbol):
        mechanics = outcome.to_capture_dict()
        score = scores[outcome.symbol]
        rank = (
            None
            if score is None
            else round(
                sum(value <= float(score) for value in score_values)
                / len(score_values),
                4,
            )
        )
        rows.append(
            {
                "symbol": outcome.symbol,
                "short_mechanics": mechanics,
                "short_mechanics_sha256": sha256_json(mechanics),
                "squeeze_fuel_pct": score,
                "rank_pct": rank,
            }
        )
    return {
        "schema_version": "chili.ortex-selection-snapshot.v1",
        "members": rows,
        "members_sha256": sha256_json(rows),
        "complete": True,
    }


def _not_applicable(symbol: str) -> OrtexShortMechanicsOutcome:
    policy = ortex_public_policy()
    return OrtexShortMechanicsOutcome.not_applicable(
        symbol=symbol,
        reason="outside_top_n",
        observed_at=OBSERVED_AT,
        policy_sha256=sha256_json(policy),
        policy=policy,
    )


def _non_equity(symbol: str) -> OrtexShortMechanicsOutcome:
    policy = ortex_public_policy()
    return OrtexShortMechanicsOutcome.not_applicable(
        symbol=symbol,
        reason="non_equity",
        observed_at=OBSERVED_AT,
        policy_sha256=sha256_json(policy),
        policy=policy,
    )


def _selection_manifest(
    outcomes: tuple[OrtexShortMechanicsOutcome, ...],
) -> dict[str, object]:
    scores = {
        outcome.symbol: squeeze_fuel_signal(
            outcome.short_interest_pct,
            outcome.cost_to_borrow,
            utilization=outcome.utilization,
            is_easy_to_borrow=outcome.is_easy_to_borrow,
        ).squeeze_pct
        for outcome in outcomes
    }
    score_values = sorted(
        float(score) for score in scores.values() if score is not None
    )
    members = []
    selected_symbols = []
    for outcome in sorted(outcomes, key=lambda item: item.symbol):
        score = scores[outcome.symbol]
        rank = (
            None
            if score is None
            else round(
                sum(value <= float(score) for value in score_values)
                / len(score_values),
                4,
            )
        )
        reference = outcome.to_selection_reference_dict()
        members.append(
            {
                "symbol": outcome.symbol,
                "ortex_selection_reference": reference,
                "squeeze_fuel_pct": score,
                "squeeze_fuel_rank_pct": rank,
            }
        )
        if reference["detail_code"] not in {
            "outside_top_n",
            "non_equity",
        }:
            selected_symbols.append(outcome.symbol)
    manifest = {
        "schema_version": "chili.ortex.squeeze-fuel-batch.v1",
        "decision_at": OBSERVED_AT.isoformat(),
        "complete": True,
        "quota_policy_sha256": sha256_json(ortex_public_policy()),
        "selected_symbols": selected_symbols,
        "members": members,
        "members_sha256": sha256_json(members),
    }
    manifest["batch_sha256"] = sha256_json(manifest)
    return manifest


def _selection_manifest_ref(
    manifest: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "chili.ortex.squeeze-fuel-batch-ref.v1",
        "batch_sha256": manifest["batch_sha256"],
        "decision_at": manifest["decision_at"],
        "complete": manifest["complete"],
        "quota_policy_sha256": manifest["quota_policy_sha256"],
        "members_sha256": manifest["members_sha256"],
    }


def _resign_selection_manifest(manifest: dict[str, object]) -> None:
    members = manifest["members"]
    assert isinstance(members, list) and members
    member = members[0]
    assert isinstance(member, dict)
    reference = member["ortex_selection_reference"]
    assert isinstance(reference, dict)
    unsigned_reference = dict(reference)
    unsigned_reference.pop("selection_reference_sha256", None)
    reference["selection_reference_sha256"] = sha256_json(
        unsigned_reference
    )
    manifest["members_sha256"] = sha256_json(members)
    unsigned_manifest = dict(manifest)
    unsigned_manifest.pop("batch_sha256", None)
    manifest["batch_sha256"] = sha256_json(unsigned_manifest)


def test_mixed_success_and_neutral_batch_rederives_only_success_rank() -> None:
    batch = _batch((_success_outcome("ACTU"), _not_applicable("BETA")))

    scored = validate_ortex_selection_batch(
        batch,
        ranked_symbol="ACTU",
        expected_rank_pct=1.0,
    )
    neutral = validate_ortex_selection_batch(
        batch,
        ranked_symbol="BETA",
        expected_rank_pct=None,
    )

    assert scored.outcome == "SUCCESS"
    assert scored.rank_pct == 1.0
    assert neutral.outcome == "NOT_APPLICABLE"
    assert neutral.rank_pct is None
    assert scored.batch_members_sha256 == neutral.batch_members_sha256
    assert scored.payload == neutral.payload


def test_all_not_applicable_batch_is_complete_without_fabricated_ranks() -> None:
    batch = _batch((_not_applicable("ACTU"), _not_applicable("BETA")))

    for symbol in ("ACTU", "BETA"):
        typed = CaptureOrtexSelectionSnapshot.from_dict(
            batch,
            expected_symbol=symbol,
        )
        assert typed.complete is True
        assert typed.outcome == "NOT_APPLICABLE"
        assert typed.rank_pct is None
        assert typed.current_member["squeeze_fuel_pct"] is None


def test_full_selection_manifest_and_compact_reference_bind_exactly() -> None:
    manifest = _selection_manifest(
        (_success_outcome("ACTU"), _not_applicable("BETA"))
    )
    policy = ortex_public_policy()
    read_at = OBSERVED_AT + timedelta(seconds=1)

    typed_manifest = validate_ortex_squeeze_fuel_batch_manifest(
        manifest,
        read_at=read_at,
        expected_quota_policy_sha256=sha256_json(policy),
        freshness_ttl_seconds=float(policy["success_cache_ttl_seconds"]),
    )
    typed_reference = validate_ortex_squeeze_fuel_batch_reference(
        _selection_manifest_ref(manifest),
        read_at=read_at,
        expected_quota_policy_sha256=sha256_json(policy),
    )

    assert (
        bind_ortex_squeeze_fuel_batch_reference(
            typed_reference,
            typed_manifest,
        )
        is typed_manifest
    )
    assert typed_manifest.selected_symbols == ("ACTU",)
    assert tuple(typed_manifest.signal_by_symbol) == ("ACTU", "BETA")


def test_selection_manifest_accepts_current_coinbase_non_equity_symbols() -> None:
    symbols = ("00-USD", "1INCH-USD", "2Z-USD")
    manifest = _selection_manifest(tuple(_non_equity(symbol) for symbol in symbols))

    typed = validate_ortex_squeeze_fuel_batch_manifest(
        manifest,
        read_at=OBSERVED_AT + timedelta(seconds=1),
        expected_quota_policy_sha256=sha256_json(ortex_public_policy()),
        freshness_ttl_seconds=float(
            ortex_public_policy()["success_cache_ttl_seconds"]
        ),
    )

    assert typed.complete is True
    assert typed.selected_symbols == ()
    assert tuple(typed.signal_by_symbol) == tuple(sorted(symbols))
    assert all(
        signal["ortex_selection_reference"]["detail_code"] == "non_equity"
        for signal in typed.signal_by_symbol.values()
    )


def test_selection_manifest_rejects_noncanonical_or_mislabeled_members() -> None:
    validate_kwargs = {
        "read_at": OBSERVED_AT + timedelta(seconds=1),
        "expected_quota_policy_sha256": sha256_json(ortex_public_policy()),
        "freshness_ttl_seconds": float(
            ortex_public_policy()["success_cache_ttl_seconds"]
        ),
    }
    selected_crypto = _selection_manifest((_non_equity("BTC-USD"),))
    selected_crypto["selected_symbols"] = ["BTC-USD"]
    _resign_selection_manifest(selected_crypto)
    with pytest.raises(CaptureContractError, match="selected symbol is invalid"):
        validate_ortex_squeeze_fuel_batch_manifest(
            selected_crypto,
            **validate_kwargs,
        )

    bare_numeric = _selection_manifest((_non_equity("1INCH-USD"),))
    members = bare_numeric["members"]
    assert isinstance(members, list) and isinstance(members[0], dict)
    members[0]["symbol"] = "1INCH"
    _resign_selection_manifest(bare_numeric)
    with pytest.raises(CaptureContractError, match="symbol-sorted/unique"):
        validate_ortex_squeeze_fuel_batch_manifest(
            bare_numeric,
            **validate_kwargs,
        )

    mislabeled = _selection_manifest((_non_equity("1INCH-USD"),))
    members = mislabeled["members"]
    assert isinstance(members, list) and isinstance(members[0], dict)
    reference = members[0]["ortex_selection_reference"]
    assert isinstance(reference, dict)
    reference["detail_code"] = "outside_top_n"
    _resign_selection_manifest(mislabeled)
    with pytest.raises(CaptureContractError, match="non-equity.*semantics"):
        validate_ortex_squeeze_fuel_batch_manifest(
            mislabeled,
            **validate_kwargs,
        )

    mislabeled_equity = _selection_manifest((_not_applicable("ACTU"),))
    members = mislabeled_equity["members"]
    assert isinstance(members, list) and isinstance(members[0], dict)
    reference = members[0]["ortex_selection_reference"]
    assert isinstance(reference, dict)
    reference["detail_code"] = "non_equity"
    _resign_selection_manifest(mislabeled_equity)
    with pytest.raises(CaptureContractError, match="non-equity.*semantics"):
        validate_ortex_squeeze_fuel_batch_manifest(
            mislabeled_equity,
            **validate_kwargs,
        )


def test_exchange_discovery_preserves_404_then_si_proof_then_ctb() -> None:
    manifest = _selection_manifest(
        (_success_outcome("ACTU", preceding_not_found=True),)
    )
    policy = ortex_public_policy()

    typed = validate_ortex_squeeze_fuel_batch_manifest(
        manifest,
        read_at=OBSERVED_AT + timedelta(seconds=1),
        expected_quota_policy_sha256=sha256_json(policy),
        freshness_ttl_seconds=float(
            policy["success_cache_ttl_seconds"]
        ),
    )

    reference = typed.signal_by_symbol["ACTU"][
        "ortex_selection_reference"
    ]
    assert [
        (endpoint["exchange"], endpoint["dataset"], endpoint["kind"])
        for endpoint in reference["endpoint_refs"]
    ] == [
        (
            "nasdaq",
            "short_interest",
            "UNSUPPORTED_SYMBOL_OR_EXCHANGE",
        ),
        ("nyse", "short_interest", "SUCCESS"),
        ("nyse", "cost_to_borrow", "SUCCESS"),
    ]


@pytest.mark.parametrize(
    "mutation",
    (
        "duplicate_exchange_dataset",
        "ctb_before_si_proof",
        "ctb_wrong_exchange",
        "attempt_after_ctb",
        "clock_regression",
        "request_hash_drift",
    ),
)
def test_endpoint_discovery_rejects_noncausal_or_unbound_chain(
    mutation: str,
) -> None:
    manifest = _selection_manifest(
        (_success_outcome("ACTU", preceding_not_found=True),)
    )
    members = manifest["members"]
    assert isinstance(members, list)
    reference = members[0]["ortex_selection_reference"]
    assert isinstance(reference, dict)
    endpoints = reference["endpoint_refs"]
    assert isinstance(endpoints, list)

    if mutation == "duplicate_exchange_dataset":
        duplicate = copy.deepcopy(endpoints[0])
        duplicate["attempt_id"] = str(uuid.uuid4())
        duplicate["endpoint_capture_sha256"] = "d" * 64
        endpoints.insert(1, duplicate)
    elif mutation == "ctb_before_si_proof":
        endpoints[:] = [endpoints[2], endpoints[0], endpoints[1]]
    elif mutation == "ctb_wrong_exchange":
        ctb = endpoints[2]
        ctb["exchange"] = "amex"
        ctb["request_sha256"] = ortex_module._request_plan(
            dataset="cost_to_borrow",
            symbol="ACTU",
            exchange="amex",
            policy_sha256=sha256_json(ortex_public_policy()),
        ).request_sha256
    elif mutation == "attempt_after_ctb":
        extra = copy.deepcopy(endpoints[0])
        extra["exchange"] = "amex"
        extra["attempt_id"] = str(uuid.uuid4())
        extra["request_sha256"] = ortex_module._request_plan(
            dataset="short_interest",
            symbol="ACTU",
            exchange="amex",
            policy_sha256=sha256_json(ortex_public_policy()),
        ).request_sha256
        extra["endpoint_capture_sha256"] = "e" * 64
        extra["received_at"] = (
            OBSERVED_AT - timedelta(seconds=2)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        extra["available_at"] = (
            OBSERVED_AT
            - timedelta(seconds=2)
            + timedelta(milliseconds=1)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        endpoints.append(extra)
    elif mutation == "clock_regression":
        endpoints[2]["received_at"] = endpoints[0]["received_at"]
        endpoints[2]["available_at"] = endpoints[0]["available_at"]
    elif mutation == "request_hash_drift":
        endpoints[1]["request_sha256"] = "f" * 64
    else:  # pragma: no cover - parameter list is closed above
        raise AssertionError(mutation)
    _resign_selection_manifest(manifest)

    policy = ortex_public_policy()
    with pytest.raises(CaptureContractError):
        validate_ortex_squeeze_fuel_batch_manifest(
            manifest,
            read_at=OBSERVED_AT + timedelta(seconds=1),
            expected_quota_policy_sha256=sha256_json(policy),
            freshness_ttl_seconds=float(
                policy["success_cache_ttl_seconds"]
            ),
        )


def test_full_selection_manifest_rejects_stale_member_source() -> None:
    manifest = _selection_manifest((_success_outcome("ACTU"),))
    policy = ortex_public_policy()

    with pytest.raises(CaptureContractError, match="stale"):
        validate_ortex_squeeze_fuel_batch_manifest(
            manifest,
            read_at=OBSERVED_AT + timedelta(hours=25),
            expected_quota_policy_sha256=sha256_json(policy),
            freshness_ttl_seconds=float(
                policy["success_cache_ttl_seconds"]
            ),
        )


def test_batch_clocks_cover_every_rank_contributor() -> None:
    first = replace(
        _success_outcome("ACTU"),
        received_at=OBSERVED_AT - timedelta(minutes=10),
        available_at=(
            OBSERVED_AT
            - timedelta(minutes=10)
            + timedelta(milliseconds=1)
        ),
    )
    second = replace(
        _success_outcome("BETA"),
        received_at=OBSERVED_AT - timedelta(minutes=1),
        available_at=(
            OBSERVED_AT
            - timedelta(minutes=1)
            + timedelta(milliseconds=1)
        ),
        cache_origin_sha256="a" * 64,
        cache_origin_received_at=OBSERVED_AT - timedelta(hours=23),
        cache_origin_available_at=(
            OBSERVED_AT
            - timedelta(hours=23)
            + timedelta(milliseconds=1)
        ),
    )

    typed = CaptureOrtexSelectionSnapshot.from_dict(
        _batch((first, second)),
        expected_symbol="ACTU",
    )

    assert typed.requested_at == min(first.received_at, second.received_at)
    assert typed.returned_at == max(first.available_at, second.available_at)
    assert typed.source_received_at == min(
        first.received_at,
        second.cache_origin_received_at,
    )
    assert typed.source_available_at == max(
        first.available_at,
        second.cache_origin_available_at,
    )


def test_future_provider_or_dataset_clock_is_rejected() -> None:
    outcome = _success_outcome("ACTU")
    before_dataset = outcome.effective_at - timedelta(days=1)
    outcome = replace(
        outcome,
        received_at=before_dataset,
        available_at=before_dataset,
    )

    with pytest.raises(CaptureContractError, match="future"):
        CaptureOrtexSelectionSnapshot.from_dict(
            _batch((outcome,)),
            expected_symbol="ACTU",
        )
