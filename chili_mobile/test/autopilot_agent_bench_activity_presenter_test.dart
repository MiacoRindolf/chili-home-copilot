import 'package:chili_mobile/src/brain/autopilot_agent_bench_activity_presenter.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('AutopilotAgentBenchActivityPresenter', () {
    test('summarizes active, open, and question workload', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'active_run_count': 2,
        'open_run_count': 3,
        'pending_question_count': 1,
        'operating_state': {
          'state': 'needs_input',
        },
      });

      expect(activity.activeRunCount, 2);
      expect(activity.openRunCount, 3);
      expect(activity.pendingQuestionCount, 1);
      expect(activity.activeChatLabel, '2 active chats');
      expect(activity.activeChipLabel, '2 active');
      expect(activity.openChatLabel, '3 open chats');
      expect(activity.openChipLabel, '3 open');
      expect(activity.questionLabel, '1 question');
      expect(activity.questionChipLabel, '1 question');
      expect(activity.needsInputChipLabel, isEmpty);
      expect(activity.hasOperatorInput, isTrue);
      expect(activity.attentionScore, greaterThan(19000));
      expect(
        activity.searchTerms,
        containsAll(<String>[
          '2 active chats',
          '3 open chats',
          '1 question',
          'needs input',
        ]),
      );
    });

    test('summarizes latest active run progress for bench rows', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'active_run_count': 1,
        'open_run_count': 1,
        'active_run_preview': {
          'run_id': 'pa_abc123',
          'status': 'running',
          'current_stage': 'implement',
          'plan_status': 'approved',
          'title': 'Tighten the Agent Bench progress line.',
          'pursuing_goal': {
            'objective': 'Tighten the Agent Bench progress line.',
            'status': 'active',
            'progress_percent': 38,
            'current_step': 'Apply compact progress UI',
            'next_action_label': 'Run bench tests',
            'completion_gate':
                'Done when the bench keeps goal context visible across filters.',
          },
          'updated_at': '2026-06-01T02:14:00Z',
          'latest_step_title': 'Applying compact bench progress UI',
          'latest_step_status': 'in_progress',
          'latest_step_stage': 'implement',
        },
      });

      expect(
        activity.activeProgressLabel,
        'Current: Applying compact bench progress UI (in progress)',
      );
      expect(activity.activeProgressDetail,
          contains('goal 38%: Tighten the Agent Bench progress line.'));
      expect(activity.activeProgressDetail,
          contains('goal step: Apply compact progress UI'));
      expect(activity.activeProgressDetail,
          contains('next goal action: Run bench tests'));
      expect(
          activity.activeProgressDetail,
          contains(
              'completion gate: Done when the bench keeps goal context visible across filters.'));
      expect(
        activity.activeGoalContractLabel,
        contains(
            'gate Done when the bench keeps goal context visible across filters.'),
      );
      expect(activity.activeGoalChipLabel, 'goal 38%');
      expect(activity.activeGoalProgressFraction, closeTo(0.38, 0.001));
      expect(activity.hasActiveGoal, isTrue);
      expect(activity.hasPursuingGoalFocus, isTrue);
      expect(activity.searchTerms, contains(activity.activeProgressLabel));
      expect(activity.searchTerms, contains(activity.activeProgressDetail));
      expect(activity.searchTerms,
          contains('Tighten the Agent Bench progress line.'));
      expect(activity.searchTerms, contains('Apply compact progress UI'));
      expect(
        activity.searchTerms,
        contains(
            'Done when the bench keeps goal context visible across filters.'),
      );
      expect(
        activity.searchTerms,
        contains(activity.activeGoalContractLabel),
      );
      expect(activity.searchTerms, contains('pursuing goal'));
      expect(activity.hasBenchAttention, isTrue);
      expect(activity.searchTerms, contains('attention'));
    });

    test('separates queued and waiting worker state from active work', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'active_run_count': 4,
        'open_run_count': 4,
        'queued_run_count': 2,
        'worker_active_run_count': 1,
        'waiting_run_count': 1,
        'active_run_preview': {
          'run_id': 'pa_queued',
          'status': 'queued',
          'current_stage': 'plan',
          'title': 'Queue this plan until a worker is available.',
          'updated_at': '2026-06-01T12:44:00Z',
        },
      });

      expect(activity.activeRunCount, 4);
      expect(activity.visibleActiveRunCount, 1);
      expect(activity.queuedRunCount, 2);
      expect(activity.waitingRunCount, 1);
      expect(activity.activeChatLabel, '1 active chat');
      expect(activity.activeChipLabel, '1 active');
      expect(activity.queuedChatLabel, '2 queued chats');
      expect(activity.queuedChipLabel, '2 queued');
      expect(activity.waitingChipLabel, '1 waiting');
      expect(activity.openChatLabel, isEmpty);
      expect(activity.activeProgressLabel, 'Current: queued for worker');
      expect(
        activity.activeProgressDetail,
        contains('should not be double-started'),
      );
      expect(activity.hasQueuedRuns, isTrue);
      expect(activity.hasBenchAttention, isTrue);
      expect(activity.searchTerms, contains('2 queued'));
      expect(activity.searchTerms, contains('1 waiting'));
    });

    test('marks board and KPI pressure as bench attention', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'kpi_lane_pressure': {
          'blocked_pr_count': 2,
          'severity': 'high',
          'pr_numbers': ['134', '132'],
        },
        'expedite_lane_pressure': {
          'top_rank': 3,
          'open_pr_blocker_count': 1,
          'severity': 'high',
          'signal': 'PR #134 no checks',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasKpiPressure, isTrue);
      expect(activity.hasExpeditePressure, isTrue);
      expect(activity.hasBenchAttention, isTrue);
      expect(activity.hasBenchSafety, isFalse);
      expect(activity.searchTerms, contains('attention'));
    });

    test('surfaces Codex-style goal pressure in bench rows', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'goal_health_pressure': {
          'automation': 'chili-llm-cost-reduction-loop',
          'status': 'PAUSED',
          'target_thread': '019e6f30-1648-7921-b6ba-c49c58d0445a',
          'goal': 'active',
          'tokens': '86132212',
          'goal_hours': '52.22',
          'session_mb': '239.6',
          'session_age': '0.15m',
          'control_risk': 'red',
          'pressure':
              'red (tokens_used_86132212, goal_hours_52.22, session_mb_239.6)',
          'stop_action': 'containment_closeout_needed',
          'reason':
              'automation is paused, but target goal is active and the session is still fresh',
          'severity': 'high',
          'current_signal': 'high goal pressure | 86132212 tokens | 52.22h',
          'next_action':
              'Contain the paused active target before trusting this goal.',
          'handoff_label': 'Copy goal-pressure handoff',
          'handoff_copy':
              'Project Autopilot agent goal-pressure handoff\nAutomation: chili-llm-cost-reduction-loop\nTarget thread: 019e6f30-1648-7921-b6ba-c49c58d0445a\nPermission boundary: goal containment only. This copied handoff does not authorize runtime restart.',
          'path': 'project_ws/AgentOps/CODEX_GOAL_HEALTH.md',
          'open_path':
              r'D:\dev\chili-home-copilot\project_ws\AgentOps\CODEX_GOAL_HEALTH.md',
        },
      });

      expect(activity.hasGoalHealthPressure, isTrue);
      expect(activity.goalHealthCritical, isTrue);
      expect(activity.goalHealthChipLabel, 'paused goal active');
      expect(activity.goalHealthActionChipLabel, 'contain goal');
      expect(activity.goalHealthTokensChipLabel, '86M tok');
      expect(activity.goalHealthLabel, 'Goal health: paused goal active');
      expect(
          activity.goalHealthDetail, contains('chili-llm-cost-reduction-loop'));
      expect(activity.goalHealthDetail, contains('target 019e6f30'));
      expect(activity.goalHealthDetail, contains('86M tok'));
      expect(activity.goalHealthDetail, contains('52.22h goal'));
      expect(activity.goalHealthDetail, contains('239.6 MB session'));
      expect(
          activity.goalHealthDetail, contains('containment closeout needed'));
      expect(
        activity.goalHealthActionDetail,
        contains('paste the containment stop message'),
      );
      expect(
        activity.goalHealthActionDetail,
        contains('quiet proof before source/runtime trust'),
      );
      expect(activity.hasGoalHealthTarget, isTrue);
      expect(activity.goalHealthOpenActionLabel, 'Review goal pressure');
      expect(activity.hasGoalHealthHandoff, isTrue);
      expect(activity.goalHealthHandoffLabel, 'Copy goal-pressure handoff');
      expect(
        activity.goalHealthHandoffCopy,
        contains('Project Autopilot agent goal-pressure handoff'),
      );
      expect(
        activity.permissionBoundaryChipLabel,
        'goal containment only',
      );
      expect(
        activity.permissionBoundaryStackLabel,
        contains('Review goal pressure -> goal containment only'),
      );
      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasBenchAttention, isTrue);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.attentionScore, greaterThanOrEqualTo(9300));
      expect(activity.searchTerms, contains('paused goal active'));
      expect(activity.searchTerms, contains('contain goal'));
      expect(activity.searchTerms, contains('containment_closeout_needed'));
      expect(activity.searchTerms, contains(activity.goalHealthActionDetail));
      expect(activity.searchTerms, contains('86M tok'));
      expect(
        activity.searchTerms,
        contains('project_ws/AgentOps/CODEX_GOAL_HEALTH.md'),
      );
      expect(activity.searchTerms, contains(activity.goalHealthHandoffCopy));
    });

    test('separates manual goal fastlane pressure from containment', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'goal_health_pressure': {
          'automation': 'manual-goal-019e7c93',
          'status': 'MANUAL_GOAL',
          'target_thread': '019e7c93-06bc-7b62-8737-5e87998c4788',
          'goal': 'active',
          'tokens': '34602196',
          'goal_hours': '38.8',
          'session_mb': '114.7',
          'session_age': '0.09m',
          'control_risk': 'yellow',
          'pressure':
              'red (tokens_used_34602196, goal_hours_38.8, session_mb_114.7)',
          'stop_action': 'manual_goal_fastlane_required',
          'reason':
              'operator-set manual goal is active and the target session is fresh',
          'severity': 'high',
          'next_action':
              'Constrain this manual goal to PR/blocker/disposition fastlane work.',
          'path': 'project_ws/AgentOps/CODEX_GOAL_HEALTH.md',
        },
      });

      expect(activity.hasGoalHealthPressure, isTrue);
      expect(activity.goalHealthCritical, isTrue);
      expect(activity.goalHealthChipLabel, 'goal overloaded');
      expect(activity.goalHealthActionChipLabel, 'fastlane goal');
      expect(
        activity.goalHealthActionDetail,
        contains('fastlane only'),
      );
      expect(
        activity.goalHealthActionDetail,
        contains('publish one exact next-owner blocker'),
      );
      expect(activity.goalHealthDetail, contains('manual goal fastlane'));
      expect(activity.goalHealthHandoffCopy,
          contains('Stop action: manual_goal_fastlane_required'));
      expect(activity.goalHealthHandoffCopy,
          contains('Copy-ready one-liner: this operator-set manual goal'));
      expect(activity.goalHealthHandoffCopy,
          contains('close the current PR/blocker/disposition item'));
      expect(activity.goalHealthHandoffCopy,
          contains('do not do backlog, status, audit, source, test, runtime'));
      expect(activity.goalHealthHandoffCopy,
          contains('manual goals remain useful only while tied to one PR'));
      expect(activity.searchTerms, contains('fastlane goal'));
      expect(
        activity.searchTerms,
        contains('manual_goal_fastlane_required'),
      );
      expect(activity.searchTerms, contains(activity.goalHealthActionDetail));
    });

    test('builds copy-ready containment stop handoff when report has no copy',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'goal_health_pressure': {
          'automation': 'inspect-project-brain-ui',
          'status': 'PAUSED',
          'target_thread': '019e6b97-a3b9-79f0-bdf0-7f7c04383311',
          'goal': 'active',
          'tokens': '71168493',
          'goal_hours': '62.16',
          'session_mb': '417.1',
          'session_age': '0.07m',
          'control_risk': 'red',
          'pressure':
              'red (tokens_used_71168493, goal_hours_62.16, session_mb_417.1)',
          'stop_action': 'containment_closeout_needed',
          'reason':
              'automation is paused, but target goal is active and the session is still fresh',
          'severity': 'high',
          'next_action':
              'Contain the active paused target before trusting source work.',
          'path': 'project_ws/AgentOps/ACTIVE_GOAL_STOP_MESSAGES.txt',
        },
      });

      expect(activity.hasGoalHealthPressure, isTrue);
      expect(activity.goalHealthActionChipLabel, 'contain goal');
      expect(activity.goalHealthHandoffCopy,
          contains('Copy-ready one-liner: stop this manually active goal now'));
      expect(activity.goalHealthHandoffCopy,
          contains('one zero-hold containment closeout'));
      expect(activity.goalHealthHandoffCopy,
          contains('go quiet with no heartbeat/read-only/status reports'));
      expect(activity.goalHealthHandoffCopy,
          contains('Quiet proof: after the closeout'));
      expect(activity.goalHealthHandoffCopy,
          contains('no thread goal update, tool call, source write'));
      expect(activity.searchTerms, contains(activity.goalHealthHandoffCopy));
    });

    test('surfaces stale active runs as bench recovery work', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'active_run_count': 1,
        'open_run_count': 1,
        'active_run_preview': {
          'run_id': 'pa_stale',
          'status': 'running',
          'current_stage': 'implement',
          'title': 'Recover a stale worker.',
          'updated_at': '2026-06-01T01:20:00Z',
          'latest_step_title': 'Running targeted bench tests',
          'latest_step_status': 'in_progress',
          'stale': true,
          'stale_kind': 'active',
          'last_seen_age_minutes': 73,
          'stale_after_minutes': 30,
          'stale_action': 'inspect_stale_run',
          'stale_action_label': 'Inspect stale run',
          'stale_detail':
              'No active run update for 73 minutes; inspect before wake, cancel, or retry.',
          'stale_safe_next_step':
              'Open the active run, confirm the worker has no fresh progress.',
          'stale_handoff_label': 'Copy stale worker handoff',
          'stale_handoff_copy':
              'Project Autopilot stale-worker recovery handoff\nRun: pa_stale\nBlocked action: do not restart containers.\n\nPermission boundary: runtime review only. This copied handoff does not authorize restart.',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasStaleActiveRun, isTrue);
      expect(activity.hasStaleRunHandoff, isTrue);
      expect(activity.activeStaleChipLabel, 'stale 73m');
      expect(activity.activeStaleActionChipLabel, 'inspect stale run');
      expect(activity.staleActiveOpenActionLabel, 'Open/copy stale run');
      expect(
        activity.activeProgressLabel,
        'Current: stale on Running targeted bench tests (73m)',
      );
      expect(
        activity.activeProgressDetail,
        contains('inspect before wake'),
      );
      expect(activity.attentionScore, greaterThan(7600));
      expect(activity.searchTerms, contains('stale 73m'));
      expect(activity.searchTerms, contains('inspect_stale_run'));
      expect(activity.searchTerms, contains('active'));
      expect(activity.searchTerms, contains('Copy stale worker handoff'));
      expect(activity.activeRunStalePermissionBoundaryLabel,
          'runtime review only');
      expect(activity.permissionBoundaryChipLabel, 'runtime review only');
      expect(activity.permissionBoundaryStackLabel,
          'Boundary: Open/copy stale run -> runtime review only');
      expect(activity.searchTerms, contains('runtime review only'));
    });

    test('falls back to stage progress when latest step is absent', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'open_run_count': 1,
        'active_run_preview': {
          'run_id': 'pa_waiting',
          'status': 'running',
          'current_stage': 'validate',
          'title': 'Run targeted validation.',
        },
      });

      expect(activity.activeProgressLabel, 'Current: working on validation');
      expect(
          activity.activeProgressDetail, contains('Run targeted validation.'));
    });

    test('summarizes latest blocked run recovery for bench rows', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'blocked_run_preview': {
          'run_id': 'pa_blocked',
          'status': 'blocked',
          'current_stage': 'validate',
          'title': 'Recover blocked bench action.',
          'reason': 'Validation failed after repair.',
          'updated_at': '2026-06-01T02:40:00Z',
          'recovery_action': 'rerun_safe',
          'recovery_action_label': 'Rerun',
          'recovery_category': 'validation_failed',
          'recovery_can_rerun': true,
          'recovery_decision_label': 'Rerun safely',
          'recovery_safe_next_step':
              'Prefill a fresh approval-first draft and require validation evidence.',
          'recovery_last_failed_step': 'flutter_test_bench',
          'recovery_last_failed_exit_code': 1,
          'recovery_last_failed_summary': 'Expected recovery chip',
          'recovery_handoff_label': 'Copy recovery handoff',
          'recovery_handoff_copy':
              'Project Autopilot blocker recovery handoff\nSafe action: prefill a fresh approval-first draft.\n\nPermission boundary: recovery review only. This copied handoff does not authorize rerun.',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasBlockedRecovery, isTrue);
      expect(activity.blockedRecoveryChipLabel, 'blocked recovery');
      expect(activity.blockedRecoveryDecisionChipLabel, 'rerun safely');
      expect(
        activity.blockedRecoveryLabel,
        'Recovery: Rerun safely after flutter_test_bench exit 1',
      );
      expect(
        activity.blockedRecoveryDetail,
        contains('Prefill a fresh approval-first draft'),
      );
      expect(
        activity.blockedRecoveryPrecheckLabel,
        'Precheck: Rerun safely, approval-first',
      );
      expect(activity.blockedRecoveryPrecheckChipLabel, 'approval-first');
      expect(
        activity.blockedRecoveryPrecheckDetail,
        contains('fresh validation evidence before merge'),
      );
      expect(
        activity.blockedRecoveryPrecheckDetail,
        contains('flutter_test_bench exit 1'),
      );
      expect(activity.blockedRecoveryOpenActionLabel, 'Rerun safely');
      expect(activity.attentionScore, greaterThan(7000));
      expect(activity.searchTerms, contains('blocked recovery'));
      expect(activity.searchTerms, contains('validation_failed'));
      expect(activity.searchTerms, contains('approval-first'));
      expect(
        activity.searchTerms,
        contains('Precheck: Rerun safely, approval-first'),
      );
      expect(activity.searchTerms, contains('Expected recovery chip'));
      expect(activity.searchTerms, contains('Copy recovery handoff'));
      expect(activity.blockedRunRecoveryPermissionBoundaryLabel,
          'recovery review only');
      expect(activity.permissionBoundaryChipLabel, 'recovery review only');
      expect(activity.searchTerms, contains('recovery review only'));
    });

    test('marks review-only blocked recovery as rerun blocked', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'blocked_run_preview': {
          'run_id': 'pa_policy',
          'status': 'blocked',
          'current_stage': 'validate',
          'title': 'Review runtime-control blocker.',
          'reason': 'Runtime control is outside Autopilot scope.',
          'recovery_can_rerun': false,
          'recovery_decision_label': 'Review only',
          'recovery_safe_next_step':
              'Open the run for context; do not rerun the same policy-blocked command.',
          'recovery_handoff_label': 'Copy recovery handoff',
          'recovery_handoff_copy':
              'Project Autopilot blocker recovery handoff\nBlocked action: do not rerun.\n\nPermission boundary: runtime review only. This copied handoff does not authorize restart.',
        },
      });

      expect(activity.hasBlockedRecovery, isTrue);
      expect(activity.blockedRecoveryPrecheckLabel,
          'Precheck: Review only, rerun blocked');
      expect(activity.blockedRecoveryPrecheckChipLabel, 'rerun blocked');
      expect(activity.blockedRecoveryPrecheckDetail,
          contains('operator review required'));
      expect(activity.blockedRecoveryPrecheckDetail,
          contains('runtime review only'));
      expect(activity.searchTerms, contains('rerun blocked'));
      expect(activity.searchTerms,
          contains('Precheck: Review only, rerun blocked'));
    });

    test('keeps needs-input visible even before a question is counted', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'active_run_count': 0,
        'open_run_count': 0,
        'pending_question_count': 0,
        'operating_state': {
          'state': 'needs_input',
        },
      });

      expect(activity.questionLabel, isEmpty);
      expect(activity.needsInputChipLabel, 'needs input');
      expect(activity.hasOperatorInput, isTrue);
      expect(activity.attentionScore, 8000);
      expect(activity.searchTerms, contains('needs input'));
    });

    test('treats blocked status as operator attention', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'status': 'blocked',
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.attentionScore, 7000);
    });

    test('surfaces scheduled quality gate pressure in bench rows', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'scheduled_quality_pressure': {
          'status': 'low_quality',
          'total': 3,
          'passed': 1,
          'repaired_count': 1,
          'low_quality_count': 1,
          'average_score': 67,
          'issue': 'missing_adversarial_probe',
          'issue_label': 'missing adversarial probe',
          'issue_count': 2,
          'next_action':
              'Require a negative, boundary, or failure-path probe before trusting readiness claims.',
          'latest_run_id': 'pa_quality',
          'latest_status': 'repaired',
          'latest_score': 76,
          'latest_initial_score': 54,
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasScheduledQualityPressure, isTrue);
      expect(activity.hasScheduledQualityTarget, isTrue);
      expect(activity.scheduledQualityChipLabel, 'quality rejected');
      expect(
          activity.scheduledQualityIssueChipLabel, 'missing adversarial probe');
      expect(activity.scheduledQualityLabel,
          'Quality gate: 1 rejected - missing adversarial probe');
      expect(activity.scheduledQualityDetail, contains('score 54->76'));
      expect(activity.scheduledQualityDetail, contains('avg 67'));
      expect(activity.scheduledQualityOpenActionLabel, 'Review quality run');
      expect(activity.benchActionStackLabels, contains('Review quality run'));
      expect(activity.attentionScore, greaterThan(7500));
      expect(activity.hasGoalReceiptPressure, isFalse);
      expect(activity.hasPursuingGoalFocus, isFalse);
      expect(
        activity.searchTerms,
        containsAll(<String>[
          'quality rejected',
          'missing adversarial probe',
          'Quality gate: 1 rejected - missing adversarial probe',
          'Require a negative, boundary, or failure-path probe before trusting readiness claims.',
          'pa_quality',
        ]),
      );
    });

    test('surfaces goal receipt repair pressure in bench rows', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'scheduled_quality_pressure': {
          'status': 'repaired',
          'total': 2,
          'passed': 1,
          'repaired_count': 1,
          'low_quality_count': 0,
          'average_score': 82,
          'issue': 'missing_goal_progress',
          'issue_label': 'goal receipt missing',
          'issue_count': 1,
          'next_action':
              'Require the report to repeat the active goal objective, current step, next action, and completion gate before it can influence operator decisions.',
          'latest_run_id': 'pa_goal_receipt',
          'latest_status': 'repaired',
          'latest_score': 82,
          'latest_initial_score': 62,
          'scheduled_quality_handoff_label': 'Copy goal receipt gate',
          'scheduled_quality_handoff_copy':
              'Project Autopilot goal receipt quality packet\nRequired evidence:\n- Exact active goal objective repeated in the scheduled report.\nForbidden actions:\n- Treating a repaired report as approval authority.',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasScheduledQualityPressure, isTrue);
      expect(activity.hasGoalReceiptPressure, isTrue);
      expect(activity.hasPursuingGoalProofPressure, isFalse);
      expect(activity.hasPursuingGoalFocus, isTrue);
      expect(activity.scheduledQualityChipLabel, 'quality repaired');
      expect(activity.scheduledQualityIssueChipLabel, 'goal receipt missing');
      expect(activity.scheduledQualityLabel,
          'Quality gate: 1 repaired - goal receipt missing');
      expect(activity.scheduledQualityDetail, contains('score 62->82'));
      expect(activity.scheduledQualityDetail, contains('pa_goal_receipt'));
      expect(activity.hasScheduledQualityHandoff, isTrue);
      expect(activity.scheduledQualityHandoffCopy,
          contains('Project Autopilot goal receipt quality packet'));
      expect(
          activity.scheduledQualityOpenActionLabel, 'Copy goal receipt gate');
      expect(
          activity.benchActionStackLabels, contains('Copy goal receipt gate'));
      expect(activity.searchTerms, contains('goal receipt missing'));
      expect(activity.searchTerms, contains('goals'));
      expect(
        activity.searchTerms,
        contains(
          'Require the report to repeat the active goal objective, current step, next action, and completion gate before it can influence operator decisions.',
        ),
      );
    });

    test('surfaces pursuing-goal proof pressure in bench rows', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'scheduled_quality_pressure': {
          'status': 'low_quality',
          'total': 3,
          'passed': 2,
          'repaired_count': 0,
          'low_quality_count': 1,
          'average_score': 79,
          'issue': 'goal_evidence_unbound',
          'issue_label': 'goal evidence unbound',
          'issue_count': 1,
          'next_action':
              'Require evidence, checks, or findings to name the active objective, scheduled request, or completion gate before trusting the goal receipt.',
          'latest_run_id': 'pa_goal_proof',
          'latest_status': 'low_quality',
          'latest_score': 79,
          'scheduled_quality_handoff_copy':
              'Project Autopilot goal proof packet\nRequired evidence:\n- Objective-tied evidence names the active goal and completion gate.\nForbidden actions:\n- Treating generic report evidence as pursuing-goal progress.',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasScheduledQualityPressure, isTrue);
      expect(activity.hasGoalReceiptPressure, isTrue);
      expect(activity.hasPursuingGoalProofPressure, isTrue);
      expect(activity.hasPursuingGoalFocus, isTrue);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.scheduledQualityIssueChipLabel, 'Pursuing goal proof');
      expect(activity.scheduledQualityLabel,
          'Pursuing goal proof: 1 rejected - goal evidence unbound');
      expect(activity.scheduledQualityDetail,
          contains('Goal progress remains untrusted'));
      expect(activity.scheduledQualityDetail, contains('active objective'));
      expect(activity.scheduledQualityOpenActionLabel, 'Copy goal proof gate');
      expect(activity.benchActionStackLabels, contains('Copy goal proof gate'));
      expect(activity.searchTerms, contains('Pursuing goal proof'));
      expect(activity.searchTerms, contains('goal proof'));
      expect(activity.searchTerms, contains('objective-tied evidence'));
      expect(activity.searchTerms, contains('completion gate proof'));
      expect(activity.searchTerms, contains('safety'));
    });

    test('surfaces PR publication receipt pressure as release trust evidence',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'scheduled_quality_pressure': {
          'status': 'low_quality',
          'total': 2,
          'passed': 1,
          'repaired_count': 0,
          'low_quality_count': 1,
          'average_score': 71,
          'issue': 'missing_pr_publication_receipt',
          'issue_count': 1,
          'next_action':
              'Require a current-head PR publication receipt before trusting green CI, publish-ready, ready-transition, or merge-ready claims.',
          'latest_run_id': 'pa_pr_receipt',
          'latest_status': 'low_quality',
          'latest_score': 61,
          'scheduled_quality_handoff_copy':
              'Project Autopilot PR publication receipt quality packet\ncurrent_head_check_receipt:\n- pr_number\n- head_sha\nSafety boundary: no publish-ready claim.',
        },
      });

      expect(activity.hasScheduledQualityPressure, isTrue);
      expect(activity.hasPrPublicationReceiptPressure, isTrue);
      expect(activity.hasGoalReceiptPressure, isFalse);
      expect(activity.hasPursuingGoalFocus, isFalse);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.scheduledQualityChipLabel, 'quality rejected');
      expect(activity.scheduledQualityIssueChipLabel, 'PR receipt missing');
      expect(activity.scheduledQualityLabel,
          'Quality gate: 1 rejected - PR receipt missing');
      expect(activity.scheduledQualityDetail, contains('current-head'));
      expect(activity.hasScheduledQualityHandoff, isTrue);
      expect(activity.scheduledQualityOpenActionLabel, 'Copy PR receipt gate');
      expect(activity.benchActionStackLabels, contains('Copy PR receipt gate'));
      expect(activity.searchTerms, contains('release trust'));
      expect(activity.searchTerms, contains('PR publication'));
      expect(activity.searchTerms, contains('current-head receipt'));
      expect(activity.searchTerms, contains('safety'));
    });

    test('warns when selected smoke passes but full benchmark is blocked', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'name': 'AgentOps Director',
        'coding_benchmark': {
          'profile': 'custom',
          'promotion_status': 'failed',
          'selected_scenarios_status': 'passed',
          'promotion_scope': 'selected_smoke_only',
          'pass_rate': '1/1',
          'source_stability': 'stable',
        },
      });

      expect(activity.hasBenchmarkPromotionScopeSignal, isTrue);
      expect(activity.benchmarkSelectedSmokePassedOnly, isTrue);
      expect(activity.benchmarkFullPromotionBlocked, isTrue);
      expect(activity.hasBenchmarkPromotionScopeWarning, isTrue);
      expect(activity.hasBenchAttention, isTrue);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.benchmarkPromotionScopeChipLabel, 'smoke passed only');
      expect(
        activity.benchmarkPromotionScopeLabel,
        'Benchmark scope: selected smoke passed, full promotion blocked',
      );
      expect(
        activity.benchmarkPromotionScopeDetail,
        contains('Selected scenario slice passed'),
      );
      expect(
        activity.benchmarkPromotionScopeDetail,
        contains('Do not treat this as promotion-ready'),
      );
      expect(
          activity.benchmarkPromotionScopeDetail, contains('profile custom'));
      expect(activity.benchmarkPromotionScopeDetail, contains('pass rate 1/1'));
      expect(activity.searchTerms, contains('smoke passed only'));
      expect(activity.searchTerms, contains('selected scenarios passed only'));
      expect(activity.searchTerms, contains('full promotion blocked'));
      expect(activity.searchTerms, contains('safety'));
    });

    test('marks full benchmark pass with source churn as unstable evidence',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'name': 'AgentOps Director',
        'coding_benchmark': {
          'profile': 'core',
          'promotion_status': 'failed',
          'selected_scenarios_status': 'passed',
          'promotion_scope': 'unstable_full_evidence',
          'pass_rate': '56/56',
          'source_stability': 'changed',
        },
      });

      expect(activity.hasBenchmarkPromotionScopeSignal, isTrue);
      expect(activity.benchmarkSelectedSmokePassedOnly, isFalse);
      expect(activity.benchmarkUnstableFullEvidence, isTrue);
      expect(activity.benchmarkFullPromotionBlocked, isTrue);
      expect(activity.hasBenchmarkPromotionScopeWarning, isTrue);
      expect(activity.hasBenchAttention, isTrue);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.benchmarkPromotionScopeChipLabel, 'full bench unstable');
      expect(
        activity.benchmarkPromotionScopeLabel,
        'Benchmark scope: full benchmark evidence unstable',
      );
      expect(
        activity.benchmarkPromotionScopeDetail,
        contains('Full coding benchmark scenarios passed'),
      );
      expect(
        activity.benchmarkPromotionScopeDetail,
        contains('source stability evidence is not clean'),
      );
      expect(activity.benchmarkPromotionScopeDetail, contains('profile core'));
      expect(
          activity.benchmarkPromotionScopeDetail, contains('pass rate 56/56'));
      expect(activity.searchTerms, contains('full bench unstable'));
      expect(activity.searchTerms, contains('source quiet'));
      expect(activity.searchTerms,
          isNot(contains('selected scenarios passed only')));
    });

    test('uses effective pass rate when stale failures are repaired', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'name': 'AgentOps Director',
        'coding_benchmark': {
          'profile': 'core',
          'promotion_status': 'failed',
          'selected_scenarios_status': 'failed',
          'promotion_scope': 'blocked',
          'pass_rate': '42/46',
          'effective_pass_rate': '46/46',
          'source_stability': 'stable',
          'repaired_failed_rows': {
            'covers_all_failed_rows': true,
            'covered_ids': ['behavior', 'review', 'repair', 'domain'],
          },
        },
      });

      expect(activity.hasBenchmarkPromotionScopeSignal, isTrue);
      expect(activity.benchmarkFullPromotionBlocked, isTrue);
      expect(activity.benchmarkPassRate, '46/46');
      expect(
          activity.benchmarkPromotionScopeDetail, contains('pass rate 46/46'));
      expect(activity.benchmarkPromotionScopeDetail, isNot(contains('42/46')));
    });

    test('surfaces frontier evidence gaps in benchmark detail and search', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'name': 'AgentOps Director',
        'coding_benchmark': {
          'profile': 'core',
          'promotion_status': 'failed',
          'selected_scenarios_status': 'failed',
          'promotion_scope': 'blocked',
          'effective_pass_rate': '46/46',
          'source_stability': 'changed',
          'frontier_evidence_gap_labels': [
            'source freshness',
            'real shadow evidence',
            'local model candidate diagnostics',
            'real PR repair inventory',
          ],
          'frontier_evidence_gaps': [
            {
              'gate': 'source_freshness',
              'label': 'source freshness',
              'actual': 'source stability changed',
              'next_action':
                  'Wait for source/test churn to settle, then rerun the full coding benchmark.',
            },
            {
              'gate': 'model_shadow_real_manifest',
              'label': 'real shadow evidence',
              'actual': 'self_test',
              'next_action':
                  'Collect transcript-verified Codex, Claude, and local-model shadow manifests.',
            },
            {
              'gate': 'local_model_candidate_run',
              'label': 'local model candidate diagnostics',
              'actual':
                  'real-chili-preflight-candidate-wins: local model timed out after 60s',
              'path':
                  'project_ws/AgentOps/local_model_candidate_runs/local-suite-timeout/suite_diagnostics.json',
              'next_action':
                  'python scripts/autopilot_local_model_candidate_runner.py --retry-from-diagnostics project_ws/AgentOps/local_model_candidate_runs/local-suite-timeout/suite_diagnostics.json --timeout-seconds 300 --json',
            },
            {
              'gate': 'hosted_pr_repair_real_inventory',
              'label': 'real PR repair inventory',
              'actual': 'self_test; promotion eligible false',
              'next_action':
                  'Collect hosted PR repair artifacts with current-head check receipts.',
            },
          ],
          'frontier_evidence_next_action':
              'Collect transcript-verified Codex, Claude, and local-model shadow manifests.',
          'frontier_model_evidence_intake': {
            'status': 'partial',
            'ready_source_count': 1,
            'required_source_count': 3,
            'missing_source_count': 2,
            'raw_source_root':
                'project_ws/AgentOps/frontier_model_evidence_intake/raw_sources',
            'prompt_pack_manifest':
                'project_ws/AgentOps/frontier_model_prompt_packs/manifest.json',
            'next_action':
                'Populate missing frontier source bundles: claude, local_model.',
            'local_model_candidate_run': {
              'status': 'passed',
              'timeout_salvaged_cases': [
                'real-chili-preflight-candidate-wins',
              ],
            },
            'sources': [
              {'source_kind': 'codex', 'status': 'ready'},
              {
                'source_kind': 'claude',
                'status': 'partial',
                'preflight_recovery_action_label':
                    'Import saved claude response',
                'preflight_recovery_response_staging_file':
                    'project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt',
                'preflight_recovery_dry_run_command':
                    'python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json --no-write',
                'preflight_recovery_all_cases_command':
                    'python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json',
                'preflight_recovery_single_case_command':
                    'python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --case-id real-chili-preflight-candidate-wins --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_single_case_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json',
                'preflight_recovery_validation_command':
                    'python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --allow-partial --json --no-write',
                'preflight_recovery_publish_command':
                    'python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --publish-scorecards --json',
                'preflight_recovery_boundary':
                    'collection and evidence import only',
              },
              {'source_kind': 'local_model', 'status': 'missing'},
            ],
          },
        },
      });

      expect(activity.hasBenchmarkEvidenceGaps, isTrue);
      expect(activity.hasBenchAttention, isTrue);
      expect(activity.benchmarkEvidenceGapLabels,
          contains('real shadow evidence'));
      expect(activity.benchmarkPromotionScopeDetail, contains('Proof gaps'));
      expect(activity.benchmarkPromotionScopeDetail,
          contains('real shadow evidence'));
      expect(activity.hasBenchmarkEvidenceRecovery, isTrue);
      expect(activity.benchmarkEvidenceRecoverySources, contains('claude'));
      expect(activity.benchmarkEvidenceRecoveryDetail,
          '1 source has safe recovery: claude');
      expect(
          activity.benchmarkEvidenceOpenActionLabel, 'Copy frontier recovery');
      expect(activity.benchmarkEvidenceIntakeChipLabel, 'intake 1/3 ready');
      expect(activity.benchmarkEvidenceIntakeDetail,
          contains('Missing: claude, local_model'));
      expect(activity.benchmarkEvidenceIntakeDetail,
          contains('Recovery: 1 source has safe recovery: claude'));
      expect(activity.benchmarkEvidenceIntakeDetail,
          contains('Local model: passed'));
      expect(activity.benchmarkEvidenceIntakeDetail,
          contains('Timeout salvage: real-chili-preflight-candidate-wins'));
      expect(activity.benchmarkEvidenceIntakeDetail,
          contains('Next: Populate missing frontier source'));
      expect(
          activity.benchActionStackLabels, contains('Copy frontier recovery'));
      expect(activity.permissionBoundaryStackLabel,
          contains('Copy frontier recovery -> evidence collection only'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('Project Autopilot frontier evidence proof packet'));
      expect(
          activity.benchmarkEvidenceHandoffCopy, contains('Recovery routes:'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('claude: Import saved claude response'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('Save all-cases response to: project_ws/AgentOps'));
      expect(
          activity.benchmarkEvidenceHandoffCopy,
          contains(
              'Dry-run import: python scripts/autopilot_frontier_source_evidence_recorder.py'));
      expect(
          activity.benchmarkEvidenceHandoffCopy, contains('--json --no-write'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('--source-kind claude --all-cases'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('--case-id real-chili-preflight-candidate-wins'));
      expect(
          activity.benchmarkEvidenceHandoffCopy,
          contains(
              'Validate after import: python scripts/autopilot_frontier_model_evidence_intake.py'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('--allow-partial --json --no-write'));
      expect(
          activity.benchmarkEvidenceHandoffCopy,
          contains(
              'Publish when all sources are ready: python scripts/autopilot_frontier_model_evidence_intake.py'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('--publish-scorecards --json'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('Boundary: collection and evidence import only'));
      expect(
          activity.benchmarkEvidenceHandoffCopy, contains('source freshness'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('real PR repair inventory'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('local model candidate diagnostics'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('local-suite-timeout/suite_diagnostics.json'));
      expect(activity.benchmarkEvidenceHandoffCopy,
          contains('does not authorize source/test edits'));
      expect(activity.searchTerms, contains('real PR repair inventory'));
      expect(
          activity.searchTerms, contains('local model candidate diagnostics'));
      expect(
        activity.searchTerms.any(
          (term) => term.contains('local model timed out after 60s'),
        ),
        isTrue,
      );
      expect(activity.searchTerms, contains('intake 1/3 ready'));
      expect(activity.searchTerms, contains('frontier recovery'));
      expect(activity.searchTerms, contains('all-cases import'));
      expect(
          activity.searchTerms, contains('1 source has safe recovery: claude'));
      expect(activity.searchTerms, contains('partial-timeout salvage'));
      expect(activity.searchTerms, contains('local model timeout salvage'));
      expect(activity.searchTerms,
          contains('real-chili-preflight-candidate-wins'));
      expect(activity.searchTerms, contains('claude'));
      expect(activity.searchTerms, contains('local_model'));
      expect(activity.searchTerms,
          contains(activity.benchmarkEvidenceHandoffCopy));
      expect(
        activity.searchTerms,
        contains(
          'Collect transcript-verified Codex, Claude, and local-model shadow manifests.',
        ),
      );
    });

    test('sorts bench agents by operator attention before name', () {
      final sorted = AutopilotAgentBenchActivityPresenter.sortAttentionFirst([
        {
          'name': 'Idle Architect',
        },
        {
          'name': 'Running Backend',
          'active_run_count': 2,
        },
        {
          'name': 'Question QA',
          'pending_question_count': 1,
        },
        {
          'name': 'Blocked DevOps',
          'status': 'blocked',
        },
        {
          'name': 'PM Pressure',
          'kpi_lane_pressure': {
            'blocked_pr_count': 9,
            'score': 2.4,
            'severity': 'high',
          },
        },
      ]);

      expect(
        sorted.map((agent) => agent['name']),
        [
          'PM Pressure',
          'Question QA',
          'Blocked DevOps',
          'Running Backend',
          'Idle Architect',
        ],
      );
    });

    test('summarizes KPI lane pressure for bench chips and search', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'name': 'Frontend',
        'kpi_lane_pressure': {
          'blocked_pr_count': 2,
          'ready_candidate_count': 1,
          'temp_artifact_count': 1,
          'score': 4.1,
          'severity': 'high',
          'current_signal':
              '2 blocked PR lane(s): #132 ci_failing, #110 ci_failing',
          'next_action': 'Finish Frontend PR pressure.',
          'pr_numbers': ['132', '110'],
          'pr_blocker_failing_count': 2,
          'pr_blocker_top_pr': '132',
          'pr_blocker_top_branch':
              'codex/sswe/pm-20260529-072-pr128-coinbase-ohlcv-test-isolation',
          'pr_blocker_top_merge': 'UNKNOWN',
          'pr_blocker_top_ci': 'test:FAILURE',
          'pr_blocker_top_posture': 'Blocked: ci_failing',
          'pr_blocker_proof_floor':
              'current-head check evidence and PM/operator disposition',
        },
      });

      expect(activity.hasKpiPressure, isTrue);
      expect(activity.kpiBlockedPrChipLabel, '2 blocked PRs');
      expect(activity.kpiPrOwnerLaneChipLabel, 'Frontend PR lane');
      expect(activity.kpiPrOwnerLaneLabel, 'Frontend PR lane: 2 blocked PRs');
      expect(activity.kpiReadyCandidateChipLabel, '1 ready');
      expect(activity.kpiTempArtifactChipLabel, '1 temp');
      expect(activity.kpiScoreChipLabel, 'KPI 4.1');
      expect(activity.kpiPrPreviewLabel, 'PR #132, #110');
      expect(activity.kpiPrOwnerLaneActionLabel, 'Copy PR lane handoff');
      expect(activity.kpiPrOwnerLaneDetail, contains('Owner lane: Frontend'));
      expect(activity.kpiPrOwnerLaneDetail, contains('PR #132, #110'));
      expect(activity.kpiPrOwnerLaneDetail,
          contains('Finish Frontend PR pressure.'));
      expect(activity.kpiPrOwnerLaneCopyText,
          contains('Project Autopilot PR owner-lane handoff'));
      expect(activity.kpiPrOwnerLaneCopyText, contains('Owner lane: Frontend'));
      expect(activity.kpiPrOwnerLaneCopyText, contains('PR #132, #110'));
      expect(
        activity.kpiPrOwnerLaneCopyText,
        contains('current-head check evidence and PM/operator disposition'),
      );
      expect(activity.kpiPrOwnerLaneCopyText,
          contains('does not authorize source edits'));
      expect(activity.kpiSeverity, 'high');
      expect(activity.attentionScore, greaterThan(5400));
      expect(activity.searchTerms, contains('Frontend PR lane'));
      expect(activity.searchTerms, contains('Frontend PR lane: 2 blocked PRs'));
      expect(activity.searchTerms, contains('Finish Frontend PR pressure.'));
      expect(activity.searchTerms, contains('PR #132, #110'));
      expect(activity.searchTerms, contains(activity.kpiPrOwnerLaneCopyText));
    });

    test('summarizes generated-state drift for bench chips and search', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'kpi_lane_pressure': {
          'severity': 'high',
          'current_signal':
              'Generated-state drift: current expedite board is newer',
          'next_action':
              'Refresh AgentOps KPI scorecard before trusting generated board guidance.',
          'generated_state_refresh_required': true,
          'generated_state_drift_kind': 'kpi_scorecard_expedite_drift',
          'generated_state_drift_count': 2,
          'generated_state_drift_label': '2 stale board actions',
          'generated_state_drift_summary':
              'current expedite board is newer; board now shows open_pr_blocker',
          'generated_state_board_generated': '2026-05-31T18:52:24Z',
          'generated_state_scorecard_generated': '2026-05-31T18:52:10Z',
          'generated_state_scorecard_top_action':
              '#2 stable_inbox_request for PM',
          'generated_state_board_top_action':
              '#2 open_pr_blocker for PM / operator / SSWE',
          'generated_state_path':
              'project_ws/AgentOps/OMNIAGENT_KPI_SCORECARD.md',
          'generated_state_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_KPI_SCORECARD.md',
        },
      });

      expect(activity.hasKpiPressure, isTrue);
      expect(activity.hasKpiGeneratedStateDrift, isTrue);
      expect(activity.kpiGeneratedStateChipLabel, '2 stale board actions');
      expect(
        activity.kpiGeneratedStateLabel,
        'Generated state: 2 stale board actions',
      );
      expect(
          activity.kpiGeneratedStateDetail, contains('Refresh AgentOps KPI'));
      expect(
        activity.kpiGeneratedStateDetail,
        contains('refresh/reconcile generated state'),
      );
      expect(activity.kpiGeneratedStateDetail,
          contains('board now #2 open_pr_blocker'));
      expect(activity.kpiGeneratedStateDetail,
          contains('scorecard shows #2 stable_inbox_request'));
      expect(activity.hasKpiGeneratedStateTarget, isTrue);
      expect(activity.kpiGeneratedStateBlocksBoardActions, isTrue);
      expect(activity.shouldPrioritizeKpiGeneratedStateAction, isTrue);
      expect(activity.kpiGeneratedStateGuardLabel, 'Refresh scorecard first');
      expect(
          activity.kpiGeneratedStateOpenActionLabel, 'Open stale-state guard');
      expect(
        activity.kpiGeneratedStatePrimaryOpenPath,
        'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_KPI_SCORECARD.md',
      );
      expect(activity.attentionScore, greaterThanOrEqualTo(6900));
      expect(activity.searchTerms, contains('2 stale board actions'));
      expect(activity.searchTerms, contains('kpi_scorecard_expedite_drift'));
      expect(
        activity.searchTerms,
        contains('project_ws/AgentOps/OMNIAGENT_KPI_SCORECARD.md'),
      );
    });

    test('prioritizes stale expedite board reconciliation before PR ranks', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'kpi_lane_pressure': {
          'severity': 'high',
          'current_signal':
              'Generated-state drift: scorecard is newer than board',
          'next_action':
              'Refresh AgentOps expedite board before trusting generated board guidance.',
          'generated_state_refresh_required': true,
          'generated_state_refresh_target': 'expedite_board',
          'generated_state_drift_kind': 'kpi_scorecard_expedite_drift',
          'generated_state_drift_count': 1,
          'generated_state_drift_label': 'stale expedite board',
          'generated_state_drift_summary':
              'scorecard now shows control_plane_containment while board shows open_pr_blocker',
          'generated_state_board_generated': '2026-06-02T01:40:59Z',
          'generated_state_scorecard_generated': '2026-06-02T06:10:47Z',
          'generated_state_path':
              'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'generated_state_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_EXPEDITE_BOARD.md',
        },
        'expedite_lane_pressure': {
          'open_pr_blocker_count': 9,
          'severity': 'high',
          'current_signal': '9 open PR blocker(s)',
          'next_action': 'Create a current-head check path.',
          'top_rank': 2,
          'top_type': 'open_pr_blocker',
          'top_owner': 'PM / operator / SSWE',
          'top_evidence':
              'PR #134 no checks; merge=DIRTY; branch=codex/brain-work',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_EXPEDITE_BOARD.md',
        },
      });

      expect(activity.hasExpeditePressure, isTrue);
      expect(activity.deliveryFocusChipLabel, 'delivery: PR blocked');
      expect(activity.kpiGeneratedStateRefreshTarget, 'expedite_board');
      expect(activity.kpiGeneratedStateBlocksBoardActions, isTrue);
      expect(activity.shouldPrioritizeKpiGeneratedStateAction, isTrue);
      expect(
        activity.kpiGeneratedStateGuardLabel,
        'Refresh expedite board first',
      );
      expect(
          activity.kpiGeneratedStateOpenActionLabel, 'Open stale-state guard');
      expect(
        activity.kpiGeneratedStatePrimaryOpenPath,
        'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_EXPEDITE_BOARD.md',
      );
      expect(activity.searchTerms, contains('expedite_board'));
      expect(activity.searchTerms, contains('stale expedite board'));
    });

    test('summarizes fresh expedite board pressure for bench chips and search',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'open_pr_blocker_count': 9,
          'ready_candidate_count': 1,
          'control_plane_count': 0,
          'severity': 'high',
          'current_signal':
              '9 open PR blocker(s), 1 ready draft(s): #134, #40, #114',
          'next_action': 'Create a current-head check path.',
          'pr_numbers': ['134', '40', '114', '112'],
          'top_rank': 2,
          'top_type': 'open_pr_blocker',
          'top_owner': 'PM / operator / SSWE',
          'top_evidence':
              'PR #134 no checks; merge=DIRTY; draft=False; branch=codex/brain-work-done-marker-recovery',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_EXPEDITE_BOARD.md',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasExpeditePressure, isTrue);
      expect(activity.expeditePrBlockerChipLabel, '9 board PRs');
      expect(activity.expediteReadyCandidateChipLabel, '1 board ready');
      expect(activity.expediteControlPlaneChipLabel, isEmpty);
      expect(activity.expediteRankChipLabel, 'board #2');
      expect(activity.expediteOwnerActionChipLabel, 'PM/SSWE next');
      expect(
        activity.expediteOwnerActionDetail,
        'Owner/action: PM / operator / SSWE | Create a current-head check path | keep lane blocked until owner supplies Worktree/branch/head evidence',
      );
      expect(activity.expeditePrPostureChipLabel, 'PR blocked');
      expect(activity.expeditePrPostureBlocked, isTrue);
      expect(
        activity.expeditePrPostureDetail,
        'PR posture: blocked by missing current-head checks; keep blocked or rebuild current-head checks with Worktree/branch/head evidence | PR #134 no checks; merge=DIRTY; draft=False; branch=codex/brain-work-done-marker-recovery.',
      );
      expect(activity.expediteEvidenceRequiredChipLabel, 'needs checks');
      expect(activity.expediteEvidenceRequiredCritical, isTrue);
      expect(
        activity.expediteEvidenceRequiredDetail,
        'Evidence required: current-head checks are missing; rebuild checks with Worktree/branch/head evidence before PR state changes | PR #134 no checks; merge=DIRTY; draft=False; branch=codex/brain-work-done-marker-recovery.',
      );
      expect(activity.hasPrPreflightPressure, isTrue);
      expect(activity.prPreflightBlocked, isTrue);
      expect(activity.prPreflightReadyCandidate, isFalse);
      expect(activity.prPreflightChipLabel, 'PR preflight blocked');
      expect(
        activity.prPreflightLabel,
        'PR preflight blocked: board #2, PR #134, 9 board PR blockers, 1 board ready',
      );
      expect(
        activity.prPreflightDetail,
        contains('blockers needs checks'),
      );
      expect(
        activity.prPreflightDetail,
        contains('next Create a current-head check path.'),
      );
      expect(activity.prDeliveryRouteChipLabel, 'current-head checks');
      expect(activity.prDeliveryRouteHighRisk, isFalse);
      expect(activity.prDeliveryRouteReviewOnly, isFalse);
      expect(
        activity.prDeliveryRouteDetail,
        contains('rebuild current-head checks from an isolated owner worktree'),
      );
      expect(
        activity.prDeliveryRouteDetail,
        contains('Worktree/branch/head plus selected-test evidence'),
      );
      expect(activity.hasPrRecoveryContract, isTrue);
      expect(activity.prRecoveryContractChipLabel, 'PR recovery contract');
      expect(
        activity.prRecoveryContractLabel,
        'PR #134 recovery: current-head checks',
      );
      expect(
        activity.prRecoveryContractDetail,
        contains('keep blocked or rebuild current-head checks'),
      );
      expect(
        activity.prRecoveryContractCopyText,
        contains('Project Autopilot PR recovery contract'),
      );
      expect(
        activity.prRecoveryContractCopyText,
        contains(
            'Safety boundary: this copied recovery contract is decision-only'),
      );
      expect(activity.expediteDecisionChipLabel, 'decide: keep/rebuild/close');
      expect(
        activity.expediteSafeDecisionDetail,
        'Decision ladder: keep blocked | rebuild current-head checks with Worktree/branch/head evidence | close or defer with PM/operator acceptance.',
      );
      expect(activity.deliveryFocusChipLabel, 'delivery: PR blocked');
      expect(activity.deliveryFocusLabel, 'Delivery focus: PR blocked');
      expect(activity.deliveryFocusCritical, isTrue);
      expect(activity.deliveryFocusPriorityScore, greaterThan(0));
      expect(activity.hasDeliveryFocusTarget, isTrue);
      expect(activity.deliveryFocusRoutesToFlow, isFalse);
      expect(activity.deliveryFocusRoutesToExpedite, isTrue);
      expect(activity.deliveryFocusActionLabel, 'Open/copy recovery plan');
      expect(
        activity.deliveryFocusPrimaryOpenPath,
        'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_EXPEDITE_BOARD.md',
      );
      expect(
        activity.deliveryFocusDetail,
        contains('current-head checks, owner evidence'),
      );
      expect(activity.expeditePrPreviewLabel, 'Board PR #134, #40, #114');
      expect(
        activity.expediteTopActionLabel,
        'Board #2: PR #134 no checks; merge=DIRTY; draft=False; branch=codex/brain-work-done-marker-re...',
      );
      expect(activity.expediteSeverity, 'high');
      expect(activity.hasExpediteOpenTarget, isTrue);
      expect(activity.expediteOpenActionLabel, 'Open/copy board #2');
      expect(
        activity.expediteBoardRowCopyText,
        contains('Rank: 2'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Evidence: PR #134 no checks; merge=DIRTY'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Next action: Create a current-head check path.'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Preflight: PR preflight blocked'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Delivery route: PR delivery route for PR #134'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Recovery contract: PR recovery for PR #134'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
          'Owner action: Owner/action: PM / operator / SSWE | Create a current-head check path',
        ),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
          'Posture: PR posture: blocked by missing current-head checks',
        ),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
          'Evidence requirement: Evidence required: current-head checks are missing',
        ),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Safe operator decision menu:'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Keep blocked: record the owner and next check path'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Rebuild checks: create a current-head check path'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Close or defer: explicitly close/defer stale PR work'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('does not authorize merge, release, deploy'),
      );
      expect(activity.attentionScore, greaterThan(19000));
      expect(activity.searchTerms, contains('9 board PRs'));
      expect(activity.searchTerms, contains('board #2'));
      expect(activity.searchTerms, contains('PM/SSWE next'));
      expect(
        activity.searchTerms,
        contains(
          'Owner/action: PM / operator / SSWE | Create a current-head check path | keep lane blocked until owner supplies Worktree/branch/head evidence',
        ),
      );
      expect(activity.searchTerms, contains('PR blocked'));
      expect(activity.searchTerms, contains('PR preflight blocked'));
      expect(activity.searchTerms, contains('current-head checks'));
      expect(activity.searchTerms, contains('PR recovery contract'));
      expect(activity.searchTerms, contains(activity.prRecoveryContractDetail));
      expect(activity.searchTerms, contains('delivery: PR blocked'));
      expect(activity.searchTerms, contains('Delivery focus: PR blocked'));
      expect(activity.searchTerms, contains(activity.deliveryFocusDetail));
      expect(activity.searchTerms, contains(activity.prDeliveryRouteDetail));
      expect(
        activity.searchTerms,
        contains(
          'PR preflight blocked: board #2, PR #134, 9 board PR blockers, 1 board ready',
        ),
      );
      expect(
        activity.searchTerms,
        contains(
          'PR posture: blocked by missing current-head checks; keep blocked or rebuild current-head checks with Worktree/branch/head evidence | PR #134 no checks; merge=DIRTY; draft=False; branch=codex/brain-work-done-marker-recovery.',
        ),
      );
      expect(activity.searchTerms, contains('needs checks'));
      expect(
        activity.searchTerms,
        contains(
          'Evidence required: current-head checks are missing; rebuild checks with Worktree/branch/head evidence before PR state changes | PR #134 no checks; merge=DIRTY; draft=False; branch=codex/brain-work-done-marker-recovery.',
        ),
      );
      expect(activity.searchTerms, contains('decide: keep/rebuild/close'));
      expect(
        activity.searchTerms,
        contains(
          'Decision ladder: keep blocked | rebuild current-head checks with Worktree/branch/head evidence | close or defer with PM/operator acceptance.',
        ),
      );
      expect(activity.searchTerms, contains('open_pr_blocker'));
      expect(activity.searchTerms, contains('PM / operator / SSWE'));
      expect(
        activity.searchTerms,
        contains('project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md'),
      );
      expect(
          activity.searchTerms, contains('Create a current-head check path.'));
      expect(
        activity.searchTerms,
        contains(
          'Board #2: PR #134 no checks; merge=DIRTY; draft=False; branch=codex/brain-work-done-marker-re...',
        ),
      );
      expect(activity.searchTerms, contains('Board PR #134, #40, #114'));
    });

    test('summarizes owner-ready-first board pressure for bench action', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'owner_ready_first_count': 2,
          'owner_ready_first_primary_owner_count': 1,
          'owner_ready_first_support_count': 1,
          'owner_ready_first_top_rank': 5,
          'owner_ready_first_pr':
              '#134 [codex] Harden Phase 5 reader and probe guardrails',
          'owner_ready_first_pr_number': '134',
          'owner_ready_first_title':
              '[codex] Harden Phase 5 reader and probe guardrails',
          'owner_ready_first_owner': 'SSWE',
          'owner_ready_first_state':
              'conflict_resolution_owner_first; draft=False; merge=DIRTY; ci=no_checks',
          'owner_ready_first_required_lanes': 'DevOps, PM, SSWE',
          'owner_ready_first_existing_request':
              'project_ws/SSWE/OUT/pr134-owner-disposition.md',
          'owner_ready_first_existing_request_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\SSWE\\OUT\\pr134-owner-disposition.md',
          'owner_ready_first_next_action':
              'SSWE owns the conflict path: resolve or publish cannot-resolve evidence from an isolated PR worktree.',
          'owner_ready_first_lane_role': 'owner',
          'owner_ready_first_path':
              'project_ws/AgentOps/PR_OWNER_READY_FIRST_BOARD.md',
          'owner_ready_first_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\PR_OWNER_READY_FIRST_BOARD.md',
          'owner_ready_first_generated_at': '2026-06-01T23:18:34Z',
          'owner_ready_first_safety':
              'this board does not authorize merge, release, deployment, runtime/database/broker actions, or shared-root source edits.',
          'severity': 'high',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasExpeditePressure, isTrue);
      expect(activity.ownerReadyFirstChipLabel, '2 owner-ready PRs');
      expect(activity.ownerReadyFirstRankChipLabel, 'owner-ready #5');
      expect(activity.ownerReadyFirstRoleChipLabel, 'PR owner');
      expect(
        activity.ownerReadyFirstLabel,
        'Owner-ready #5 #134 SSWE',
      );
      expect(
        activity.ownerReadyFirstDetail,
        contains('owns this PR lane'),
      );
      expect(
        activity.ownerReadyFirstDetail,
        contains('SSWE owns the conflict path'),
      );
      expect(activity.hasExpediteOpenTarget, isTrue);
      expect(
        activity.expeditePrimaryOpenPath,
        'D:\\dev\\chili-home-copilot\\project_ws\\SSWE\\OUT\\pr134-owner-disposition.md',
      );
      expect(
        activity.expediteOpenActionLabel,
        'Open/copy owner-ready #5',
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Project Autopilot owner-ready-first row'),
      );
      expect(activity.expediteBoardRowCopyText, contains('Rank: 5'));
      expect(activity.expediteBoardRowCopyText, contains('Lane role: owner'));
      expect(
        activity.expediteBoardRowCopyText,
        contains('Safety boundary: this board does not authorize merge'),
      );
      expect(activity.attentionScore, greaterThanOrEqualTo(10000));
      expect(activity.searchTerms, contains('2 owner-ready PRs'));
      expect(activity.searchTerms, contains('owner-ready #5'));
      expect(activity.searchTerms, contains('PR owner'));
      expect(activity.searchTerms, contains('owner ready first'));
      expect(
        activity.searchTerms,
        contains('project_ws/AgentOps/PR_OWNER_READY_FIRST_BOARD.md'),
      );
    });

    test('surfaces stable inbox board rows as operator bench pressure', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'stable_inbox_request_count': 1,
          'open_pr_blocker_count': 9,
          'severity': 'high',
          'current_signal': '1 stable inbox request(s), 9 open PR blocker(s)',
          'next_action': 'Process this before routine work.',
          'top_rank': 2,
          'top_type': 'stable_inbox_request',
          'top_owner': 'PM',
          'top_evidence':
              'project_ws/PM/IN/urgent.md priority=Urgent; PM captain refresh needed',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_EXPEDITE_BOARD.md',
          'stable_inbox_top_rank': 2,
          'stable_inbox_request_path': 'project_ws/PM/IN/urgent.md',
          'stable_inbox_request_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\PM\\IN\\urgent.md',
          'stable_inbox_request_sha256': 'abc123',
          'stable_inbox_request_priority': 'Urgent',
          'stable_inbox_request_from': 'AgentOps',
          'stable_inbox_request_to': 'PM',
          'stable_inbox_request_backlog_id': 'PR134-CAPTAIN-REFRESH',
          'stable_inbox_request_preview':
              'Refresh or re-enter Critical Path Captain Mode for PR #134.',
          'stable_inbox_request_expected_deliverable':
              'PM disposition names whether PR #134 stays blocked, rebuilds checks, or closes.',
          'stable_inbox_request_success_criteria':
              'Disposition cites this request path and SHA256.',
          'stable_inbox_request_safety':
              'Do not mutate source, PR state, runtime, DB, broker/API, or live behavior from this handoff alone.',
          'stable_inbox_request_stale_for_latest': true,
          'stable_inbox_latest_request_path':
              'project_ws/PM/IN/20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
          'stable_inbox_latest_request_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\PM\\IN\\20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
          'stable_inbox_latest_request_sha256': 'def456',
          'stable_inbox_latest_request_created': '2026-06-01T01:02:23Z',
          'stable_inbox_latest_request_priority': 'Urgent',
          'stable_inbox_latest_request_from': 'AgentOps',
          'stable_inbox_latest_request_to': 'PM',
          'stable_inbox_latest_request_backlog_id': 'PR134-CAPTAIN-REFRESH',
          'stable_inbox_latest_request_preview':
              'Supersede earlier PR #134 refresh.',
          'stable_inbox_latest_request_expected_deliverable':
              'PM closes the older request as superseded and publishes one corrected captain decision.',
          'stable_inbox_latest_request_success_criteria':
              'PM cites this newer request path and SHA256 before routine work continues.',
          'stable_inbox_latest_request_safety':
              'Do not mutate source, PR state, runtime, DB, broker/API, or live behavior from this handoff alone.',
          'stable_inbox_latest_handoff_copy':
              'Project Autopilot stable-inbox request handoff\nRequests:\n- Rank ?: project_ws/PM/IN/20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
          'stable_inbox_freshness_detail':
              'board row points at project_ws/PM/IN/urgent.md; newer request project_ws/PM/IN/20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
          'stable_inbox_handoff_label': 'Copy stable-inbox handoff',
          'stable_inbox_handoff_copy':
              'Project Autopilot stable-inbox request handoff\nRequests:\n- Rank 2: project_ws/PM/IN/urgent.md',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasExpeditePressure, isTrue);
      expect(activity.expediteStableInboxChipLabel, '1 board inbox');
      expect(activity.stableInboxPriorityChipLabel, 'urgent inbox');
      expect(activity.stableInboxFreshnessChipLabel, 'stale board inbox');
      expect(activity.stableInboxCompletionContractChipLabel, 'proof contract');
      expect(activity.hasStableInboxCompletionContract, isTrue);
      expect(
        activity.stableInboxCompletionContractDetail,
        contains(
            'Completion contract: Deliverable: PM closes the older request'),
      );
      expect(
        activity.stableInboxCompletionContractDetail,
        contains('Success: PM cites this newer request path and SHA256'),
      );
      expect(
        activity.stableInboxCompletionContractDetail,
        contains('Safety: Do not mutate source'),
      );
      expect(activity.expediteRankChipLabel, 'board #2');
      expect(activity.hasStableInboxHandoff, isTrue);
      expect(activity.expediteOpenActionLabel, 'Open/copy latest inbox');
      expect(activity.expeditePrPostureChipLabel, isEmpty);
      expect(activity.expediteEvidenceRequiredChipLabel, isEmpty);
      expect(activity.stableInboxClassificationChipLabel, isEmpty);
      expect(activity.stableInboxClassificationDetail, isEmpty);
      expect(
        activity.expeditePrimaryOpenPath,
        'D:\\dev\\chili-home-copilot\\project_ws\\PM\\IN\\20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
      );
      expect(
          activity.expediteDecisionChipLabel, 'decide: accept/defer/escalate');
      expect(
        activity.expediteTopActionLabel,
        'Board #2: project_ws/PM/IN/urgent.md priority=Urgent; PM captain refresh needed',
      );
      expect(
        activity.stableInboxReviewDetail,
        'Stale inbox #2 | newer 2026-06-01T01:02:23Z | Urgent | AgentOps -> PM | PR134-CAPTAIN-REFRESH | Supersede earlier PR #134 refresh.',
      );
      expect(activity.attentionScore, greaterThan(26000));
      expect(activity.searchTerms, contains('1 board inbox'));
      expect(activity.searchTerms, contains('urgent inbox'));
      expect(activity.searchTerms, contains('stale board inbox'));
      expect(activity.searchTerms, contains('proof contract'));
      expect(activity.searchTerms, contains('stable_inbox_request'));
      expect(activity.searchTerms, contains('PM'));
      expect(activity.searchTerms, contains('project_ws/PM/IN/urgent.md'));
      expect(
        activity.searchTerms,
        contains(
          'project_ws/PM/IN/20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
        ),
      );
      expect(activity.searchTerms, contains('PR134-CAPTAIN-REFRESH'));
      expect(
        activity.searchTerms,
        contains(
          'Stale inbox #2 | newer 2026-06-01T01:02:23Z | Urgent | AgentOps -> PM | PR134-CAPTAIN-REFRESH | Supersede earlier PR #134 refresh.',
        ),
      );
      expect(
        activity.searchTerms,
        contains(activity.stableInboxCompletionContractDetail),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Project Autopilot stable-inbox request handoff'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
            '20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md'),
      );
    });

    test('marks live-money stable inbox packets as classification-only', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'stable_inbox_request_count': 1,
          'severity': 'high',
          'current_signal': '1 stable inbox request(s)',
          'next_action': 'Process this before routine work.',
          'top_rank': 3,
          'top_type': 'stable_inbox_request',
          'top_owner': 'PM',
          'top_evidence':
              'project_ws/PM/IN/20260601-091953Z-from-Risk-to-PM-pm070-uni-live-entry-knc-overfill-runtime-proof.md priority=Urgent; Risk requests PM/operator classification for a new PM-070 live-money evidence packet after the prior KNC closeout: UNI-USD alert 87151 was placed live.',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'stable_inbox_top_rank': 3,
          'stable_inbox_request_path':
              'project_ws/PM/IN/20260601-091953Z-from-Risk-to-PM-pm070-uni-live-entry-knc-overfill-runtime-proof.md',
          'stable_inbox_request_priority': 'Urgent',
          'stable_inbox_request_from': 'Risk',
          'stable_inbox_request_to': 'PM',
          'stable_inbox_request_backlog_id': 'PM-070',
          'stable_inbox_request_preview':
              'Risk requests PM/operator classification for a new PM-070 live-money evidence packet after a KNC overfill closeout.',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.expediteStableInboxChipLabel, '1 board inbox');
      expect(activity.stableInboxPriorityChipLabel, 'urgent inbox');
      expect(activity.stableInboxClassificationOnly, isTrue);
      expect(
          activity.stableInboxClassificationChipLabel, 'classification only');
      expect(
        activity.stableInboxClassificationDetail,
        contains(
          'Classification only: PM/operator may review evidence and record a disposition',
        ),
      );
      expect(
        activity.stableInboxClassificationDetail,
        contains('does not authorize runtime refresh'),
      );
      expect(activity.expediteDecisionChipLabel,
          'decide: classify/defer/escalate');
      expect(
        activity.expediteOwnerActionDetail,
        'Owner/action: PM | Process this before routine work | classification-only disposition required; no runtime, broker/API, or live-trading authority',
      );
      expect(
        activity.expediteSafeDecisionDetail,
        'Decision ladder: classify evidence only | defer with reason | escalate execution ambiguity.',
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
          'Classification boundary: Classification only: PM/operator may review evidence',
        ),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
          'Classify evidence: record the PM/operator disposition without changing runtime',
        ),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('does not authorize merge, release, deploy'),
      );
      expect(activity.searchTerms, contains('classification only'));
      expect(
        activity.searchTerms,
        contains(
          'Decision ladder: classify evidence only | defer with reason | escalate execution ambiguity.',
        ),
      );
      expect(
        activity.searchTerms,
        anyElement(contains('Risk requests PM/operator classification')),
      );
    });

    test('surfaces structured live-control gate on stable inbox bench rows',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'stable_inbox_request_count': 1,
          'severity': 'high',
          'current_signal': '1 stable inbox request(s)',
          'next_action': 'Process this before routine work.',
          'top_rank': 2,
          'top_type': 'stable_inbox_request',
          'top_owner': 'PM',
          'top_evidence':
              'project_ws/PM/IN/20260601-101435Z-from-Risk-to-PM-pm070-post-0927-live-order-actions.md priority=Urgent; live broker-action PM-070 evidence',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'stable_inbox_top_rank': 2,
          'stable_inbox_request_path':
              'project_ws/PM/IN/20260601-101435Z-from-Risk-to-PM-pm070-post-0927-live-order-actions.md',
          'stable_inbox_request_priority': 'Urgent',
          'stable_inbox_request_from': 'Risk',
          'stable_inbox_request_to': 'PM',
          'stable_inbox_request_backlog_id': 'PM-070',
          'stable_inbox_request_preview':
              'Risk reports live broker-action evidence for 00-USD and UP-USD.',
          'stable_inbox_live_broker_action_count': 1,
          'stable_inbox_live_broker_label': 'live broker proof-floor request',
          'stable_inbox_live_broker_detail':
              'Live broker proof-floor request: PM/operator boundary required before broker truth readback, source/runtime trust, PR readiness, release, or live behavior.',
          'stable_inbox_live_broker_proof_floor_utc': '2026-06-01T10:14:35Z',
          'stable_inbox_handoff_copy':
              'Project Autopilot stable-inbox request handoff\nLive-control gate:\n- project_ws/PM/IN/20260601-101435Z-from-Risk-to-PM-pm070-post-0927-live-order-actions.md: PM/operator boundary required before broker truth readback.\nBlocked action: do not mutate source, runtime, DB, broker/API, git, PR state, release posture, or live behavior from this copied handoff alone.',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.stableInboxHasLiveControlGate, isTrue);
      expect(activity.stableInboxClassificationOnly, isFalse);
      expect(activity.stableInboxClassificationChipLabel, isEmpty);
      expect(
        activity.stableInboxLiveControlGateChipLabel,
        '1 live-control gate',
      );
      expect(
        activity.stableInboxLiveControlGateDetail,
        contains('PM/operator boundary required'),
      );
      expect(
        activity.stableInboxLiveControlGateDetail,
        contains('broker truth readback'),
      );
      expect(activity.expediteDecisionChipLabel,
          'decide: classify/defer/escalate');
      expect(
        activity.expediteOwnerActionDetail,
        'Owner/action: PM | Process this before routine work | live-control disposition required; no runtime, broker/API, release, or live-trading authority',
      );
      expect(
        activity.expediteSafeDecisionDetail,
        'Decision ladder: classify live-control evidence | defer with reason | escalate execution ambiguity.',
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Live-control gate: Live broker proof-floor request'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Classify live-control evidence'),
      );
      expect(activity.searchTerms, contains('1 live-control gate'));
      expect(
        activity.searchTerms,
        anyElement(contains('broker truth readback')),
      );
    });

    test('surfaces control-plane proof gate on stable inbox bench rows', () {
      const targetOne = '019e6f30-1648-7921-b6ba-c49c58d0445a';
      const targetTwo = '019e6f77-704c-7141-babd-deb633fde065';
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'stable_inbox_request_count': 1,
          'severity': 'high',
          'current_signal': '1 stable inbox request(s)',
          'next_action': 'Process this before routine work.',
          'top_rank': 2,
          'top_type': 'stable_inbox_request',
          'top_owner': 'PM',
          'top_evidence':
              'project_ws/PM/IN/control-proof.md priority=Urgent; Route current control-plane stop proof for active quarantined target threads.',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'stable_inbox_top_rank': 2,
          'stable_inbox_request_path': 'project_ws/PM/IN/control-proof.md',
          'stable_inbox_request_priority': 'Urgent',
          'stable_inbox_request_from': 'AgentOps',
          'stable_inbox_request_to': 'PM',
          'stable_inbox_request_backlog_id': 'FASTLANE',
          'stable_inbox_request_preview':
              'Route current control-plane stop proof for active quarantined target threads.',
          'stable_inbox_control_plane_action_count': 1,
          'stable_inbox_control_plane_label': 'control-plane proof request',
          'stable_inbox_control_plane_detail':
              'Control-plane proof request for 2 target threads: $targetOne, $targetTwo. PM/operator/control-plane must stop, contain, or explicitly block the target thread(s), then record a quiet-window proof later than 2026-06-01T14:30:55Z before source/runtime trust can be restored.',
          'stable_inbox_control_plane_proof_floor_utc': '2026-06-01T14:30:55Z',
          'stable_inbox_control_plane_thread_ids': [targetOne, targetTwo],
          'stable_inbox_handoff_copy':
              'Project Autopilot stable-inbox request handoff\nControl-plane proof gate:\n- project_ws/PM/IN/control-proof.md: target(s) $targetOne, $targetTwo; proof later than 2026-06-01T14:30:55Z.\nBlocked control-plane action: do not stop sessions, clear quarantine, or restore source/runtime trust from this copied handoff alone.',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.stableInboxHasControlPlaneGate, isTrue);
      expect(activity.stableInboxClassificationOnly, isFalse);
      expect(activity.stableInboxClassificationChipLabel, isEmpty);
      expect(activity.stableInboxControlPlaneChipLabel, 'control-plane proof');
      expect(activity.stableInboxControlPlaneGateDetail,
          contains('2 target threads'));
      expect(activity.stableInboxControlPlaneGateDetail, contains(targetOne));
      expect(activity.stableInboxControlPlaneGateDetail,
          contains('2026-06-01T14:30:55Z'));
      expect(
        activity.expediteDecisionChipLabel,
        'decide: prove/terminate/block',
      );
      expect(
        activity.expediteOwnerActionDetail,
        contains('control-plane proof required'),
      );
      expect(
        activity.expediteSafeDecisionDetail,
        'Decision ladder: prove containment | terminate target | keep blocked.',
      );
      expect(activity.deliveryFocusChipLabel, 'delivery: PM proof');
      expect(activity.deliveryFocusLabel, 'Delivery focus: PM proof');
      expect(activity.deliveryFocusCritical, isTrue);
      expect(activity.hasDeliveryFocusTarget, isTrue);
      expect(activity.deliveryFocusRoutesToFlow, isFalse);
      expect(activity.deliveryFocusRoutesToExpedite, isTrue);
      expect(activity.deliveryFocusActionLabel, 'Open/copy PM proof');
      expect(
        activity.deliveryFocusPrimaryOpenPath,
        'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
      );
      expect(
        activity.deliveryFocusDetail,
        contains('process the PM control-plane proof request'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Control-plane proof gate'),
      );
      expect(activity.expediteBoardRowCopyText, contains('quiet-window proof'));
      expect(activity.searchTerms, contains('control-plane proof'));
      expect(activity.searchTerms, contains('delivery: PM proof'));
      expect(activity.searchTerms, contains(activity.deliveryFocusDetail));
      expect(activity.searchTerms, anyElement(contains(targetTwo)));
    });

    test('surfaces heartbeat delivery lag as repair work, not PR posture', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'open_pr_blocker_count': 9,
          'stable_inbox_request_count': 1,
          'severity': 'high',
          'current_signal': '9 open PR blocker(s), 1 heartbeat delivery lag',
          'next_action':
              'Process the oldest pending request or repair the owner heartbeat before routine work.',
          'top_rank': 2,
          'top_type': 'heartbeat_delivery_lag',
          'top_owner': 'DevOps',
          'top_evidence':
              '20260601-091724Z-from-PM-to-DevOps-pm071-runtime-adoption-proof-plan.md stable for 4.56m; automation=devops-release-engineer; target session stale 41.04m.',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'stable_inbox_top_rank': 4,
          'stable_inbox_request_path':
              'project_ws/DevOps/IN/20260601-134449Z-from-AgentOps-to-DevOps-pr-blocker-freshness-after-containment.md',
          'stable_inbox_request_priority': 'High',
          'stable_inbox_request_expected_deliverable':
              'Publish one DevOps OUT report with current PR blocker order.',
          'stable_inbox_request_success_criteria':
              'The report cites 10 open PRs checked, 9 blocked, review gaps 0, active quarantined targets 1.',
          'stable_inbox_request_safety':
              'Do not mutate source, branch, PR state, runtime, DB, broker, automation, release, or live-trading behavior.',
        },
      });

      expect(activity.expediteRankChipLabel, 'board #2');
      expect(activity.expediteOwnerActionChipLabel, 'DevOps next');
      expect(activity.expediteHeartbeatLagChipLabel, 'heartbeat lag');
      expect(activity.expediteHeartbeatInboxRouteChipLabel, 'heartbeat inbox');
      expect(activity.expeditePendingRequestChipLabel, 'process request');
      expect(activity.expediteHeartbeatRepairChipLabel, 'repair heartbeat');
      expect(activity.expeditePrPostureChipLabel, isEmpty);
      expect(activity.expediteEvidenceRequiredChipLabel, isEmpty);
      expect(
        activity.expediteOwnerActionDetail,
        'Owner/action: DevOps | Process the oldest pending request or repair the owner heartbeat before routine work | process oldest pending request or repair owner heartbeat before routine work',
      );
      expect(
        activity.expediteHeartbeatLagDetail,
        contains(
          'Heartbeat lag: owner session stale; process oldest pending request or repair owner heartbeat before routine work',
        ),
      );
      expect(
        activity.expediteHeartbeatLagDetail,
        contains(
          '20260601-091724Z-from-PM-to-DevOps-pm071-runtime-adoption-proof-plan.md stable for 4.56m',
        ),
      );
      expect(
        activity.expeditePendingRequestDetail,
        contains('close the pending stable inbox request'),
      );
      expect(
        activity.expediteHeartbeatInboxRouteDetail,
        contains('Open board #4'),
      );
      expect(
        activity.expediteHeartbeatInboxRouteDetail,
        contains('20260601-134449Z'),
      );
      expect(
        activity.expediteHeartbeatInboxRouteDetail,
        contains('before heartbeat repair'),
      );
      expect(
        activity.expeditePendingRequestDetail,
        contains('processed receipt evidence; use the proof contract'),
      );
      expect(
        activity.expeditePendingRequestDetail,
        contains('higher-ranked containment still blocks source/runtime trust'),
      );
      expect(
        activity.expeditePendingRequestDetail,
        contains('request 20260601-134449Z'),
      );
      expect(
        activity.expediteHeartbeatRepairDetail,
        contains('confirming no duplicate worker owns the lane'),
      );
      expect(
        activity.expediteDecisionChipLabel,
        'decide: process/repair/block',
      );
      expect(
        activity.expediteSafeDecisionDetail,
        'Decision ladder: process pending request | repair owner heartbeat | keep blocked or escalate.',
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
          'Heartbeat: Heartbeat lag: owner session stale; process oldest pending request or repair owner heartbeat before routine work',
        ),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Heartbeat inbox route: Open board #4'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Process pending request: have the owner close'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Repair heartbeat: reconnect or repair the owner heartbeat'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
            'Process request lane: Process request: close the pending stable inbox request'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
            'Heartbeat repair lane: Repair heartbeat: reconnect or repair the owner heartbeat only after confirming no duplicate worker owns the lane'),
      );
      expect(activity.searchTerms, contains('heartbeat lag'));
      expect(activity.searchTerms, contains('heartbeat inbox'));
      expect(activity.searchTerms,
          contains(activity.expediteHeartbeatInboxRouteDetail));
      expect(activity.searchTerms, contains('process request'));
      expect(activity.searchTerms, contains('repair heartbeat'));
      expect(activity.searchTerms,
          contains(activity.expeditePendingRequestDetail));
      expect(activity.searchTerms,
          contains(activity.expediteHeartbeatRepairDetail));
      expect(
        activity.searchTerms,
        contains(
          'Decision ladder: process pending request | repair owner heartbeat | keep blocked or escalate.',
        ),
      );
    });

    test('summarizes grouped heartbeat delivery lag rows', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'severity': 'high',
          'current_signal': '2 heartbeat delivery lag row(s)',
          'next_action':
              'Process each oldest pending request or repair owner heartbeats before routine work; keep containment and PR blockers visible while repairing stalled owner lanes.',
          'top_rank': 2,
          'top_type': 'heartbeat_delivery_lag_group',
          'top_owner': 'AgentOps / stalled owners',
          'top_evidence':
              '2 stalled owner lanes: AgentOps: request-a.md stable for 6.47m; automation=agentops-director; target session stale 35.19m; Frontend: request-b.md stable for 3.59m; automation=frontend-principal; target session stale 43.52m.',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
        },
      });

      expect(activity.expediteHeartbeatLagChipLabel, '2 heartbeat lags');
      expect(activity.expediteOwnerActionChipLabel, 'AgentOps next');
      expect(activity.expeditePrPostureChipLabel, isEmpty);
      expect(activity.expediteEvidenceRequiredChipLabel, isEmpty);
      expect(
        activity.expediteHeartbeatLagDetail,
        contains('2 stalled owner lanes'),
      );
      expect(
        activity.expediteDecisionChipLabel,
        'decide: process/repair/block',
      );
      expect(activity.searchTerms, contains('2 heartbeat lags'));
    });

    test('surfaces processed stable inbox receipts in bench rows', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'stable_inbox_request_count': 1,
          'severity': 'high',
          'current_signal': '1 stable inbox request(s)',
          'next_action': 'Process this before routine work.',
          'top_rank': 2,
          'top_type': 'stable_inbox_request',
          'top_owner': 'PM',
          'top_evidence':
              'project_ws/PM/IN/urgent.md priority=Urgent; PM captain refresh needed',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'stable_inbox_top_rank': 2,
          'stable_inbox_request_path': 'project_ws/PM/IN/urgent.md',
          'stable_inbox_request_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\PM\\IN\\urgent.md',
          'stable_inbox_request_priority': 'Urgent',
          'stable_inbox_request_from': 'AgentOps',
          'stable_inbox_request_to': 'PM',
          'stable_inbox_request_backlog_id': 'PR134-CAPTAIN-REFRESH',
          'stable_inbox_request_preview':
              'Refresh or re-enter Critical Path Captain Mode for PR #134.',
          'stable_inbox_request_stale_for_latest': true,
          'stable_inbox_latest_request_path':
              'project_ws/PM/IN/20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
          'stable_inbox_latest_request_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\PM\\IN\\20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
          'stable_inbox_latest_request_created': '2026-06-01T01:02:23Z',
          'stable_inbox_latest_request_priority': 'Urgent',
          'stable_inbox_latest_request_from': 'AgentOps',
          'stable_inbox_latest_request_to': 'PM',
          'stable_inbox_latest_request_backlog_id': 'PR134-CAPTAIN-REFRESH',
          'stable_inbox_latest_request_preview':
              'Supersede earlier PR #134 refresh.',
          'stable_inbox_latest_request_processed': true,
          'stable_inbox_latest_request_processed_utc': '2026-06-01T01:15:45Z',
          'stable_inbox_latest_request_processed_status':
              'satisfied_by_current_captain_correction_closeout',
          'stable_inbox_latest_request_processed_result':
              'PR134_CORRECTED_CAPTAIN_REQUEST_CLOSED_NO_NEW_CAPTAIN',
          'stable_inbox_latest_request_processed_report_path':
              'project_ws/PM/OUT/20260601-011545Z-pm-backstop-alpha-decay-and-pr134-correction.md',
          'stable_inbox_latest_request_processed_summary':
              'Corrected request is already closed by current captain evidence.',
          'stable_inbox_processed_detail':
              'processed 2026-06-01T01:15:45Z; satisfied_by_current_captain_correction_closeout; PR134_CORRECTED_CAPTAIN_REQUEST_CLOSED_NO_NEW_CAPTAIN',
          'stable_inbox_freshness_detail':
              'board row points at project_ws/PM/IN/urgent.md; newer request project_ws/PM/IN/20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
          'stable_inbox_latest_handoff_copy':
              'Project Autopilot stable-inbox request handoff\nRequests:\n- Rank ?: project_ws/PM/IN/20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
        },
      });

      expect(activity.stableInboxProcessed, isTrue);
      expect(activity.stableInboxProcessedChipLabel, 'processed inbox');
      expect(
        activity.stableInboxProcessedReviewDetail,
        contains('PR134_CORRECTED_CAPTAIN_REQUEST_CLOSED_NO_NEW_CAPTAIN'),
      );
      expect(
        activity.stableInboxReviewDetail,
        contains('Processed receipt'),
      );
      expect(activity.expediteOpenActionLabel, 'Copy inbox receipt');
      expect(activity.expeditePrimaryOpenPath, isEmpty);
      expect(activity.hasExpediteOpenTarget, isTrue);
      expect(
        activity.expediteBoardRowCopyText,
        contains('Project Autopilot processed stable-inbox receipt'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
          'Request: project_ws/PM/IN/20260601-010223Z-from-AgentOps-to-PM-pr134-captain-refresh-correction.md',
        ),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Safe action: review the processed receipt'),
      );
      expect(activity.searchTerms, contains('processed inbox'));
      expect(
        activity.searchTerms,
        contains('PR134_CORRECTED_CAPTAIN_REQUEST_CLOSED_NO_NEW_CAPTAIN'),
      );
      expect(
        activity.searchTerms,
        contains(
          'project_ws/PM/OUT/20260601-011545Z-pm-backstop-alpha-decay-and-pr134-correction.md',
        ),
      );
    });

    test('adds containment-safe decisions to copied board handoffs', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'control_plane_count': 1,
          'severity': 'high',
          'current_signal': '1 containment action(s)',
          'next_action':
              'Prove containment or termination before accepting source/runtime trust.',
          'top_rank': 1,
          'top_type': 'control_plane_containment',
          'top_owner': 'AgentOps / operator',
          'top_evidence': '1 quarantined target still active',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
        },
      });

      expect(activity.expediteOpenActionLabel, 'Open/copy board #1');
      expect(
          activity.expediteDecisionChipLabel, 'decide: prove/terminate/block');
      expect(activity.expediteBoardRowCopyText, contains('Rank: 1'));
      expect(
        activity.expediteBoardRowCopyText,
        contains('Prove containment: attach current quiet-window proof'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Terminate target: stop or archive the target'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Keep blocked: leave source/runtime trust blocked'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('does not authorize merge, release, deploy'),
      );
      expect(activity.searchTerms, contains('decide: prove/terminate/block'));
    });

    test('labels ready candidate board rows as review, not merge authority',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'ready_candidate_count': 1,
          'severity': 'warning',
          'current_signal': '1 ready-candidate lane(s): #111 ready-candidate',
          'next_action':
              'Ask PM/operator to accept, defer, or reject ready transition before author action.',
          'pr_numbers': ['111'],
          'top_rank': 11,
          'top_type': 'ready_candidate_still_draft',
          'top_owner': 'PM / owner',
          'top_evidence': 'PR #111 test:SUCCESS; merge=CLEAN; draft=True',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
        },
      });

      expect(activity.expediteReadyCandidateChipLabel, '1 board ready');
      expect(activity.expediteOwnerActionChipLabel, 'PM next');
      expect(
        activity.expediteOwnerActionDetail,
        'Owner/action: PM / owner | Ask PM/operator to accept, defer, or reject ready transition before author action | PM/operator acceptance required before promotion',
      );
      expect(activity.expeditePrPostureChipLabel, 'ready review');
      expect(activity.expeditePrPostureBlocked, isFalse);
      expect(
        activity.expeditePrPostureDetail,
        'PR posture: ready review; verify current-head checks and PM/operator acceptance before promoting | PR #111 test:SUCCESS; merge=CLEAN; draft=True.',
      );
      expect(activity.expediteDecisionChipLabel, 'decide: promote/defer/close');
      expect(
        activity.expediteSafeDecisionDetail,
        'Decision ladder: promote ready | defer behind blockers | reject or close.',
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
          'Owner action: Owner/action: PM / owner | Ask PM/operator to accept, defer, or reject ready transition before author action',
        ),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains(
          'Posture: PR posture: ready review; verify current-head checks and PM/operator acceptance before promoting',
        ),
      );
      expect(activity.searchTerms, contains('PM next'));
      expect(activity.searchTerms, contains('ready review'));
      expect(
        activity.searchTerms,
        contains(
          'PR posture: ready review; verify current-head checks and PM/operator acceptance before promoting | PR #111 test:SUCCESS; merge=CLEAN; draft=True.',
        ),
      );
      expect(activity.expediteEvidenceRequiredChipLabel, isEmpty);
      expect(activity.expediteEvidenceRequiredDetail, isEmpty);
    });

    test('classifies expedite board evidence requirements', () {
      AutopilotAgentBenchActivityPresentation activityFor(String evidence) =>
          AutopilotAgentBenchActivityPresenter.fromAgent({
            'expedite_lane_pressure': {
              'open_pr_blocker_count': 1,
              'severity': 'high',
              'current_signal': '1 open PR blocker(s)',
              'next_action': 'Assign owner.',
              'top_rank': 3,
              'top_type': 'open_pr_blocker',
              'top_owner': 'SSWE',
              'top_evidence': evidence,
            },
          });

      final failing = activityFor('PR #40 test:FAILURE; merge=DIRTY');
      expect(failing.expediteEvidenceRequiredChipLabel, 'failing CI');
      expect(failing.expediteEvidenceRequiredCritical, isTrue);
      expect(
        failing.expediteEvidenceRequiredDetail,
        contains('checks are failing'),
      );

      final dirty = activityFor('PR #134 merge=DIRTY; draft=False');
      expect(dirty.expediteEvidenceRequiredChipLabel, 'dirty merge');
      expect(dirty.expediteEvidenceRequiredCritical, isFalse);
      expect(
        dirty.expediteEvidenceRequiredDetail,
        contains('merge state is dirty'),
      );

      final unstable = activityFor('PR #131 merge=UNSTABLE; draft=True');
      expect(unstable.expediteEvidenceRequiredChipLabel, 'unstable merge');
      expect(
        unstable.expediteEvidenceRequiredDetail,
        contains('merge state is unstable'),
      );
    });

    test('puts fresh board blockers ahead of stale routine activity', () {
      final sorted = AutopilotAgentBenchActivityPresenter.sortAttentionFirst([
        {
          'name': 'Running Frontend',
          'active_run_count': 3,
        },
        {
          'name': 'PM Board Pressure',
          'expedite_lane_pressure': {
            'open_pr_blocker_count': 5,
          },
        },
      ]);

      expect(
        sorted.map((agent) => agent['name']),
        ['PM Board Pressure', 'Running Frontend'],
      );
    });

    test('summarizes live agent-flow lane pressure for bench chips and search',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'agent_flow_pressure': {
          'pending_count': 2,
          'stable_pending_count': 1,
          'urgent_count': 1,
          'captain_expired_count': 1,
          'shape_invalid_count': 1,
          'current_signal': '1 expired captain request, 1 urgent inbox request',
          'next_action':
              'PM has an expired PR captain refresh waiting before routine work.',
          'severity': 'high',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasFlowPressure, isTrue);
      expect(activity.flowCaptainChipLabel, '1 expired captain');
      expect(activity.flowUrgentChipLabel, '1 urgent inbox');
      expect(activity.flowStableChipLabel, '1 stable inbox');
      expect(activity.flowIssueChipLabel, '1 malformed');
      expect(activity.flowSeverity, 'high');
      expect(activity.attentionScore, greaterThan(20000));
      expect(activity.searchTerms, contains('1 expired captain'));
      expect(
        activity.searchTerms,
        contains(
            'PM has an expired PR captain refresh waiting before routine work.'),
      );
    });

    test('surfaces coordination drift from mailbox repairs and goal pressure',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'goal_health_pressure': {
          'automation': 'inspect-project-brain-ui',
          'status': 'PAUSED',
          'target_thread': '019e6b97-a3b9-79f0-bdf0-7f7c04383311',
          'goal': 'active',
          'tokens': '70607742',
          'control_risk': 'red',
          'pressure': 'red (tokens_used_70607742)',
          'severity': 'high',
          'next_action': 'containment_closeout_needed',
        },
        'agent_flow_pressure': {
          'pending_count': 5,
          'stable_pending_count': 2,
          'mailbox_malformed_correction_count': 2,
          'superseded_shape_invalid_count': 2,
          'paused_automation_count': 1,
          'repository_root_dirty_count': 605,
          'repository_root_change_count': 605,
          'repository_root_branch': 'main',
          'source_isolation_blocked': true,
          'current_signal':
              '2 builder/linter corrections, 1 paused automation target',
          'next_action':
              'Use mailbox builder/linter corrections before trusting handoffs.',
          'severity': 'high',
        },
      });

      expect(activity.hasFlowCoordinationDrift, isTrue);
      expect(activity.flowCoordinationDriftChipLabel, '2 builder fixes');
      expect(
        activity.flowCoordinationDriftLabel,
        contains('2 builder/linter corrections'),
      );
      expect(
        activity.flowCoordinationDriftDetail,
        contains(
            '2 superseded malformed requests recognized as non-actionable'),
      );
      expect(
          activity.flowCoordinationDriftDetail, contains('paused goal active'));
      expect(
        activity.flowCoordinationDriftDetail,
        contains('605 shared-root changes'),
      );
      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasBenchAttention, isTrue);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.attentionScore, greaterThan(25000));
      expect(activity.searchTerms, contains('2 builder fixes'));
      expect(
        activity.searchTerms,
        contains(activity.flowCoordinationDriftDetail),
      );
    });

    test('prioritizes mailbox builder corrections as a safety handoff', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'open_pr_blocker_count': 4,
          'top_rank': 1,
          'path': 'project_ws/AgentOps/PR_OWNER_READY_FIRST_BOARD.md',
          'open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\PR_OWNER_READY_FIRST_BOARD.md',
        },
        'agent_flow_pressure': {
          'pending_count': 5,
          'stable_pending_count': 2,
          'mailbox_malformed_correction_count': 2,
          'superseded_shape_invalid_count': 2,
          'current_signal':
              '2 builder/linter corrections, 2 superseded malformed requests',
          'next_action':
              'Acknowledge the bad SHA and require new-agent-mailbox-request plus mailbox lint.',
          'next_action_path':
              'project_ws/PM/IN/20260602-051137Z-from-AgentOps-to-PM-fix-malformed-outbound-mailbox-request.md',
          'next_action_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\PM\\IN\\20260602-051137Z-from-AgentOps-to-PM-fix-malformed-outbound-mailbox-request.md',
          'next_action_handoff_label': 'Copy mailbox builder/linter correction',
          'next_action_handoff_copy':
              'Project Autopilot mailbox builder/linter correction handoff\nRequired tools: scripts/new-agent-mailbox-request.ps1 and scripts/agent-mailbox-lint.ps1 -Path <request> -Json\nSafety boundary: review-only; no source, runtime, git, PR, DB, broker, release, or live behavior mutation authorized.',
          'next_action_handoff_mutates_control_plane': false,
          'severity': 'high',
        },
      });

      expect(activity.hasBenchSafety, isTrue);
      expect(activity.flowCoordinationDriftChipLabel, '2 builder fixes');
      expect(activity.shouldPrioritizeFlowSafetyAction, isTrue);
      expect(
        activity.flowSafetyPriorityReason,
        'Safety first: 2 mailbox builder fixes before PR work',
      );
      expect(activity.flowOpenActionLabel,
          'Copy mailbox builder/linter correction');
      expect(activity.flowSafetyActionLabel,
          'Copy mailbox builder/linter correction');
      expect(
        activity.benchActionStackLabels,
        [
          'Copy mailbox builder/linter correction',
          'Open/copy board #1',
        ],
      );
      expect(
        activity.benchActionStackLabel,
        'Next: Copy mailbox builder/linter correction | Then: Open/copy board #1',
      );
      expect(
        activity.flowActionCopyText,
        contains('Project Autopilot mailbox builder/linter correction handoff'),
      );
    });

    test('prioritizes active lock starvation as a safety handoff', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'open_pr_blocker_count': 3,
          'top_rank': 2,
          'path': 'project_ws/AgentOps/PR_OWNER_READY_FIRST_BOARD.md',
          'open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\PR_OWNER_READY_FIRST_BOARD.md',
        },
        'agent_flow_pressure': {
          'pending_count': 1,
          'stable_pending_count': 1,
          'active_lock_starvation_count': 1,
          'current_signal': '1 active lock warning',
          'next_action':
              'SDBA should release or close its own lock before routine work.',
          'next_action_path': 'project_ws/SDBA/OUT/_state/run.lock',
          'next_action_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\SDBA\\OUT\\_state\\run.lock',
          'next_action_handoff_label': 'Copy lock handoff',
          'next_action_handoff_copy':
              'Project Autopilot lock handoff\nOwner: SDBA\nPID running: yes\nSafety boundary: review-only; does not authorize source, runtime, git, PR, release, or live-behavior changes.',
          'next_action_handoff_mutates_control_plane': false,
          'severity': 'high',
        },
      });

      expect(activity.hasFlowPressure, isTrue);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.flowIssueChipLabel, '1 lock warning');
      expect(activity.flowOpenActionLabel, 'Copy lock handoff');
      expect(activity.flowSafetyActionLabel, 'Copy lock handoff');
      expect(activity.shouldPrioritizeFlowSafetyAction, isTrue);
      expect(
        activity.flowSafetyPriorityReason,
        'Safety first: active lock warning before PR work',
      );
      expect(activity.benchActionStackLabels, [
        'Copy lock handoff',
        'Open/copy board #2',
      ]);
      expect(
        activity.benchActionStackLabel,
        'Next: Copy lock handoff | Then: Open/copy board #2',
      );
      expect(
        activity.flowActionCopyText,
        contains('Project Autopilot lock handoff'),
      );
      expect(activity.flowNextActionHandoffMutatesControlPlane, isFalse);
      expect(activity.attentionScore, greaterThan(5000));
      expect(activity.searchTerms, contains('safety'));
      expect(activity.searchTerms, contains('1 lock warning'));
    });

    test('puts live captain pressure ahead of ordinary active chats', () {
      final sorted = AutopilotAgentBenchActivityPresenter.sortAttentionFirst([
        {
          'name': 'Running Frontend',
          'active_run_count': 3,
        },
        {
          'name': 'PM Captain',
          'agent_flow_pressure': {
            'captain_expired_count': 1,
            'stable_pending_count': 1,
          },
        },
      ]);

      expect(
        sorted.map((agent) => agent['name']),
        ['PM Captain', 'Running Frontend'],
      );
    });

    test('summarizes live containment pressure for bench chips and search', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'agent_flow_pressure': {
          'control_plane_blocker_count': 2,
          'quarantined_target_count': 1,
          'quarantine_thread_id': '019e6f30-1648-7921-b6ba-c49c58d0445a',
          'quarantine_status': 'CONTROL_PLANE_CONTAINMENT_REQUIRED',
          'quarantine_operator_label': 'Still active',
          'quarantine_required_proof':
              'No target-session writes for 5 minutes after containment proof.',
          'quarantine_operator_guidance':
              'Stop the target outside Autopilot before restoring trust.',
          'quarantine_session_age_minutes': 1,
          'quarantine_session_goal_status': 'active',
          'quarantine_session_path': 'sessions/2026/06/01/target.jsonl',
          'quarantine_activity_state': 'active',
          'quarantine_target_last_write_utc': '2026-06-01T07:24:09Z',
          'quarantine_proof_floor_utc': '2026-06-01T07:24:09Z',
          'quarantine_proof_window_minutes': 5,
          'quarantine_proof_remaining_minutes': 4,
          'quarantine_proof_next_check_utc': '2026-06-01T07:28:09Z',
          'quarantine_proof_satisfied': false,
          'paused_automation_count': 1,
          'paused_automation_id': 'chili-llm-cost-reduction-loop',
          'paused_automation_name': 'LLM Cost Reduction Loop',
          'paused_automation_status': 'PAUSED',
          'paused_automation_thread_id': '019e6f30-1648-7921-b6ba-c49c58d0445a',
          'paused_automation_session_age_minutes': 1,
          'paused_automation_threshold_minutes': 30,
          'paused_automation_goal_status': 'active',
          'paused_automation_session_path': 'sessions/2026/06/01/target.jsonl',
          'paused_automation_guidance':
              'A paused schedule should not keep writing while PAUSED.',
          'paused_automation_covered_by_quarantine': true,
          'paused_automation_operator_handoff_label': 'Copy pause handoff',
          'paused_automation_operator_handoff_copy':
              'Project Autopilot paused-automation handoff',
          'current_signal': '1 containment blocker, 1 paused automation target',
          'next_action':
              'Prove containment before accepting source/runtime trust.',
          'next_action_path':
              'project_ws/AgentOps/TARGET_THREAD_QUARANTINE.json',
          'severity': 'high',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasFlowPressure, isTrue);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.flowContainmentChipLabel, '1 containment');
      expect(activity.flowQuarantineTrustStateChipLabel, 'trust blocked');
      expect(activity.flowQuarantineTrustBlocked, isTrue);
      expect(
        activity.flowQuarantineTrustStateDetail,
        'Trust state: blocked; 4m quiet proof window remains before review can start.',
      );
      expect(activity.flowQuarantineProofChipLabel, '4m proof left');
      expect(activity.flowQuarantineProofReady, isFalse);
      expect(activity.flowQuarantineProofWaiting, isTrue);
      expect(activity.flowSafetyActionLabel, 'Wait 4m');
      expect(activity.flowSafetyActionEnabled, isFalse);
      expect(activity.benchActionStackLabels, ['Wait 4m']);
      expect(
        activity.flowQuarantineProofRefreshDetail,
        'Auto-refresh proof at 2026-06-01T07:28:09Z.',
      );
      expect(
        activity.flowQuarantineProofDetail,
        contains(
          'wait 4m target 019e6f30 | CONTROL PLANE CONTAINMENT REQUIRED | Still active',
        ),
      );
      expect(
        activity.flowQuarantineProofFloorDetail,
        'Proof floor: target wrote 1m ago | containment must be later than 2026-06-01T07:24:09Z | active | goal active | session sessions/2026/06/01/target.jsonl',
      );
      expect(
        activity.flowQuarantineClearanceChecklistDetail,
        'Clearance checklist: wait 4m quiet window | proof later than 2026-06-01T07:24:09Z | confirm target stopped/contained | confirm no watcher/tool session remains | PM/operator disposition current before trust',
      );
      expect(
        activity.flowQuarantineRequirementDetail,
        'Required proof: No target-session writes for 5 minutes after containment proof.',
      );
      expect(
        activity.flowQuarantineGuidanceDetail,
        'Operator guidance: Stop the target outside Autopilot before restoring trust.',
      );
      expect(
        activity.flowActionCopyText,
        contains(
          'Trust state: blocked; 4m quiet proof window remains before review can start.',
        ),
      );
      expect(
        activity.flowActionCopyText,
        contains(
          'Required proof: No target-session writes for 5 minutes after containment proof.',
        ),
      );
      expect(
        activity.flowActionCopyText,
        contains(
          'Proof floor: containment must be later than 2026-06-01T07:24:09Z',
        ),
      );
      expect(
        activity.flowActionCopyText,
        contains('Clearance checklist:'),
      );
      expect(
        activity.flowActionCopyText,
        contains(
          '1. Do not clear quarantine yet; wait 4m of quiet proof window.',
        ),
      );
      expect(
        activity.flowActionCopyText,
        contains(
          '5. Keep source/runtime trust blocked until PM/operator disposition and registry evidence are current.',
        ),
      );
      expect(
        activity.flowActionCopyText,
        contains('Session path: sessions/2026/06/01/target.jsonl'),
      );
      expect(
        activity.flowActionCopyText,
        contains(
          'Operator guidance: Stop the target outside Autopilot before restoring trust.',
        ),
      );
      expect(activity.flowPausedAutomationChipLabel, '1 paused automation');
      expect(
        activity.flowPausedAutomationDetail,
        'Paused automation: LLM Cost Reduction Loop | chili-llm-cost-reduction-loop | PAUSED | target 019e6f30 | wrote 1m ago | threshold 30m | goal active | already quarantined',
      );
      expect(
        activity.flowPausedAutomationGuidanceDetail,
        'Paused automation guidance: A paused schedule should not keep writing while PAUSED.',
      );
      expect(activity.flowSeverity, 'high');
      expect(activity.attentionScore, greaterThan(20000));
      expect(activity.searchTerms, contains('safety'));
      expect(activity.searchTerms, contains('1 containment'));
      expect(activity.searchTerms, contains('trust blocked'));
      expect(
        activity.searchTerms,
        contains(
          'Trust state: blocked; 4m quiet proof window remains before review can start.',
        ),
      );
      expect(activity.searchTerms, contains('4m proof left'));
      expect(
        activity.searchTerms,
        contains('chili-llm-cost-reduction-loop'),
      );
      expect(
        activity.searchTerms,
        contains('A paused schedule should not keep writing while PAUSED.'),
      );
      expect(
        activity.searchTerms,
        contains(
          'Required proof: No target-session writes for 5 minutes after containment proof.',
        ),
      );
      expect(
        activity.searchTerms,
        contains(
          'Proof floor: target wrote 1m ago | containment must be later than 2026-06-01T07:24:09Z | active | goal active | session sessions/2026/06/01/target.jsonl',
        ),
      );
      expect(
        activity.searchTerms,
        contains(
          'Clearance checklist: wait 4m quiet window | proof later than 2026-06-01T07:24:09Z | confirm target stopped/contained | confirm no watcher/tool session remains | PM/operator disposition current before trust',
        ),
      );
      expect(
        activity.searchTerms,
        contains('Prove containment before accepting source/runtime trust.'),
      );
    });

    test('marks live containment proof windows ready without trust authority',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'agent_flow_pressure': {
          'control_plane_blocker_count': 1,
          'quarantined_target_count': 1,
          'quarantine_thread_id': '019e6f30-1648-7921-b6ba-c49c58d0445a',
          'quarantine_status': 'CONTROL_PLANE_CONTAINMENT_REQUIRED',
          'quarantine_session_age_minutes': 5,
          'quarantine_activity_state': 'proof_window_satisfied',
          'quarantine_target_last_write_utc': '2026-06-01T07:24:09Z',
          'quarantine_proof_floor_utc': '2026-06-01T07:24:09Z',
          'quarantine_proof_window_minutes': 5,
          'quarantine_proof_remaining_minutes': 0,
          'quarantine_proof_satisfied': true,
          'next_action_path':
              'project_ws/AgentOps/TARGET_THREAD_QUARANTINE.json',
        },
      });

      expect(activity.hasBenchSafety, isTrue);
      expect(activity.flowQuarantineTrustStateChipLabel, 'proof review');
      expect(activity.flowQuarantineTrustBlocked, isFalse);
      expect(
        activity.flowQuarantineTrustStateDetail,
        'Trust state: proof review only; source/runtime trust stays blocked until registry evidence and PM/operator disposition are current.',
      );
      expect(activity.flowQuarantineProofChipLabel, 'proof ready');
      expect(activity.flowQuarantineProofReady, isTrue);
      expect(activity.flowQuarantineProofWaiting, isFalse);
      expect(activity.flowSafetyActionLabel, 'Review registry');
      expect(activity.flowSafetyActionEnabled, isTrue);
      expect(activity.flowQuarantineProofRefreshDetail, isEmpty);
      expect(
        activity.flowQuarantineProofDetail,
        contains('review registry before clearing source/runtime trust'),
      );
      expect(
        activity.flowQuarantineProofFloorDetail,
        contains(
          'Proof floor met: target wrote 5m ago | containment must be later than 2026-06-01T07:24:09Z',
        ),
      );
      expect(
        activity.flowQuarantineClearanceChecklistDetail,
        contains(
          'Clearance checklist: review registry evidence | proof later than 2026-06-01T07:24:09Z',
        ),
      );
      expect(
        activity.flowActionCopyText,
        contains(
          'Trust state: proof review only; source/runtime trust stays blocked until registry evidence and PM/operator disposition are current.',
        ),
      );
      expect(
        activity.flowActionCopyText,
        contains(
          '1. Review registry evidence before clearing source/runtime trust.',
        ),
      );
      expect(activity.searchTerms, contains('proof review'));
      expect(activity.searchTerms, contains('proof ready'));
    });

    test('names grouped live quarantine pressure in bench copy', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'agent_flow_pressure': {
          'control_plane_blocker_count': 2,
          'quarantined_target_count': 2,
          'quarantine_thread_id': '019e6f30-1648-7921-b6ba-c49c58d0445a',
          'quarantine_status': 'CONTROL_PLANE_CONTAINMENT_REQUIRED',
          'quarantine_operator_label': 'Still active',
          'quarantine_required_proof':
              'No target-session writes for 5 minutes after containment proof.',
          'quarantine_operator_guidance':
              'Terminate or contain both target threads before restoring trust.',
          'quarantine_target_last_write_utc': '2026-06-01T12:08:12Z',
          'quarantine_proof_floor_utc': '2026-06-01T12:08:12Z',
          'quarantine_proof_window_minutes': 5,
          'quarantine_proof_remaining_minutes': 4,
          'quarantine_proof_satisfied': false,
          'next_action_path':
              'project_ws/AgentOps/TARGET_THREAD_QUARANTINE.json',
        },
      });

      expect(activity.flowContainmentChipLabel, '2 containment');
      expect(
        activity.flowSafetyPriorityReason,
        'Safety first: 2 active quarantines blocks PR work',
      );
      expect(
        activity.flowQuarantineProofDetail,
        contains('top target 019e6f30 of 2 active'),
      );
      expect(
        activity.flowQuarantineClearanceChecklistDetail,
        contains('confirm all 2 targets stopped/contained'),
      );
      expect(
        activity.flowActionCopyText,
        contains(
          'Confirm all 2 quarantined targets, including top target thread 019e6f30-1648-7921-b6ba-c49c58d0445a are stopped or contained',
        ),
      );
      expect(activity.searchTerms, contains('2 containment'));
      expect(
        activity.searchTerms,
        contains(
          'Clearance checklist: wait 4m quiet window | proof later than 2026-06-01T12:08:12Z | confirm all 2 targets stopped/contained | confirm no watcher/tool session remains | PM/operator disposition current before trust',
        ),
      );
    });

    test(
        'summarizes live worktree isolation pressure for bench chips and search',
        () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'open_pr_blocker_count': 9,
          'top_rank': 2,
          'top_type': 'open_pr_blocker',
          'top_evidence':
              'PR #134 no checks; merge=DIRTY; branch=codex/brain-work',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_EXPEDITE_BOARD.md',
        },
        'agent_flow_pressure': {
          'control_plane_blocker_count': 2,
          'worktree_hygiene_count': 2,
          'dirty_worktree_count': 1,
          'detached_uncontained_worktree_count': 1,
          'worktree_change_count': 487,
          'repository_root_dirty_count': 1,
          'repository_root_change_count': 486,
          'repository_root_branch':
              'codex/brain-work-done-marker-recovery...origin/codex/brain-work-done-marker-recovery [ahead 4, behind 151]',
          'source_isolation_blocked': true,
          'current_signal':
              '1 detached worktree, 1 dirty worktree, 486 shared-root source changes, 1 worktree change',
          'next_action':
              'Resolve branch-delivery hygiene before trusting source/runtime evidence.',
          'next_action_path':
              'project_ws/AgentOps/AGENT_WORKSPACE_ISOLATION_MODE.md',
          'next_action_open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\AGENT_WORKSPACE_ISOLATION_MODE.md',
          'next_action_handoff_label': 'Copy worktree hygiene handoff',
          'next_action_handoff_copy':
              'Project Autopilot worktree hygiene handoff\nInstruction: bind detached worktrees before trusting downstream evidence.\n\nPermission boundary: source review only. This copied handoff does not authorize source edits or worktree cleanup.',
          'next_action_handoff_mutates_control_plane': false,
          'severity': 'high',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasFlowPressure, isTrue);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.flowWorktreeIssueCount, 2);
      expect(activity.hasFlowSourceIsolation, isTrue);
      expect(activity.flowSourceIsolationChipLabel, 'source blocked');
      expect(activity.flowSourceTrustChipLabel, '486 root changes');
      expect(
        activity.flowSourceTrustEvidenceLabel,
        'needs Worktree/branch/HEAD evidence',
      );
      expect(
        activity.flowSourceIsolationLabel,
        'Source blocked: 486 shared-root changes | codex/brain-work-done-marker-recovery...origin/codex/brain-work-done-...',
      );
      expect(
        activity.flowSourceTrustDetail,
        'Source trust gate: 486 shared-root changes | branch codex/brain-work-done-marker-recovery...origin/codex/brain-work-done-... | needs Worktree/branch/HEAD evidence',
      );
      expect(activity.flowWorktreeChipLabel, '487 worktree changes');
      expect(activity.hasExpediteOpenTarget, isTrue);
      expect(activity.expediteOpenActionLabel, 'Open/copy board #2');
      expect(activity.hasFlowActionTarget, isTrue);
      expect(activity.shouldPrioritizeFlowSafetyAction, isTrue);
      expect(activity.deliveryFocusChipLabel, 'delivery: containment');
      expect(activity.hasDeliveryFocusTarget, isTrue);
      expect(activity.deliveryFocusRoutesToFlow, isTrue);
      expect(activity.deliveryFocusRoutesToExpedite, isFalse);
      expect(
        activity.deliveryFocusActionLabel,
        'Copy worktree hygiene handoff',
      );
      expect(
        activity.flowSafetyPriorityReason,
        'Safety first: 486 shared-root changes block PR work',
      );
      expect(activity.benchActionStackLabels, [
        'Copy worktree hygiene handoff',
        'Open/copy board #2',
      ]);
      expect(
        activity.benchActionStackLabel,
        'Next: Copy worktree hygiene handoff | Then: Open/copy board #2',
      );
      expect(activity.flowActionPermissionBoundaryLabel, 'source review only');
      expect(activity.permissionBoundaryChipLabel, 'source review only');
      expect(
        activity.permissionBoundaryStackLabel,
        'Boundary: Copy worktree hygiene handoff -> source review only',
      );
      expect(activity.flowOpenActionLabel, 'Copy worktree hygiene handoff');
      expect(
        activity.flowPrimaryOpenPath,
        'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\AGENT_WORKSPACE_ISOLATION_MODE.md',
      );
      expect(
        activity.deliveryFocusPrimaryOpenPath,
        'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\AGENT_WORKSPACE_ISOLATION_MODE.md',
      );
      expect(
        activity.flowActionCopyText,
        contains('Project Autopilot worktree hygiene handoff'),
      );
      expect(activity.flowNextActionHandoffMutatesControlPlane, isFalse);
      expect(activity.flowSeverity, 'high');
      expect(activity.attentionScore, greaterThan(17000));
      expect(activity.searchTerms, contains('safety'));
      expect(activity.searchTerms, contains('source blocked'));
      expect(
        activity.searchTerms,
        contains(
          'Source blocked: 486 shared-root changes | codex/brain-work-done-marker-recovery...origin/codex/brain-work-done-...',
        ),
      );
      expect(activity.searchTerms, contains('486 root changes'));
      expect(
        activity.searchTerms,
        contains(
          'Source trust gate: 486 shared-root changes | branch codex/brain-work-done-marker-recovery...origin/codex/brain-work-done-... | needs Worktree/branch/HEAD evidence',
        ),
      );
      expect(
        activity.searchTerms,
        contains('needs Worktree/branch/HEAD evidence'),
      );
      expect(activity.searchTerms, contains('487 worktree changes'));
      expect(
        activity.searchTerms,
        contains('Safety first: 486 shared-root changes block PR work'),
      );
      expect(
        activity.searchTerms,
        contains(
          'Next: Copy worktree hygiene handoff | Then: Open/copy board #2',
        ),
      );
      expect(activity.searchTerms, contains('source review only'));
      expect(
        activity.searchTerms,
        contains(
          'Boundary: Copy worktree hygiene handoff -> source review only',
        ),
      );
      expect(
        activity.searchTerms,
        contains('Copy worktree hygiene handoff'),
      );
      expect(
        activity.searchTerms,
        contains(
          'Resolve branch-delivery hygiene before trusting source/runtime evidence.',
        ),
      );
    });

    test('synthesizes PR preflight blockers across board and source gates', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'open_pr_blocker_count': 9,
          'severity': 'high',
          'top_rank': 7,
          'top_type': 'open_pr_blocker',
          'top_owner': 'PM / operator / SSWE',
          'top_evidence':
              'PR #134 no checks; merge=DIRTY; draft=False; branch=codex/brain-work-done-marker-recovery',
          'next_action': 'Keep this PR frozen; do not repair it in place.',
          'path': 'project_ws/AgentOps/OMNIAGENT_EXPEDITE_BOARD.md',
          'open_path':
              'D:\\dev\\chili-home-copilot\\project_ws\\AgentOps\\OMNIAGENT_EXPEDITE_BOARD.md',
        },
        'agent_flow_pressure': {
          'worktree_hygiene_count': 1,
          'dirty_worktree_count': 1,
          'worktree_merge_conflict_count': 1,
          'worktree_change_count': 3,
          'repository_root_dirty_count': 1,
          'repository_root_change_count': 3,
          'repository_root_merge_conflict_count': 1,
          'repository_root_branch': 'main [ahead 1]',
          'source_isolation_blocked': true,
          'current_signal': '1 shared-root merge conflict',
          'next_action':
              'Resolve unmerged git conflict entries before PR prep.',
          'next_action_path':
              'project_ws/AgentOps/AGENT_WORKSPACE_ISOLATION_MODE.md',
          'severity': 'high',
        },
      });

      expect(activity.hasPrPreflightPressure, isTrue);
      expect(activity.prPreflightBlocked, isTrue);
      expect(activity.prPreflightChipLabel, 'PR preflight blocked');
      expect(activity.prDeliveryRouteChipLabel, 'clean rebuild only');
      expect(activity.prDeliveryRouteHighRisk, isTrue);
      expect(
        activity.prDeliveryRouteDetail,
        contains('keep the current PR frozen; do not repair it in place'),
      );
      expect(
        activity.prDeliveryRouteDetail,
        contains('clean owner worktree after PM/operator acceptance'),
      );
      expect(activity.hasPrRecoveryContract, isTrue);
      expect(activity.prRecoveryContractChipLabel, 'PR recovery contract');
      expect(
        activity.prRecoveryContractLabel,
        'PR #134 recovery: clean rebuild only',
      );
      expect(
        activity.prRecoveryContractDetail,
        contains('PM/operator must accept close/recreate'),
      );
      expect(
        activity.prRecoveryContractDetail,
        contains('do not repair it in place'),
      );
      expect(
        activity.prRecoveryContractCopyText,
        contains('Allowed decisions:'),
      );
      expect(
        activity.prRecoveryContractCopyText,
        contains('Rebuild only through a clean owner worktree'),
      );
      expect(
        activity.prPreflightLabel,
        'PR preflight blocked: board #7, PR #134, 9 board PR blockers',
      );
      expect(
        activity.prPreflightDetail,
        contains('blockers 1 shared-root merge conflict'),
      );
      expect(activity.prPreflightDetail, contains('1 merge conflict'));
      expect(
        activity.prPreflightDetail,
        contains('needs conflict-free Worktree/branch/HEAD evidence'),
      );
      expect(
        activity.prPreflightDetail,
        contains('Resolve unmerged git conflict entries before PR prep.'),
      );
      expect(
        activity.prPreflightDetail,
        contains('PR #134 no checks; merge=DIRTY'),
      );
      expect(activity.prPreflightDetail,
          contains('PR delivery route for PR #134'));
      expect(
        activity.expediteBoardRowCopyText,
        contains('Preflight: PR preflight blocked'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Delivery route: PR delivery route for PR #134'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Recovery contract: PR recovery for PR #134'),
      );
      expect(activity.searchTerms, contains('PR preflight blocked'));
      expect(activity.searchTerms, contains('clean rebuild only'));
      expect(activity.searchTerms, contains(activity.prDeliveryRouteDetail));
      expect(activity.searchTerms, contains(activity.prRecoveryContractDetail));
      expect(
        activity.searchTerms,
        contains(
          'PR preflight blocked: board #7, PR #134, 9 board PR blockers',
        ),
      );
      expect(activity.searchTerms, contains(activity.prPreflightDetail));
      expect(activity.attentionScore, greaterThan(35000));
    });

    test('surfaces KPI PR blocker proof floor on bench preflight', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'kpi_lane_pressure': {
          'blocked_pr_count': 3,
          'score': 1.4,
          'severity': 'high',
          'current_signal':
              '3 blocked PR lane(s): #134 ci_missing_checks, #132 ci_failing, #131 ci_failing',
          'next_action': 'Process oldest blocker before routine work.',
          'pr_numbers': ['134', '132', '131'],
          'pr_blocker_dirty_count': 1,
          'pr_blocker_no_checks_count': 1,
          'pr_blocker_failing_count': 2,
          'pr_blocker_non_draft_count': 1,
          'pr_blocker_top_pr': '134',
          'pr_blocker_top_branch': 'codex/brain-work-done-marker-recovery',
          'pr_blocker_top_merge': 'DIRTY',
          'pr_blocker_top_ci': 'no checks',
          'pr_blocker_top_posture': 'Blocked: ci_missing_checks',
          'pr_blocker_proof_floor':
              'PR blocker proof floor: 3 blocked PR(s), 1 dirty, 1 missing checks, 2 failing CI, 1 non-draft; top PR #134; CI no checks; merge DIRTY; branch codex/brain-work-done-marker-recovery; Blocked: ci_missing_checks. Required proof before PR movement: current head SHA, branch, clean owner worktree or owner path, focused check evidence, and PM/operator disposition for dirty, no-check, non-draft, or frozen PRs.',
          'pr_blocker_gate_state': 'blocked_until_current_head_proof',
          'pr_blocker_gate_label': 'Blocked until PR proof',
          'pr_blocker_allowed_decisions': [
            'Keep blocked with owner, next check path, and blocker evidence.',
            'Close or recreate only after explicit PM/operator acceptance.',
            'Clean owner-worktree rebuild with branch, worktree, and current-head proof.',
            'Run one named owner-repair path with focused check evidence.',
          ],
          'pr_blocker_required_proof':
              'Current head SHA, branch, clean owner worktree or owner path, focused check evidence, and PM/operator disposition for dirty, no-check, non-draft, or frozen PRs.',
          'pr_blocker_blocked_action':
              'Do not mutate PR state, close/recreate, merge, push, or start broad source repair from this lane until the proof floor is attached.',
        },
      });

      expect(activity.hasPrPreflightPressure, isTrue);
      expect(activity.prPreflightBlocked, isTrue);
      expect(activity.kpiBlockedPrChipLabel, '3 blocked PRs');
      expect(
        activity.kpiPrBlockerProofChipLabel,
        'proof 1 no-check, 1 dirty, 2 failing',
      );
      expect(activity.kpiPrBlockerGateChipLabel, 'Blocked until PR proof');
      expect(
        activity.kpiPrBlockerDecisionDetail,
        contains('PR blocker decision gate'),
      );
      expect(
        activity.kpiPrBlockerDecisionDetail,
        contains('blocked until current head proof'),
      );
      expect(
        activity.kpiPrBlockerDecisionDetail,
        contains('Clean owner-worktree rebuild'),
      );
      expect(
        activity.kpiPrBlockerDecisionMenuCopyText,
        contains('Decision gate: Blocked until PR proof'),
      );
      expect(
        activity.kpiPrBlockerDecisionMenuCopyText,
        contains('Allowed decisions:'),
      );
      expect(
        activity.kpiPrBlockerTopDetail,
        'PR #134; CI no checks; merge DIRTY; branch codex/brain-work-done-marker-recovery; Blocked: ci_missing_checks',
      );
      expect(
        activity.kpiPrBlockerProofFloorDetail,
        contains('PR blocker proof floor'),
      );
      expect(
        activity.kpiPrBlockerProofFloorDetail,
        contains('current head SHA'),
      );
      expect(
        activity.kpiPrBlockerProofFloorDetail,
        contains('Blocked action: Do not mutate PR state'),
      );
      expect(activity.hasPrRecoveryContract, isTrue);
      expect(
        activity.prRecoveryContractCopyText,
        contains('Proof floor: PR blocker proof floor'),
      );
      expect(
        activity.prRecoveryContractCopyText,
        contains('Decision gate: Blocked until PR proof'),
      );
      expect(
        activity.prRecoveryContractCopyText,
        contains('Top blocker: PR #134; CI no checks; merge DIRTY'),
      );
      expect(
        activity.prRecoveryContractCopyText,
        contains('Blocked action: Do not mutate PR state'),
      );
      expect(
        activity.prPreflightLabel,
        'PR preflight blocked: PR #134, 3 scorecard PR blockers',
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Proof floor: PR blocker proof floor'),
      );
      expect(activity.prPreflightDetail, contains('PR blocker proof floor'));
      expect(activity.prPreflightDetail, contains('PM/operator disposition'));
      expect(activity.deliveryFocusChipLabel, 'delivery: PR blocked');
      expect(activity.hasDeliveryFocusTarget, isTrue);
      expect(activity.deliveryFocusRoutesToPrBlockerPacket, isTrue);
      expect(activity.deliveryFocusRoutesToExpedite, isFalse);
      expect(activity.deliveryFocusPrimaryOpenPath, isEmpty);
      expect(activity.deliveryFocusActionLabel, 'Copy PR proof packet');
      expect(
        activity.prBlockerProofFloorCopyText,
        contains('Decision gate: Blocked until PR proof'),
      );
      expect(
        activity.searchTerms,
        contains('proof 1 no-check, 1 dirty, 2 failing'),
      );
      expect(activity.searchTerms, contains('Copy PR proof packet'));
      expect(activity.searchTerms, contains('Blocked until PR proof'));
      expect(
        activity.searchTerms,
        contains('blocked_until_current_head_proof'),
      );
      expect(
        activity.searchTerms,
        contains(
          'PR #134; CI no checks; merge DIRTY; branch codex/brain-work-done-marker-recovery; Blocked: ci_missing_checks',
        ),
      );
    });

    test('routes owner-ready conflict PRs to isolated worktree evidence', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'expedite_lane_pressure': {
          'owner_ready_first_count': 1,
          'owner_ready_first_top_rank': 9,
          'owner_ready_first_pr': '#40 Q1.T1.7 closeout - CPCV readiness docs',
          'owner_ready_first_pr_number': '40',
          'owner_ready_first_state':
              'conflict_resolution_owner_first; draft=False; merge=DIRTY; ci=failing',
          'owner_ready_first_required_lanes': 'DevOps, PM',
          'owner_ready_first_next_action':
              'PM owns the conflict path: resolve or publish cannot-resolve evidence from an isolated PR worktree, then coordinate DevOps for CI/review refresh.',
          'owner_ready_first_lane_role': 'owner',
          'owner_ready_first_path':
              'project_ws/AgentOps/PR_OWNER_READY_FIRST_BOARD.md',
        },
      });

      expect(activity.hasPrPreflightPressure, isTrue);
      expect(activity.ownerReadyFirstChipLabel, 'owner-ready PR');
      expect(activity.prDeliveryRouteChipLabel, 'isolated worktree');
      expect(activity.prDeliveryRouteHighRisk, isFalse);
      expect(
        activity.prDeliveryRouteDetail,
        contains('resolve or publish cannot-resolve evidence'),
      );
      expect(
        activity.prDeliveryRouteDetail,
        contains('isolated PR worktree'),
      );
      expect(
        activity.prDeliveryRouteDetail,
        contains('coordinate required support lanes for CI/review refresh'),
      );
      expect(activity.hasPrRecoveryContract, isTrue);
      expect(activity.prRecoveryContractChipLabel, 'PR recovery contract');
      expect(
        activity.prRecoveryContractLabel,
        'PR #40 recovery: isolated worktree',
      );
      expect(
        activity.prRecoveryContractDetail,
        contains('cannot-resolve evidence from an isolated PR worktree'),
      );
      expect(
        activity.prRecoveryContractDetail,
        contains('No PR-state movement until PM/operator accepts'),
      );
      expect(
        activity.expediteBoardRowCopyText,
        contains('Recovery contract: PR recovery for PR #40'),
      );
      expect(activity.searchTerms, contains('isolated worktree'));
      expect(activity.searchTerms, contains(activity.prDeliveryRouteDetail));
      expect(activity.searchTerms, contains(activity.prRecoveryContractDetail));
    });

    test('surfaces merge conflicts as PR preflight blockers in bench rows', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'agent_flow_pressure': {
          'control_plane_blocker_count': 1,
          'worktree_hygiene_count': 1,
          'dirty_worktree_count': 1,
          'worktree_merge_conflict_count': 1,
          'worktree_change_count': 3,
          'repository_root_dirty_count': 1,
          'repository_root_change_count': 3,
          'repository_root_merge_conflict_count': 1,
          'repository_root_branch': 'main [ahead 1]',
          'source_isolation_blocked': true,
          'current_signal': '1 shared-root merge conflict',
          'next_action':
              'Resolve unmerged git conflict entries before PR prep.',
          'next_action_path':
              'project_ws/AgentOps/AGENT_WORKSPACE_ISOLATION_MODE.md',
          'severity': 'high',
        },
      });

      expect(activity.hasOperatorInput, isTrue);
      expect(activity.hasFlowPressure, isTrue);
      expect(activity.hasBenchSafety, isTrue);
      expect(activity.flowWorktreeChipLabel, '1 merge conflict');
      expect(activity.flowSourceTrustChipLabel, '1 root conflict');
      expect(
        activity.flowSourceTrustEvidenceLabel,
        'needs conflict-free Worktree/branch/HEAD evidence',
      );
      expect(
        activity.flowSourceIsolationLabel,
        'Source blocked: 1 shared-root merge conflict | main [ahead 1]',
      );
      expect(
        activity.flowSourceTrustDetail,
        'Source trust gate: 1 shared-root merge conflict | branch main [ahead 1] | needs conflict-free Worktree/branch/HEAD evidence',
      );
      expect(activity.hasPrPreflightPressure, isTrue);
      expect(activity.prPreflightBlocked, isTrue);
      expect(activity.prPreflightReadyCandidate, isFalse);
      expect(activity.prPreflightChipLabel, 'PR preflight blocked');
      expect(activity.prPreflightLabel, 'PR preflight blocked: PR work');
      expect(
        activity.prPreflightDetail,
        contains('blockers 1 shared-root merge conflict, 1 merge conflict'),
      );
      expect(
        activity.prPreflightDetail,
        contains('needs conflict-free Worktree/branch/HEAD evidence'),
      );
      expect(
        activity.prPreflightDetail,
        contains('Resolve unmerged git conflict entries before PR prep.'),
      );
      expect(
        activity.flowSafetyPriorityReason,
        'Safety first: 1 shared-root merge conflict blocks PR work',
      );
      expect(activity.attentionScore, greaterThan(25000));
      expect(activity.searchTerms, contains('1 merge conflict'));
      expect(activity.searchTerms, contains('1 root conflict'));
      expect(activity.searchTerms, contains('PR preflight blocked'));
      expect(activity.searchTerms, contains('PR preflight blocked: PR work'));
      expect(
        activity.searchTerms,
        contains('needs conflict-free Worktree/branch/HEAD evidence'),
      );
      expect(
        activity.searchTerms,
        contains('Safety first: 1 shared-root merge conflict blocks PR work'),
      );
    });

    test('puts live containment pressure ahead of questions and PR pressure',
        () {
      final sorted = AutopilotAgentBenchActivityPresenter.sortAttentionFirst([
        {
          'name': 'Question QA',
          'pending_question_count': 1,
        },
        {
          'name': 'PM PR Pressure',
          'kpi_lane_pressure': {
            'blocked_pr_count': 4,
          },
        },
        {
          'name': 'AgentOps Containment',
          'agent_flow_pressure': {
            'quarantined_target_count': 1,
            'control_plane_blocker_count': 1,
          },
        },
      ]);

      expect(
        sorted.map((agent) => agent['name']),
        ['AgentOps Containment', 'PM PR Pressure', 'Question QA'],
      );
    });

    test('promotes release-trust target rows before raw PR pressure', () {
      final sorted = AutopilotAgentBenchActivityPresenter.sortAttentionFirst(
        [
          {
            'name': 'PR Train',
            'kpi_lane_pressure': {
              'blocked_pr_count': 9,
              'severity': 'high',
            },
          },
          {
            'name': 'AgentOps Director',
          },
        ],
        readiness: {
          'operator_inbox': {
            'release_trust_summary': {
              'blocker_count': 1,
              'group_counts': {
                'release_trust': 1,
                'pr_health': 0,
                'evidence_quality': 0,
              },
              'next_action_agent': 'AgentOps',
              'next_action_path': 'project_ws/AgentOps/QUARANTINE_GUARDIAN.md',
            },
          },
        },
      );

      expect(
        sorted.map((agent) => agent['name']),
        ['AgentOps Director', 'PR Train'],
      );
    });

    test('explains release-trust target focus for bench rows', () {
      final focus =
          AutopilotAgentBenchActivityPresenter.releaseTrustFocusForAgent(
        {
          'name': 'AgentOps Director',
        },
        readiness: {
          'operator_inbox': {
            'next_action_agent': 'AgentOps',
            'release_trust_summary': {
              'blocker_count': 1,
              'group_counts': {
                'release_trust': 1,
                'pr_health': 0,
                'evidence_quality': 0,
              },
              'next_action_agent': 'AgentOps',
              'next_action_category': 'source_trust',
              'next_action_label': 'Review containment',
              'next_action_detail':
                  'One active quarantined target must be contained first.',
              'next_action_path': 'project_ws/AgentOps/QUARANTINE_GUARDIAN.md',
              'next_action_handoff_label': 'Copy containment handoff',
              'next_action_handoff_copy':
                  'Project Autopilot containment handoff',
              'detail': 'Release trust has 1 report blocker.',
            },
          },
        },
      );
      final unrelated =
          AutopilotAgentBenchActivityPresenter.releaseTrustFocusForAgent(
        {
          'name': 'PR Train',
        },
        readiness: {
          'operator_inbox': {
            'release_trust_summary': {
              'blocker_count': 1,
              'group_counts': {
                'release_trust': 1,
                'pr_health': 0,
                'evidence_quality': 0,
              },
              'next_action_agent': 'AgentOps',
            },
          },
        },
      );

      expect(focus.active, isTrue);
      expect(focus.chipLabel, 'trust gate');
      expect(focus.gateChipLabel, '1 gate blocked');
      expect(
        focus.gateSummaryLabel,
        contains('Green CI is evidence, not authority'),
      );
      expect(focus.gateDetail, contains('Draft ready separate gate'));
      expect(focus.gateDetail, contains('Merge decision separate gate'));
      expect(focus.gateDetail, contains('Release/runtime blocked'));
      expect(focus.gateDetail, contains('Main green CI is not release'));
      expect(focus.hasGateSignal, isTrue);
      expect(focus.priorityLabel, 'Release trust: Review containment');
      expect(focus.detail, contains('Owner: AgentOps'));
      expect(focus.detail, contains('source trust'));
      expect(focus.detail, contains('One active quarantined target'));
      expect(focus.detail, contains('AgentOps/QUARANTINE_GUARDIAN.md'));
      expect(focus.actionLabel, 'Review containment');
      expect(focus.path, 'project_ws/AgentOps/QUARANTINE_GUARDIAN.md');
      expect(focus.handoffLabel, 'Copy containment handoff');
      expect(focus.handoffCopy, 'Project Autopilot containment handoff');
      expect(focus.hasAction, isTrue);
      expect(unrelated.active, isFalse);
      expect(unrelated.hasAction, isFalse);
    });

    test('routes PR publication gate packets as release-trust bench action',
        () {
      final focus =
          AutopilotAgentBenchActivityPresenter.releaseTrustFocusForAgent(
        {
          'name': 'SRE',
        },
        readiness: {
          'operator_inbox': {
            'release_trust_summary': {
              'blocker_count': 1,
              'group_counts': {
                'release_trust': 0,
                'pr_health': 1,
                'evidence_quality': 0,
              },
              'items': [
                {
                  'agent': 'SRE',
                  'category': 'ci_blocked',
                  'label': 'Review current-head checks',
                  'action_label': 'Review current-head checks',
                  'path':
                      'project_ws/SRE/OUT/_state/20260531-0939Z-agent-pr-blocker-health.json',
                  'open_path':
                      r'D:\dev\chili-home-copilot\project_ws\SRE\OUT\_state\20260531-0939Z-agent-pr-blocker-health.json',
                  'pr_publish_verdict': 'not_publishable',
                  'pr_publish_gate_state': 'blocked_until_review_ready_packet',
                  'pr_publish_next_gate': 'fresh_current_head_checks',
                  'pr_publish_blockers': ['current_head_checks_missing'],
                  'pr_publish_required_evidence': [
                    'exact_current_head_sha',
                    'fresh_current_head_checks',
                  ],
                  'pr_publish_forbidden_actions': [
                    'publish_ready_claim',
                    'push_or_pr_creation',
                  ],
                  'pr_publish_packet_label': 'Copy PR publication gate',
                  'pr_publish_packet_copy':
                      'Project Autopilot PR publication decision packet\n'
                          'Forbidden actions until the gate clears:\n'
                          '- push_or_pr_creation',
                },
              ],
            },
          },
        },
      );

      expect(focus.active, isTrue);
      expect(focus.chipLabel, 'PR gate');
      expect(focus.gateChipLabel, '3 gates blocked');
      expect(focus.gateDetail, contains('Draft ready blocked'));
      expect(focus.gateDetail, contains('Merge decision blocked'));
      expect(focus.gateDetail, contains('Release/runtime blocked'));
      expect(focus.gateDetail, contains('Needs Exact current head sha'));
      expect(focus.priorityLabel, 'Release trust: Copy PR publication gate');
      expect(focus.actionLabel, 'Copy PR publication gate');
      expect(focus.handoffLabel, 'Copy PR publication gate');
      expect(
        focus.handoffCopy,
        contains('Project Autopilot PR publication decision packet'),
      );
      expect(
        focus.path,
        'project_ws/SRE/OUT/_state/20260531-0939Z-agent-pr-blocker-health.json',
      );
      expect(focus.item['pr_publish_verdict'], 'not_publishable');
      expect(focus.hasAction, isTrue);
    });

    test('honors expedite board rank before raw blocker volume', () {
      final sorted = AutopilotAgentBenchActivityPresenter.sortAttentionFirst([
        {
          'name': 'PR Train',
          'kpi_lane_pressure': {
            'blocked_pr_count': 9,
          },
          'expedite_lane_pressure': {
            'open_pr_blocker_count': 9,
            'top_rank': 3,
            'top_type': 'open_pr_blocker',
            'top_evidence': 'PR #134 no checks; merge=DIRTY',
          },
        },
        {
          'name': 'PM Operations',
          'expedite_lane_pressure': {
            'stable_inbox_request_count': 1,
            'stable_inbox_top_rank': 2,
            'stable_inbox_request_priority': 'Urgent',
            'top_rank': 2,
            'top_type': 'stable_inbox_request',
            'top_evidence':
                'PM-070 SENT/00/KNC broker-action classification request',
          },
        },
      ]);

      expect(
        sorted.map((agent) => agent['name']),
        ['PM Operations', 'PR Train'],
      );
    });

    test('keeps equal-attention agents alphabetized for stable scrolling', () {
      final sorted = AutopilotAgentBenchActivityPresenter.sortAttentionFirst([
        {'name': 'Security'},
        {'name': 'Frontend'},
        {'name': 'AgentOps'},
      ]);

      expect(
        sorted.map((agent) => agent['name']),
        ['AgentOps', 'Frontend', 'Security'],
      );
    });

    test('clamps malformed and negative counts', () {
      final activity = AutopilotAgentBenchActivityPresenter.fromAgent({
        'active_run_count': -2,
        'open_run_count': 'nope',
        'pending_question_count': '-4',
      });

      expect(activity.activeRunCount, 0);
      expect(activity.openRunCount, 0);
      expect(activity.pendingQuestionCount, 0);
      expect(activity.activeChatLabel, isEmpty);
      expect(activity.openChatLabel, isEmpty);
      expect(activity.questionLabel, isEmpty);
      expect(activity.searchTerms, isEmpty);
      expect(activity.attentionScore, 0);
    });
  });
}
