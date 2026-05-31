import 'package:chili_mobile/src/brain/autopilot_inbox_item_presenter.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('AutopilotInboxItemPresenter', () {
    test('summarizes fresh evidence and superseded report snapshots', () {
      final evidence = AutopilotInboxItemPresenter.reportEvidence({
        'created_at': '2026-05-31T10:43:58Z',
        'path':
            'project_ws/SRE/OUT/evidence/20260531-1043Z-agent-pr-blocker-health.json',
        'report_source_kind': 'evidence',
        'report_supersedes_older_count': 3,
      });

      expect(evidence.sourceLabel, 'source evidence');
      expect(evidence.supersessionLabel, 'supersedes 3');
      expect(evidence.openActionLabel, 'Open current evidence');
      expect(
        evidence.detail,
        'Evidence seen 2026-05-31T10:43:58Z, source evidence, '
        'supersedes 3 older snapshots, '
        'OUT/evidence/20260531-1043Z-agent-pr-blocker-health.json.',
      );
    });

    test('labels state snapshots as older report context', () {
      final evidence = AutopilotInboxItemPresenter.reportEvidence({
        'created_at': '2026-05-31T09:39:47Z',
        'path':
            'project_ws/SRE/OUT/_state/20260531-0939Z-agent-pr-blocker-health.json',
        'report_source_kind': 'state',
      });

      expect(evidence.sourceLabel, 'source state snapshot');
      expect(evidence.isStateSnapshot, isTrue);
      expect(evidence.openActionLabel, 'Open state snapshot');
      expect(
        evidence.detail,
        'Evidence seen 2026-05-31T09:39:47Z, source state snapshot, '
        'OUT/_state/20260531-0939Z-agent-pr-blocker-health.json.',
      );
    });

    test('falls back to a plain open action for external report paths', () {
      final label = AutopilotInboxItemPresenter.openActionLabel({
        'path': 'project_ws/SRE/OUT/report.md',
      });

      expect(label, 'Open report');
    });

    test('summarizes runtime-pressure inbox evidence and handoff', () {
      final runtime = AutopilotInboxItemPresenter.runtimePressure({
        'runtime_pressure_blocker_count': 2,
        'runtime_pressure_warning_count': 1,
        'runtime_pressure_handoff_label': 'Copy runtime handoff',
        'runtime_pressure_handoff_copy':
            'Project Autopilot runtime-pressure handoff',
        'runtime_pressure_items': [
          {
            'kind': 'runtime_source_posture',
            'label': 'Runtime source posture',
            'detail':
                'Runtime source posture: chili from project_ws/_worktrees/slice.',
            'path':
                'project_ws/SRE/OUT/evidence/20260531-1138Z-runtime-source-docker-readback.json',
          },
          {
            'kind': 'runtime_db_pressure',
            'label': 'Runtime DB pressure',
            'detail':
                'Runtime DB pressure: 2 active, 53 waiting, 1 advisory lock.',
            'path':
                'project_ws/SRE/OUT/evidence/20260531-1138Z-runtime-db-backup-pressure-readback.json',
          },
        ],
      });

      expect(runtime.blockerLabel, '2 runtime blockers');
      expect(runtime.warningLabel, '1 runtime warning');
      expect(runtime.evidenceCountLabel, '2 evidence items');
      expect(runtime.handoffLabel, 'Copy runtime handoff');
      expect(
        runtime.handoffCopy,
        'Project Autopilot runtime-pressure handoff',
      );
      expect(runtime.hasEvidence, isTrue);
      expect(runtime.evidenceDetails, hasLength(2));
      expect(
        runtime.evidenceDetails.first.detail,
        'Runtime source posture: chili from project_ws/_worktrees/slice. | '
        'OUT/evidence/20260531-1138Z-runtime-source-docker-readback.json',
      );
    });

    test('keeps runtime-pressure presentation quiet without evidence', () {
      final runtime = AutopilotInboxItemPresenter.runtimePressure({
        'path': 'project_ws/SRE/OUT/evidence/runtime-basic-http-sample.json',
      });

      expect(runtime.hasEvidence, isFalse);
      expect(runtime.hasHandoff, isFalse);
      expect(runtime.evidenceDetails, isEmpty);
    });

    test('stays quiet when an inbox item has no freshness signal', () {
      final evidence = AutopilotInboxItemPresenter.reportEvidence({
        'path': 'project_ws/SRE/OUT/report.md',
      });

      expect(evidence.hasSource, isFalse);
      expect(evidence.hasSupersession, isFalse);
      expect(evidence.hasDetail, isFalse);
    });
  });
}
