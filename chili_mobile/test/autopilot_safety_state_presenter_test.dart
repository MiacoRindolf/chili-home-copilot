import 'package:chili_mobile/src/brain/autopilot_safety_state_presenter.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('AutopilotSafetyStatePresenter', () {
    test('summarizes release, control-plane, and next action blockers', () {
      final state = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 3,
          'next_action_label': 'Review PM request',
          'next_action_detail': 'PM has a stable safety decision request.',
          'next_action_agent': 'PM',
          'next_action_kind': 'agent_flow',
          'next_action_path': 'project_ws/PM/IN/safety-strip.md',
          'next_action_open_path':
              'D:/dev/chili-home-copilot/project_ws/PM/IN/safety-strip.md',
          'next_action_button_label': 'Open',
          'next_action_handoff_label': 'Copy review handoff',
          'next_action_handoff_copy': 'Review the PM safety strip request.',
          'release_trust_summary': {
            'blocker_count': 2,
            'group_counts': {
              'release_trust': 1,
              'pr_health': 1,
              'evidence_quality': 0,
            },
          },
          'items': [
            {
              'path': 'project_ws/PM/IN/safety-strip.md',
              'created_at': '2026-05-31T09:01:00Z',
              'worktree_head_short': '444b4e84ba76',
            },
          ],
        },
        'agent_flow': {
          'quarantined_target_active_count': 1,
          'control_plane_trust': {
            'blocker_count': 1,
            'high_risk_count': 1,
            'next_action_detail': 'Stop the quarantined target first.',
          },
        },
      });

      expect(state.severity, AutopilotSafetySeverity.blocked);
      expect(state.releaseBlocked, isTrue);
      expect(state.releaseLabel, 'Release blocked');
      expect(state.releaseDetail, '2 blockers: 1 release, 1 PR.');
      expect(state.controlBlocked, isTrue);
      expect(state.controlLabel, 'Control plane blocked');
      expect(
        state.controlDetail,
        '1 quarantine, 1 high-risk. '
        'Stop the quarantined target first.',
      );
      expect(state.nextActionLabel, 'Review PM request');
      expect(state.nextActionAgent, 'PM');
      expect(state.nextActionPath, 'project_ws/PM/IN/safety-strip.md');
      expect(
        state.nextActionOpenPath,
        'D:/dev/chili-home-copilot/project_ws/PM/IN/safety-strip.md',
      );
      expect(state.safeActionButtonLabel, 'Open');
      expect(state.hasOpenTarget, isTrue);
      expect(state.hasHandoff, isTrue);
      expect(
        state.evidenceDetail,
        'Seen 2026-05-31T09:01:00Z | head 444b4e84ba76 | '
        'PM/IN/safety-strip.md',
      );
      expect(
        state.nonAuthorizationDetail,
        contains('no release, runtime, PR ready/merge'),
      );
      expect(state.hasAction, isTrue);
    });

    test('keeps the strip clear when trust and inbox are quiet', () {
      final state = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 0,
          'next_action': 'keep_monitoring',
          'release_trust_summary': {
            'blocker_count': 0,
          },
        },
        'agent_flow': {
          'control_plane_trust': {
            'blocker_count': 0,
            'high_risk_count': 0,
          },
        },
      });

      expect(state.severity, AutopilotSafetySeverity.clear);
      expect(state.releaseLabel, 'Release guarded');
      expect(state.releaseDetail, 'No release-trust blocker is surfaced.');
      expect(state.runtimeLabel, 'Runtime guarded');
      expect(
        state.runtimeDetail,
        'No runtime pressure evidence is surfaced.',
      );
      expect(state.controlLabel, 'Control plane clear');
      expect(state.controlDetail, 'No control-plane blocker is surfaced.');
      expect(state.evidenceDetail, 'No evidence path surfaced.');
      expect(
        state.nonAuthorizationDetail,
        'The strip does not grant release, runtime, PR, or live-behavior '
        'permission.',
      );
      expect(state.nextActionLabel, 'No operator action');
      expect(state.hasAction, isFalse);
      expect(state.hasOpenTarget, isFalse);
    });

    test('surfaces runtime pressure as a first-class blocker', () {
      final state = AutopilotSafetyStatePresenter.fromReadiness({
        'runtime_pressure': {
          'status': 'warning',
          'blocker_count': 2,
          'warning_count': 0,
          'detail':
              'Runtime HTTP pressure: ops_health_14d status timeout exit 28. '
                  'Runtime DB pressure: 5 active, 16 idle in transaction.',
          'next_action': 'review_runtime_pressure',
          'next_action_label': 'Review runtime pressure',
          'next_action_detail':
              'Open the latest SRE runtime evidence before trusting live behavior.',
          'next_action_handoff_label': 'Copy runtime handoff',
          'next_action_handoff_copy':
              'Project Autopilot runtime-pressure handoff',
          'path':
              'project_ws/SRE/OUT/evidence/20260531-1107Z-runtime-db-pressure-detail.json',
          'open_path':
              'D:/dev/chili-home-copilot/project_ws/SRE/OUT/evidence/20260531-1107Z-runtime-db-pressure-detail.json',
          'created_at': '2026-05-31T11:07:39Z',
          'items': [
            {
              'kind': 'runtime_db_pressure',
              'created_at': '2026-05-31T11:07:39Z',
              'path':
                  'project_ws/SRE/OUT/evidence/20260531-1107Z-runtime-db-pressure-detail.json',
            },
          ],
        },
        'operator_inbox': {
          'total_action_count': 0,
          'next_action': 'keep_monitoring',
          'release_trust_summary': {
            'blocker_count': 0,
          },
        },
        'agent_flow': {
          'control_plane_trust': {
            'blocker_count': 0,
            'high_risk_count': 0,
          },
        },
      });

      expect(state.severity, AutopilotSafetySeverity.blocked);
      expect(state.runtimeLabel, 'Runtime blocked');
      expect(state.runtimeBlockerCount, 2);
      expect(
        state.runtimeDetail,
        contains('ops_health_14d status timeout exit 28'),
      );
      expect(state.nextActionLabel, 'Review runtime pressure');
      expect(state.nextActionKind, 'review_runtime_pressure');
      expect(state.safeActionButtonLabel, 'Review runtime');
      expect(state.nextActionHandoffLabel, 'Copy runtime handoff');
      expect(
        state.nextActionHandoffCopy,
        'Project Autopilot runtime-pressure handoff',
      );
      expect(state.hasHandoff, isTrue);
      expect(
        state.evidenceDetail,
        'Seen 2026-05-31T11:07:39Z | source runtime db pressure | '
        'OUT/evidence/20260531-1107Z-runtime-db-pressure-detail.json',
      );
      expect(state.hasOpenTarget, isTrue);
    });

    test('keeps operator inbox action ahead of control and release actions',
        () {
      final state = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 1,
          'next_action': 'answer_question',
          'next_action_label': 'Answer question',
          'next_action_detail': 'The operator question is waiting.',
          'next_action_kind': 'question',
          'release_trust_summary': {
            'blocker_count': 1,
            'next_action_label': 'Review source trust',
          },
        },
        'agent_flow': {
          'control_plane_trust': {
            'blocker_count': 1,
            'next_action_label': 'Needs operator stop',
          },
        },
      });

      expect(state.nextActionLabel, 'Answer question');
      expect(state.nextActionKind, 'question');
    });

    test('falls through from keep-monitoring to control then release actions',
        () {
      final controlState = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 1,
          'next_action': 'keep_monitoring',
          'next_action_label': 'Keep monitoring',
          'release_trust_summary': {
            'blocker_count': 1,
            'next_action_label': 'Review source trust',
          },
        },
        'agent_flow': {
          'control_plane_trust': {
            'blocker_count': 1,
            'next_action_label': 'Needs operator stop',
            'next_action_kind': 'quarantined_target',
          },
        },
      });

      expect(controlState.nextActionLabel, 'Needs operator stop');
      expect(controlState.nextActionKind, 'quarantined_target');

      final releaseState = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 1,
          'next_action': 'keep_monitoring',
          'next_action_label': 'Keep monitoring',
          'release_trust_summary': {
            'blocker_count': 1,
            'next_action_label': 'Review source trust',
            'next_action_agent': 'Risk',
            'items': [
              {
                'path': 'project_ws/Risk/OUT/review.md',
                'open_path':
                    'D:/dev/chili-home-copilot/project_ws/Risk/OUT/review.md',
              },
            ],
          },
        },
        'agent_flow': {
          'control_plane_trust': {
            'blocker_count': 0,
          },
        },
      });

      expect(releaseState.nextActionLabel, 'Review source trust');
      expect(releaseState.nextActionAgent, 'Risk');
      expect(releaseState.nextActionPath, 'project_ws/Risk/OUT/review.md');
      expect(releaseState.safeActionButtonLabel, 'Review');
    });

    test('surfaces source and CI release blockers when categories exist', () {
      final state = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 1,
          'release_trust_summary': {
            'blocker_count': 2,
            'category_counts': {
              'source_trust': 1,
              'ci_blocked': 1,
            },
          },
        },
        'agent_flow': {
          'control_plane_trust': {
            'blocker_count': 0,
          },
        },
      });

      expect(state.releaseDetail, '2 blockers: 1 source, 1 CI.');
    });

    test('names the exact PR current-head blocker from release trust items',
        () {
      final state = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 1,
          'release_trust_summary': {
            'blocker_count': 1,
            'category_counts': {
              'ci_blocked': 1,
            },
            'items': [
              {
                'pr_number': '134',
                'pr_branch': 'codex/brain-work-done-marker-recovery',
                'ci_state': 'no_checks',
                'ci_summary': 'no checks',
                'blocker_kind': 'ci_missing_checks',
                'path':
                    'project_ws/SRE/OUT/_state/agent-pr-blocker-health.json',
              },
            ],
          },
        },
        'agent_flow': {
          'control_plane_trust': {
            'blocker_count': 0,
          },
        },
      });

      expect(
        state.releaseDetail,
        '1 blocker: 1 CI. PR #134 current-head blocker on '
        'codex/brain-work-done-marker-recovery: ci missing checks, no checks.',
      );
      expect(
        state.evidenceDetail,
        'OUT/_state/agent-pr-blocker-health.json',
      );
    });

    test('surfaces release-trust evidence freshness and supersession', () {
      final state = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 1,
          'release_trust_summary': {
            'blocker_count': 1,
            'category_counts': {
              'ci_blocked': 1,
            },
            'items': [
              {
                'pr_number': '134',
                'pr_branch': 'codex/brain-work-done-marker-recovery',
                'ci_state': 'no_checks',
                'ci_summary': 'no checks',
                'blocker_kind': 'ci_missing_checks',
                'created_at': '2026-05-31T10:27:10Z',
                'source_kind': 'evidence',
                'supersedes_older_count': 2,
                'path':
                    'project_ws/SRE/OUT/evidence/20260531-1027Z-agent-pr-blocker-health.json',
              },
            ],
          },
        },
        'agent_flow': {
          'control_plane_trust': {
            'blocker_count': 0,
          },
        },
      });

      expect(
        state.releaseDetail,
        contains(
          'Evidence seen 2026-05-31T10:27:10Z, from evidence, '
          'OUT/evidence/20260531-1027Z-agent-pr-blocker-health.json, '
          'supersedes 2 older snapshots.',
        ),
      );
      expect(
        state.evidenceDetail,
        'Seen 2026-05-31T10:27:10Z | source evidence | '
        'supersedes 2 older snapshots | '
        'OUT/evidence/20260531-1027Z-agent-pr-blocker-health.json',
      );
    });

    test('treats uncovered paused automation as blocked control-plane trust',
        () {
      final state = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 1,
          'next_action': 'keep_monitoring',
          'release_trust_summary': {
            'blocker_count': 0,
          },
        },
        'agent_flow': {
          'paused_automation_uncovered_count': 1,
          'control_plane_trust': {
            'blocker_count': 1,
            'high_risk_count': 1,
            'category_counts': {
              'paused_automation': 1,
            },
            'next_action_label': 'Verify paused automation',
            'next_action_detail': 'Paused schedule is not containment proof.',
          },
        },
      });

      expect(state.severity, AutopilotSafetySeverity.blocked);
      expect(state.controlBlocked, isTrue);
      expect(
        state.controlDetail,
        '1 paused automation, 1 high-risk. '
        'Paused schedule is not containment proof.',
      );
      expect(state.nextActionLabel, 'Verify paused automation');
    });

    test('surfaces the top quarantined target as a control target row', () {
      final state = AutopilotSafetyStatePresenter.fromReadiness({
        'operator_inbox': {
          'total_action_count': 1,
          'next_action': 'keep_monitoring',
          'release_trust_summary': {
            'blocker_count': 0,
          },
        },
        'agent_flow': {
          'quarantined_target_active_count': 2,
          'control_plane_trust': {
            'blocker_count': 2,
            'high_risk_count': 2,
            'category_counts': {
              'quarantine': 2,
            },
            'items': [
              {
                'kind': 'quarantined_target',
                'label': 'Needs operator stop',
                'status': 'CONTROL_PLANE_TERMINATION_REQUIRED',
                'thread_id': '019e4398-f673-7902-a3d0-1fafd8e84878',
                'proof_remaining_minutes': 4,
                'source_path':
                    'project_ws/AgentOps/TARGET_THREAD_QUARANTINE.json',
                'open_path':
                    'D:/dev/chili-home-copilot/project_ws/AgentOps/TARGET_THREAD_QUARANTINE.json',
                'detail': 'Use the Codex control plane to stop this target.',
                'handoff_label': 'Copy stop handoff',
                'handoff_copy': 'Project Autopilot quarantine handoff',
              },
            ],
            'next_action_label': 'Needs operator stop',
            'next_action_kind': 'quarantined_target',
          },
        },
      });

      expect(state.severity, AutopilotSafetySeverity.blocked);
      expect(state.controlTargetLabel, 'Quarantine: Needs operator stop');
      expect(
        state.controlTargetDetail,
        'target 019e4398 | control plane termination required | '
        'needs operator stop | 4 min proof left',
      );
      expect(
        state.controlTargetPath,
        'project_ws/AgentOps/TARGET_THREAD_QUARANTINE.json',
      );
      expect(
        state.controlTargetOpenPath,
        'D:/dev/chili-home-copilot/project_ws/AgentOps/TARGET_THREAD_QUARANTINE.json',
      );
      expect(state.controlTargetHandoffLabel, 'Copy stop handoff');
      expect(
        state.controlTargetHandoffCopy,
        'Project Autopilot quarantine handoff',
      );
      expect(state.hasControlTargetHandoff, isTrue);
      expect(state.hasOpenTarget, isTrue);
    });
  });
}
