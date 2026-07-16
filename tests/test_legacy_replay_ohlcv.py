from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
import json

import pandas as pd
import pytest

from app.services.trading.momentum_neural.legacy_replay_ohlcv import (
    LEGACY_EVIDENCE_ROLE,
    LegacyReplayArtifactError,
    LegacyReplayCertificationError,
    load_legacy_replay_symbol_ohlcv,
)


def _write_artifact(tmp_path, rows, **overrides):
    payload = {
        "date": "2026-07-13",
        "engine": "v2",
        "ran_at_utc": "2026-07-14T01:00:00.086091+00:00",
        "series": {"VEEE": rows},
    }
    payload.update(overrides)
    path = tmp_path / "legacy_live.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _five_minutes_unsorted_with_duplicate():
    return [
        ["09:34", 14.0, 15.0, 13.5, 14.5, 50],
        ["09:30", 10.0, 11.0, 9.0, 10.5, 10],
        ["09:32", 12.0, 13.0, 11.5, 12.5, 30],
        ["09:31", 11.0, 12.0, 10.5, 11.5, 20],
        ["09:33", 13.0, 14.0, 12.5, 13.5, 40],
        ["09:30", 10.0, 11.0, 9.0, 10.5, 10],
    ]


def test_loads_sorts_dedupes_and_causally_aggregates(tmp_path):
    path = _write_artifact(tmp_path, _five_minutes_unsorted_with_duplicate())

    loaded = load_legacy_replay_symbol_ohlcv(
        path,
        symbol="veee",
        assumed_timezone="America/New_York",
    )

    frames = loaded.frames_by_interval
    one_minute = frames["1m"]
    assert loaded.symbol == "VEEE"
    assert list(frames) == ["1m", "5m", "15m"]
    assert len(one_minute) == 5
    assert one_minute.index.is_monotonic_increasing
    assert str(one_minute.index.tz) == "UTC"
    # 09:30 EDT is 13:30 UTC on the artifact date.
    assert one_minute.index[0] == pd.Timestamp("2026-07-13T13:30:00Z")

    expected_aggregate = {
        "Open": 10.0,
        "High": 15.0,
        "Low": 9.0,
        "Close": 14.5,
        "Volume": 150.0,
    }
    assert frames["5m"].iloc[0].to_dict() == expected_aggregate
    assert frames["15m"].iloc[0].to_dict() == expected_aggregate
    assert frames["5m"].index[0] == pd.Timestamp("2026-07-13T13:30:00Z")


def test_frames_are_provider_ready_and_incomplete_aggregate_stays_hidden(tmp_path):
    path = _write_artifact(tmp_path, _five_minutes_unsorted_with_duplicate())
    loaded = load_legacy_replay_symbol_ohlcv(
        path,
        symbol="VEEE",
        assumed_timezone="America/New_York",
    )
    replay_now = [datetime(2026, 7, 13, 13, 34, 59, tzinfo=timezone.utc)]
    provider = loaded.recorded_provider(clock=lambda: replay_now[0])

    assert provider("VEEE", interval="5m").empty
    replay_now[0] = datetime(2026, 7, 13, 13, 35, tzinfo=timezone.utc)
    visible = provider("VEEE", interval="5m")

    assert len(visible) == 1
    assert visible.iloc[0]["Close"] == pytest.approx(14.5)


def test_returns_immutable_fail_closed_evidence_metadata(tmp_path):
    path = _write_artifact(tmp_path, _five_minutes_unsorted_with_duplicate())
    expected_sha = hashlib.sha256(path.read_bytes()).hexdigest()

    loaded = load_legacy_replay_symbol_ohlcv(
        path,
        symbol="VEEE",
        assumed_timezone="UTC",
    )
    evidence = loaded.evidence

    assert evidence.sha256 == expected_sha
    assert evidence.role == LEGACY_EVIDENCE_ROLE
    assert evidence.certification_ready is False
    assert evidence.coverage_credit_allowed is False
    assert evidence.ran_at_utc == datetime(
        2026, 7, 14, 1, 0, 0, 86091, tzinfo=timezone.utc
    )
    assert evidence.file_created_at_utc is not None
    assert evidence.file_modified_at_utc is not None
    assert "available_at_missing" in evidence.missing_provenance_reasons
    assert "artifact_timezone_not_declared" in evidence.missing_provenance_reasons
    assert "continuous_capture_coverage_unproven" in evidence.missing_provenance_reasons
    with pytest.raises(FrozenInstanceError):
        evidence.certification_ready = True


def test_returned_frames_are_defensive_copies(tmp_path):
    path = _write_artifact(tmp_path, _five_minutes_unsorted_with_duplicate())
    loaded = load_legacy_replay_symbol_ohlcv(
        path,
        symbol="VEEE",
        assumed_timezone="UTC",
    )

    first = loaded.frames_by_interval
    first["1m"].iloc[0, first["1m"].columns.get_loc("Close")] = 999.0

    assert loaded.frames_by_interval["1m"].iloc[0]["Close"] != 999.0
    with pytest.raises(TypeError):
        first["2m"] = first["1m"]


@pytest.mark.parametrize("flag", ["certification_mode", "coverage_credit"])
def test_loader_refuses_certification_and_coverage_credit(tmp_path, flag):
    path = _write_artifact(tmp_path, _five_minutes_unsorted_with_duplicate())

    with pytest.raises(LegacyReplayCertificationError, match="cannot be used"):
        load_legacy_replay_symbol_ohlcv(
            path,
            symbol="VEEE",
            assumed_timezone="UTC",
            **{flag: True},
        )


@pytest.mark.parametrize("flag", ["certification_mode", "coverage_credit"])
def test_provider_factory_refuses_certification_and_coverage_credit(tmp_path, flag):
    path = _write_artifact(tmp_path, _five_minutes_unsorted_with_duplicate())
    loaded = load_legacy_replay_symbol_ohlcv(
        path,
        symbol="VEEE",
        assumed_timezone="UTC",
    )

    with pytest.raises(LegacyReplayCertificationError, match="cannot be used"):
        loaded.recorded_provider(**{flag: True})


def test_conflicting_duplicate_minute_fails_closed(tmp_path):
    path = _write_artifact(
        tmp_path,
        [
            ["09:30", 10.0, 11.0, 9.0, 10.5, 100],
            ["09:30", 10.0, 12.0, 9.0, 11.5, 200],
        ],
    )

    with pytest.raises(LegacyReplayArtifactError, match="conflicting duplicate"):
        load_legacy_replay_symbol_ohlcv(
            path,
            symbol="VEEE",
            assumed_timezone="UTC",
        )


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (["9:30", 10.0, 11.0, 9.0, 10.5, 100], "canonical HH:MM"),
        (["09:30", 10.0, 9.5, 9.0, 10.5, 100], "high is below"),
        (["09:30", 10.0, 11.0, 10.25, 10.5, 100], "low is above"),
        (["09:30", 10.0, 11.0, 9.0, 10.5, -1], "non-negative"),
    ],
)
def test_invalid_minute_rows_fail_closed(tmp_path, row, message):
    path = _write_artifact(tmp_path, [row])

    with pytest.raises(LegacyReplayArtifactError, match=message):
        load_legacy_replay_symbol_ohlcv(
            path,
            symbol="VEEE",
            assumed_timezone="UTC",
        )


def test_missing_ran_at_is_preserved_as_a_provenance_reason(tmp_path):
    path = _write_artifact(
        tmp_path,
        _five_minutes_unsorted_with_duplicate(),
        ran_at_utc=None,
    )

    loaded = load_legacy_replay_symbol_ohlcv(
        path,
        symbol="VEEE",
        assumed_timezone="UTC",
    )

    assert loaded.evidence.ran_at_utc is None
    assert "artifact_ran_at_missing" in loaded.evidence.missing_provenance_reasons


def test_assumed_timezone_is_mandatory_and_validated(tmp_path):
    path = _write_artifact(tmp_path, _five_minutes_unsorted_with_duplicate())

    with pytest.raises(LegacyReplayArtifactError, match="assumed_timezone is required"):
        load_legacy_replay_symbol_ohlcv(
            path,
            symbol="VEEE",
            assumed_timezone="",
        )
    with pytest.raises(LegacyReplayArtifactError, match="unknown assumed_timezone"):
        load_legacy_replay_symbol_ohlcv(
            path,
            symbol="VEEE",
            assumed_timezone="Mars/Olympus_Mons",
        )
