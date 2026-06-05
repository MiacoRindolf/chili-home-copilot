import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/skills/skills_models.dart';
import 'package:chili_mobile/src/skills/skills_screen.dart';

void main() {
  group('parseSkills', () {
    test('parses name / description / steps / saved_at', () {
      final List<Skill> s = parseSkills(<Map<String, dynamic>>[
        <String, dynamic>{
          'name': 'Retry with backoff',
          'when_to_use': 'On a transient tool error.',
          'procedure': <String>['Catch the error', 'Wait', 'Retry once'],
          'saved_at': 1700000000,
        },
      ]);
      expect(s.length, 1);
      expect(s.first.name, 'Retry with backoff');
      expect(s.first.description, 'On a transient tool error.');
      expect(s.first.steps, hasLength(3));
      expect(s.first.steps.first, 'Catch the error');
      expect(s.first.savedAtMs, 1700000000 * 1000);
    });

    test('drops unnamed; tolerates map-steps and string-steps', () {
      final List<Skill> s = parseSkills(<Map<String, dynamic>>[
        <String, dynamic>{'description': 'no name'}, // dropped
        <String, dynamic>{
          'name': 'A',
          'steps': <Map<String, dynamic>>[
            <String, dynamic>{'step': 'do x'},
            <String, dynamic>{'action': 'do y'},
          ],
        },
        <String, dynamic>{'name': 'B', 'steps': 'line1\nline2'},
      ]);
      expect(s.map((Skill x) => x.name).toList(), <String>['A', 'B']);
      expect(s[0].steps, <String>['do x', 'do y']);
      expect(s[1].steps, <String>['line1', 'line2']);
    });

    test('empty input → empty', () {
      expect(parseSkills(<Map<String, dynamic>>[]), isEmpty);
    });
  });

  group('SkillsScreen widget', () {
    testWidgets('renders skills with steps', (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: SkillsScreen(
          fetcher: () async => <Map<String, dynamic>>[
            <String, dynamic>{
              'name': 'Retry with backoff',
              'when_to_use': 'On a transient error.',
              'procedure': <String>['Catch', 'Retry'],
            },
          ],
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('Skills'), findsOneWidget);
      expect(find.text('Retry with backoff'), findsOneWidget);
      expect(find.text('1 learned'), findsOneWidget);
      expect(find.text('Catch'), findsOneWidget);
    });

    testWidgets('shows empty state when no skills', (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: SkillsScreen(fetcher: () async => <Map<String, dynamic>>[]),
      ));
      await tester.pumpAndSettle();
      expect(find.text('No skills learned yet'), findsOneWidget);
    });
  });
}
