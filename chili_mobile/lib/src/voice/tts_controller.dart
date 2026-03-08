import 'dart:async';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/widgets.dart';

import '../network/chili_api_client.dart';

/// Encapsulates TTS playback for Chili using the backend's Edge TTS neural
/// voice (en-US-AndrewMultilingualNeural).
///
/// Audio is fetched via the streaming endpoint and played from memory with
/// [BytesSource].  [_ttsPlaying] is only set to `true` once actual audio
/// playback begins — not when the network fetch starts — so the wake-word
/// listener won't show "follow-up" prematurely.
class TtsController {
  TtsController({
    required ChiliApiClient client,
    required ValueNotifier<bool>? ttsPlaying,
    required VoidCallback onFinish,
    ValueNotifier<String?>? lastTtsText,
  })  : _client = client,
        _ttsPlaying = ttsPlaying,
        _onFinish = onFinish,
        _lastTtsText = lastTtsText {
    _audioPlayer.onPlayerStateChanged.listen(_onPlayerStateChanged);
  }

  final ChiliApiClient _client;
  final ValueNotifier<bool>? _ttsPlaying;
  final VoidCallback _onFinish;
  final ValueNotifier<String?>? _lastTtsText;

  final AudioPlayer _audioPlayer = AudioPlayer();
  StreamSubscription<void>? _ttsCompleteSub;
  bool _disposed = false;
  bool _speaking = false;

  Timer? _echoTextTimer;

  Future<void> speak(String text) async {
    if (_disposed) return;
    final trimmed = _cleanForTts(text.trim());
    if (trimmed.isEmpty) {
      _finish();
      return;
    }

    _speaking = true;

    _lastTtsText?.value = trimmed;
    _echoTextTimer?.cancel();
    _echoTextTimer = Timer(const Duration(seconds: 15), () {
      _lastTtsText?.value = null;
    });

    try {
      // Prefer the streaming endpoint (lower total latency).
      List<int>? audioBytes = await _client.fetchTtsStreaming(trimmed);
      audioBytes ??= await _client.fetchTts(trimmed);

      if (_disposed || !_speaking) {
        _finish();
        return;
      }
      if (audioBytes == null || audioBytes.isEmpty) {
        debugPrint('[TtsController] TTS: no audio returned');
        _finish();
        return;
      }

      await _audioPlayer.stop();

      _ttsCompleteSub?.cancel();
      _ttsCompleteSub = _audioPlayer.onPlayerComplete.listen((_) {
        if (!_disposed) {
          WidgetsBinding.instance.addPostFrameCallback((_) => _finish());
        }
      });

      // Signal "playing" only now — right before actual audio starts.
      _ttsPlaying?.value = true;

      await _audioPlayer.play(
        BytesSource(Uint8List.fromList(audioBytes)),
      );
    } catch (e) {
      debugPrint('[TtsController] TTS error: $e');
      _finish();
    }
  }

  Future<void> stop() async {
    if (_disposed) return;
    _speaking = false;
    _ttsCompleteSub?.cancel();
    _ttsCompleteSub = null;
    try {
      await _audioPlayer.stop();
    } catch (_) {}
    _finish();
  }

  void _onPlayerStateChanged(PlayerState state) {
    if (_disposed) return;
    if (state == PlayerState.completed || state == PlayerState.stopped) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _finish());
    }
  }

  void _finish() {
    if (_disposed) return;
    _speaking = false;
    _ttsPlaying?.value = false;
    _onFinish();
  }

  /// Strip markdown/HTML so Edge TTS reads clean prose.
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
    _speaking = false;
    _echoTextTimer?.cancel();
    _ttsCompleteSub?.cancel();
    _ttsCompleteSub = null;
    try {
      await _audioPlayer.stop();
    } catch (_) {}
    await _audioPlayer.dispose();
  }
}
