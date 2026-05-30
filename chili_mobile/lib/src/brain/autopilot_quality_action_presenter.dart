enum AutopilotQualityTargetRoute {
  attachScreenshot,
  inboxItem,
  openRun,
  startQueued,
}

class AutopilotQualityTargetAction {
  const AutopilotQualityTargetAction({
    required this.buttonLabel,
    required this.route,
  });

  final String buttonLabel;
  final AutopilotQualityTargetRoute route;
}

class AutopilotQualityActionPresenter {
  static const actionAttachVisualQa = 'attach_visual_qa';
  static const actionDrainQueued = 'drain_queued_run';

  static AutopilotQualityTargetAction targetAction({
    required String action,
    String actionLabel = '',
    String kind = '',
  }) {
    final cleanAction = action.trim();
    final cleanLabel = actionLabel.trim();
    final cleanKind = kind.trim();

    if (cleanAction == actionDrainQueued) {
      return AutopilotQualityTargetAction(
        buttonLabel: cleanLabel.isEmpty ? 'Start' : cleanLabel,
        route: AutopilotQualityTargetRoute.startQueued,
      );
    }
    if (cleanAction == actionAttachVisualQa) {
      return AutopilotQualityTargetAction(
        buttonLabel: cleanLabel.isEmpty ? 'Screenshot' : cleanLabel,
        route: AutopilotQualityTargetRoute.attachScreenshot,
      );
    }
    if (cleanKind.isNotEmpty) {
      return AutopilotQualityTargetAction(
        buttonLabel: cleanLabel.isEmpty ? 'Open target' : cleanLabel,
        route: AutopilotQualityTargetRoute.inboxItem,
      );
    }
    return AutopilotQualityTargetAction(
      buttonLabel: cleanLabel.isEmpty ? 'Open target' : cleanLabel,
      route: AutopilotQualityTargetRoute.openRun,
    );
  }
}
