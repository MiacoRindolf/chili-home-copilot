import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/agents/agent_activity_service.dart';
import 'package:chili_mobile/src/agents/run_health.dart';

AgentRun _run(String? outcome) =>
    AgentRun(when: '2026-06-05T10:00:00Z', title: 'run', outcome: outcome);

void main() {
  group('classifyOutcome', () {
    test('positive outcomes are good', () {
      for (final String s in <String>['ok', 'success', 'completed', 'FILLED',
          'approved', 'executed', '3 patterns · ok']) {
        expect(classifyOutcome(s), RunOutcomeKind.good, reason: s);
      }
    });

    test('negative outcomes are bad and take precedence', () {
      for (final String s in <String>['failed', 'error', 'rejected',
          'timeout', 'aborted', 'ok · failed']) {
        expect(classifyOutcome(s), RunOutcomeKind.bad, reason: s);
      }
    });

    test('null / empty / informational outcomes are neutral', () {
      expect(classifyOutcome(null), RunOutcomeKind.neutral);
      expect(classifyOutcome(''), RunOutcomeKind.neutral);
      expect(classifyOutcome('HOLD'), RunOutcomeKind.neutral);
      expect(classifyOutcome('running'), RunOutcomeKind.neutral);
    });
  });

  group('summarizeRuns', () {
    test('counts buckets and computes success rate over scored runs only', () {
      final RunHealth h = summarizeRuns(<AgentRun>[
        _run('ok'),
        _run('completed'),
        _run('failed'),
        _run('HOLD'), // neutral — excluded from ratio
        _run(null), // neutral
      ]);
      expect(h.total, 5);
      expect(h.good, 2);
      expect(h.bad, 1);
      expect(h.neutral, 2);
      expect(h.scored, 3);
      expect(h.successRate, closeTo(2 / 3, 1e-9));
      expect(h.successLabel, '2/3 ok');
    });

    test('all-neutral runs have no success rate', () {
      final RunHealth h = summarizeRuns(<AgentRun>[_run('HOLD'), _run(null)]);
      expect(h.scored, 0);
      expect(h.successRate, isNull);
      expect(h.successLabel, isNull);
    });

    test('empty list', () {
      final RunHealth h = summarizeRuns(const <AgentRun>[]);
      expect(h.total, 0);
      expect(h.successRate, isNull);
    });
  });

  group('RunOutcomeStrip widget', () {
    testWidgets('renders one tooltip segment per run', (WidgetTester t) async {
      final List<AgentRun> runs = <AgentRun>[
        _run('ok'),
        _run('failed'),
        _run('HOLD'),
      ];
      await t.pumpWidget(MaterialApp(
        home: Scaffold(body: RunOutcomeStrip(runs)),
      ));
      expect(find.byType(Tooltip), findsNWidgets(3));
    });

    testWidgets('empty runs render nothing', (WidgetTester t) async {
      await t.pumpWidget(const MaterialApp(
        home: Scaffold(body: RunOutcomeStrip(<AgentRun>[])),
      ));
      expect(find.byType(Tooltip), findsNothing);
    });
  });
}
