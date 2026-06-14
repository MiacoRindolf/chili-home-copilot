import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:chili_mobile/src/brain/diff_view.dart';
import 'package:chili_mobile/src/brain/autonomy_run_presenter.dart';

const _sample = '''
--- a/app/services/code_dispatch/scorer.py
+++ b/app/services/code_dispatch/scorer.py
@@ -4,7 +4,7 @@
   1 - local Ollama
   2 - Groq free tier
   3 - OpenAI gpt-4o-mini
-  4 - OpenAI gpt-4o or Anthropic claude-opus-4.6 (premium)
+  4 - frontier escalation (FRONTIER_MODEL, e.g. gpt-5.5)
 \"\"\"
 from __future__ import annotations
''';

void main() {
  group('parseUnifiedDiff', () {
    test('splits into files with typed +/- lines and counts', () {
      final files = parseUnifiedDiff(_sample);
      expect(files, hasLength(1));
      final f = files.first;
      expect(f.path, 'app/services/code_dispatch/scorer.py');
      expect(f.added, 1);
      expect(f.removed, 1);
      expect(f.lines.any((l) => l.kind == DiffLineKind.hunk), isTrue);
    });

    test('handles multiple files', () {
      const two = '''
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-a
+b
--- a/y.py
+++ b/y.py
@@ -1 +1 @@
-c
+d
''';
      final files = parseUnifiedDiff(two);
      expect(files.map((f) => f.path), ['x.py', 'y.py']);
      expect(files.every((f) => f.added == 1 && f.removed == 1), isTrue);
    });

    test('tolerant of empty/garbage input', () {
      expect(parseUnifiedDiff(''), isEmpty);
      expect(parseUnifiedDiff('not a diff at all'), isEmpty);
    });

    test('handles /dev/null new files', () {
      const created = '''
--- /dev/null
+++ b/new_file.py
@@ -0,0 +1,2 @@
+import os
+x = 1
''';
      final files = parseUnifiedDiff(created);
      expect(files.single.path, 'new_file.py');
      expect(files.single.added, 2);
    });
  });

  group('AutonomyRunPresenter.diffContent', () {
    test('returns the patch for a diff artifact', () {
      final c = AutonomyRunPresenter.diffContent({
        'artifact_type': 'diff',
        'content': _sample,
      });
      expect(c.contains('@@'), isTrue);
      expect(c.contains('frontier escalation'), isTrue);
    });

    test('reads from content_json.diff fallback', () {
      final c = AutonomyRunPresenter.diffContent({
        'artifact_type': 'diff',
        'content': '',
        'content_json': {'diff': _sample},
      });
      expect(c.contains('@@'), isTrue);
    });

    test('empty for non-diff or summary-only artifacts', () {
      expect(
          AutonomyRunPresenter.diffContent(
              {'artifact_type': 'commit', 'content': 'abc'}),
          '');
      expect(
          AutonomyRunPresenter.diffContent({
            'artifact_type': 'diff',
            'content': 'Generated a patch for scorer.py.',
          }),
          '');
    });
  });

  testWidgets('DiffView renders a collapsible file with +/- counts',
      (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(child: DiffView(_sample)),
      ),
    ));
    expect(find.text('app/services/code_dispatch/scorer.py'), findsOneWidget);
    expect(find.text('+1'), findsOneWidget);
    expect(find.text('-1'), findsOneWidget);
    // Collapsed by default — body lines hidden until tapped.
    expect(find.textContaining('frontier escalation'), findsNothing);
    await tester.tap(find.text('app/services/code_dispatch/scorer.py'));
    await tester.pumpAndSettle();
    expect(find.textContaining('frontier escalation'), findsOneWidget);
  });
}
