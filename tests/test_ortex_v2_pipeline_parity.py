from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import pipeline
from app.services.trading.momentum_neural import short_mechanics as sm
from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 27, 12, 30, tzinfo=UTC)
EFFECTIVE = datetime(2026, 7, 27, tzinfo=UTC)


def _outcome(
    symbol: str,
    *,
    kind: sm.OrtexOutcomeKind,
    short_interest_pct: float | None = None,
    cost_to_borrow: float | None = None,
) -> sm.OrtexShortMechanicsOutcome:
    policy = sm.ortex_public_policy()
    policy_sha256 = sm.ortex_public_policy_sha256()
    endpoints: tuple[sm.OrtexEndpointOutcome, ...] = ()
    resolved_exchange = None
    if kind is sm.OrtexOutcomeKind.SUCCESS:
        resolved_exchange = "nasdaq"
        rows = (
            ("short_interest", short_interest_pct),
            ("cost_to_borrow", cost_to_borrow),
        )
        built = []
        for index, (dataset, value) in enumerate(rows):
            field = (
                "shortInterestPcFreeFloat"
                if dataset == "short_interest"
                else "costToBorrowAll"
            )
            row = {"date": "2026-07-27", field: value}
            raw = json.dumps(
                {"rows": [row]},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            request_path = (
                f"/stock/nasdaq/{symbol}/short_interest"
                if dataset == "short_interest"
                else f"/stock/nasdaq/{symbol}/ctb/all"
            )
            query_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "provider": "ortex",
                        "dataset": dataset,
                        "symbol": symbol,
                        "exchange": "nasdaq",
                        "path": request_path,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            request_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "method": "GET",
                        "origin": policy["origin"],
                        "api_prefix": policy["api_prefix"],
                        "query_sha256": query_sha256,
                        "quota_policy_sha256": policy_sha256,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            quota = sm.OrtexQuotaProvenance(
                attempt_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"chili:test:{symbol}:{dataset}",
                    )
                ),
                bundle_sha256=hashlib.sha256(
                    f"{symbol}:bundle".encode()
                ).hexdigest(),
                bundle_index=index,
                owner_token_sha256=hashlib.sha256(
                    f"{symbol}:owner".encode()
                ).hexdigest(),
                request_sha256=request_sha256,
                month_start=datetime(2026, 7, 1, tzinfo=UTC),
                quota_used_after=index + 1,
                monthly_limit=1000,
                reserved_at=NOW,
                lease_expires_at=NOW,
                transport_committed=True,
                completion_kind="COMPLETED",
            )
            endpoint_kind = (
                sm.OrtexOutcomeKind.SUCCESS
                if value is not None
                else sm.OrtexOutcomeKind.AUTHORITATIVE_EMPTY
            )
            built.append(
                sm.OrtexEndpointOutcome(
                    kind=endpoint_kind,
                    dataset=dataset,
                    symbol=symbol,
                    exchange="nasdaq",
                    request_path=request_path,
                    request_sha256=request_sha256,
                    query_sha256=query_sha256,
                    quota_policy_sha256=policy_sha256,
                    http_status=200,
                    provider_event_at=None,
                    effective_at=EFFECTIVE,
                    received_at=NOW,
                    available_at=NOW,
                    raw_response_b64=base64.b64encode(raw).decode("ascii"),
                    raw_response_sha256=hashlib.sha256(raw).hexdigest(),
                    selected_row_sha256=hashlib.sha256(
                        json.dumps(
                            row,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    value=value,
                    detail_code=(
                        "provider_row"
                        if value is not None
                        else "provider_empty_rows"
                    ),
                    quota=quota,
                )
            )
        endpoints = tuple(built)
    return sm.OrtexShortMechanicsOutcome(
        kind=kind,
        symbol=symbol,
        requested_exchange=None,
        resolved_exchange=resolved_exchange,
        endpoints=endpoints,
        short_interest_pct=short_interest_pct,
        cost_to_borrow=cost_to_borrow,
        utilization=None,
        is_easy_to_borrow=(
            None if cost_to_borrow is None else cost_to_borrow <= 1.0
        ),
        provider_event_at=None,
        effective_at=EFFECTIVE,
        received_at=NOW,
        available_at=NOW,
        detail_code=(
            "provider_success"
            if kind is sm.OrtexOutcomeKind.SUCCESS
            else "synthetic_unavailable"
        ),
        policy=policy,
        quota_policy_sha256=policy_sha256,
    )


class _Provider:
    network_fallback_allowed = False

    def __init__(self, outcomes: dict[str, sm.OrtexShortMechanicsOutcome]):
        self.outcomes = outcomes
        self.calls: list[str] = []

    def get_short_mechanics_outcome(
        self,
        *,
        symbol: str,
        exchange_hint: str | None,
    ) -> sm.OrtexShortMechanicsOutcome:
        assert exchange_hint is None
        self.calls.append(symbol)
        return self.outcomes[symbol]


def _signals() -> dict[str, dict[str, float]]:
    return {
        "AAA": {
            "vol_ratio": 30.0,
            "daily_change_pct": 80.0,
            "float_shares": 1_000_000.0,
            "dollar_volume": 5_000_000.0,
        },
        "BBB": {
            "vol_ratio": 20.0,
            "daily_change_pct": 60.0,
            "float_shares": 2_000_000.0,
            "dollar_volume": 4_000_000.0,
        },
        "CCC": {
            "vol_ratio": 10.0,
            "daily_change_pct": 40.0,
            "float_shares": 3_000_000.0,
            "dollar_volume": 3_000_000.0,
        },
        "BTC-USD": {
            "vol_ratio": 8.0,
            "daily_change_pct": 20.0,
            "dollar_volume": 2_000_000.0,
        },
        "1INCH-USD": {
            "vol_ratio": 7.0,
            "daily_change_pct": 18.0,
            "dollar_volume": 1_500_000.0,
        },
    }


def _apply(
    monkeypatch: pytest.MonkeyPatch,
    *,
    signals: dict[str, dict[str, float]],
    provider: _Provider,
    top_n: int,
) -> dict[str, float]:
    monkeypatch.setattr(
        pipeline.settings,
        "chili_momentum_squeeze_fuel_top_n",
        top_n,
    )
    with sm.ortex_outcome_provider(provider):
        return pipeline._apply_ortex_squeeze_fuel_batch(
            SimpleNamespace(get_bind=lambda: None),
            ross_signals=signals,
            weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
            decision_at=NOW,
        )


def test_complete_batch_stamps_compact_refs_and_reproducible_ranks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _signals()
    provider = _Provider(
        {
            "AAA": _outcome(
                "AAA",
                kind=sm.OrtexOutcomeKind.SUCCESS,
                short_interest_pct=0.40,
                cost_to_borrow=20.0,
            ),
            "BBB": _outcome(
                "BBB",
                kind=sm.OrtexOutcomeKind.SUCCESS,
                short_interest_pct=0.20,
                cost_to_borrow=8.0,
            ),
        }
    )

    weights = _apply(
        monkeypatch,
        signals=signals,
        provider=provider,
        top_n=2,
    )

    assert provider.calls == ["AAA", "BBB"]
    assert "squeeze_fuel" in weights
    assert signals["AAA"]["squeeze_fuel_rank_pct"] == 1.0
    assert signals["BBB"]["squeeze_fuel_rank_pct"] == 0.5
    assert "squeeze_fuel_pct" not in signals["CCC"]
    assert (
        signals["CCC"]["ortex_selection_reference"]["kind"]
        == "NOT_APPLICABLE"
    )
    assert (
        signals["BTC-USD"]["ortex_selection_reference"]["detail_code"]
        == "non_equity"
    )
    assert (
        signals["1INCH-USD"]["ortex_selection_reference"]["detail_code"]
        == "non_equity"
    )
    serialized = json.dumps(signals, sort_keys=True)
    assert "raw_response_b64" not in serialized
    assert "selection_reference_sha256" in serialized


def test_complete_batch_emits_hash_bound_global_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _signals()
    provider = _Provider(
        {
            "AAA": _outcome(
                "AAA",
                kind=sm.OrtexOutcomeKind.SUCCESS,
                short_interest_pct=0.40,
                cost_to_borrow=20.0,
            ),
            "BBB": _outcome(
                "BBB",
                kind=sm.OrtexOutcomeKind.SUCCESS,
                short_interest_pct=0.20,
                cost_to_borrow=8.0,
            ),
        }
    )
    monkeypatch.setattr(
        pipeline.settings,
        "chili_momentum_squeeze_fuel_top_n",
        2,
    )
    status: dict[str, object] = {}
    with sm.ortex_outcome_provider(provider):
        pipeline._apply_ortex_squeeze_fuel_batch(
            SimpleNamespace(get_bind=lambda: None),
            ross_signals=signals,
            weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
            decision_at=NOW,
            batch_status_out=status,
        )

    assert list(status) == [
        "schema_version",
        "decision_at",
        "complete",
        "quota_policy_sha256",
        "selected_symbols",
        "members",
        "members_sha256",
        "batch_sha256",
    ]
    assert status["selected_symbols"] == ["AAA", "BBB"]
    assert [member["symbol"] for member in status["members"]] == [
        "1INCH-USD",
        "AAA",
        "BBB",
        "BTC-USD",
        "CCC",
    ]
    valid, reason = pipeline._validate_ortex_batch_status(
        status,
        ross_signals=signals,
        required_symbol="CCC",
        read_at=NOW,
    )
    assert (valid, reason) == (True, "complete")
    compact_reference = pipeline._ortex_batch_reference(status)
    assert (
        pipeline.ortex_batch_readiness_reason(
            {
                "extra": {
                    pipeline.ORTEX_SQUEEZE_BATCH_STATUS_KEY: compact_reference,
                    "ross_signals": signals,
                }
            },
            symbol="AAA",
            manifest=status,
            read_at=NOW,
        )
        is None
    )
    tampered_projection = copy.deepcopy(signals)
    tampered_projection["AAA"]["squeeze_fuel_rank_pct"] = 0.01
    assert pipeline.ortex_batch_readiness_reason(
        {
            "extra": {
                pipeline.ORTEX_SQUEEZE_BATCH_STATUS_KEY: compact_reference,
                "ross_signals": tampered_projection,
            }
        },
        symbol="AAA",
        manifest=status,
        read_at=NOW,
    ) == "ortex_batch_signal_economics_mismatch"

    new_crosser_projection = copy.deepcopy(signals)
    new_crosser_projection["ZOOM"] = {
        "ticker": "ZOOM",
        "signal_type": "tape_delta_ignite",
    }
    assert pipeline._validate_ortex_batch_status(
        status,
        ross_signals=new_crosser_projection,
        read_at=NOW,
    ) == (False, "ortex_batch_signal_outside_field")

    forged = copy.deepcopy(status)
    forged["members"][0]["squeeze_fuel_rank_pct"] = 0.01
    assert pipeline.ortex_batch_readiness_reason(
        {
            "extra": {
                pipeline.ORTEX_SQUEEZE_BATCH_STATUS_KEY: compact_reference,
                "ross_signals": signals,
            }
        },
        symbol="AAA",
        manifest=forged,
        read_at=NOW,
    ) == "ortex_batch_reference_or_manifest_invalid"


def test_batch_decision_clock_advances_to_provider_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = {"AAA": _signals()["AAA"]}
    provider = _Provider(
        {
            "AAA": _outcome(
                "AAA",
                kind=sm.OrtexOutcomeKind.SUCCESS,
                short_interest_pct=0.40,
                cost_to_borrow=20.0,
            )
        }
    )
    monkeypatch.setattr(
        pipeline.settings,
        "chili_momentum_squeeze_fuel_top_n",
        1,
    )
    status: dict[str, object] = {}

    with sm.ortex_outcome_provider(provider):
        pipeline._apply_ortex_squeeze_fuel_batch(
            SimpleNamespace(get_bind=lambda: None),
            ross_signals=signals,
            weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
            decision_at=NOW - timedelta(seconds=5),
            batch_status_out=status,
        )

    assert status["decision_at"] == NOW.isoformat()
    assert pipeline._validate_ortex_batch_status(
        status,
        ross_signals=signals,
        required_symbol="AAA",
        read_at=NOW,
    ) == (True, "complete")


def test_empty_field_is_typed_incomplete_without_provider_or_clock_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider({})
    monkeypatch.setattr(
        pipeline.settings,
        "chili_momentum_squeeze_fuel_top_n",
        12,
    )
    status: dict[str, object] = {}

    with sm.ortex_outcome_provider(provider):
        weights = pipeline._apply_ortex_squeeze_fuel_batch(
            SimpleNamespace(get_bind=lambda: None),
            ross_signals={},
            weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
            decision_at=NOW,
            batch_status_out=status,
        )

    assert weights == ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED
    assert provider.calls == []
    assert status["decision_at"] == NOW.isoformat()
    assert status["complete"] is False
    assert status["members"] == []


def test_field_rejects_unicode_casefold_alias_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider({})
    signals = {
        "ßTC-USD": copy.deepcopy(_signals()["BTC-USD"]),
        "SSTC-USD": copy.deepcopy(_signals()["BTC-USD"]),
    }
    monkeypatch.setattr(
        pipeline.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )

    with sm.ortex_outcome_provider(provider):
        with pytest.raises(ValueError, match="non-ASCII"):
            pipeline.prepare_ortex_squeeze_fuel_field(
                SimpleNamespace(get_bind=lambda: None),
                ross_signals=signals,
                weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
                decision_at=NOW,
            )

    assert provider.calls == []


def test_off_path_strips_stale_ortex_values_without_mutating_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _signals()
    signals["AAA"].update(
        {
            "squeeze_fuel_pct": 0.99,
            "squeeze_fuel_rank_pct": 1.0,
            "short_interest_pct": 0.50,
            "ortex_selection_reference": {"stale": True},
        }
    )
    original = copy.deepcopy(signals)
    monkeypatch.setattr(
        pipeline.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        False,
    )

    prepared, weights, status = pipeline.prepare_ortex_squeeze_fuel_field(
        SimpleNamespace(get_bind=lambda: None),
        ross_signals=signals,
        weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
        decision_at=NOW,
    )

    assert signals == original
    assert status is None
    assert weights == dict(ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
    assert all(
        key not in prepared["AAA"]
        for key in pipeline._ORTEX_DERIVED_SIGNAL_KEYS
    )


def test_one_transient_member_cannot_create_partial_rank_or_weight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _signals()
    provider = _Provider(
        {
            "AAA": _outcome(
                "AAA",
                kind=sm.OrtexOutcomeKind.SUCCESS,
                short_interest_pct=0.40,
                cost_to_borrow=20.0,
            ),
            "BBB": _outcome(
                "BBB",
                kind=sm.OrtexOutcomeKind.TRANSIENT_UNAVAILABLE,
            ),
        }
    )

    weights = _apply(
        monkeypatch,
        signals=signals,
        provider=provider,
        top_n=2,
    )

    assert weights == dict(ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
    for signal in signals.values():
        assert "squeeze_fuel_pct" not in signal
        assert "squeeze_fuel_rank_pct" not in signal
        assert "short_interest_pct" not in signal
        assert "cost_to_borrow" not in signal
        assert "ortex_selection_reference" in signal


def test_operationally_unavailable_batch_is_neutral_at_live_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _signals()
    provider = _Provider(
        {
            "AAA": _outcome(
                "AAA",
                kind=sm.OrtexOutcomeKind.BACKOFF_ACTIVE,
            ),
        }
    )
    monkeypatch.setattr(
        pipeline.settings,
        "chili_momentum_squeeze_fuel_top_n",
        1,
    )
    status: dict[str, object] = {}
    with sm.ortex_outcome_provider(provider):
        weights = pipeline._apply_ortex_squeeze_fuel_batch(
            SimpleNamespace(get_bind=lambda: None),
            ross_signals=signals,
            weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
            decision_at=NOW,
            batch_status_out=status,
        )
    reference = {
        "schema_version": "chili.ortex.squeeze-fuel-batch-ref.v1",
        "batch_sha256": status["batch_sha256"],
        "decision_at": status["decision_at"],
        "complete": status["complete"],
        "quota_policy_sha256": status["quota_policy_sha256"],
        "members_sha256": status["members_sha256"],
    }
    readiness = {
        "extra": {
            "ross_signals": signals,
            pipeline.ORTEX_SQUEEZE_BATCH_STATUS_KEY: reference,
        }
    }

    assert weights == dict(ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
    assert status["complete"] is False
    assert pipeline.ortex_batch_readiness_reason(
        readiness,
        symbol="AAA",
        manifest=status,
        read_at=NOW,
        allow_supplemental_neutral=False,
    ) == "ortex_batch_coverage_unavailable"
    assert pipeline.ortex_batch_readiness_reason(
        readiness,
        symbol="AAA",
        manifest=status,
        read_at=NOW,
        allow_supplemental_neutral=True,
    ) is None
    assert pipeline.ortex_batch_readiness_reason(
        readiness,
        symbol="CCC",
        manifest=status,
        read_at=NOW,
        allow_supplemental_neutral=True,
    ) is None


@pytest.mark.parametrize(
    "kind",
    (
        sm.OrtexOutcomeKind.MALFORMED_RESPONSE,
        sm.OrtexOutcomeKind.CONTRACT_MISMATCH,
        sm.OrtexOutcomeKind.PERMANENT_REJECTED,
        sm.OrtexOutcomeKind.DISABLED,
    ),
)
def test_non_operational_ortex_failure_remains_fail_closed_at_live_entry(
    monkeypatch: pytest.MonkeyPatch,
    kind: sm.OrtexOutcomeKind,
) -> None:
    signals = _signals()
    provider = _Provider({"AAA": _outcome("AAA", kind=kind)})
    monkeypatch.setattr(
        pipeline.settings,
        "chili_momentum_squeeze_fuel_top_n",
        1,
    )
    status: dict[str, object] = {}
    with sm.ortex_outcome_provider(provider):
        pipeline._apply_ortex_squeeze_fuel_batch(
            SimpleNamespace(get_bind=lambda: None),
            ross_signals=signals,
            weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
            decision_at=NOW,
            batch_status_out=status,
        )
    readiness = {
        "extra": {
            "ross_signals": signals,
            pipeline.ORTEX_SQUEEZE_BATCH_STATUS_KEY: {
                "schema_version": "chili.ortex.squeeze-fuel-batch-ref.v1",
                "batch_sha256": status["batch_sha256"],
                "decision_at": status["decision_at"],
                "complete": status["complete"],
                "quota_policy_sha256": status["quota_policy_sha256"],
                "members_sha256": status["members_sha256"],
            },
        }
    }

    assert pipeline.ortex_batch_readiness_reason(
        readiness,
        symbol="AAA",
        manifest=status,
        read_at=NOW,
        allow_supplemental_neutral=True,
    ) == "ortex_batch_coverage_unavailable"


def test_runtime_sizing_has_no_second_ortex_or_current_catalyst_fetch() -> None:
    source = __import__("inspect").getsource(
        __import__(
            "app.services.trading.momentum_neural.live_runner",
            fromlist=["tick_live_session"],
        ).tick_live_session
    )
    assert "get_short_mechanics" not in source
    assert "catalyst_grade_rank" not in source
    assert '_kc_row.get("squeeze_fuel_pct")' in source
    assert '"strong_catalyst_symbols"' in source
    assert '"weak_catalyst_symbols"' in source
    assert '"fake_catalyst_symbols"' in source
    assert "persisted_catalyst_news_grade_rank" in source


def test_live_guard_bypasses_only_master_off_or_crypto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.trading.momentum_neural import live_runner

    class _NoDbRead:
        def query(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("guard bypass must not read the DB")

    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        False,
    )
    assert (
        live_runner._live_ortex_entry_readiness_reason(
            _NoDbRead(),  # type: ignore[arg-type]
            execution_readiness={},
            symbol="AAA",
            read_at=NOW,
        )
        is None
    )
    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )
    assert (
        live_runner._live_ortex_entry_readiness_reason(
            _NoDbRead(),  # type: ignore[arg-type]
            execution_readiness={},
            symbol="BTC-USD",
            read_at=NOW,
        )
        is None
    )


def test_live_guard_binds_hub_manifest_before_entry_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspect

    from app.services.trading.momentum_neural import live_runner

    readiness = {
        "extra": {
            pipeline.ORTEX_SQUEEZE_BATCH_STATUS_KEY: {
                "schema_version": "chili.ortex.squeeze-fuel-batch-ref.v1",
            }
        }
    }
    manifest = {"schema_version": "chili.ortex.squeeze-fuel-batch.v1"}
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_ortex_batch_manifest_from_hub",
        lambda db, *, batch_reference: (
            calls.append(("resolve", batch_reference)) or manifest,
            None,
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "ortex_batch_readiness_reason",
        lambda execution_readiness, *, symbol, manifest, read_at,
        allow_supplemental_neutral=False: (
            calls.append(
                (
                    "validate",
                    (
                        execution_readiness,
                        symbol,
                        manifest,
                        read_at,
                        allow_supplemental_neutral,
                    ),
                )
            )
            or None
        ),
    )

    assert (
        live_runner._live_ortex_entry_readiness_reason(
            object(),  # type: ignore[arg-type]
            execution_readiness=readiness,
            symbol="AAA",
            read_at=NOW,
        )
        is None
    )
    assert [name for name, _value in calls] == ["resolve", "validate"]
    assert calls[1][1] == (readiness, "AAA", manifest, NOW, False)
    source = inspect.getsource(live_runner.tick_live_session)
    guard_index = source.index("_live_ortex_entry_readiness_reason")
    defer_index = source.index("live_entry_ortex_coverage_unavailable")
    bbo_index = source.index("spread_bps_live")
    assert guard_index < defer_index < bbo_index
    assert '"opportunity_consumed": False' in source
    assert '"risk_reserved": False' in source
    assert '"order_posted": False' in source


def test_live_guard_propagates_typed_missing_hub_without_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.trading.momentum_neural import live_runner

    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_ortex_batch_manifest_from_hub",
        lambda *_args, **_kwargs: (None, "ortex_batch_manifest_missing"),
    )
    monkeypatch.setattr(
        pipeline,
        "ortex_batch_readiness_reason",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing manifest must not reach validation")
        ),
    )

    assert live_runner._live_ortex_entry_readiness_reason(
        object(),  # type: ignore[arg-type]
        execution_readiness={
            "extra": {
                pipeline.ORTEX_SQUEEZE_BATCH_STATUS_KEY: {
                    "schema_version": "chili.ortex.squeeze-fuel-batch-ref.v1"
                }
            }
        },
        symbol="AAA",
        read_at=NOW,
    ) == "ortex_batch_manifest_missing"


def test_live_hub_manifest_resolver_is_read_only_exact_and_deep_copied() -> None:
    manifest = {
        "schema_version": "chili.ortex.squeeze-fuel-batch.v1",
        "batch_sha256": "a" * 64,
        "decision_at": NOW.isoformat(),
        "complete": True,
        "quota_policy_sha256": "b" * 64,
        "members_sha256": "c" * 64,
        "members": [{"symbol": "AAA"}],
    }
    reference = pipeline._ortex_batch_reference(manifest)

    class _Query:
        def __init__(self, row: object) -> None:
            self.row = row
            self.populate_count = 0
            self.filter_count = 0

        def populate_existing(self) -> "_Query":
            self.populate_count += 1
            return self

        def filter(self, *_args: object) -> "_Query":
            self.filter_count += 1
            return self

        def one_or_none(self) -> object:
            return self.row

    class _ReadOnlyDb:
        def __init__(self, row: object) -> None:
            self.query_object = _Query(row)
            self.query_count = 0

        def query(self, *_args: object) -> _Query:
            self.query_count += 1
            return self.query_object

        def add(self, *_args: object) -> None:
            raise AssertionError("resolver attempted an ORM mutation")

        def flush(self) -> None:
            raise AssertionError("resolver attempted to flush")

        def commit(self) -> None:
            raise AssertionError("resolver attempted to commit")

    hub_state = {pipeline.ORTEX_SQUEEZE_BATCH_STATUS_KEY: manifest}
    db = _ReadOnlyDb(SimpleNamespace(local_state=hub_state))
    resolved, reason = pipeline.resolve_ortex_batch_manifest_from_hub(
        db,  # type: ignore[arg-type]
        batch_reference=reference,
    )

    assert reason is None
    assert resolved == manifest
    assert resolved is not manifest
    assert db.query_count == 1
    assert db.query_object.populate_count == 1
    assert db.query_object.filter_count == 1
    assert resolved is not None
    resolved["members"].append({"symbol": "MUTATED"})
    assert manifest["members"] == [{"symbol": "AAA"}]


@pytest.mark.parametrize(
    ("row", "reference_mutation", "expected_reason"),
    [
        (
            SimpleNamespace(local_state={}),
            None,
            "ortex_batch_manifest_missing",
        ),
        (
            SimpleNamespace(
                local_state={
                    pipeline.ORTEX_SQUEEZE_BATCH_STATUS_KEY: {
                        "batch_sha256": "a" * 64,
                        "decision_at": NOW.isoformat(),
                        "complete": True,
                        "quota_policy_sha256": "b" * 64,
                        "members_sha256": "c" * 64,
                    }
                }
            ),
            ("members_sha256", "d" * 64),
            "ortex_batch_manifest_reference_mismatch",
        ),
    ],
)
def test_live_hub_manifest_resolver_missing_and_mismatch(
    row: object,
    reference_mutation: tuple[str, object] | None,
    expected_reason: str,
) -> None:
    manifest = {
        "schema_version": "chili.ortex.squeeze-fuel-batch.v1",
        "batch_sha256": "a" * 64,
        "decision_at": NOW.isoformat(),
        "complete": True,
        "quota_policy_sha256": "b" * 64,
        "members_sha256": "c" * 64,
    }
    reference = pipeline._ortex_batch_reference(manifest)
    if reference_mutation is not None:
        reference[reference_mutation[0]] = reference_mutation[1]

    class _Query:
        def populate_existing(self) -> "_Query":
            return self

        def filter(self, *_args: object) -> "_Query":
            return self

        def one_or_none(self) -> object:
            return row

    class _Db:
        def query(self, *_args: object) -> _Query:
            return _Query()

    assert pipeline.resolve_ortex_batch_manifest_from_hub(
        _Db(),  # type: ignore[arg-type]
        batch_reference=reference,
    ) == (None, expected_reason)


def test_live_hub_manifest_resolver_read_error_is_typed() -> None:
    manifest = {
        "batch_sha256": "a" * 64,
        "decision_at": NOW.isoformat(),
        "complete": True,
        "quota_policy_sha256": "b" * 64,
        "members_sha256": "c" * 64,
    }

    class _Db:
        def query(self, *_args: object) -> object:
            raise RuntimeError("read failed")

    assert pipeline.resolve_ortex_batch_manifest_from_hub(
        _Db(),  # type: ignore[arg-type]
        batch_reference=pipeline._ortex_batch_reference(manifest),
    ) == (None, "ortex_batch_manifest_read_unavailable")


def test_sealed_replay_ortex_guard_consumes_before_live_sizing_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspect

    from app.services.trading.momentum_neural import live_runner

    class _NoDb:
        def query(self, *_args: object) -> object:
            raise AssertionError("sealed replay guard reached current DB")

    mechanics = {"schema_version": "runtime-view.v1", "symbol": "AAA"}
    runtime_sha256 = live_runner._captured_paper_sha256(mechanics)
    calls: list[str] = []

    def provider(symbol: str) -> dict[str, object]:
        calls.append(symbol)
        return {
            "schema_version": "chili.ortex-selection-result.v2",
            "symbol": "AAA",
            "short_mechanics": mechanics,
            "short_mechanics_sha256": "a" * 64,
            "short_mechanics_runtime_sha256": runtime_sha256,
            "squeeze_fuel_pct": 0.91,
            "rank_pct": 1.0,
            "batch_members_sha256": "b" * 64,
            "complete": True,
            "supplemental_neutral_reason": None,
        }

    original = {"extra": {"strong_catalyst_symbols": ["AAA"]}}
    with live_runner.replay_ortex_selection_provider(provider):
        assert (
            live_runner._live_ortex_entry_readiness_reason(
                _NoDb(),  # type: ignore[arg-type]
                execution_readiness=original,
                symbol="AAA",
                read_at=NOW,
            )
            is None
        )
        projected = live_runner._overlay_replay_ortex_selection(
            original,
            symbol="AAA",
        )

    assert calls == ["AAA"]
    assert "ross_signals" not in original["extra"]
    signal = projected["extra"]["ross_signals"]["AAA"]
    assert signal["squeeze_fuel_pct"] == pytest.approx(0.91)
    assert signal["squeeze_fuel_rank_pct"] == pytest.approx(1.0)
    assert signal["ortex_replay_receipt"] == {
        "schema_version": "chili.replay-ortex-decision-receipt.v1",
        "short_mechanics_sha256": "a" * 64,
        "short_mechanics_runtime_sha256": runtime_sha256,
        "batch_members_sha256": "b" * 64,
    }
    source = inspect.getsource(live_runner.tick_live_session)
    assert source.index("_overlay_replay_ortex_selection") < source.index(
        "squeeze_entry_size_multiplier"
    )
    assert source.index("_overlay_replay_ortex_selection") < source.index(
        "triple_confluence_kelly_multiplier"
    )


def test_sealed_replay_operational_neutral_requires_paper_or_replay_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspect

    from app.services.trading.momentum_neural import captured_paper_dispatcher
    from app.services.trading.momentum_neural import live_runner
    from app.services.trading.momentum_neural import replay_v3

    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )
    mechanics = {"schema_version": "runtime-view.v1", "symbol": "AAA"}
    result = {
        "schema_version": "chili.ortex-selection-result.v2",
        "symbol": "AAA",
        "short_mechanics": mechanics,
        "short_mechanics_sha256": "a" * 64,
        "short_mechanics_runtime_sha256": (
            live_runner._captured_paper_sha256(mechanics)
        ),
        "squeeze_fuel_pct": None,
        "rank_pct": None,
        "batch_members_sha256": "b" * 64,
        "complete": False,
        "supplemental_neutral_reason": "operational_unavailable",
    }
    original = {"extra": {"strong_catalyst_symbols": ["AAA"]}}

    with live_runner.replay_ortex_selection_provider(lambda _symbol: result):
        assert live_runner._live_ortex_entry_readiness_reason(
            object(),  # type: ignore[arg-type]
            execution_readiness=original,
            symbol="AAA",
            read_at=NOW,
        ) == "ortex_batch_coverage_unavailable"

    with live_runner.replay_ortex_selection_provider(
        lambda _symbol: result,
        allow_supplemental_neutral=True,
    ):
        assert (
            live_runner._live_ortex_entry_readiness_reason(
                object(),  # type: ignore[arg-type]
                execution_readiness=original,
                symbol="AAA",
                read_at=NOW,
            )
            is None
        )
        assert (
            live_runner._overlay_replay_ortex_selection(
                original,
                symbol="AAA",
            )
            is original
        )
    driver_source = inspect.getsource(replay_v3.ReplayV3Driver)
    assert "allow_supplemental_neutral=True" in driver_source

    owner_marker = {
        "account_scope": "alpaca:paper",
        "live_cash_authorized": False,
    }
    monkeypatch.setattr(
        captured_paper_dispatcher,
        "revalidate_captured_paper_session_owner",
        lambda _session: owner_marker,
    )
    with live_runner.replay_ortex_selection_provider(lambda _symbol: result):
        assert (
            live_runner._live_ortex_entry_readiness_reason(
                object(),  # type: ignore[arg-type]
                execution_readiness=original,
                symbol="AAA",
                read_at=NOW,
                locked_session=object(),  # type: ignore[arg-type]
            )
            is None
        )
        assert (
            live_runner._overlay_replay_ortex_selection(
                original,
                symbol="AAA",
            )
            is original
        )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "symbol",
            "BBB",
            "sealed_replay_ortex_result_identity_mismatch",
        ),
        (
            "batch_members_sha256",
            "not-a-hash",
            "sealed_replay_ortex_batch_members_sha256_is_invalid",
        ),
        (
            "short_mechanics_runtime_sha256",
            "c" * 64,
            "sealed_replay_ortex_runtime_content_hash_mismatch",
        ),
    ],
)
def test_sealed_replay_ortex_guard_rejects_identity_and_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    expected: str,
) -> None:
    from app.services.trading.momentum_neural import live_runner
    from app.services.trading.momentum_neural.replay_errors import (
        ReplayInputContractError,
    )

    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )
    mechanics = {"schema_version": "runtime-view.v1", "symbol": "AAA"}
    result: dict[str, object] = {
        "schema_version": "chili.ortex-selection-result.v2",
        "symbol": "AAA",
        "short_mechanics": mechanics,
        "short_mechanics_sha256": "a" * 64,
        "short_mechanics_runtime_sha256": (
            live_runner._captured_paper_sha256(mechanics)
        ),
        "squeeze_fuel_pct": 0.91,
        "rank_pct": 1.0,
        "batch_members_sha256": "b" * 64,
        "complete": True,
        "supplemental_neutral_reason": None,
    }
    result[field] = value
    with live_runner.replay_ortex_selection_provider(lambda _symbol: result):
        with pytest.raises(ReplayInputContractError, match=expected):
            live_runner._live_ortex_entry_readiness_reason(
                object(),  # type: ignore[arg-type]
                execution_readiness={},
                symbol="AAA",
                read_at=NOW,
            )


def test_real_live_fsm_consumes_incomplete_sealed_ortex_at_entry_guard(
    db,
    monkeypatch: pytest.MonkeyPatch,
    stable_non_alpaca_account_identity,
) -> None:
    """The real FSM, not a post-step harness hook, owns receipt consumption."""

    from unittest.mock import patch

    import pandas as pd

    from app.config import settings
    from app.models.core import User
    from app.models.trading import MomentumSymbolViability
    from app.services.trading.momentum_neural import live_runner
    from app.services.trading.momentum_neural.live_fsm import (
        STATE_LIVE_PENDING_ENTRY,
        STATE_WATCHING_LIVE,
    )
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
    )
    from app.services.trading.momentum_neural.risk_policy import (
        RISK_SNAPSHOT_KEY,
    )
    from app.services.trading.venue.protocol import (
        FreshnessMeta,
        NormalizedProduct,
        NormalizedTicker,
    )
    from tests.test_momentum_live_runner import _mk_adapter
    from tests.test_momentum_paper_runner import _seed_live_eligible_row

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(
        settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )
    monkeypatch.setattr(
        live_runner,
        "_venue_broker_connected",
        lambda _family: True,
    )
    monkeypatch.setattr(
        live_runner,
        "runner_boundary_risk_ok",
        lambda *_args, **_kwargs: (True, {"allowed": True}),
    )
    monkeypatch.setattr(
        live_runner,
        "_replay_aware_fetch_ohlcv_df",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )
    variant_id, _variant = _seed_live_eligible_row(db, symbol="AAA")
    viability = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == "AAA",
            MomentumSymbolViability.variant_id == variant_id,
        )
        .one()
    )
    viability.live_eligible = True
    viability.paper_eligible = True
    viability.freshness_ts = NOW.replace(tzinfo=None)
    user = User(name=f"OrtexRealFsm_{uuid.uuid4().hex[:10]}")
    db.add(user)
    db.flush()
    session = create_trading_automation_session(
        db,
        user_id=int(user.id),
        symbol="AAA",
        variant_id=variant_id,
        execution_family="robinhood_spot",
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {
                "allowed": True,
                "evaluated_at_utc": NOW.isoformat(),
            },
            "non_alpaca_account_identity": (
                stable_non_alpaca_account_identity
            ),
            "momentum_risk_policy_summary": {
                "disable_live_if_governance_inhibit": True
            },
        },
    )
    db.commit()
    adapter = _mk_adapter()
    fresh = FreshnessMeta(
        retrieved_at_utc=NOW,
        max_age_seconds=120.0,
    )
    adapter.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="AAA",
            bid=9.99,
            ask=10.01,
            mid=10.00,
            spread_bps=20.0,
            freshness=fresh,
        ),
        fresh,
    )
    adapter.get_product.return_value = (
        NormalizedProduct(
            product_id="AAA",
            base_currency="AAA",
            quote_currency="USD",
            status="online",
            trading_disabled=False,
            cancel_only=False,
            limit_only=False,
            post_only=False,
            auction_mode=False,
            base_increment=1.0,
            base_min_size=1.0,
        ),
        fresh,
    )
    mechanics = {"schema_version": "runtime-view.v1", "symbol": "AAA"}
    calls: list[str] = []

    def provider(symbol: str) -> dict[str, object]:
        calls.append(symbol)
        return {
            "schema_version": "chili.ortex-selection-result.v2",
            "symbol": "AAA",
            "short_mechanics": mechanics,
            "short_mechanics_sha256": "a" * 64,
            "short_mechanics_runtime_sha256": (
                live_runner._captured_paper_sha256(mechanics)
            ),
            "squeeze_fuel_pct": None,
            "rank_pct": None,
            "batch_members_sha256": "b" * 64,
            "complete": False,
            "supplemental_neutral_reason": "operational_unavailable",
        }

    with live_runner.replay_clock(NOW), (
        live_runner.replay_ortex_selection_provider(provider)
    ), patch(
        "app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
        return_value=False,
    ):
        result = live_runner.tick_live_session(
            db,
            int(session.id),
            adapter_factory=lambda: adapter,
        )

    db.refresh(session)
    assert calls == ["AAA"], result
    assert result["reason"] == "ortex_batch_coverage_unavailable"
    assert result["deferred"] is True
    assert session.state == STATE_WATCHING_LIVE
    adapter.place_market_order.assert_not_called()
    adapter.place_limit_order_gtc.assert_not_called()
