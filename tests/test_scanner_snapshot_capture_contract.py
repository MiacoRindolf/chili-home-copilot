from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import uuid

import pytest

from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureClocks,
    CaptureContractError,
    CaptureEvent,
    CaptureRunIdentity,
    CaptureScannerProfile,
    CaptureScannerSnapshot,
    CaptureScannerSnapshotQuery,
    CaptureStream,
    SCANNER_SNAPSHOT_PROVIDER,
    build_scanner_snapshot_payload,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 15, 13, 0, tzinfo=UTC)
CONFIG_SHA256 = "b" * 64


def _epoch_ns(value: datetime) -> int:
    utc = value.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000


def _identity(*, config_sha256: str = CONFIG_SHA256) -> CaptureRunIdentity:
    return CaptureRunIdentity(
        run_id=str(uuid.UUID("00000000-0000-0000-0000-000000000715")),
        generation=7,
        code_build_sha256="a" * 64,
        config_sha256=config_sha256,
        feature_flags_sha256="c" * 64,
        account_identity_sha256="d" * 64,
        broker="alpaca",
        broker_environment="paper",
    )


def _profile(*, max_age_seconds: float = 300.0) -> CaptureScannerProfile:
    return CaptureScannerProfile(
        profile_id="equity_ross_smallcap",
        asset_class="equity",
        price_min=1.0,
        price_max=20.0,
        min_dollar_volume=1_000_000.0,
        min_change_pct=5.0,
        snapshot_max_age_seconds=max_age_seconds,
    )


def _query(
    *,
    symbol: str = "VEEE",
    max_age_seconds: float = 300.0,
    provider_cache_ttl_seconds: float | None = None,
    config_sha256: str = CONFIG_SHA256,
) -> CaptureScannerSnapshotQuery:
    profile = _profile(max_age_seconds=max_age_seconds)
    effective_ttl = (
        min(1_800.0, max(60.0, max_age_seconds))
        if provider_cache_ttl_seconds is None
        else provider_cache_ttl_seconds
    )
    return CaptureScannerSnapshotQuery(
        symbol=symbol,
        include_otc=False,
        max_age_seconds=max_age_seconds,
        provider_cache_ttl_seconds=effective_ttl,
        profile=profile,
        profile_sha256=profile.profile_sha256,
        config_sha256=config_sha256,
    )


def _source_projection() -> dict:
    return {
        "ticker": "VEEE",
        "todaysChangePerc": 31.0,
        "updated": _epoch_ns(BASE),
        "lastTrade": {"p": 4.20, "t": _epoch_ns(BASE)},
        "day": {"c": 4.05, "vw": 3.95, "v": 500_000.0},
        "min": {"c": 4.18, "av": 600_000.0},
    }


def _event(
    query: CaptureScannerSnapshotQuery,
    *,
    payload: dict | None = None,
    market_reference_at: datetime = BASE,
    identity: CaptureRunIdentity | None = None,
    raw_query: dict | None = None,
) -> CaptureEvent:
    resolved_payload = payload or build_scanner_snapshot_payload(
        query,
        market_reference_at=market_reference_at,
        source_projection=_source_projection(),
    )
    return CaptureEvent(
        identity=identity or _identity(config_sha256=query.config_sha256),
        sequence=1,
        stream=CaptureStream.SCANNER_SNAPSHOT,
        provider=SCANNER_SNAPSHOT_PROVIDER,
        symbol="VEEE",
        clocks=CaptureClocks(
            market_reference_at=market_reference_at,
            received_at=market_reference_at + timedelta(milliseconds=10),
            available_at=market_reference_at + timedelta(milliseconds=20),
        ),
        query=raw_query or query.to_dict(),
        payload=resolved_payload,
    )


def test_typed_scanner_snapshot_binds_query_clock_projection_and_provenance() -> None:
    query = _query()
    event = _event(query)

    snapshot = CaptureScannerSnapshot.from_event(event)

    assert event.query_sha256 == query.query_sha256
    assert snapshot.query.profile.profile_id == "equity_ross_smallcap"
    assert snapshot.query.profile_sha256 == query.profile.profile_sha256
    assert snapshot.query.config_sha256 == event.identity.config_sha256
    assert snapshot.event.clocks.market_reference_at == BASE
    assert snapshot.price == 4.20
    assert snapshot.change_pct == 31.0
    assert snapshot.share_volume == 600_000.0
    assert snapshot.dollar_volume == 2_520_000.0
    assert snapshot.source_projection["lastTrade"]["p"] == 4.20
    with pytest.raises(TypeError, match="immutable"):
        snapshot.source_projection["day"]["v"] = 1.0


def test_typed_scanner_snapshot_preserves_genuinely_missing_values() -> None:
    query = _query()
    source = {
        "ticker": "VEEE",
        "todaysChangePerc": None,
        "updated": _epoch_ns(BASE),
        "lastTrade": {"p": None, "t": None},
        "day": {"c": None, "vw": None, "v": None},
        "min": {"c": None, "av": None},
    }
    payload = build_scanner_snapshot_payload(
        query,
        market_reference_at=BASE,
        source_projection=source,
    )

    snapshot = CaptureScannerSnapshot.from_event(_event(query, payload=payload))

    assert snapshot.price is None
    assert snapshot.change_pct is None
    assert snapshot.share_volume is None
    assert snapshot.dollar_volume is None
    assert snapshot.event.payload["resolved"] == {
        "price": None,
        "change_pct": None,
        "share_volume": None,
        "dollar_volume": None,
    }


@pytest.mark.parametrize(
    ("path", "invalid"),
    (
        (("lastTrade", "p"), True),
        (("lastTrade", "p"), "4.20"),
        (("day", "v"), math.nan),
        (("min", "av"), math.inf),
        (("day", "c"), -0.01),
        (("min", "av"), -1.0),
    ),
)
def test_scanner_source_projection_rejects_nonfinite_non_numeric_and_negative_values(
    path: tuple[str, str],
    invalid: object,
) -> None:
    source = _source_projection()
    source[path[0]][path[1]] = invalid

    with pytest.raises(CaptureContractError):
        build_scanner_snapshot_payload(
            _query(),
            market_reference_at=BASE,
            source_projection=source,
        )


def test_scanner_recomputes_resolved_values_and_rejects_self_attestation() -> None:
    query = _query()
    payload = build_scanner_snapshot_payload(
        query,
        market_reference_at=BASE,
        source_projection=_source_projection(),
    )
    payload["resolved"]["price"] = 19.99

    with pytest.raises(
        CaptureContractError,
        match="resolved values differ from their source projection",
    ):
        CaptureScannerSnapshot.from_event(_event(query, payload=payload))


def test_scanner_rejects_source_mutation_without_matching_content_address() -> None:
    query = _query()
    payload = build_scanner_snapshot_payload(
        query,
        market_reference_at=BASE,
        source_projection=_source_projection(),
    )
    payload["source_projection"]["day"]["v"] = 900_000.0

    with pytest.raises(CaptureContractError, match="projection content hash mismatch"):
        CaptureScannerSnapshot.from_event(_event(query, payload=payload))


@pytest.mark.parametrize(
    ("target", "expected"),
    (
        ("payload", "payload fields do not match schema"),
        ("query", "query fields do not match schema"),
        ("profile", "profile fields do not match schema"),
        ("source", "source projection fields do not match schema"),
        ("resolved", "resolved fields do not match schema"),
    ),
)
def test_scanner_contract_rejects_extra_fields_at_every_layer(
    target: str,
    expected: str,
) -> None:
    query = _query()
    raw_query = query.to_dict()
    payload = build_scanner_snapshot_payload(
        query,
        market_reference_at=BASE,
        source_projection=_source_projection(),
    )
    if target == "payload":
        payload["candidate_self_attested"] = True
    elif target == "query":
        raw_query["candidate_self_attested"] = True
    elif target == "profile":
        raw_query["profile"]["candidate_self_attested"] = True
    elif target == "source":
        payload["source_projection"]["candidate_self_attested"] = True
    else:
        payload["resolved"]["candidate_self_attested"] = True

    with pytest.raises(CaptureContractError, match=expected):
        CaptureScannerSnapshot.from_event(
            _event(query, payload=payload, raw_query=raw_query)
        )


@pytest.mark.parametrize(
    ("requested", "effective"),
    ((10.0, 60.0), (300.0, 300.0), (2_000.0, 1_800.0)),
)
def test_scanner_query_binds_massive_provider_max_age_semantics(
    requested: float,
    effective: float,
) -> None:
    query = _query(
        max_age_seconds=requested,
        provider_cache_ttl_seconds=effective,
    )

    assert query.max_age_seconds == requested
    assert query.provider_cache_ttl_seconds == effective


def test_scanner_query_rejects_wrong_cache_ttl_profile_age_and_numeric_types() -> None:
    with pytest.raises(CaptureContractError, match="provider max_age semantics"):
        _query(max_age_seconds=300.0, provider_cache_ttl_seconds=301.0)

    profile = _profile(max_age_seconds=300.0)
    with pytest.raises(CaptureContractError, match="differs from its resolved profile"):
        CaptureScannerSnapshotQuery(
            symbol="VEEE",
            include_otc=False,
            max_age_seconds=120.0,
            provider_cache_ttl_seconds=120.0,
            profile=profile,
            profile_sha256=profile.profile_sha256,
            config_sha256=CONFIG_SHA256,
        )

    with pytest.raises(CaptureContractError, match="finite number"):
        _query(max_age_seconds=True)  # type: ignore[arg-type]
    with pytest.raises(CaptureContractError, match="finite number"):
        _query(max_age_seconds=math.nan)


def test_scanner_rejects_symbol_query_clock_profile_and_config_mismatches() -> None:
    query = _query()

    lower_query = query.to_dict()
    lower_query["symbol"] = "veee"
    with pytest.raises(CaptureContractError, match="canonical equity"):
        CaptureScannerSnapshot.from_event(_event(query, raw_query=lower_query))

    wrong_clock_payload = build_scanner_snapshot_payload(
        query,
        market_reference_at=BASE,
        source_projection=_source_projection(),
    )
    wrong_clock_payload["market_reference_at"] = (
        BASE + timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    with pytest.raises(
        CaptureContractError,
        match="provider market clock differs from its source projection",
    ):
        CaptureScannerSnapshot.from_event(
            _event(
                query,
                payload=wrong_clock_payload,
                market_reference_at=BASE + timedelta(seconds=1),
            )
        )

    wrong_profile_payload = build_scanner_snapshot_payload(
        query,
        market_reference_at=BASE,
        source_projection=_source_projection(),
    )
    wrong_profile_payload["profile_sha256"] = "f" * 64
    with pytest.raises(CaptureContractError, match="profile provenance mismatch"):
        CaptureScannerSnapshot.from_event(_event(query, payload=wrong_profile_payload))

    with pytest.raises(CaptureContractError, match="run/query config mismatch"):
        CaptureScannerSnapshot.from_event(
            _event(query, identity=_identity(config_sha256="e" * 64))
        )


def test_scanner_query_and_payload_hashes_bind_exact_content() -> None:
    query = _query()
    payload = build_scanner_snapshot_payload(
        query,
        market_reference_at=BASE,
        source_projection=_source_projection(),
    )
    payload["query_sha256"] = "f" * 64

    with pytest.raises(CaptureContractError, match="payload/query hash mismatch"):
        CaptureScannerSnapshot.from_event(_event(query, payload=payload))

    wrong_profile_query = deepcopy(query.to_dict())
    wrong_profile_query["profile"]["price_max"] = 21.0
    with pytest.raises(CaptureContractError, match="profile content hash mismatch"):
        CaptureScannerSnapshot.from_event(_event(query, raw_query=wrong_profile_query))
