import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

/// Saves / loads the Agents app state (per-agent status, enabled, config,
/// last-run + any custom agents). Pure I/O around [SharedPreferences]; the
/// [AgentRegistry] produces/consumes the plain maps. Mirrors
/// `WorkspacePersistence`.
class AgentPersistence {
  static const String _key = 'chili_agents_v1';

  static Future<void> save(List<Map<String, dynamic>> agents) async {
    final SharedPreferences prefs = await SharedPreferences.getInstance();
    if (agents.isEmpty) {
      await prefs.remove(_key);
    } else {
      await prefs.setString(_key, jsonEncode(agents));
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
