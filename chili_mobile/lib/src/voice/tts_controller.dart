import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:flutter_tts/flutter_tts.dart';

/// Encapsulates TTS playback for Chili using on-device speech synthesis
/// (Windows SAPI / macOS NSSpeechSynthesizer / Linux espeak).
///
/// This gives near-instant time-to-first-audio (<100 ms) instead of the
/// 3-4 second round-trip required by the server-side Edge TTS endpoint.
class TtsController {
  TtsController({
    required ValueNotifier<bool>? ttsPlaying,
    required VoidCallback onFinish,
    ValueNotifier<String?>? lastTtsText,
  })  : _ttsPlaying = ttsPlaying,
        _onFinish = onFinish,
        _lastTtsText = lastTtsText {
    _initFlutterTts();
  }

  final ValueNotifier<bool>? _ttsPlaying;
  final VoidCallback _onFinish;
  final ValueNotifier<String?>? _lastTtsText;

  final FlutterTts _flutterTts = FlutterTts();
  bool _disposed = false;

  Timer? _echoTextTimer;

  void _initFlutterTts() {
    _flutterTts.setCompletionHandler(() {
      if (!_disposed) {
        WidgetsBinding.instance.addPostFrameCallback((_) => _finish());
      }
    });
    _flutterTts.setCancelHandler(() {
      if (!_disposed) {
        WidgetsBinding.instance.addPostFrameCallback((_) => _finish());
      }
    });
    _flutterTts.setErrorHandler((msg) {
      debugPrint('[TtsController] flutter_tts error: $msg');
      if (!_disposed) {
        WidgetsBinding.instance.addPostFrameCallback((_) => _finish());
      }
    });

    _flutterTts.setSpeechRate(0.5);
    _flutterTts.setPitch(1.0);
    _flutterTts.setVolume(1.0);
    _setVoice();
  }

  Future<void> _setVoice() async {
    try {
      final voices = await _flutterTts.getVoices as List<dynamic>;
      final voiceList = voices.cast<Map<dynamic, dynamic>>();

      // Prefer a male English neural voice (Windows 11 ships these).
      final preferred = [
        'Microsoft Andrew',
        'Microsoft Guy',
        'Microsoft David',
        'en-us-guynneural',
        'en-us-andrewneural',
      ];

      Map<dynamic, dynamic>? best;
      for (final pref in preferred) {
        final match = voiceList.where(
          (v) => (v['name'] ?? '').toString().toLowerCase().contains(pref.toLowerCase()),
        );
        if (match.isNotEmpty) {
          best = match.first;
          break;
        }
      }

      // Fallback: any English voice.
      best ??= voiceList.where(
        (v) => (v['locale'] ?? '').toString().startsWith('en'),
      ).firstOrNull;

      if (best != null) {
        await _flutterTts.setVoice({
          'name': best['name'].toString(),
          'locale': best['locale'].toString(),
        });
        debugPrint('[TtsController] Voice: ${best['name']}');
      }
    } catch (e) {
      debugPrint('[TtsController] Voice selection failed: $e');
    }
  }

  Future<void> speak(String text) async {
    if (_disposed) return;
    final trimmed = _cleanForTts(text.trim());
    if (trimmed.isEmpty) {
      _finish();
      return;
    }

    _lastTtsText?.value = trimmed;
    _echoTextTimer?.cancel();
    _echoTextTimer = Timer(const Duration(seconds: 10), () {
      _lastTtsText?.value = null;
    });

    _ttsPlaying?.value = true;

    try {
      await _flutterTts.speak(trimmed);
    } catch (e) {
      debugPrint('[TtsController] TTS error: $e');
      _finish();
    }
  }

  Future<void> stop() async {
    if (_disposed) return;
    try {
      await _flutterTts.stop();
    } catch (_) {}
    _finish();
  }

  void _finish() {
    if (_disposed) return;
    _ttsPlaying?.value = false;
    _onFinish();
  }

  /// Strip markdown/HTML for cleaner speech.
  static String _cleanForTts(String text) {
    var clean = text;
    clean = clean.replaceAll(RegExp(r'<[^>]*>'), '');
    clean = clean.replaceAll(RegExp(r'\*\*'), '');
    clean = clean.replaceAll(RegExp(r'[#_`\[\]]'), '');
    clean = clean.replaceAll(RegExp(r'\n{2,}'), '. ');
    clean = clean.replaceAll(RegExp(r'\n'), ' ');
    clean = clean.replaceAll(RegExp(r'\s{2,}'), ' ');
    return clean.trim();
  }

  Future<void> dispose() async {
    if (_disposed) return;
    _disposed = true;
    _echoTextTimer?.cancel();
    try {
      await _flutterTts.stop();
    } catch (_) {}
  }
}
