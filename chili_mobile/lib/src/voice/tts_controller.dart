import 'dart:async';
import 'dart:io';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/widgets.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import '../network/chili_api_client.dart';

/// Encapsulates TTS playback for Chili, including temp file management,
/// AudioPlayer wiring, and the ttsPlaying flag.
class TtsController {
  TtsController({
    required ChiliApiClient client,
    required ValueNotifier<bool>? ttsPlaying,
    required VoidCallback onFinish,
  })  : _client = client,
        _ttsPlaying = ttsPlaying,
        _onFinish = onFinish;

  final ChiliApiClient _client;
  final ValueNotifier<bool>? _ttsPlaying;
  final VoidCallback _onFinish;

  final AudioPlayer _audioPlayer = AudioPlayer();
  StreamSubscription<void>? _ttsCompleteSub;
  bool _disposed = false;

  Future<void> speak(String text) async {
    if (_disposed) return;
    final trimmed = text.trim();
    if (trimmed.isEmpty) {
      _finish();
      return;
    }
    _ttsPlaying?.value = true;
    try {
      final audioBytes = await _client.fetchTts(trimmed);
      if (_disposed) {
        _finish();
        return;
      }
      if (audioBytes == null || audioBytes.isEmpty) {
        debugPrint('[TtsController] TTS: no audio (fetch failed or empty)');
        _finish();
        return;
      }
      final dir = await getTemporaryDirectory();
      if (_disposed) {
        _finish();
        return;
      }
      final file = File(
        p.join(dir.path, 'chili_tts_${DateTime.now().millisecondsSinceEpoch}.mp3'),
      );
      await file.writeAsBytes(audioBytes);
      if (_disposed) {
        _finish();
        try {
          if (file.existsSync()) file.deleteSync();
        } catch (_) {}
        return;
      }
      await _audioPlayer.stop();
      void onDone() {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          _finish();
        });
        try {
          if (file.existsSync()) file.deleteSync();
        } catch (_) {}
      }

      _ttsCompleteSub?.cancel();
      _ttsCompleteSub = _audioPlayer.onPlayerComplete.listen((_) => onDone());
      await _audioPlayer.play(DeviceFileSource(file.path));
    } catch (e) {
      debugPrint('[TtsController] TTS error: $e');
      _finish();
    }
  }

  Future<void> stop() async {
    if (_disposed) return;
    _ttsCompleteSub?.cancel();
    _ttsCompleteSub = null;
    try {
      await _audioPlayer.stop();
    } catch (_) {}
    _finish();
  }

  void _finish() {
    if (_disposed) return;
    _ttsPlaying?.value = false;
    _onFinish();
  }

  Future<void> dispose() async {
    if (_disposed) return;
    _disposed = true;
    _ttsCompleteSub?.cancel();
    _ttsCompleteSub = null;
    try {
      await _audioPlayer.stop();
    } catch (_) {}
    await _audioPlayer.dispose();
  }
}

