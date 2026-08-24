"""Ang NUMERO ang migration ID -- ang pangalan ay label lang para sa tao.

ANG NAKAWALA (2026-08-24). Ang ``_assert_migration_ids_unique`` ay
naghahambing ng BUONG string. Dalawang branch ang parehong kumuha ng **370**::

    370_depth_snapshot_provider_at                 (depth quote clock)
    370_captured_paper_shadow_latency_telemetry    (Codex, naka-apply na sa DB)

Magkaiba ang buong string, kaya **walang exception at walang babala** -- at
parehong tinanggap ng ``schema_version``. Dalawang migration na lang na parehong
"370" magpakailanman, sa isang file na ang mismong contract ay nagsasabing ang
mga ID ay *sequential and never reused*.

Ang tseke ng RETIRED at ang tseke ng eksaktong-duplicate ay parehong umiiral na.
Ito ang natitirang butas sa pagitan nila.

Runnable: pytest tests/test_migration_id_numeric_collision.py -v
"""
from __future__ import annotations

import pytest

from app import migrations as mg


def _run_guard(monkeypatch, ids):
    """Patakbuhin ang tunay na guard laban sa isang binuong listahan ng ID."""
    monkeypatch.setattr(mg, "MIGRATIONS", [(vid, lambda _c: None) for vid in ids])
    mg._assert_migration_ids_unique()


def test_the_real_list_passes():
    """ANG TUNAY NA BANTAY: ang naka-ship na listahan ay dapat malinis."""
    mg._assert_migration_ids_unique()


def test_two_different_names_sharing_a_number_are_rejected(monkeypatch):
    """ANG EKSAKTONG KASO NA NAKAWALA."""
    with pytest.raises(RuntimeError, match="NUMBER reuse"):
        _run_guard(monkeypatch, [
            "369_squeeze_regime_daily",
            "370_depth_snapshot_provider_at",
            "370_captured_paper_shadow_latency_telemetry",
        ])


def test_the_error_names_both_offenders(monkeypatch):
    """Ang mensahe ay dapat sabihin KUNG ALIN -- kung hindi, walang silbi ang crash."""
    with pytest.raises(RuntimeError) as e:
        _run_guard(monkeypatch, [
            "370_depth_snapshot_provider_at",
            "370_captured_paper_shadow_latency_telemetry",
        ])
    msg = str(e.value)
    assert "370" in msg
    assert "depth_snapshot_provider_at" in msg
    assert "captured_paper_shadow_latency_telemetry" in msg


def test_sequential_ids_still_pass(monkeypatch):
    """Huwag masyadong mahigpit -- ang normal na pagkakasunod ay dapat dumaan."""
    _run_guard(monkeypatch, [
        "368_sec_fails_to_deliver",
        "369_squeeze_regime_daily",
        "370_captured_paper_shadow_latency_telemetry",
        "371_depth_snapshot_provider_at",
    ])


def test_gaps_are_allowed(monkeypatch):
    """Ang 366/367 ay NAKALAAN para kay Codex at nilaktawan sa listahan.
    Ang mga puwang ay legal; ang PAGDODOBLE ang hindi."""
    _run_guard(monkeypatch, [
        "365_outcomes_breaker_index",
        "368_sec_fails_to_deliver",
        "371_depth_snapshot_provider_at",
    ])


def test_the_exact_duplicate_check_still_fires_first(monkeypatch):
    """Ang lumang tseke ay hindi dapat naapektuhan ng bago."""
    with pytest.raises(RuntimeError, match="ID reuse"):
        _run_guard(monkeypatch, [
            "370_depth_snapshot_provider_at",
            "370_depth_snapshot_provider_at",
        ])


def test_the_shipped_list_has_no_duplicate_numbers():
    """Direktang pahayag laban sa tunay na listahan -- hindi sa isang fixture."""
    nums = [vid.split("_", 1)[0] for vid, _fn in mg.MIGRATIONS]
    dupes = sorted({n for n in nums if nums.count(n) > 1})
    assert not dupes, f"dobleng numero ng migration: {dupes}"


def test_371_is_the_one_that_ships_not_370():
    """Ang 370 ay pag-aari ni Codex (naka-apply na sa buhay na DB)."""
    ids = [vid for vid, _fn in mg.MIGRATIONS]
    assert "371_depth_snapshot_provider_at" in ids
    assert "370_depth_snapshot_provider_at" not in ids
