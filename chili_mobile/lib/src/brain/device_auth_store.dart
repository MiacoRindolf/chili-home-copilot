import 'package:shared_preferences/shared_preferences.dart';

/// Persists the mobile pairing token for Brain / dispatch API calls.
class DeviceAuthStore {
  DeviceAuthStore._();

  static const _kToken = 'chili_device_token';
  static const _kLastProjectId = 'chili_brain_last_project_id';

  static Future<String?> getToken() async {
    final p = await SharedPreferences.getInstance();
    return p.getString(_kToken);
  }

  static Future<void> setToken(String value) async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_kToken, value);
  }

  static Future<void> clear() async {
    final p = await SharedPreferences.getInstance();
    await p.remove(_kToken);
  }

  static Future<int?> getLastProjectId() async {
    final p = await SharedPreferences.getInstance();
    if (!p.containsKey(_kLastProjectId)) return null;
    return p.getInt(_kLastProjectId);
  }

  static Future<void> setLastProjectId(int id) async {
    final p = await SharedPreferences.getInstance();
    await p.setInt(_kLastProjectId, id);
  }
}
