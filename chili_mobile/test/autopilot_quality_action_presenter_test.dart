import 'package:chili_mobile/src/brain/autopilot_quality_action_presenter.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('AutopilotQualityActionPresenter', () {
    test('routes visual QA actions to direct screenshot capture', () {
      final action = AutopilotQualityActionPresenter.targetAction(
        action: AutopilotQualityActionPresenter.actionAttachVisualQa,
      );

      expect(action.buttonLabel, 'Screenshot');
      expect(action.route, AutopilotQualityTargetRoute.attachScreenshot);
    });

    test('routes queued runtime actions to worker start', () {
      final action = AutopilotQualityActionPresenter.targetAction(
        action: AutopilotQualityActionPresenter.actionDrainQueued,
      );

      expect(action.buttonLabel, 'Start');
      expect(action.route, AutopilotQualityTargetRoute.startQueued);
    });

    test('keeps explicit operator recovery labels', () {
      final action = AutopilotQualityActionPresenter.targetAction(
        action: 'recover_blocker',
        actionLabel: 'Rerun',
        kind: 'blocker',
      );

      expect(action.buttonLabel, 'Rerun');
      expect(action.route, AutopilotQualityTargetRoute.inboxItem);
    });

    test('falls back to opening a target run', () {
      final action = AutopilotQualityActionPresenter.targetAction(
        action: 'review_quality',
      );

      expect(action.buttonLabel, 'Open target');
      expect(action.route, AutopilotQualityTargetRoute.openRun);
    });
  });
}
