import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/games/runescape_prices.dart';

void main() {
  group('parseOpenSearch (GAME-10)', () {
    test('extracts the suggestion titles', () {
      final List<String> s = parseOpenSearch(<Object?>[
        'abyssal',
        <Object?>['Abyssal whip', 'Abyssal demon', 'Abyssal vine whip'],
        <Object?>['d1', 'd2', 'd3'],
        <Object?>['u1', 'u2', 'u3'],
      ]);
      expect(s, <String>['Abyssal whip', 'Abyssal demon', 'Abyssal vine whip']);
    });

    test('tolerates malformed payloads', () {
      expect(parseOpenSearch(null), isEmpty);
      expect(parseOpenSearch(<Object?>['q']), isEmpty);
      expect(parseOpenSearch(<Object?>['q', 'notalist']), isEmpty);
    });
  });

  group('parseWeirdGloopLatest (GAME-10)', () {
    test('reads the priced entry', () {
      final ItemPrice? p = parseWeirdGloopLatest(<String, dynamic>{
        'Abyssal whip': <String, dynamic>{
          'id': '4151',
          'price': 85139,
          'volume': 944,
          'timestamp': '2026-06-06T07:15:01.000Z',
        },
      });
      expect(p, isNotNull);
      expect(p!.name, 'Abyssal whip');
      expect(p.id, '4151');
      expect(p.price, 85139);
      expect(p.volume, 944);
      expect(p.timestampMs, greaterThan(0));
    });

    test('returns null on an error / unpriced body', () {
      expect(
          parseWeirdGloopLatest(<String, dynamic>{'success': false}), isNull);
      expect(parseWeirdGloopLatest(<String, dynamic>{}), isNull);
    });
  });

  group('formatting (GAME-10)', () {
    test('formatGp compacts', () {
      expect(formatGp(850), '850');
      expect(formatGp(85139), '85.1K');
      expect(formatGp(1500000), '1.50M');
      expect(formatGp(2300000000), '2.30B');
    });

    test('formatGpFull groups thousands', () {
      expect(formatGpFull(85139), '85,139');
      expect(formatGpFull(1500000), '1,500,000');
      expect(formatGpFull(42), '42');
    });
  });

  group('RuneScapePrices service (GAME-10)', () {
    test('search hits the wiki opensearch endpoint', () async {
      Uri? hit;
      final RuneScapePrices svc = RuneScapePrices(get: (Uri url) async {
        hit = url;
        return jsonEncode(<Object?>[
          'whip',
          <Object?>['Abyssal whip'],
          <Object?>[''],
          <Object?>[''],
        ]);
      });
      final List<String> names = await svc.search('whip');
      expect(names, <String>['Abyssal whip']);
      expect(hit!.host, 'runescape.wiki');
      expect(hit!.queryParameters['action'], 'opensearch');
      expect(hit!.queryParameters['search'], 'whip');
    });

    test('priceByName hits WeirdGloop and parses the price', () async {
      Uri? hit;
      final RuneScapePrices svc = RuneScapePrices(get: (Uri url) async {
        hit = url;
        return jsonEncode(<String, dynamic>{
          'Abyssal whip': <String, dynamic>{
            'id': '4151',
            'price': 85139,
            'volume': 944,
            'timestamp': '2026-06-06T07:15:01.000Z',
          },
        });
      });
      final ItemPrice? p = await svc.priceByName('Abyssal whip');
      expect(p?.price, 85139);
      expect(hit!.host, 'api.weirdgloop.org');
      expect(hit!.queryParameters['name'], 'Abyssal whip');
    });

    test('lookup resolves a fuzzy query via search then price', () async {
      final RuneScapePrices svc = RuneScapePrices(get: (Uri url) async {
        if (url.host == 'api.weirdgloop.org') {
          final String name = url.queryParameters['name'] ?? '';
          if (name == 'abyssal whip') {
            return jsonEncode(<String, dynamic>{'success': false}); // exact miss
          }
          return jsonEncode(<String, dynamic>{
            'Abyssal whip': <String, dynamic>{
              'id': '4151',
              'price': 85139,
              'volume': 944,
              'timestamp': '2026-06-06T07:15:01.000Z',
            },
          });
        }
        // opensearch resolves to the canonical name
        return jsonEncode(<Object?>[
          'abyssal whip',
          <Object?>['Abyssal whip'],
          <Object?>[''],
          <Object?>[''],
        ]);
      });
      final ItemPrice? p = await svc.lookup('abyssal whip');
      expect(p, isNotNull);
      expect(p!.name, 'Abyssal whip');
      expect(p.price, 85139);
    });
  });
}
