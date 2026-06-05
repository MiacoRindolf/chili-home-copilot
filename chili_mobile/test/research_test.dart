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

  group('filterResearchTopics (SF-1)', () {
    const List<ResearchTopic> topics = <ResearchTopic>[
      ResearchTopic(topic: 'Rate cuts', summary: 'Fed easing', relevance: 0.9),
      ResearchTopic(topic: 'AI chips', summary: 'GPU demand', relevance: 0.8),
    ];
    test('empty query returns all; matches topic or summary', () {
      expect(filterResearchTopics(topics, '').length, 2);
      expect(filterResearchTopics(topics, 'RATE').single.topic, 'Rate cuts');
      expect(filterResearchTopics(topics, 'gpu').single.topic, 'AI chips');
      expect(filterResearchTopics(topics, 'zzz'), isEmpty);
    });
  });

  group('isHttpUrl / ResearchSource.isOpenable (RS-5)', () {
    test('accepts http/https with a host, rejects other schemes', () {
      expect(isHttpUrl('https://nature.com/q'), isTrue);
      expect(isHttpUrl('http://example.org'), isTrue);
      expect(isHttpUrl('  https://example.org  '), isTrue); // trimmed
      expect(isHttpUrl('file:///etc/passwd'), isFalse);
      expect(isHttpUrl('javascript:alert(1)'), isFalse);
      expect(isHttpUrl('data:text/html,x'), isFalse);
      expect(isHttpUrl('https://'), isFalse); // no host
      expect(isHttpUrl('not a url'), isFalse);
      expect(isHttpUrl(''), isFalse);
    });

    test('isOpenable reflects isHttpUrl', () {
      expect(
          const ResearchSource(title: 'N', url: 'https://nature.com')
              .isOpenable,
          isTrue);
      expect(
          const ResearchSource(title: 'F', url: 'file:///x').isOpenable,
          isFalse);
    });
  });

  group('sortResearchTopics (RS-4)', () {
    const List<ResearchTopic> topics = <ResearchTopic>[
      ResearchTopic(topic: 'Zinc', summary: '', relevance: 0.4),
      ResearchTopic(topic: 'Apples', summary: '', relevance: 0.9),
      ResearchTopic(topic: 'Mango', summary: '', relevance: 0.9), // ties Apples
    ];

    test('relevance sorts highest-first, ties break by title A→Z', () {
      expect(
          sortResearchTopics(topics, ResearchTopicSort.relevance)
              .map((ResearchTopic t) => t.topic),
          <String>['Apples', 'Mango', 'Zinc']);
    });

    test('title sorts A→Z', () {
      expect(
          sortResearchTopics(topics, ResearchTopicSort.title)
              .map((ResearchTopic t) => t.topic),
          <String>['Apples', 'Mango', 'Zinc']);
    });

    test('does not mutate the input list', () {
      final List<ResearchTopic> input = List<ResearchTopic>.of(topics);
      sortResearchTopics(input, ResearchTopicSort.relevance);
      expect(input.first.topic, 'Zinc'); // original order preserved
    });

    test('researchSortLabel covers every variant', () {
      for (final ResearchTopicSort s in ResearchTopicSort.values) {
        expect(researchSortLabel(s), isNotEmpty);
      }
    });
  });

  group('ResearchScreen widget', () {
    testWidgets('RS-4: topics render most-relevant-first with a sort toggle',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: ResearchScreen(
          fetcher: () async => <String, dynamic>{
            'title': 'D',
            'topics': <Map<String, dynamic>>[
              <String, dynamic>{
                'topic': 'LowRel',
                'summary': 's',
                'relevance_score': 0.2,
              },
              <String, dynamic>{
                'topic': 'HighRel',
                'summary': 's',
                'relevance_score': 0.95,
              },
            ],
            'sources': <Map<String, dynamic>>[],
          },
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.widgetWithText(ChoiceChip, 'Relevance'), findsOneWidget);
      // Default relevance-desc → HighRel sits above LowRel.
      final double highY = tester.getTopLeft(find.text('HighRel')).dy;
      final double lowY = tester.getTopLeft(find.text('LowRel')).dy;
      expect(highY, lessThan(lowY));
    });

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

      expect(find.text('Research'), findsWidgets); // header title + run button
      expect(find.text('Quantum'), findsOneWidget);
      expect(find.text('1 topics'), findsOneWidget);

      await tester.tap(find.widgetWithText(OutlinedButton, 'Open report'));
      await tester.pumpAndSettle();
      expect(opened, isTrue);
      expect(find.textContaining('Opened research report'), findsOneWidget);
    });

    testWidgets('RS-5: tapping a source opens its URL via the injected opener',
        (WidgetTester tester) async {
      String? opened;
      await tester.pumpWidget(MaterialApp(
        home: ResearchScreen(
          fetcher: () async => <String, dynamic>{
            'title': 'D',
            'topics': <Map<String, dynamic>>[
              <String, dynamic>{
                'topic': 'Quantum',
                'summary': 's',
                'relevance_score': 0.5,
              },
            ],
            'sources': <Map<String, dynamic>>[
              <String, dynamic>{
                'title': 'Nature',
                'url': 'https://nature.com/quantum',
              },
            ],
          },
          urlOpener: (String url) async {
            opened = url;
            return true;
          },
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('Nature'), findsOneWidget);
      expect(find.byIcon(Icons.open_in_new), findsWidgets); // source + report btn
      await tester.tap(find.text('Nature'));
      await tester.pump();
      expect(opened, 'https://nature.com/quantum');
    });

    testWidgets('shows empty state when there is no research',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: ResearchScreen(
          fetcher: () async => <String, dynamic>{'topics': <Object?>[]},
          reportOpener: () async => false,
          runner: (String _) async => const <String, dynamic>{},
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('No research yet'), findsOneWidget);
    });

    testWidgets('RS-2: run research stores a topic and reloads the digest',
        (WidgetTester tester) async {
      int fetches = 0;
      String? ranTopic;
      await tester.pumpWidget(MaterialApp(
        home: ResearchScreen(
          // 1st load empty; after a run, the reload returns the new topic.
          fetcher: () async {
            fetches++;
            return fetches == 1
                ? <String, dynamic>{'topics': <Object?>[]}
                : <String, dynamic>{
                    'topics': <Map<String, dynamic>>[
                      <String, dynamic>{'topic': 'Bitcoin ETFs', 'summary': 'x'},
                    ],
                  };
          },
          reportOpener: () async => false,
          runner: (String t) async {
            ranTopic = t;
            return <String, dynamic>{'ok': true, 'stored': true, 'topic': t};
          },
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('No research yet'), findsOneWidget);

      await tester.enterText(find.byType(TextField).first, 'Bitcoin ETFs');
      await tester.tap(find.widgetWithText(FilledButton, 'Research'));
      await tester.pumpAndSettle();

      expect(ranTopic, 'Bitcoin ETFs');
      expect(find.text('Bitcoin ETFs'), findsWidgets); // reloaded digest
    });

    testWidgets('RC-1: Discuss button pivots a topic to chat',
        (WidgetTester tester) async {
      String? discussed;
      await tester.pumpWidget(MaterialApp(
        home: ResearchScreen(
          fetcher: () async => <String, dynamic>{
            'topics': <Map<String, dynamic>>[
              <String, dynamic>{'topic': 'Quantum', 'summary': 's'},
            ],
          },
          reportOpener: () async => false,
          runner: (String _) async => const <String, dynamic>{},
          onDiscuss: (String t) => discussed = t,
        ),
      ));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('Discuss in Chat'));
      await tester.pump();
      expect(discussed, 'Quantum');
    });

    testWidgets('SF-1: typing filters topics; no-match offers research',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: ResearchScreen(
          fetcher: () async => <String, dynamic>{
            'topics': <Map<String, dynamic>>[
              <String, dynamic>{'topic': 'Quantum', 'summary': 's'},
              <String, dynamic>{'topic': 'Bitcoin', 'summary': 's'},
            ],
          },
          reportOpener: () async => false,
          runner: (String _) async => const <String, dynamic>{},
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('Quantum'), findsOneWidget);
      expect(find.text('Bitcoin'), findsOneWidget);

      await tester.enterText(find.byType(TextField).first, 'quan');
      await tester.pump();
      expect(find.text('Quantum'), findsOneWidget);
      expect(find.text('Bitcoin'), findsNothing);

      await tester.enterText(find.byType(TextField).first, 'zzzz');
      await tester.pump();
      expect(find.textContaining('No topics match'), findsOneWidget);
    });
  });
}
