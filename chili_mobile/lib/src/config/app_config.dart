import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// App-wide config persisted via SharedPreferences.
/// Wake word and always-listening are configurable for different households.
class AppConfig {
  AppConfig._();
  static final AppConfig instance = AppConfig._();

  static const _keyWakeWord = 'chili_wake_word';
  static const _keyAlwaysListening = 'chili_always_listening';
  static const _defaultWakeWord = 'chili';
  static const _defaultAlwaysListening = true;

  SharedPreferences? _prefs;
  String _wakeWord = _defaultWakeWord;
  bool _alwaysListening = _defaultAlwaysListening;

  String get wakeWord => _wakeWord;
  bool get alwaysListening => _alwaysListening;

  bool get isLoaded => _prefs != null;

  Future<void> load() async {
    _prefs = await SharedPreferences.getInstance();
    _wakeWord = _prefs!.getString(_keyWakeWord) ?? _defaultWakeWord;
    _alwaysListening = _prefs!.getBool(_keyAlwaysListening) ?? _defaultAlwaysListening;
  }

  Future<void> setWakeWord(String word) async {
    final trimmed = word.trim().toLowerCase();
    if (trimmed.isEmpty) return;
    _wakeWord = trimmed;
    await _prefs?.setString(_keyWakeWord, _wakeWord);
  }

  Future<void> setAlwaysListening(bool value) async {
    _alwaysListening = value;
    await _prefs?.setBool(_keyAlwaysListening, _alwaysListening);
  }

  /// Case-insensitive check: does [text] start with the configured wake word?
  bool isWakeWordMatch(String text) {
    final t = text.trim().toLowerCase();
    final w = _wakeWord.toLowerCase();
    if (w.isEmpty) return false;
    return t == w || t.startsWith('$w ');
  }

  /// Strip the wake word from the start of [text] and return the rest.
  String stripWakeWord(String text) {
    final t = text.trim();
    final w = _wakeWord.trim().toLowerCase();
    if (w.isEmpty) return t;
    final lower = t.toLowerCase();
    if (lower == w) return '';
    if (lower.startsWith('$w ')) return t.substring(w.length + 1).trim();
    if (lower.startsWith('$w,')) return t.substring(w.length + 1).trim();
    return t;
  }
}
