import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

/// Stores / loads the CHILI OS desktop session (open windows + geometry) so the
/// workspace restores where you left it. Pure I/O around [SharedPreferences];
/// the controller produces/consumes the plain maps.
class WorkspacePersistence {
  static const String _key = 'chili_ws_layout_v1';

  static Future<void> save(List<Map<String, Object>> windows) async {
    final SharedPreferences prefs = await SharedPreferences.getInstance();
    if (windows.isEmpty) {
      await prefs.remove(_key);
    } else {
      await prefs.setString(_key, jsonEncode(windows));
    }
  }

  static Future<List<Map<String, dynamic>>> load() async {
    final SharedPreferences prefs = await SharedPreferences.getInstance();
    final String? raw = prefs.getString(_key);
    if (raw == null || raw.isEmpty) return <Map<String, dynamic>>[];
    try {
      final Object? decoded = jsonDecode(raw);
      if (decoded is! List) return <Map<String, dynamic>>[];
      return decoded
          .whereType<Map<dynamic, dynamic>>()
          .map((Map<dynamic, dynamic> e) => Map<String, dynamic>.from(e))
          .toList();
    } catch (_) {
      return <Map<String, dynamic>>[]; // corrupt payload → start fresh
    }
  }
}
