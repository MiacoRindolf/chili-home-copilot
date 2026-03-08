import 'dart:async';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/widgets.dart';

import '../network/chili_api_client.dart';

/// Encapsulates TTS playback for Chili using the backend's Edge TTS neural
/// voice (en-US-AndrewMultilingualNeural).
///
/// Text is split into sentence-level segments.  The first segment is fetched
/// and played immediately (~500 ms to first audio) while subsequent segments
/// are pre-fetched in the background.  This dramatically reduces perceived
/// latency compared to waiting for the full message audio.
///
/// [_ttsPlaying] is only set to `true` once actual audio playback begins so
/// the wake-word listener won't show "follow-up" prematurely.
class TtsController {
  TtsController({
    required ChiliApiClient client,
    required ValueNotifier<bool>? ttsPlaying,
    required VoidCallback onFinish,
    ValueNotifier<String?>? lastTtsText,
  })  : _client = client,
        _ttsPlaying = ttsPlaying,
        _onFinish = onFinish,
        _lastTtsText = lastTtsText;

  final ChiliApiClient _client;
  final ValueNotifier<bool>? _ttsPlaying;
  final VoidCallback _onFinish;
  final ValueNotifier<String?>? _lastTtsText;

  final AudioPlayer _audioPlayer = AudioPlayer();
  bool _disposed = false;
  bool _speaking = false;
  Completer<void>? _segmentDone;

  Timer? _echoTextTimer;
  StreamSubscription<void>? _completeSub;

  Future<void> speak(String text) async {
    if (_disposed) return;
    final clean = _cleanForTts(text.trim());
    if (clean.isEmpty) {
      _finish();
      return;
    }

    _speaking = true;

    _lastTtsText?.value = clean;
    _echoTextTimer?.cancel();
    _echoTextTimer = Timer(const Duration(seconds: 30), () {
      _lastTtsText?.value = null;
    });

    final segments = _splitIntoSegments(clean);
    debugPrint('[TtsController] ${segments.length} segment(s) to play');

    Future<List<int>?>? prefetch;
    bool signalled = false;

    for (int i = 0; i < segments.length; i++) {
      if (!_speaking || _disposed) break;

      // Obtain audio: first segment is fetched now; others from pre-fetch.
      List<int>? bytes;
      if (i == 0) {
        bytes = await _fetchAudio(segments[i]);
      } else {
        try {
          bytes = await prefetch;
        } catch (_) {
          bytes = null;
        }
      }

      if (bytes == null || bytes.isEmpty || !_speaking || _disposed) break;

      // Pre-fetch next segment while the current one plays.
      if (i + 1 < segments.length && _speaking) {
        prefetch = _fetchAudio(segments[i + 1]);
      }

      if (!signalled) {
        _ttsPlaying?.value = true;
        signalled = true;
      }

      // Play this segment and await completion.
      await _playSegment(Uint8List.fromList(bytes));
    }

    _finish();
  }

  Future<List<int>?> _fetchAudio(String text) async {
    try {
      List<int>? bytes = await _client.fetchTtsStreaming(text);
      bytes ??= await _client.fetchTts(text);
      return bytes;
    } catch (e) {
      debugPrint('[TtsController] fetch error: $e');
      return null;
    }
  }

  /// Plays a single audio segment and returns when it finishes.
  Future<void> _playSegment(Uint8List bytes) async {
    _segmentDone = Completer<void>();

    _completeSub?.cancel();
    _completeSub = _audioPlayer.onPlayerComplete.listen((_) {
      if (_segmentDone != null && !_segmentDone!.isCompleted) {
        _segmentDone!.complete();
      }
    });

    try {
      await _audioPlayer.play(BytesSource(bytes));
      // Wait for segment to finish playing (with a safety timeout).
      await _segmentDone!.future.timeout(
        const Duration(seconds: 120),
        onTimeout: () {},
      );
    } catch (e) {
      debugPrint('[TtsController] playSegment error: $e');
      if (_segmentDone != null && !_segmentDone!.isCompleted) {
        _segmentDone!.complete();
      }
    }
  }

  Future<void> stop() async {
    if (_disposed) return;
    _speaking = false;
    if (_segmentDone != null && !_segmentDone!.isCompleted) {
      _segmentDone!.complete();
    }
    _completeSub?.cancel();
    _completeSub = null;
    try {
      await _audioPlayer.stop();
    } catch (_) {}
    _finish();
  }

  void _finish() {
    if (_disposed) return;
    _speaking = false;
    _ttsPlaying?.value = false;
    _onFinish();
  }

  // ── Text processing ──────────────────────────────────────────────────

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

  /// Split text into sentence-sized segments for chunked TTS.
  ///
  /// The first segment is kept short (~60 chars / 1-2 sentences) so first
  /// audio arrives as fast as possible.  Subsequent segments are larger
  /// (~150 chars) since they're pre-fetched while the prior segment plays.
  static List<String> _splitIntoSegments(String text) {
    final parts = text.split(RegExp(r'(?<=[.!?])\s+'));
    if (parts.length <= 1) return [text];

    final segments = <String>[];
    var buffer = '';
    for (final part in parts) {
      buffer = buffer.isEmpty ? part : '$buffer $part';
      final threshold = segments.isEmpty ? 60 : 150;
      if (buffer.length >= threshold) {
        segments.add(buffer);
        buffer = '';
      }
    }
    if (buffer.isNotEmpty) segments.add(buffer);
    return segments;
  }

  // ── Lifecycle ────────────────────────────────────────────────────────

  Future<void> dispose() async {
    if (_disposed) return;
    _disposed = true;
    _speaking = false;
    _echoTextTimer?.cancel();
    if (_segmentDone != null && !_segmentDone!.isCompleted) {
      _segmentDone!.complete();
    }
    _completeSub?.cancel();
    _completeSub = null;
    try {
      await _audioPlayer.stop();
    } catch (_) {}
    await _audioPlayer.dispose();
  }
}
