"""Fail-closed manifest validation for governed trading evidence consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "CURRENT_EVIDENCE_TARGET_BRANCH",
    "CURRENT_V16_VALIDATOR_ARTIFACT",
    "CURRENT_V16_VALIDATOR_SHA256",
    "CURRENT_V36_INDEX_ARTIFACT",
    "CURRENT_V36_INDEX_SHA256",
    "CURRENT_V15_VALIDATOR_ARTIFACT",
    "CURRENT_V15_VALIDATOR_SHA256",
    "CURRENT_V35_INDEX_ARTIFACT",
    "CURRENT_V35_INDEX_SHA256",
    "LEGACY_V13_VALIDATOR_ARTIFACT",
    "LEGACY_V13_VALIDATOR_SHA256",
    "LEGACY_V33_INDEX_ARTIFACT",
    "LEGACY_V33_INDEX_SHA256",
    "LEGACY_V14_VALIDATOR_ARTIFACT",
    "LEGACY_V14_VALIDATOR_SHA256",
    "LEGACY_V34_INDEX_ARTIFACT",
    "LEGACY_V34_INDEX_SHA256",
    "ManifestBindingPolicy",
    "ManifestValidationResult",
    "V33_POLICY",
    "V34_POLICY",
    "V35_POLICY",
    "V36_POLICY",
    "validate_consumer_manifest",
    "validate_manifest",
]


CURRENT_EVIDENCE_TARGET_BRANCH = "codex/brain-work-done-marker-recovery"
CURRENT_EVIDENCE_TARGET_REMOTE_REF = "origin/codex/brain-work-done-marker-recovery"

V36_CURRENT_BRANCH_HEAD = "85e777c5e6e679fc55856e6d9398bfef556685d0"
V36_PREDECESSOR_EFE9_HEAD = "efe9c14e7850265caa2137db8ddbc52d9fccdeaf"
V36_PREDECESSOR_571_HEAD = "571445d1d6f6fdfe786d537962f4388b15add7df"
V35_CURRENT_BRANCH_HEAD = "7e394580016236ef484f937b9a5f8a3847cdc1b5"
V35_INTERMEDIATE_HEAD_AFTER_B8BEE = "78f10e8d5feb5b1d944c3b5a18eec9f317dc92fd"
V34_PREVIOUS_GOVERNED_HEAD = "b8bee4bd26cb17cd679b74c31de98bcf4a50218f"
V33_PRIOR_INCIDENT_HEAD = "8426169fbef6da11331a0bcd95ca77ca9f08da07"
MERGED_PR_122_HEAD = "3ba5aacaeca3c2433e3bf5ec09d6c483e6bc2984"
PR_123_MAIN_HEAD = "dd468151409ce2e9d467477201827d7f773db182"

CURRENT_V36_INDEX_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-070600Z-"
    "mlops-governed-evidence-blocker-index-v36.json"
)
CURRENT_V36_INDEX_SHA256 = (
    "7FAC696C0B09A0F5AA7BB1908A562206F9E590E97EF1BCC18352CF4175A676B2"
)
CURRENT_V16_VALIDATOR_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-070600Z-"
    "pm065-v36-consumer-manifest-validator-spec.json"
)
CURRENT_V16_VALIDATOR_SHA256 = (
    "4E49874F81116AADC5250DC4A0E69125CF866A26531A825D56D4F56C8B8BC1AC"
)

CURRENT_V35_INDEX_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-064200Z-"
    "mlops-governed-evidence-blocker-index-v35.json"
)
CURRENT_V35_INDEX_SHA256 = (
    "B79F58566FF010C488871B8D219A19D399B8606D75D6D1AA3A9916AC61974C41"
)
CURRENT_V15_VALIDATOR_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-064200Z-"
    "pm065-v35-consumer-manifest-validator-spec.json"
)
CURRENT_V15_VALIDATOR_SHA256 = (
    "D63D1FFEFAD465266C028145490892358BE2E7A685FDE1F30080A1BAFE81756E"
)

LEGACY_V34_INDEX_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-062300Z-"
    "mlops-governed-evidence-blocker-index-v34.json"
)
LEGACY_V34_INDEX_SHA256 = (
    "38F0054EC81B15E2D68C04DE5DC669BA9908F23CEF9555A5A29EE0E4E28A8591"
)
LEGACY_V14_VALIDATOR_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-062300Z-"
    "pm065-v34-consumer-manifest-validator-spec.json"
)
LEGACY_V14_VALIDATOR_SHA256 = (
    "0731E02A26B4464AEDDC928F1E05C111F45E59A810DE81CFA22B914077D96CF4"
)

LEGACY_V33_INDEX_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-060400Z-"
    "mlops-governed-evidence-blocker-index-v33.json"
)
LEGACY_V33_INDEX_SHA256 = (
    "B6D7634D1FF83959E919CE66AB287B31F2F619906E79432E6E032F844F700E61"
)
LEGACY_V13_VALIDATOR_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-060400Z-"
    "pm065-v33-consumer-manifest-validator-spec.json"
)
LEGACY_V13_VALIDATOR_SHA256 = (
    "731C5F0B8526685F2BC567E5CDA4BBD99AA89815D6A26644857921BBDF4467AA"
)

PRELIMINARY_0629_V34_INDEX_SHA256 = (
    "34BA67001A45DAD356A643B12DCEDB18B3A21E192898E381E70D2A5CA44CF390"
)
PRELIMINARY_0629_V14_VALIDATOR_SHA256 = (
    "C612691ACDE01910214260FD16D72974FFBE10FB27FD4277E9CA30AE491C1D21"
)

REQUIRED_BASE_MANIFEST_FIELDS = frozenset(
    {
        "manifest_schema",
        "manifest_generated_utc",
        "consumer_name",
        "evidence_target_branch",
        "evidence_target_head",
        "mlops_blocker_index_sha256",
        "mlops_validator_spec_sha256",
        "source_artifacts",
        "excluded_commit_ids",
        "excluded_remote_refs",
        "excluded_branch_refs",
        "excluded_pr_refs",
        "excluded_check_run_refs",
        "excluded_runtime_windows",
        "required_fail_closed_exclusions",
        "non_clearance_assertions",
        "latest_required_containment_floor_utc",
        "checks_run_or_skipped",
        "remaining_risks",
        "safety_constraints",
    }
)

MANIFEST_ALIAS_FIELDS = (
    ("mlops_blocker_index_artifact", "mlops_blocker_index_ref"),
    ("mlops_validator_spec_artifact", "mlops_validator_spec_ref"),
    ("consumer_surfaces_fail_closed", "required_consumer_surfaces_fail_closed"),
)

REQUIRED_SAFETY_CONSTRAINTS = frozenset(
    {
        "broker_apis_authoritative",
        "deterministic_safety_gates_preserved",
        "db_tests_must_use_test_suffix",
        "no_magic_number_thresholds",
        "kill_switches_preserved",
        "drawdown_breakers_preserved",
        "trading_trades_compatibility_view_preserved",
        "position_identity_constraints_preserved",
    }
)

REQUIRED_V36_FAIL_CLOSED_EXCLUSIONS = frozenset(
    {
        "post0636_7e394_v35_stream_stale_for_exact_85e777_head",
        "post0652_571445_coinbase_product_cache_not_clean_broker_truth_or_valuation_evidence",
        "post0656_efe9_stop_position_display_not_clean_runtime_or_protective_stop_evidence",
        "post0700_current_branch_head_85e777_not_clean_source_lineage",
        "post0700_85e777_no_github_checks_not_ci_evidence",
        "post0700_85e777_no_branch_workflow_not_release_readiness",
        "post0700_85e777_no_open_pr_not_pr_readiness",
        "post0700_85e777_autotrader_position_sizer_skip_requires_owner_review",
        "post0700_85e777_dirty_bind_mount_not_runtime_trust",
        "post0702_quarantined_target_active_after_85e777_branch_movement",
        "post0632_pr123_main_ci_green_not_nonmain_runtime_or_evidence_clearance",
        "pm065_source_lineage_not_satisfied_without_manifest_exact_hash_binding",
    }
)

REQUIRED_V35_FAIL_CLOSED_EXCLUSIONS = frozenset(
    {
        "post0614_b8bee_v34_stream_stale_for_exact_7e394_head",
        "post0636_current_branch_head_7e394_not_clean_source_lineage",
        "post0636_7e394_no_github_checks_not_ci_evidence",
        "post0636_7e394_no_branch_workflow_not_release_readiness",
        "post0636_7e394_nonmain_ahead9_behind21_not_pr_readiness",
        "post0636_7e394_broker_truth_live_display_and_recert_rescue_scope_requires_owner_review",
        "post0636_7e394_dirty_bind_mount_not_runtime_trust",
        "post0632_pr123_main_ci_green_not_nonmain_runtime_or_evidence_clearance",
        "post0640_quarantined_target_active_after_7e394_branch_movement",
        "pm065_source_lineage_not_satisfied_without_manifest_exact_hash_binding",
    }
)

REQUIRED_V34_FAIL_CLOSED_EXCLUSIONS = frozenset(
    {
        "post0546_branch_push_stream_still_evidence_only",
        "post0614_current_branch_head_b8bee4b_not_clean_source_lineage",
        "post0614_b8bee4b_no_github_checks_not_ci_evidence",
        "post0614_b8bee4b_no_branch_workflow_not_release_readiness",
        "post0614_b8bee4b_nonmain_ahead7_behind19_not_pr_readiness",
        "post0614_b8bee4b_autotrader_backtest_queue_brain_work_scope_requires_clean_review",
        "post0614_b8bee4b_target_local_tests_not_clean_verification",
        "post0614_b8bee4b_branch_head_stream_requires_v34_manifest_binding",
        "post0616_active_target_after_b8bee4b_control_plane_failure",
        "dirty_shared_bind_mount_not_reproducible_source_state",
        "pm065_source_lineage_not_satisfied_without_manifest_exact_hash_binding",
    }
)

REQUIRED_V33_FAIL_CLOSED_EXCLUSIONS = frozenset(
    {
        "post0546_target_authored_nonmain_commit_8426169_not_clean_source_lineage",
        "post0546_remote_branch_move_after_merged_pr_not_pr_review_coverage",
        "post0546_pushed_head_8426169_no_open_pr_not_pr_readiness",
        "post0546_pushed_head_8426169_no_github_checks_not_ci_evidence",
        "post0546_broker_live_control_credential_adjacent_commit_scope_requires_owner_reviews",
        "post0546_target_local_tests_on_pushed_head_not_clean_verification",
        "post0546_branch_head_without_pr_or_checks_not_release_readiness",
        "post0546_remote_ref_movement_not_runtime_provenance",
        "post0546_pushed_branch_source_lineage_not_evidence_or_model_promotion_input",
        "post0554_shared_worktree_writes_after_branch_push_not_containment",
        "post0556_active_target_after_branch_push_control_plane_failure",
        "post0608_active_target_after_branch_push_control_plane_failure",
        "post0546_branch_push_stream_requires_v33_manifest_binding",
    }
)

REQUIRED_V36_CONSUMER_SURFACES = frozenset(
    {
        "labels",
        "training",
        "evaluation",
        "backtests",
        "scanner_metrics",
        "transaction_cost_analysis",
        "pnl_statistics",
        "calibration",
        "threshold_tuning",
        "model_health",
        "release_readiness",
        "pr_readiness",
        "source_lineage",
        "runtime_provenance",
        "broker_truth_claims",
        "autotrader_execution_evidence",
        "position_sizing_evidence",
        "evidence_promotion",
        "model_promotion",
    }
)

REQUIRED_V35_CONSUMER_SURFACES = frozenset(
    {
        "labels",
        "training",
        "evaluation",
        "backtests",
        "scanner_metrics",
        "transaction_cost_analysis",
        "pnl_statistics",
        "calibration",
        "threshold_tuning",
        "model_health",
        "release_readiness",
        "pr_readiness",
        "source_lineage",
        "runtime_provenance",
        "broker_truth_claims",
        "evidence_promotion",
        "model_promotion",
    }
)

REQUIRED_V34_CONSUMER_SURFACES = frozenset(
    {
        "labels",
        "training",
        "evaluation",
        "backtests",
        "transaction_cost_analysis",
        "pnl_statistics",
        "calibration",
        "threshold_tuning",
        "model_health",
        "release_readiness",
        "pr_readiness",
        "source_lineage",
        "runtime_provenance",
        "broker_truth_claims",
        "evidence_promotion",
        "model_promotion",
    }
)

REQUIRED_V33_CONSUMER_SURFACES = frozenset(
    {
        "labels",
        "training",
        "evaluation",
        "promotion",
        "tca",
        "backtest",
        "pnl",
        "calibration",
        "threshold_tuning",
        "model_health",
        "sizing",
        "capital_allocation",
        "release_readiness",
        "pr_readiness",
        "ci_or_check_evidence",
        "broker_sync_freshness",
        "phase5i_phase5j",
        "runtime_provenance",
        "model_lineage",
        "source_lineage",
        "remote_branch_lineage",
        "evidence_promotion",
        "broker_authority",
        "db_effects_clearance",
        "credential_routing_clearance",
    }
)

REQUIRED_V35_NON_CLEARANCE_FALSE = frozenset(
    {
        "approved_to_push",
        "approved_to_merge",
        "approved_to_deploy",
        "approved_for_release",
        "approved_for_runtime_refresh",
        "approved_for_model_promotion",
        "approved_for_evidence_promotion",
        "approved_for_live_trading_behavior_change",
        "approved_to_reset_breakers",
        "approved_to_mutate_broker_or_database_state",
    }
)

REQUIRED_V36_NON_CLEARANCE_FALSE = REQUIRED_V35_NON_CLEARANCE_FALSE | frozenset(
    {
        "approved_for_autotrader_execution_trust",
        "approved_for_position_sizing_evidence",
    }
)
REQUIRED_V34_NON_CLEARANCE_FALSE = REQUIRED_V35_NON_CLEARANCE_FALSE - frozenset(
    {"approved_for_runtime_refresh"}
)
REQUIRED_V33_NON_CLEARANCE_FALSE = REQUIRED_V34_NON_CLEARANCE_FALSE

REQUIRED_V36_SOURCE_ARTIFACTS = (
    (
        "project_ws/MLOps/OUT/20260530-070500Z-current-branch-85e777-mlops-evidence.json",
        "295262A6520B65C8B2D24A208C57B3F580864B46E5DC284CA905B2B203B10ADF",
    ),
    (
        "project_ws/MLOps/IN/20260530-070000Z-from-AlgoTraderArchitect-to-MLOps-pm070-85e777-successor-binding.md",
        "C7370AD6469446922FFCDF4F37F6E59340998E9E86DED3F962B665459636BD03",
    ),
    (
        "project_ws/AlgoTraderArchitect/OUT/20260530-070000Z-85e777-current-head-governance-note.md",
        "228CEE30FD55E046D093ED2525B13E552E4DBE177115A7B4A5CDE1E4B3E417F2",
    ),
    (
        "project_ws/PM/IN/20260530-070000Z-from-AlgoTraderArchitect-to-PM-pm070-85e777-current-head.md",
        "279756D5BA62463B6A895E208C2AF6266384D143262A2171A4F2DB4494D18F90",
    ),
    (
        "project_ws/Risk/IN/20260530-070000Z-from-AlgoTraderArchitect-to-Risk-pm070-85e777-live-control-review.md",
        "D220195232E3236CCD2E52A3E8B13DF4DD7465D94224763B6190BDBBD26E1A23",
    ),
    (
        "project_ws/AgentOps/OUT/20260530-070234Z-agentops-85e777-containment-refresh.md",
        "A85A0F4529B82BEA3A5E1929AE140B1BAE647F53D050711172EA795BC4123558",
    ),
    (
        "project_ws/PM/OUT/20260530-070100Z-pm-efe9c14-successor-head-disposition.md",
        "DD253D0AE0BAC2900E7DD48221DB7E136F68128553CE5D36C5820607119A167E",
    ),
    (
        "project_ws/Risk/OUT/20260530-065600Z-devops-571445-branch-head-risk-classification.md",
        "F0CD8C540A087C664C39E99BC6770B85194487ACA450746D0B549C141DCAA72E",
    ),
    (
        "project_ws/DevOps/OUT/20260530-065200Z-devops-571445-current-head-release-block-refresh.md",
        "2E3C7155ACE00DE231D017E67918FD3C22D291DF1127016FD80B79318F6272A5",
    ),
    (
        "project_ws/PM/OUT/20260530-064900Z-pm-7e394-v35-v15-binding.md",
        "17AF884068FCFFB143845C42E4F3B7F18EEF9FF91625FCCBB3B488E1FDF2225C",
    ),
    (
        CURRENT_V35_INDEX_ARTIFACT,
        CURRENT_V35_INDEX_SHA256,
    ),
)

REQUIRED_V35_SOURCE_ARTIFACTS = (
    (
        "project_ws/MLOps/OUT/20260530-064100Z-current-branch-pr123-mlops-evidence.json",
        "164EBB4C0CA99DF0A4B4645CE0F55CFEBD0C80CF7C5B0214842859FA98777A2D",
    ),
    (
        "project_ws/PM/OUT/20260530-063400Z-pm-v34-v14-authoritative-binding.md",
        "07CD4E08CB05732E0772327A8624F2E6AF2E7C05753489C64CBAD781B668D287",
    ),
    (
        "project_ws/PM/OUT/20260530-063700Z-pm-pr123-ci-green-release-block-refresh.md",
        "5A71ED97A063DBAFFFF1A9F6DE0320E928456BAC7015BDFD55998FC7C7912539",
    ),
    (
        "project_ws/PM/IN/20260530-063600Z-from-DevOps-to-PM-7e394-current-branch-head-release-block.md",
        "9E5A367EB9C8A49355952D241B9B6AE055934EE16B2075A45D62D41671F5D6B0",
    ),
    (
        "project_ws/Risk/IN/20260530-063600Z-from-DevOps-to-Risk-7e394-branch-head-live-control-review.md",
        "9120D81E94744C77DAA8CC8E5F60EA45397EB4AEF067F9D97AD95FD9D8F2C7C5",
    ),
    (
        "project_ws/SRE/IN/20260530-063600Z-from-DevOps-to-SRE-7e394-runtime-provenance-watch.md",
        "275FAE4DEFD45B85A2801CAFBD661A96C4E28EE8FEDCEF7BFE4706256A8F4CD4",
    ),
    (
        "project_ws/AgentOps/IN/20260530-063600Z-from-DevOps-to-AgentOps-7e394-target-active-branch-head.md",
        "9C9088A58C29E716A367833367DBB36E8B129B487079BDBBDCBE69EFA18745C1",
    ),
    (
        LEGACY_V34_INDEX_ARTIFACT,
        LEGACY_V34_INDEX_SHA256,
    ),
)

REQUIRED_V34_SOURCE_ARTIFACTS = (
    (
        "project_ws/DevOps/OUT/20260530-061700Z-devops-release-watch-b8bee-nonmain-block.md",
        "376EBC4BD7891558F067254B62285D7781A8521B1FEDE640073E95326FDE326D",
    ),
    (
        "project_ws/Risk/OUT/20260530-062100Z-devops-post0614-b8bee-branch-head-risk-disposition.md",
        "24317D2EF95843E025D32603A9431733F6D0BF7DD2FBF79E0B9D784FB1D80532",
    ),
    (
        "project_ws/PM/IN/20260530-062101Z-from-Risk-to-PM-urgent-b8bee-branch-head-classification.md",
        "81EFBB2B994328C2620C02D4412EAEB1E36ADB190C82E276AA8847D1C4B42EE9",
    ),
    (
        "project_ws/PM/OUT/20260530-062000Z-pm-b8bee-branch-head-disposition.md",
        "34F3A334EFD95B0089A20EFF374A63904DB4444C9BA726A5D3F8722D3B719F96",
    ),
    (
        "project_ws/DS/OUT/20260530-061900Z-ds-v33-consumer-binding-implementation-brief.md",
        "518946D0F60CF0A715699027F9F54FD79298EF11C9C871DC0A3A30C969713128",
    ),
    (
        "project_ws/MLOps/IN/20260530-062400Z-from-DS-to-MLOps-urgent-post0614-b8bee-successor-evidence-governance.md",
        "335BFDDCEAE4AB916C0492F6C3D9814B3F2F9A3358D0635766EE12679B94BD7B",
    ),
    (
        "project_ws/PM/BRANCHES.md",
        "685BC8691A111EBD5EDEED30DB5178E396FF99B148F3B2A9EA4BC88F99E02D20",
    ),
    (
        "project_ws/PM/PR_BLOCKERS.md",
        "57B09DB31170BE137F77C27C70CE1E6933AF65F745EA98490DDF7FDC95AF75C2",
    ),
    (
        LEGACY_V33_INDEX_ARTIFACT,
        LEGACY_V33_INDEX_SHA256,
    ),
    (
        LEGACY_V13_VALIDATOR_ARTIFACT,
        LEGACY_V13_VALIDATOR_SHA256,
    ),
)

REQUIRED_V33_SOURCE_ARTIFACTS = (
    (
        "project_ws/DevOps/OUT/20260530-055800Z-devops-post0546-pushed-branch-release-blocked.md",
        "3EF892B6584EFF1598F6EC4FEA5F14771544889CAA84D66F28FA7B25D72AEA6F",
    ),
    (
        "project_ws/SRE/OUT/20260530-055600Z-sre-idle-patrol-post0554-target-writes.md",
        "A386D2F67F988CAE34F355268F008589D87F781D1BE40E73E5672084A4258404",
    ),
    (
        "project_ws/SDBA/OUT/20260530-055630Z-sdba-post0550-backup-and-runtime-write-watch.md",
        "AB29B4FF99E84818B560744A2E20D1D8620FBB2C14D4A699BAE4CD00EEC8B33E",
    ),
)

REQUIRED_V36_EXCLUDED_REFERENCES = {
    "excluded_commit_ids": frozenset(
        {
            MERGED_PR_122_HEAD,
            V33_PRIOR_INCIDENT_HEAD,
            V34_PREVIOUS_GOVERNED_HEAD,
            V35_INTERMEDIATE_HEAD_AFTER_B8BEE,
            V35_CURRENT_BRANCH_HEAD,
            V36_PREDECESSOR_571_HEAD,
            V36_PREDECESSOR_EFE9_HEAD,
            V36_CURRENT_BRANCH_HEAD,
            PR_123_MAIN_HEAD,
        }
    ),
    "excluded_remote_refs": frozenset({CURRENT_EVIDENCE_TARGET_REMOTE_REF, "origin/main"}),
    "excluded_branch_refs": frozenset({CURRENT_EVIDENCE_TARGET_BRANCH}),
    "excluded_pr_refs": frozenset({"PR #122", "PR #123"}),
    "excluded_check_run_refs": frozenset(
        {
            "GitHub check-runs count 0 for 85e777c5e6e679fc55856e6d9398bfef556685d0",
            "GitHub status contexts total 0 and pending for 85e777c5e6e679fc55856e6d9398bfef556685d0",
            "No branch workflow run for 85e777c5e6e679fc55856e6d9398bfef556685d0",
            "PR #123 main CI run 26676647858 success applies only to dd468151409ce2e9d467477201827d7f773db182",
        }
    ),
    "excluded_runtime_windows": frozenset(
        {
            "post-2026-05-30T07:00:00Z 85e777 current branch-head stream",
            "post-2026-05-30T06:32:55Z PR #123 main-CI-green release-block stream",
            "post-2026-05-30T07:02:32.9704738Z active-target containment floor",
        }
    ),
}

REQUIRED_V35_EXCLUDED_REFERENCES = {
    "excluded_commit_ids": frozenset(
        {
            MERGED_PR_122_HEAD,
            V33_PRIOR_INCIDENT_HEAD,
            V34_PREVIOUS_GOVERNED_HEAD,
            V35_INTERMEDIATE_HEAD_AFTER_B8BEE,
            V35_CURRENT_BRANCH_HEAD,
            PR_123_MAIN_HEAD,
        }
    ),
    "excluded_remote_refs": frozenset({CURRENT_EVIDENCE_TARGET_REMOTE_REF, "origin/main"}),
    "excluded_branch_refs": frozenset({CURRENT_EVIDENCE_TARGET_BRANCH}),
    "excluded_pr_refs": frozenset({"PR #122", "PR #123"}),
    "excluded_check_run_refs": frozenset(
        {
            "GitHub check-runs count 0 for 7e394580016236ef484f937b9a5f8a3847cdc1b5",
            "GitHub status contexts total 0 and pending for 7e394580016236ef484f937b9a5f8a3847cdc1b5",
            "PR #123 main CI run 26676647858 success applies only to dd468151409ce2e9d467477201827d7f773db182",
        }
    ),
    "excluded_runtime_windows": frozenset(
        {
            "post-2026-05-30T06:36:00Z 7e394 current branch-head stream",
            "post-2026-05-30T06:32:55Z PR #123 main-CI-green release-block stream",
            "post-2026-05-30T06:40:15Z active-target containment floor",
        }
    ),
}

REQUIRED_V34_EXCLUDED_REFERENCES = {
    "excluded_commit_ids": frozenset(
        {MERGED_PR_122_HEAD, V33_PRIOR_INCIDENT_HEAD, V34_PREVIOUS_GOVERNED_HEAD}
    ),
    "excluded_remote_refs": frozenset({CURRENT_EVIDENCE_TARGET_REMOTE_REF}),
    "excluded_branch_refs": frozenset({CURRENT_EVIDENCE_TARGET_BRANCH}),
    "excluded_pr_refs": frozenset({"PR #122"}),
    "excluded_check_run_refs": frozenset(
        {
            "GitHub check-runs count 0 for b8bee4bd26cb17cd679b74c31de98bcf4a50218f",
            "GitHub status contexts total 0 and pending for b8bee4bd26cb17cd679b74c31de98bcf4a50218f",
        }
    ),
    "excluded_runtime_windows": frozenset(
        {
            "post-2026-05-30T05:46:00Z branch-push stream",
            "post-2026-05-30T06:14:00Z b8bee4b current branch-head stream",
            "post-2026-05-30T06:27:14Z active-target containment floor",
        }
    ),
}

REQUIRED_V33_EXCLUDED_REFERENCES = {
    "excluded_commit_ids": frozenset({MERGED_PR_122_HEAD, V33_PRIOR_INCIDENT_HEAD}),
    "excluded_remote_refs": frozenset({CURRENT_EVIDENCE_TARGET_REMOTE_REF}),
    "excluded_branch_refs": frozenset({CURRENT_EVIDENCE_TARGET_BRANCH}),
    "excluded_pr_refs": frozenset({"PR #122"}),
    "excluded_check_run_refs": frozenset(
        {
            "GitHub check-runs count 0 for 8426169fbef6da11331a0bcd95ca77ca9f08da07",
            "GitHub status contexts total 0 for 8426169fbef6da11331a0bcd95ca77ca9f08da07",
        }
    ),
    "excluded_runtime_windows": frozenset(
        {
            "post-2026-05-30T05:46:00Z branch-push stream",
            "post-2026-05-30T06:08:25Z active-target containment floor",
        }
    ),
}


@dataclass(frozen=True)
class ManifestBindingPolicy:
    name: str
    accepted_target_heads: frozenset[str]
    descendant_marker_heads: frozenset[str]
    blocker_index_artifact: str
    blocker_index_sha256: str
    validator_spec_artifact: str
    validator_spec_sha256: str
    required_source_artifacts: tuple[tuple[str, str], ...]
    required_fail_closed_exclusions: frozenset[str]
    required_consumer_surfaces: frozenset[str]
    required_excluded_references: dict[str, frozenset[str]]
    required_non_clearance_false: frozenset[str]
    latest_required_containment_floor_utc: str


V36_POLICY = ManifestBindingPolicy(
    name="v36_v16_85e777_current_head",
    accepted_target_heads=frozenset(
        {
            V36_CURRENT_BRANCH_HEAD,
            V36_PREDECESSOR_EFE9_HEAD,
            V36_PREDECESSOR_571_HEAD,
        }
    ),
    descendant_marker_heads=frozenset(
        {
            V36_CURRENT_BRANCH_HEAD,
            V36_PREDECESSOR_EFE9_HEAD,
            V36_PREDECESSOR_571_HEAD,
        }
    ),
    blocker_index_artifact=CURRENT_V36_INDEX_ARTIFACT,
    blocker_index_sha256=CURRENT_V36_INDEX_SHA256,
    validator_spec_artifact=CURRENT_V16_VALIDATOR_ARTIFACT,
    validator_spec_sha256=CURRENT_V16_VALIDATOR_SHA256,
    required_source_artifacts=REQUIRED_V36_SOURCE_ARTIFACTS,
    required_fail_closed_exclusions=REQUIRED_V36_FAIL_CLOSED_EXCLUSIONS,
    required_consumer_surfaces=REQUIRED_V36_CONSUMER_SURFACES,
    required_excluded_references=REQUIRED_V36_EXCLUDED_REFERENCES,
    required_non_clearance_false=REQUIRED_V36_NON_CLEARANCE_FALSE,
    latest_required_containment_floor_utc="2026-05-30T07:02:32.9704738Z",
)

V35_POLICY = ManifestBindingPolicy(
    name="v35_v15_7e394_current_head",
    accepted_target_heads=frozenset(
        {V35_CURRENT_BRANCH_HEAD, V35_INTERMEDIATE_HEAD_AFTER_B8BEE}
    ),
    descendant_marker_heads=frozenset(
        {V35_CURRENT_BRANCH_HEAD, V35_INTERMEDIATE_HEAD_AFTER_B8BEE}
    ),
    blocker_index_artifact=CURRENT_V35_INDEX_ARTIFACT,
    blocker_index_sha256=CURRENT_V35_INDEX_SHA256,
    validator_spec_artifact=CURRENT_V15_VALIDATOR_ARTIFACT,
    validator_spec_sha256=CURRENT_V15_VALIDATOR_SHA256,
    required_source_artifacts=REQUIRED_V35_SOURCE_ARTIFACTS,
    required_fail_closed_exclusions=REQUIRED_V35_FAIL_CLOSED_EXCLUSIONS,
    required_consumer_surfaces=REQUIRED_V35_CONSUMER_SURFACES,
    required_excluded_references=REQUIRED_V35_EXCLUDED_REFERENCES,
    required_non_clearance_false=REQUIRED_V35_NON_CLEARANCE_FALSE,
    latest_required_containment_floor_utc="2026-05-30T06:40:15Z",
)

V34_POLICY = ManifestBindingPolicy(
    name="v34_v14_b8bee_inherited_head",
    accepted_target_heads=frozenset({V34_PREVIOUS_GOVERNED_HEAD}),
    descendant_marker_heads=frozenset(),
    blocker_index_artifact=LEGACY_V34_INDEX_ARTIFACT,
    blocker_index_sha256=LEGACY_V34_INDEX_SHA256,
    validator_spec_artifact=LEGACY_V14_VALIDATOR_ARTIFACT,
    validator_spec_sha256=LEGACY_V14_VALIDATOR_SHA256,
    required_source_artifacts=REQUIRED_V34_SOURCE_ARTIFACTS,
    required_fail_closed_exclusions=REQUIRED_V34_FAIL_CLOSED_EXCLUSIONS,
    required_consumer_surfaces=REQUIRED_V34_CONSUMER_SURFACES,
    required_excluded_references=REQUIRED_V34_EXCLUDED_REFERENCES,
    required_non_clearance_false=REQUIRED_V34_NON_CLEARANCE_FALSE,
    latest_required_containment_floor_utc="2026-05-30T06:27:14Z",
)

V33_POLICY = ManifestBindingPolicy(
    name="v33_v13_8426169_prior_incident_head",
    accepted_target_heads=frozenset({V33_PRIOR_INCIDENT_HEAD}),
    descendant_marker_heads=frozenset(),
    blocker_index_artifact=LEGACY_V33_INDEX_ARTIFACT,
    blocker_index_sha256=LEGACY_V33_INDEX_SHA256,
    validator_spec_artifact=LEGACY_V13_VALIDATOR_ARTIFACT,
    validator_spec_sha256=LEGACY_V13_VALIDATOR_SHA256,
    required_source_artifacts=REQUIRED_V33_SOURCE_ARTIFACTS,
    required_fail_closed_exclusions=REQUIRED_V33_FAIL_CLOSED_EXCLUSIONS,
    required_consumer_surfaces=REQUIRED_V33_CONSUMER_SURFACES,
    required_excluded_references=REQUIRED_V33_EXCLUDED_REFERENCES,
    required_non_clearance_false=REQUIRED_V33_NON_CLEARANCE_FALSE,
    latest_required_containment_floor_utc="2026-05-30T06:08:25Z",
)

POLICIES = (V36_POLICY, V35_POLICY, V34_POLICY, V33_POLICY)

# Backwards-compatible aliases for callers/tests created against the first PM-070 request.
CURRENT_V34_INDEX_ARTIFACT = LEGACY_V34_INDEX_ARTIFACT
CURRENT_V34_INDEX_SHA256 = LEGACY_V34_INDEX_SHA256
CURRENT_V14_VALIDATOR_ARTIFACT = LEGACY_V14_VALIDATOR_ARTIFACT
CURRENT_V14_VALIDATOR_SHA256 = LEGACY_V14_VALIDATOR_SHA256
CURRENT_EVIDENCE_TARGET_HEAD = V34_PREVIOUS_GOVERNED_HEAD
REQUIRED_FAIL_CLOSED_EXCLUSIONS = REQUIRED_V34_FAIL_CLOSED_EXCLUSIONS
REQUIRED_CONSUMER_SURFACES = REQUIRED_V34_CONSUMER_SURFACES
REQUIRED_NON_CLEARANCE_FALSE = REQUIRED_V34_NON_CLEARANCE_FALSE
REQUIRED_SOURCE_ARTIFACTS = REQUIRED_V34_SOURCE_ARTIFACTS
REQUIRED_EXCLUDED_REFERENCES = REQUIRED_V34_EXCLUDED_REFERENCES


@dataclass(frozen=True)
class ManifestValidationResult:
    accepted: bool
    status: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    policy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "policy": self.policy,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_consumer_manifest(manifest: Any) -> ManifestValidationResult:
    """Validate a consumer manifest against the governed fail-closed policy set."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(manifest, dict):
        return ManifestValidationResult(
            accepted=False,
            status="FAIL_CLOSED",
            errors=("manifest_not_object",),
        )

    policy = _select_policy(manifest, errors)
    _require_fields(manifest, errors)
    if policy is not None:
        _require_exact_value(
            manifest, errors, "evidence_target_branch", CURRENT_EVIDENCE_TARGET_BRANCH
        )
        _require_exact_value(
            manifest,
            errors,
            "latest_required_containment_floor_utc",
            policy.latest_required_containment_floor_utc,
        )
        _require_policy_binding(manifest, errors, policy)
        _require_source_artifacts(manifest, errors, policy.required_source_artifacts)
        _require_set_field(
            manifest,
            errors,
            ("required_fail_closed_exclusions",),
            policy.required_fail_closed_exclusions,
            "missing_fail_closed_exclusion",
        )
        _require_set_field(
            manifest,
            errors,
            ("consumer_surfaces_fail_closed", "required_consumer_surfaces_fail_closed"),
            policy.required_consumer_surfaces,
            "missing_consumer_surface",
        )
        for field_name, expected_values in policy.required_excluded_references.items():
            _require_set_field(
                manifest,
                errors,
                (field_name,),
                expected_values,
                f"missing_{field_name[:-1] if field_name.endswith('s') else field_name}",
            )
        _require_non_clearance(manifest, errors, policy.required_non_clearance_false)
        _require_set_field(
            manifest,
            errors,
            ("safety_constraints",),
            REQUIRED_SAFETY_CONSTRAINTS,
            "missing_safety_constraint",
        )

    if errors:
        return ManifestValidationResult(
            accepted=False,
            status="FAIL_CLOSED",
            errors=tuple(errors),
            warnings=tuple(warnings),
            policy=policy.name if policy is not None else None,
        )

    return ManifestValidationResult(
        accepted=True,
        status="EVIDENCE_GOVERNED_NON_CLEARANCE",
        errors=(),
        warnings=tuple(warnings),
        policy=policy.name if policy is not None else None,
    )


validate_manifest = validate_consumer_manifest


def _select_policy(
    manifest: dict[str, Any], errors: list[str]
) -> ManifestBindingPolicy | None:
    target_head = str(manifest.get("evidence_target_head") or "").strip()
    descendant_heads = _string_set(
        manifest.get("evidence_target_ancestor_heads")
        or manifest.get("evidence_target_descends_from_heads")
        or manifest.get("descendant_of_heads")
    )
    for policy in POLICIES:
        if target_head in policy.accepted_target_heads:
            return policy
        if policy.descendant_marker_heads and descendant_heads & policy.descendant_marker_heads:
            return policy
    if target_head:
        errors.append(f"unsupported_evidence_target_head:{target_head}")
    else:
        errors.append("missing_required_field:evidence_target_head")
    return None


def _normalize_sha(value: Any) -> str:
    return str(value or "").strip().upper()


def _field_value(manifest: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in manifest:
            return manifest[alias]
    return None


def _string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value}
    return set()


def _require_fields(manifest: dict[str, Any], errors: list[str]) -> None:
    for field_name in sorted(REQUIRED_BASE_MANIFEST_FIELDS):
        if field_name not in manifest:
            errors.append(f"missing_required_field:{field_name}")
    for aliases in MANIFEST_ALIAS_FIELDS:
        if _field_value(manifest, aliases) is None:
            errors.append(f"missing_required_field:{aliases[0]}")


def _require_exact_value(
    manifest: dict[str, Any], errors: list[str], field_name: str, expected: Any
) -> None:
    if manifest.get(field_name) != expected:
        errors.append(f"exact_value_mismatch:{field_name}")


def _require_policy_binding(
    manifest: dict[str, Any],
    errors: list[str],
    policy: ManifestBindingPolicy,
) -> None:
    blocker_artifact = _field_value(
        manifest, ("mlops_blocker_index_artifact", "mlops_blocker_index_ref")
    )
    validator_artifact = _field_value(
        manifest, ("mlops_validator_spec_artifact", "mlops_validator_spec_ref")
    )
    blocker_sha = _normalize_sha(manifest.get("mlops_blocker_index_sha256"))
    validator_sha = _normalize_sha(manifest.get("mlops_validator_spec_sha256"))

    if blocker_sha == PRELIMINARY_0629_V34_INDEX_SHA256:
        errors.append("preliminary_0629_v34_index_hash_not_accepted")
    if validator_sha == PRELIMINARY_0629_V14_VALIDATOR_SHA256:
        errors.append("preliminary_0629_v14_validator_hash_not_accepted")
    if policy is V35_POLICY and (
        blocker_sha == LEGACY_V34_INDEX_SHA256
        or validator_sha == LEGACY_V14_VALIDATOR_SHA256
        or blocker_artifact == LEGACY_V34_INDEX_ARTIFACT
        or validator_artifact == LEGACY_V14_VALIDATOR_ARTIFACT
    ):
        errors.append("v34_v14_stale_for_v35_target_head")
    if policy is V36_POLICY and (
        blocker_sha == CURRENT_V35_INDEX_SHA256
        or validator_sha == CURRENT_V15_VALIDATOR_SHA256
        or blocker_artifact == CURRENT_V35_INDEX_ARTIFACT
        or validator_artifact == CURRENT_V15_VALIDATOR_ARTIFACT
    ):
        errors.append("v35_v15_stale_for_v36_target_head")
    if blocker_artifact != policy.blocker_index_artifact:
        errors.append(f"{policy.name}:blocker_index_artifact_required")
    if blocker_sha != policy.blocker_index_sha256:
        errors.append(f"{policy.name}:blocker_index_sha_required")
    if validator_artifact != policy.validator_spec_artifact:
        errors.append(f"{policy.name}:validator_spec_artifact_required")
    if validator_sha != policy.validator_spec_sha256:
        errors.append(f"{policy.name}:validator_spec_sha_required")


def _require_source_artifacts(
    manifest: dict[str, Any],
    errors: list[str],
    required_source_artifacts: tuple[tuple[str, str], ...],
) -> None:
    source_artifacts = manifest.get("source_artifacts")
    if not isinstance(source_artifacts, list):
        errors.append("source_artifacts_not_list")
        return
    by_path: dict[str, str] = {}
    for item in source_artifacts:
        if isinstance(item, dict):
            artifact = item.get("artifact") or item.get("path") or item.get("ref")
            by_path[str(artifact or "")] = _normalize_sha(item.get("sha256"))
    for artifact, sha256 in required_source_artifacts:
        if by_path.get(artifact) != sha256:
            errors.append(f"source_artifact_exact_hash_required:{artifact}")


def _require_set_field(
    manifest: dict[str, Any],
    errors: list[str],
    field_aliases: tuple[str, ...],
    required: frozenset[str],
    error_prefix: str,
) -> None:
    values = _field_value(manifest, field_aliases)
    if not isinstance(values, list):
        errors.append(f"{field_aliases[0]}_not_list")
        return
    observed = {str(value) for value in values}
    for value in sorted(required - observed):
        errors.append(f"{error_prefix}:{value}")


def _require_non_clearance(
    manifest: dict[str, Any],
    errors: list[str],
    required_false: frozenset[str],
) -> None:
    assertions = manifest.get("non_clearance_assertions")
    if not isinstance(assertions, dict):
        errors.append("non_clearance_assertions_not_object")
        return
    for name in sorted(required_false):
        if name not in assertions:
            errors.append(f"missing_non_clearance_assertion:{name}")
        elif assertions[name] is not False:
            errors.append(f"non_clearance_assertion_must_be_false:{name}")
