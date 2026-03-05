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

  /// Known phonetic variants for common wake words (STT often mistranscribes "chili" many ways).
  static const Map<String, List<String>> _phoneticVariants = {
    'chili': [
      'chilly', 'chile', 'chilli', 'chillie', 'chily', 'chilii',
      'julie', 'jilly', 'july',
      'jimmy', 'gilly', 'chilee', 'chelsea', 'tilly', 'shilly', 'chilley',
      'chilii', 'chelie', 'chilie', 'gillie', 'jemmy', 'chily',
    ],
  };

  /// Short filler words that can appear before the wake word (e.g. "a chilly", "hi julie" → match).
  static const Set<String> _leadingFillers = {
    'a', 'ah', 'oh', 'um', 'uh', 'the', 'and', 'or',
    'hi', 'he', 'i', 'its',
  };

  /// Levenshtein distance so we can fuzzy-match minor transcription errors.
  static int _levenshtein(String a, String b) {
    if (a.isEmpty) return b.length;
    if (b.isEmpty) return a.length;
    final m = a.length;
    final n = b.length;
    final d = List.generate(m + 1, (_) => List.filled(n + 1, 0));
    for (var i = 0; i <= m; i++) {
      d[i][0] = i;
    }
    for (var j = 0; j <= n; j++) {
      d[0][j] = j;
    }
    for (var j = 1; j <= n; j++) {
      for (var i = 1; i <= m; i++) {
        final cost = a[i - 1] == b[j - 1] ? 0 : 1;
        d[i][j] = [
          d[i - 1][j] + 1,
          d[i][j - 1] + 1,
          d[i - 1][j - 1] + cost,
        ].reduce((x, y) => x < y ? x : y);
      }
    }
    return d[m][n];
  }

  List<String> _getWakeWordVariants() {
    final w = _wakeWord.toLowerCase();
    final list = [w];
    final known = _phoneticVariants[w];
    if (known != null) list.addAll(known);
    return list;
  }

  /// Drops leading filler words so "a chilly" or "oh hey chile" still match.
  String _skipLeadingFillers(String text) {
    var rest = text.trim().toLowerCase();
    while (rest.isNotEmpty) {
      final first = _firstWord(rest);
      if (first.isEmpty || !_leadingFillers.contains(first)) break;
      rest = rest.substring(_firstWordLength(rest)).trim();
    }
    return rest;
  }

  /// First word of [text] (lowercase, before space/comma/end).
  static String _firstWord(String text) {
    final t = text.trim().toLowerCase();
    final end = t.indexOf(' ');
    final comma = t.indexOf(',');
    if (end < 0 && comma < 0) return t;
    if (end < 0) return t.substring(0, comma);
    if (comma < 0) return t.substring(0, end);
    final cut = end < comma ? end : comma;
    return t.substring(0, cut);
  }

  /// Second word (after first space), or empty.
  static String _secondWord(String text) {
    final t = text.trim().toLowerCase();
    final firstSpace = t.indexOf(' ');
    if (firstSpace < 0) return '';
    final rest = t.substring(firstSpace + 1).trim();
    final end = rest.indexOf(' ');
    final comma = rest.indexOf(',');
    if (end < 0 && comma < 0) return rest;
    if (end < 0) return rest.substring(0, comma);
    if (comma < 0) return rest.substring(0, end);
    return rest.substring(0, end < comma ? end : comma);
  }

  /// Length of first word in original [text] (for stripping).
  static int _firstWordLength(String text) {
    final t = text.trim();
    if (t.isEmpty) return 0;
    var i = 0;
    while (i < t.length && t[i] != ' ' && t[i] != ',') {
      i++;
    }
    return i;
  }

  bool _isWakeWordOrVariant(String word) {
    final w = _wakeWord.toLowerCase();
    if (w.isEmpty) return false;
    if (word == w) return true;
    if (_getWakeWordVariants().contains(word)) return true;
    return _levenshtein(word, w) <= 2;
  }

  /// Case-insensitive check: "[Chili] ..." or "Hey [Chili] ..." (and phonetic variants).
  /// Allows one leading filler word so "a chilly" or "oh hey chile" still trigger.
  bool isWakeWordMatch(String text) {
    final w = _wakeWord.toLowerCase();
    if (w.isEmpty) return false;
    final cleaned = _skipLeadingFillers(text);
    if (cleaned.isEmpty) return false;
    final first = _firstWord(cleaned);
    if (first.isEmpty) return false;
    if (_isWakeWordOrVariant(first)) return true;
    if (first == 'hey') {
      final second = _secondWord(cleaned);
      return second.isNotEmpty && _isWakeWordOrVariant(second);
    }
    return false;
  }

  /// Strip the wake phrase (and any leading filler) from the start of [text].
  String stripWakeWord(String text) {
    final t = text.trim();
    if (_wakeWord.trim().isEmpty) return t;
    if (!isWakeWordMatch(text)) return t;
    final cleaned = _skipLeadingFillers(t);
    if (cleaned.isEmpty) return '';
    // Strip "hey " + wake word from cleaned
    if (_firstWord(cleaned) == 'hey') {
      final restAfterHey = cleaned.substring(_firstWordLength(cleaned)).trim();
      var rest = restAfterHey.substring(_firstWordLength(restAfterHey)).trim();
      if (rest.startsWith(',')) rest = rest.substring(1).trim();
      return rest;
    }
    // Strip wake word only from cleaned
    var rest = cleaned.substring(_firstWordLength(cleaned)).trim();
    if (rest.startsWith(',')) rest = rest.substring(1).trim();
    return rest;
  }
}
