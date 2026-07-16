from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re

import pytest


MANIFEST_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "ross_replay"
    / "small_account_challenge_manifest.json"
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_catalog_is_complete_and_matches_audited_playlist_metadata(manifest):
    expected = [
        (1, "2026-05-30", "2IMvfIR1TPA", "I Reset My Account to $2,000 (AGAIN) Charles Schwab ThinkorSwim Challenge Ep 1"),
        (2, "2026-06-08", "RMxnG64WuLc", "We Just Had A Stock Go Up 5,000% Today!"),
        (3, "2026-06-09", "znf0QIW8KRE", "This Is Crazy! We Just Had ANOTHER 2,000% Short Squeeze!"),
        (4, "2026-06-10", "ltdLO7sAzow", "We Just Had A Stock Go Up 950% in 2hrs"),
        (5, "2026-06-11", "5UBYoh6J2e0", "Trading a Chinese Short Squeeze! ThinkorSwim Small Account Challenge Day 4"),
        (6, "2026-06-16", "c814DtTOGDg", "I Grew My Account +50% in 10 Minutes"),
        (7, "2026-06-18", "Zf2p9c1qHjg", "RED DAY on Day 6 of my Small Account Challenge"),
        (8, "2026-06-23", "10ogBWqgprI", "Account Is Up +330% in 8 Days..."),
        (9, "2026-06-24", "D8Guwf84eAA", 'The "Blue Sky" Day Trading Pattern'),
        (10, "2026-06-25", "P5Sn_mWJdy0", "Day Trading the Break of VWAP Setup"),
        (11, "2026-06-26", "P5qdiBNct1c", "Day 11 of Growing a $2,000 Account with Charles Schwab"),
        (12, "2026-06-29", "tQ7cYcI2tNk", "+500% Short Squeeze on Biotech Acquisition News"),
        (13, "2026-06-30", "b1pOO_bkbdA", "Breaking News Sends A Stock Up 222% in 15min"),
        (14, "2026-07-01", "HTgOZI8GOV0", "Narrowly Avoided a Big Loss!"),
        (15, "2026-07-02", "zxzE4I3Iig8", "I ALMOST Had The Biggest Green Day So Far..."),
        (16, "2026-07-07", "550XNdh4y5k", "It's All About Your Profit/Loss Ratio!!"),
        (17, "2026-07-09", "miyJZq-5uIg", "My Account Is Up Over 1,000% in 17 Days..."),
        (18, "2026-07-13", "S2sOq-stPgA", "Big Winner THEN An Even Bigger Loser!!"),
        (19, "2026-07-14", "ChLgwLS9eJY", "A 350% Short Squeeze!"),
    ]
    actual = [
        (row["playlist_index"], row["date"], row["video"]["id"], row["video"]["title"])
        for row in manifest["entries"]
    ]

    assert manifest["schema_version"] == 1
    assert manifest["playlist"]["id"] == "PL1xI23WKVWie6vdrBAiRZhcUx2u5KhEZ4"
    assert manifest["playlist"]["entry_count"] == 19
    assert actual == expected


def test_every_entry_is_after_fact_only_and_has_valid_identity(manifest):
    entries = manifest["entries"]

    assert manifest["evidence_role"] == "after_fact_only"
    assert [row["playlist_index"] for row in entries] == list(range(1, 20))
    assert len({row["video"]["id"] for row in entries}) == 19
    assert len({row["date"] for row in entries}) == 19

    for row in entries:
        assert row["evidence_role"] == "after_fact_only"
        assert row["entry_kind"] in {"challenge_introduction", "trade_recap"}
        assert date.fromisoformat(row["date"])
        assert re.fullmatch(r"[A-Za-z0-9_-]{11}", row["video"]["id"])
        assert row["video"]["title"].strip()
        assert row["video"]["url"] == f'https://www.youtube.com/watch?v={row["video"]["id"]}'
        assert isinstance(row["phase_labels"], list)


def test_phase_labels_are_limited_to_audited_anchor_episodes(manifest):
    labels_by_date = {
        row["date"]: row["phase_labels"]
        for row in manifest["entries"]
        if row["phase_labels"]
    }
    label_ids = [label["label_id"] for labels in labels_by_date.values() for label in labels]

    assert set(labels_by_date) == {"2026-06-26", "2026-07-07", "2026-07-13", "2026-07-14"}
    assert len(label_ids) == len(set(label_ids)) == 12

    for trade_date, labels in labels_by_date.items():
        for label in labels:
            assert label["label_id"].startswith(f"{trade_date}_")
            assert re.fullmatch(r"[A-Z]{1,5}", label["symbol"])
            assert label["phase"].strip()
            assert label["benchmark_target"] in {"trade", "reject", "contain_loss"}
            assert label["timing_basis"] == "sequence_only_no_event_timestamp"
            assert label["sequence_position"].strip()
            assert label["evidence"]


def test_phase_labels_do_not_claim_unverified_market_event_timestamps(manifest):
    prohibited_keys = {
        "event_time",
        "event_time_et",
        "event_timestamp",
        "exact_event_time",
        "approx_event_time_et",
    }

    for row in manifest["entries"]:
        for label in row["phase_labels"]:
            assert prohibited_keys.isdisjoint(label)
            assert "timestamp" not in label["sequence_position"].lower()


def test_verified_anchor_rollups_match_phase_outcomes(manifest):
    by_date = {row["date"]: row for row in manifest["entries"]}

    assert by_date["2026-06-26"]["verified_small_account_day_pnl_usd"] == pytest.approx(
        1095.08 + 254.02
    )
    assert by_date["2026-07-07"]["verified_small_account_day_pnl_usd"] == pytest.approx(
        476.77 - 259.97
    )
    july_13 = by_date["2026-07-13"]
    assert july_13["verified_small_account_day_pnl_usd"] == pytest.approx(205.18)
    backside = next(
        label
        for label in july_13["phase_labels"]
        if label["label_id"] == "2026-07-13_PLSM_backside_fomo_curl"
    )
    assert backside["ross_outcome"] == {
        "account": "small",
        "pnl_usd": None,
        "scope": "trade_specific_loss_unavailable_not_derived_from_day_swing",
        "cumulative_day_pnl_after_exit_usd": -560.56,
    }
    assert "derivation" not in backside["ross_outcome"]
    assert "-2924" not in json.dumps(july_13, sort_keys=True)
    assert by_date["2026-07-14"]["verified_small_account_day_pnl_usd"] == pytest.approx(
        1423.34 + 740.99
    )


def test_anchor_labels_preserve_phase_specific_controls(manifest):
    labels = {
        label["label_id"]: label
        for row in manifest["entries"]
        for label in row["phase_labels"]
    }

    assert labels["2026-06-26_ZDAI_front_side_second_pullback"]["benchmark_target"] == "trade"
    assert labels["2026-06-26_SDOT_opening_bell_rejection"]["benchmark_target"] == "contain_loss"
    assert labels["2026-07-07_SILO_front_side_dip_break_nine"]["benchmark_target"] == "trade"
    assert labels["2026-07-07_SILO_vwap_rejection_end_phase"]["benchmark_target"] == "reject"
    assert labels["2026-07-07_CLRO_double_top_flush"]["benchmark_target"] == "contain_loss"
    assert labels["2026-07-13_QTTB_explicit_no_trade"]["benchmark_target"] == "reject"
    assert labels["2026-07-13_PLSM_front_side_first_dip"]["benchmark_target"] == "trade"
    assert labels["2026-07-13_PLSM_backside_fomo_curl"]["benchmark_target"] == "reject"
    assert labels["2026-07-13_VEEE_fresh_front_side_pullback"]["benchmark_target"] == "trade"
    assert labels["2026-07-14_NXTC_rejection_reclaim_breakout"]["benchmark_target"] == "trade"
    assert labels["2026-07-14_UBXG_micro_pullback_vwap_bounce"]["benchmark_target"] == "trade"
    assert "did not see a headline" in " ".join(
        labels["2026-07-14_UBXG_micro_pullback_vwap_bounce"]["evidence"]
    )


def test_july_13_context_times_are_role_qualified_and_non_executable(manifest):
    labels = {
        label["label_id"]: label
        for row in manifest["entries"]
        for label in row["phase_labels"]
    }
    qttb = labels["2026-07-13_QTTB_explicit_no_trade"]
    backside = labels["2026-07-13_PLSM_backside_fomo_curl"]
    veee = labels["2026-07-13_VEEE_fresh_front_side_pullback"]

    assert qttb["after_fact_headline_context"] == {
        "role": "enrichment_headline_context_never_phase_boundary",
        "enrichment_video_id": "RZbM0qXOFbc",
        "headline_context_time_et": "06:59:00",
        "creates_canonical_label": False,
    }
    assert backside["after_fact_timing_context"] == {
        "role": "approximate_recap_context_never_phase_boundary_or_fill",
        "approx_window_et": {"start": "08:22:30", "end": "08:30:40"},
        "exact_fill_time": None,
    }
    assert veee["after_fact_timing_context"]["scanner_observations_et"] == [
        {"time": "08:58:59", "price": 8.89, "volume": 2750000},
        {"time": "08:59:08", "price": 9.72, "volume": 3010000},
    ]
    assert veee["after_fact_timing_context"]["approx_execution_window_et"] == {
        "start": "09:17:30",
        "end": "09:21:10",
        "certainty": "approximate",
    }
    assert veee["after_fact_timing_context"]["exact_order_time"] is None
    assert veee["cross_account_enrichment"]["symbol"] == "VE"
    assert veee["cross_account_enrichment"]["campaign_start_approx_et"] == "08:50:00"
    assert veee["cross_account_enrichment"]["campaign_pnl_usd_approx"] == 34000
    assert veee["cross_account_enrichment"]["creates_canonical_label"] is False

    for label in (qttb, backside, veee):
        assert "coverage_audit_phase_time_et" not in label
        assert "coverage_audit_time_role" not in label
