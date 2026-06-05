import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/brain/autopilot_ui.dart';

Widget _host(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  testWidgets('ApSectionHeader uppercases its title', (WidgetTester tester) async {
    await tester.pumpWidget(_host(const ApSectionHeader('Recent runs')));
    expect(find.text('RECENT RUNS'), findsOneWidget);
  });

  testWidgets('ApStatusPill shows its label', (WidgetTester tester) async {
    await tester.pumpWidget(_host(
      const ApStatusPill('Live', color: Colors.green, icon: Icons.bolt),
    ));
    expect(find.text('Live'), findsOneWidget);
    expect(find.byIcon(Icons.bolt), findsOneWidget);
  });

  testWidgets('ApStatCard shows value and label', (WidgetTester tester) async {
    await tester.pumpWidget(_host(
      const ApStatCard(label: 'Runs (5m)', value: '12', icon: Icons.bolt),
    ));
    expect(find.text('12'), findsOneWidget);
    expect(find.text('Runs (5m)'), findsOneWidget);
  });

  testWidgets('ApEmptyState shows message, detail and action', (WidgetTester tester) async {
    await tester.pumpWidget(_host(
      ApEmptyState(
        icon: Icons.inbox,
        message: 'Nothing here',
        detail: 'Queue a task to begin',
        action: FilledButton(onPressed: () {}, child: const Text('Queue')),
      ),
    ));
    expect(find.text('Nothing here'), findsOneWidget);
    expect(find.text('Queue a task to begin'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Queue'), findsOneWidget);
  });

  test('apRunStatusColor maps states to sensible buckets', () {
    const ColorScheme cs = ColorScheme.light();
    expect(apRunStatusColor('completed', cs), Colors.green);
    expect(apRunStatusColor('failed', cs), cs.error);
    expect(apRunStatusColor('running', cs), cs.primary);
    expect(apRunStatusColor('pending', cs), Colors.amber.shade700);
    expect(apRunStatusColor('whatever', cs), cs.onSurfaceVariant);
  });
}
