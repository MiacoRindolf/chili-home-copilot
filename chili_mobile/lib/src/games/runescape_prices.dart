import 'dart:convert';

import 'package:http/http.dart' as http;

/// A RuneScape (RS3) Grand Exchange price result (GAME-10).
class ItemPrice {
  const ItemPrice({
    required this.name,
    required this.id,
    required this.price,
    required this.volume,
    required this.timestampMs,
  });

  final String name;
  final String id;
  final int price; // GE guide price in gp
  final int volume; // daily trade volume
  final int timestampMs;
}

/// Parse a MediaWiki opensearch response `[query, [titles], [descs], [urls]]`
/// into the list of item-name suggestions. Pure + tolerant. GAME-10.
List<String> parseOpenSearch(Object? json) {
  if (json is List && json.length >= 2 && json[1] is List) {
    return <String>[
      for (final Object? t in (json[1] as List))
        if (t != null && '$t'.trim().isNotEmpty) '$t'.trim(),
    ];
  }
  return const <String>[];
}

/// Parse a WeirdGloop `/exchange/history/rs/latest` response — a map keyed by
/// item name → {id, price, volume, timestamp}. Returns the first priced entry,
/// or null on an error/empty body. Pure. GAME-10.
ItemPrice? parseWeirdGloopLatest(Map<String, dynamic> json) {
  for (final MapEntry<String, dynamic> e in json.entries) {
    final Object? v = e.value;
    if (v is Map && v['price'] is num) {
      return ItemPrice(
        name: e.key,
        id: '${v['id'] ?? ''}',
        price: (v['price'] as num).toInt(),
        volume: (v['volume'] as num?)?.toInt() ?? 0,
        timestampMs:
            DateTime.tryParse('${v['timestamp']}')?.millisecondsSinceEpoch ?? 0,
      );
    }
  }
  return null;
}

/// Compact gp formatting: 85139 → "85.1K", 1500000 → "1.50M". GAME-10.
String formatGp(int gp) {
  final int a = gp.abs();
  final String sign = gp < 0 ? '-' : '';
  if (a >= 1000000000) return '$sign${(a / 1e9).toStringAsFixed(2)}B';
  if (a >= 1000000) return '$sign${(a / 1e6).toStringAsFixed(2)}M';
  if (a >= 1000) return '$sign${(a / 1e3).toStringAsFixed(1)}K';
  return '$gp';
}

/// Full gp with thousands separators: 85139 → "85,139". GAME-10.
String formatGpFull(int gp) {
  final String digits = gp.abs().toString();
  final StringBuffer b = StringBuffer(gp < 0 ? '-' : '');
  for (int i = 0; i < digits.length; i++) {
    if (i > 0 && (digits.length - i) % 3 == 0) b.write(',');
    b.write(digits[i]);
  }
  return b.toString();
}

/// Looks up RS3 Grand Exchange prices via the RuneScape Wiki opensearch (to
/// resolve a typed query to a real item name) and the WeirdGloop exchange API
/// (for the current GE price). Both are free, keyless, and used by the wiki
/// itself. Injectable [get] for tests. GAME-10.
class RuneScapePrices {
  RuneScapePrices({Future<String> Function(Uri url)? get})
      : _get = get ?? _defaultGet;

  final Future<String> Function(Uri url) _get;

  static const String _ua =
      'CHILI-Home-Copilot/1.0 (desktop game frame price lookup)';

  static Future<String> _defaultGet(Uri url) async {
    final http.Response r =
        await http.get(url, headers: <String, String>{'User-Agent': _ua});
    if (r.statusCode != 200) {
      throw http.ClientException('HTTP ${r.statusCode}', url);
    }
    return r.body;
  }

  /// Item-name suggestions for a typed query (autocomplete).
  Future<List<String>> search(String query) async {
    final String q = query.trim();
    if (q.isEmpty) return const <String>[];
    final Uri url = Uri.parse(
        'https://runescape.wiki/api.php?action=opensearch'
        '&search=${Uri.encodeQueryComponent(q)}'
        '&limit=8&namespace=0&format=json');
    return parseOpenSearch(jsonDecode(await _get(url)));
  }

  /// Current GE price for an exact item name.
  Future<ItemPrice?> priceByName(String name) async {
    final String n = name.trim();
    if (n.isEmpty) return null;
    final Uri url = Uri.parse(
        'https://api.weirdgloop.org/exchange/history/rs/latest'
        '?name=${Uri.encodeQueryComponent(n)}');
    final Object? json = jsonDecode(await _get(url));
    if (json is Map<String, dynamic>) return parseWeirdGloopLatest(json);
    return null;
  }

  /// Resolve a typed query to a priced item: try it as an exact name first,
  /// then fall back to the top opensearch match(es).
  Future<ItemPrice?> lookup(String query) async {
    try {
      final ItemPrice? exact = await priceByName(query);
      if (exact != null) return exact;
    } catch (_) {
      // ignore — fall through to search
    }
    final List<String> names = await search(query);
    for (final String n in names.take(3)) {
      try {
        final ItemPrice? p = await priceByName(n);
        if (p != null) return p;
      } catch (_) {
        // try the next candidate
      }
    }
    return null;
  }
}
