from __future__ import annotations

from pathlib import Path

import pytest


REPORT = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "STRATEGY"
    / "CC_REPORTS"
    / "2026-07-01_ross-warrior-playbook-certification.md"
)
SETUP_REPORT = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "STRATEGY"
    / "CC_REPORTS"
    / "2026-07-01_ross-setup-end-to-end-audit.md"
)

CURRENT_IMAGE = "chili-app:codex-ross-hygiene-catalyst120-20260703-1035"
CURRENT_DIGEST = "sha256:be3832a22b205ee43510a895a272eb8f4ba07742524b26398b740bf463fdd66f"
ROSS_COURSES = Path(r"D:\CHILI-Docker\chili-data\ross_courses")
ROSS_BASE_COURSE = Path(r"D:\CHILI-Docker\chili-data\ross_course")
ROSS_PLAYLIST = Path(r"D:\dev\chili-home-copilot\project_ws\_ross_playlist")


def _text() -> str:
    return REPORT.read_text(encoding="utf-8")


def _setup_text() -> str:
    return SETUP_REPORT.read_text(encoding="utf-8")


def test_ross_warrior_certification_report_has_required_matrix_sections() -> None:
    text = _text()
    required_headers = [
        "## Current Runtime Boundary",
        "## Source Inventory",
        "## Course Anchor Evidence Map",
        "## Applied + Working + Deployed",
        "## Applied + Deployed But Bugged / Not Actually Effective In Live",
        "## Built Locally And Deployed To Canonical Worker",
        "## Implemented But Disabled / Dark-Flagged / Config Trap",
        "## Partially Implemented / Wrong Math / Wrong Data Source / Stale Provider Assumptions",
        "## Missing But Mechanizable And Likely Alpha-Positive",
        "## Missing But Not Mechanizable / Out Of Scope With Current Brokers/Providers",
        "## Duplicate / Obsolete Prior Claims Now False",
        "## Quant / Math Notes",
        "## Shortest Safe Path To Certification",
        "## Open Proof Gates From 2026-07-01 Coordination",
        "## Named Incident Coverage Matrix",
        "## Final Certification Verdict",
    ]

    missing = [header for header in required_headers if header not in text]
    assert missing == []


def test_ross_warrior_certification_report_current_runtime_boundary_is_not_stale() -> None:
    text = _text()
    setup_text = _setup_text()
    top = "\n".join(text.splitlines()[:24])
    setup_top = "\n".join(setup_text.splitlines()[:24])
    boundary = text.split("## Current Runtime Boundary", 1)[1].split("## Source Inventory", 1)[0]

    assert CURRENT_IMAGE in top
    assert CURRENT_DIGEST in top
    assert CURRENT_IMAGE in setup_top
    assert CURRENT_DIGEST in setup_top
    assert CURRENT_IMAGE in boundary
    assert CURRENT_DIGEST in boundary
    assert "Current in-container feature readiness snapshot" in boundary
    assert "feature_rows=184" in boundary
    for label in (
        "wedge_break_entry",
        "absorption_snap_entry",
        "blue_sky_entry",
        "round_number_entry_timing",
        "bull_flag_entry",
        "cup_and_handle_entry",
        "ma_vwap_pullback",
        "tape_hold_entry",
        "premarket_pivot_macd_entry",
        "big_buyer_bid_starter",
    ):
        assert label in boundary
    assert "Historical rows below mention older `codex-replay-label-*`, `codex-envguard6-*`" in top
    assert "codex-envguard6-triggertrace-20260702-1120` with event-driven loop enabled" not in top
    assert "Running canonical container image label: `chili-app:codex-envguard6-triggertrace" not in boundary


def test_ross_warrior_certification_report_has_single_authoritative_current_image() -> None:
    text = _text()
    setup_text = _setup_text()
    stale_current_claims = [
        "Current runtime check shows exactly one canonical `chili-clean-recovery-momentum-exec` on image `chili-app:codex-envguard6",
        "current canonical worker image `chili-app:codex-readiness-regression-restore",
        "Deployed in the current canonical image `chili-app:codex-readiness-regression-restore",
        "Deployed in canonical image `chili-app:codex-envguard6",
        "Final packaged/deployed image on 2026-07-02",
        ".env` now points `CHILI_MOMENTUM_EXEC_IMAGE=chili-app:codex-readiness-regression-restore",
        "Deployed in older worker lineage",
        "Deployed lineage includes",
        "Present in deployed lineage",
        "WORKING, telemetry locally improved",
        "Current local math",
    ]
    forbidden_soak_blocker_phrases = [
        "market-soak blocker",
        "live-soak blocker",
        "soak blocker",
        "hidden market-soak",
        "hidden live-soak",
    ]

    assert CURRENT_IMAGE in text
    assert "Historical packaged/deployed image on 2026-07-02" in text
    assert all(claim not in text for claim in stale_current_claims)
    assert all(claim not in setup_text for claim in stale_current_claims)
    assert all(phrase not in text for phrase in forbidden_soak_blocker_phrases)
    assert all(phrase not in setup_text for phrase in forbidden_soak_blocker_phrases)


def test_ross_warrior_certification_report_visual_boundary_prevents_pnl_overclaim() -> None:
    text = _text()
    top = "\n".join(text.splitlines()[:40])

    assert "0 source-before-opportunity-certifying rows" in top
    assert "Replay v3 source/PnL labels must remain fail-closed" in top
    assert "visual_review_only_no_pnl_claim" in top
    assert "full Replay v3 must still run before any PnL/min-max claim" in top
    assert "transcript text is discovery/index only" in _text()
    assert "TRADE_NO_TRADE" in text
    assert "SOURCE_BEFORE" in text
    assert "OUTCOME" in text
    assert "CANF alone has `OUTCOME=true`" in text
    assert "trade_no_trade_certifying_symbol_count" in text
    assert "pnl_source_certifying_symbol_count" in text
    assert "ross_outcome_certifying_symbol_count" in text
    assert (
        "certifying_symbol_count_semantics=legacy_trade_no_trade_only_not_source_before_or_pnl_certification"
        in text
    )
    assert "--strict-all" in text
    assert "not_ready_evidence_count=0" in text
    assert "invalid_review_count=0" in text
    assert "source_before_opportunity_certifying_count=0" in text
    assert "Source-before timestamp guard on 2026-07-03" in text
    assert "source_observed_at" in text
    assert "opportunity_ts" in text
    assert "visual source timestamp is after the replay opportunity" in text


def test_daily_room_overhead_is_classified_as_deployed_not_missing() -> None:
    text = _text()
    partial = text.split("## Partially Implemented / Wrong Math / Wrong Data Source / Stale Provider Assumptions", 1)[1].split(
        "## Missing But Mechanizable And Likely Alpha-Positive", 1
    )[0]
    missing = text.split("## Missing But Mechanizable And Likely Alpha-Positive", 1)[1].split(
        "## Missing But Not Mechanizable", 1
    )[0]

    assert "Daily 200-EMA room / overhead selection" in partial
    assert "risk_policy.daily_room_size_down_multiplier" in partial
    assert "WORKING / DEPLOYED" in partial
    assert "Daily 200-EMA room / overhead selection" not in missing


def test_process_adherence_is_diagnostic_not_hidden_live_gate() -> None:
    text = _text()
    missing = text.split("## Missing But Mechanizable And Likely Alpha-Positive", 1)[1].split(
        "## Missing But Not Mechanizable", 1
    )[0]

    assert "feedback_query.process_adherence_diagnostic" in missing
    assert "enablement_gate=false" in missing
    assert "LABEL + DIAGNOSTIC IMPLEMENTED" in missing
    assert "SIZING USE MISSING" in missing


def test_ross_course_inventory_paths_support_report_anchors() -> None:
    if not ROSS_COURSES.exists():
        pytest.skip("local Ross course extraction is not mounted")

    expected_courses = {"AS101", "HVM101", "PSY101", "RH101", "SCAL101", "SS101", "TOS101"}
    present_courses = {path.name for path in ROSS_COURSES.iterdir() if path.is_dir()}
    assert expected_courses <= present_courses

    assert sum(1 for _path in ROSS_COURSES.rglob("*.txt")) >= 350
    assert sum(1 for _path in (ROSS_COURSES / "_frames").rglob("*.jpg")) >= 3000
    assert ROSS_BASE_COURSE.exists()
    assert sum(1 for _path in ROSS_BASE_COURSE.glob("*.txt")) >= 40
    assert ROSS_PLAYLIST.exists()
    assert sum(1 for _path in ROSS_PLAYLIST.glob("*.txt")) >= 70

    anchors = [
        ROSS_COURSES / "SS101" / "003_Part_1_Stock_Selection.txt",
        ROSS_COURSES / "SS101" / "008_Part_1_Daily_Chart_Patterns.txt",
        ROSS_COURSES / "HVM101" / "006_My_Strategy_The_Foundation.txt",
        ROSS_COURSES / "SS101" / "029_Part_1_Level_2_and_Time_and_Sales.txt",
        ROSS_COURSES / "SS101" / "083_Part_2_The_Strategy_I_Would_Use_as_a_Beginner.txt",
        ROSS_COURSES / "SS101" / "018_Break_of_VWAP_Sub_VWAP_Trap_Fade_Off_VWAP_Patt.txt",
        ROSS_COURSES / "TOS101" / "009_Strategy_3_Halts.txt",
        ROSS_COURSES / "TOS101" / "002_Risk_Management.txt",
        ROSS_COURSES / "_frames" / "SS101" / "008_Part_1_Daily_Chart_Patterns",
        ROSS_COURSES / "_frames" / "HVM101" / "006_My_Strategy_The_Foundation",
        ROSS_COURSES / "_frames" / "SS101" / "029_Part_1_Level_2_and_Time_and_Sales",
        ROSS_COURSES / "_frames" / "SS101" / "083_Part_2_The_Strategy_I_Would_Use_as_a_Beginner",
        ROSS_COURSES / "_frames" / "SS101" / "018_Break_of_VWAP_Sub_VWAP_Trap_Fade_Off_VWAP_Patt",
    ]
    missing = [str(path) for path in anchors if not path.exists()]
    assert missing == []


def test_ross_warrior_certification_report_final_verdict_stays_partial() -> None:
    text = _text()
    final = text.split("## Final Certification Verdict", 1)[1]

    assert "not Ross-playbook operationally complete" in final
    assert "NOT READY" in final
    assert "READY / MECHANICALLY DEPLOYED" in final


def test_setup_end_to_end_audit_current_runtime_boundary_is_not_stale() -> None:
    text = _setup_text()
    top = "\n".join(text.splitlines()[:20])

    assert "current-runtime correction" in top
    assert CURRENT_IMAGE in top
    assert CURRENT_DIGEST in top
    assert (
        "The latest controlled reload moved the canonical worker to "
        "`chili-app:main-clean-codex-regression-restore"
    ) not in top
    assert "Current running image `chili-app:main-clean-codex-regression-restore" not in text
    assert "scheduler_priority_claim_ready=true" not in text
    assert "Earlier `codex-replay-label-*`, `main-clean-codex-regression-restore-*`, `rosslane-*`, `envguard*`" in top
    assert (
        "boundary-telemetry, budget-telemetry, Ross-pillar-telemetry, replay-cert, event-replay-snap, "
        "expirybridge, regression-restore, and `codex-ross-hygiene-contracts-redintraday-*` images"
    ) in top
    assert "live PnL and Ross-complete certification are **NOT READY**" in top
    assert "Current-state supersession note" in text
    assert "historical findings, not current truth" in text
