import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/research/research_models.dart';
import 'package:chili_mobile/src/research/research_screen.dart';

void main() {
  group('parseResearchDigest', () {
    test('parses topics + sources + title', () {
      final ResearchDigest d = parseResearchDigest(<String, dynamic>{
        'title': 'Weekly Digest',
        'topic_count': 2,
        'topics': <Map<String, dynamic>>[
          <String, dynamic>{
            'topic': 'Rate cuts',
            'summary': '**Fed** signalled cuts.',
            'relevance_score': 0.92,
          },
          <String, dynamic>{'topic': 'AI chips', 'summary': 'Demand up.'},
        ],
        'sources': <Map<String, dynamic>>[
          <String, dynamic>{'title': 'Reuters', 'url': 'https://www.reuters.com/x'},
          <String, dynamic>{'url': 'https://bloomberg.com/y'},
        ],
      });
      expect(d.title, 'Weekly Digest');
      expect(d.topicCount, 2);
      expect(d.topics.length, 2);
      expect(d.topics.first.topic, 'Rate cuts');
      expect(d.topics.first.relevance, closeTo(0.92, 1e-9));
      expect(d.sources.length, 2);
      expect(d.sources.first.host, 'reuters.com'); // www stripped
      expect(d.sources[1].title, 'https://bloomberg.com/y'); // falls back to url
      expect(d.isEmpty, isFalse);
    });

    test('tolerates missing / malformed fields', () {
      final ResearchDigest d = parseResearchDigest(<String, dynamic>{});
      expect(d.isEmpty, isTrue);
      expect(d.title, 'Research Digest');
      expect(d.topicCount, 0);

      final ResearchDigest d2 = parseResearchDigest(<String, dynamic>{
        'topics': <Object?>['not a map', 42],
        'sources': <Object?>[
          <String, dynamic>{'title': 'no url'}, // dropped (no url)
        ],
      });
      expect(d2.topics, isEmpty);
      expect(d2.sources, isEmpty);
    });

    test('topic_count falls back to topics length', () {
      final ResearchDigest d = parseResearchDigest(<String, dynamic>{
        'topics': <Map<String, dynamic>>[
          <String, dynamic>{'topic': 'a', 'summary': 's'},
        ],
      });
      expect(d.topicCount, 1);
    });
  });

  group('ResearchScreen widget', () {
    testWidgets('renders topics from an injected digest + Open report enabled',
        (WidgetTester tester) async {
      bool opened = false;
      await tester.pumpWidget(MaterialApp(
        home: ResearchScreen(
          fetcher: () async => <String, dynamic>{
            'title': 'D',
            'topic_count': 1,
            'topics': <Map<String, dynamic>>[
              <String, dynamic>{
                'topic': 'Quantum',
                'summary': 'Big news.',
                'relevance_score': 0.5,
              },
            ],
            'sources': <Map<String, dynamic>>[
              <String, dynamic>{'title': 'Nature', 'url': 'https://nature.com/q'},
            ],
          },
          reportOpener: () async {
            opened = true;
            return true;
          },
        ),
      ));
      await tester.pumpAndSettle();

      expect(find.text('Research'), findsOneWidget);
      expect(find.text('Quantum'), findsOneWidget);
      expect(find.text('1 topics'), findsOneWidget);

      await tester.tap(find.widgetWithText(OutlinedButton, 'Open report'));
      await tester.pumpAndSettle();
      expect(opened, isTrue);
      expect(find.textContaining('Opened research report'), findsOneWidget);
    });

    testWidgets('shows empty state when there is no research',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: ResearchScreen(
          fetcher: () async => <String, dynamic>{'topics': <Object?>[]},
          reportOpener: () async => false,
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('No research yet'), findsOneWidget);
    });
  });
}
