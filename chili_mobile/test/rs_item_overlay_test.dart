import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/games/rs_item_overlay.dart';
import 'package:chili_mobile/src/games/runescape_prices.dart';

RuneScapePrices _fakePrices({bool found = true}) {
  return RuneScapePrices(get: (Uri url) async {
    if (url.host == 'api.weirdgloop.org') {
      if (!found) return jsonEncode(<String, dynamic>{'success': false});
      return jsonEncode(<String, dynamic>{
        'Abyssal whip': <String, dynamic>{
          'id': '4151',
          'price': 85139,
          'volume': 944,
          'timestamp': '2026-06-06T07:15:01.000Z',
        },
      });
    }
    if (url.host == 'runescape.wiki' &&
        url.queryParameters['action'] == 'opensearch') {
      return jsonEncode(<Object?>[
        'abyssal whip',
        <Object?>['Abyssal whip'],
        <Object?>[''],
        <Object?>[''],
      ]);
    }
    // wiki query (extract) — no thumbnail so no Image.network in tests.
    return jsonEncode(<String, dynamic>{
      'query': <String, dynamic>{
        'pages': <String, dynamic>{
          '1819': <String, dynamic>{
            'extract': 'The abyssal whip is a one-handed melee weapon.',
          },
        },
      },
    });
  });
}

Future<void> _drain(WidgetTester tester) async {
  for (int i = 0; i < 6; i++) {
    await tester.pump(const Duration(milliseconds: 60));
  }
}

void main() {
  testWidgets('shows an idle hint before searching', (WidgetTester tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(body: RsItemOverlay(prices: _fakePrices())),
    ));
    await tester.pump();
    expect(find.textContaining('Type an item'), findsOneWidget);
  });

  testWidgets('searches and shows the GE price + blurb',
      (WidgetTester tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(body: RsItemOverlay(prices: _fakePrices())),
    ));
    await tester.pump();
    await tester.enterText(find.byType(TextField), 'abyssal whip');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await _drain(tester);

    expect(find.text('85,139 gp'), findsOneWidget);
    expect(find.text('Abyssal whip'), findsOneWidget);
    expect(find.textContaining('Vol 944/day'), findsOneWidget);
    expect(find.textContaining('one-handed melee weapon'), findsOneWidget);
  });

  testWidgets('shows a friendly message when nothing is found',
      (WidgetTester tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(body: RsItemOverlay(prices: _fakePrices(found: false))),
    ));
    await tester.pump();
    await tester.enterText(find.byType(TextField), 'zzzzz');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await _drain(tester);
    expect(find.textContaining('No GE price'), findsOneWidget);
  });
}
