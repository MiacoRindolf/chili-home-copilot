import 'package:chili_mobile/src/brain/autopilot_safety_state_presenter.dart';
import 'package:chili_mobile/src/brain/autopilot_safety_state_strip.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('AutopilotSafetyStateStrip', () {
    testWidgets('keeps review and copy controls reachable at narrow width',
        (tester) async {
      var opened = 0;
      var copied = 0;
      var openedControl = 0;
      var copiedControl = 0;

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: Center(
              child: SizedBox(
                width: 320,
                child: AutopilotSafetyStateStrip(
                  state: _blockedActionState(),
                  onOpenTarget: () => opened += 1,
                  onCopyHandoff: () => copied += 1,
                  onOpenControlTarget: () => openedControl += 1,
                  onCopyControlTarget: () => copiedControl += 1,
                ),
              ),
            ),
          ),
        ),
      );

      expect(tester.takeException(), isNull);
      expect(find.text('Safety state'), findsOneWidget);
      expect(find.text('blocked'), findsOneWidget);
      expect(find.text('Quarantine: Still active'), findsOneWidget);
      expect(find.text('Open'), findsOneWidget);
      expect(find.text('Open target'), findsOneWidget);
      expect(find.byTooltip('Copy review handoff'), findsOneWidget);
      expect(find.byTooltip('Copy containment handoff'), findsOneWidget);
      expect(
        find.textContaining('Evidence only: no release, runtime'),
        findsOneWidget,
      );

      await tester.tap(find.text('Open'));
      await tester.pump();
      await tester.tap(find.byTooltip('Copy review handoff'));
      await tester.pump();
      await tester.tap(find.text('Open target'));
      await tester.pump();
      await tester.tap(find.byTooltip('Copy containment handoff'));
      await tester.pump();

      expect(opened, 1);
      expect(copied, 1);
      expect(openedControl, 1);
      expect(copiedControl, 1);
    });

    testWidgets('does not render action controls while busy', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 320,
              child: AutopilotSafetyStateStrip(
                state: _blockedActionState(),
                busy: true,
                onOpenTarget: () {},
                onCopyHandoff: () {},
                onOpenControlTarget: () {},
                onCopyControlTarget: () {},
              ),
            ),
          ),
        ),
      );

      final openButton = tester.widget<OutlinedButton>(
        find.widgetWithText(OutlinedButton, 'Open'),
      );
      final copyButton = tester.widget<IconButton>(
        find.ancestor(
          of: find.byTooltip('Copy review handoff'),
          matching: find.byType(IconButton),
        ),
      );
      final controlButton = tester.widget<OutlinedButton>(
        find.widgetWithText(OutlinedButton, 'Open target'),
      );
      final controlCopyButton = tester.widget<IconButton>(
        find.ancestor(
          of: find.byTooltip('Copy containment handoff'),
          matching: find.byType(IconButton),
        ),
      );

      expect(openButton.onPressed, isNull);
      expect(copyButton.onPressed, isNull);
      expect(controlButton.onPressed, isNull);
      expect(controlCopyButton.onPressed, isNull);
      expect(tester.takeException(), isNull);
    });

    testWidgets('clear state stays compact without false safe wording',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 280,
              child: AutopilotSafetyStateStrip(state: _clearState()),
            ),
          ),
        ),
      );

      expect(tester.takeException(), isNull);
      expect(find.text('clear'), findsOneWidget);
      expect(find.text('No operator action'), findsOneWidget);
      expect(find.textContaining('does not grant release'), findsOneWidget);
      expect(find.textContaining('everything is safe'), findsNothing);
      expect(find.byType(OutlinedButton), findsNothing);
    });
  });
}

AutopilotSafetyState _blockedActionState() {
  return const AutopilotSafetyState(
    severity: AutopilotSafetySeverity.blocked,
    releaseLabel: 'Release blocked',
    releaseDetail: '2 blockers: 1 source, 1 CI.',
    releaseBlockerCount: 2,
    releaseBlocked: true,
    runtimeLabel: 'Runtime blocked',
    runtimeDetail: 'Runtime HTTP pressure: ops_health_14d status timeout.',
    runtimeBlockerCount: 1,
    runtimeBlocked: true,
    runtimePath: 'project_ws/SRE/OUT/evidence/runtime-http-sample.json',
    runtimeOpenPath:
        'D:/dev/chili-home-copilot/project_ws/SRE/OUT/evidence/runtime-http-sample.json',
    controlLabel: 'Control plane blocked',
    controlDetail: '1 quarantine, 1 paused automation.',
    controlBlockerCount: 2,
    controlHighRiskCount: 1,
    controlBlocked: true,
    controlTargetLabel: 'Quarantine: Still active',
    controlTargetDetail: 'target 019e6f30 | still active | 3 min proof left',
    controlTargetPath: 'project_ws/AgentOps/TARGET_THREAD_QUARANTINE.json',
    controlTargetOpenPath:
        'D:/dev/chili-home-copilot/project_ws/AgentOps/TARGET_THREAD_QUARANTINE.json',
    controlTargetHandoffLabel: 'Copy containment handoff',
    controlTargetHandoffCopy: 'Project Autopilot quarantine handoff',
    evidenceLabel: 'Evidence',
    evidenceDetail:
        'Seen 2026-05-31T09:01:00Z | head 444b4e84ba76 | PM/IN/safety-strip.md',
    evidencePath: 'project_ws/PM/IN/safety-strip.md',
    evidenceOpenPath:
        'D:/dev/chili-home-copilot/project_ws/PM/IN/safety-strip.md',
    nextActionLabel: 'Review PM request',
    nextActionDetail: 'PM has a stable safety decision request.',
    nextActionAgent: 'PM',
    nextActionKind: 'agent_flow',
    nextActionRunId: '',
    nextActionPath: 'project_ws/PM/IN/safety-strip.md',
    nextActionOpenPath:
        'D:/dev/chili-home-copilot/project_ws/PM/IN/safety-strip.md',
    nextActionRecoveryAction: '',
    nextActionButtonLabel: 'Open',
    nextActionHandoffLabel: 'Copy review handoff',
    nextActionHandoffCopy: 'Review the PM safety strip request.',
    inboxActionCount: 3,
    nonAuthorizationDetail:
        'Evidence only: no release, runtime, PR ready/merge, route cutover, or live behavior is authorized.',
  );
}

AutopilotSafetyState _clearState() {
  return const AutopilotSafetyState(
    severity: AutopilotSafetySeverity.clear,
    releaseLabel: 'Release guarded',
    releaseDetail: 'No release-trust blocker is surfaced.',
    releaseBlockerCount: 0,
    releaseBlocked: false,
    runtimeLabel: 'Runtime guarded',
    runtimeDetail: 'No runtime pressure evidence is surfaced.',
    runtimeBlockerCount: 0,
    runtimeBlocked: false,
    runtimePath: '',
    runtimeOpenPath: '',
    controlLabel: 'Control plane clear',
    controlDetail: 'No control-plane blocker is surfaced.',
    controlBlockerCount: 0,
    controlHighRiskCount: 0,
    controlBlocked: false,
    evidenceLabel: 'Evidence',
    evidenceDetail: 'No evidence path surfaced.',
    evidencePath: '',
    evidenceOpenPath: '',
    nextActionLabel: 'No operator action',
    nextActionDetail: '',
    nextActionAgent: '',
    nextActionKind: '',
    nextActionRunId: '',
    nextActionPath: '',
    nextActionOpenPath: '',
    nextActionRecoveryAction: '',
    nextActionButtonLabel: '',
    nextActionHandoffLabel: '',
    nextActionHandoffCopy: '',
    inboxActionCount: 0,
    nonAuthorizationDetail:
        'The strip does not grant release, runtime, PR, or live-behavior permission.',
  );
}
