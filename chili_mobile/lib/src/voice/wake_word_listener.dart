import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:record/record.dart';

import '../config/app_config.dart';
import '../network/chili_api_client.dart';

/// Listens continuously for the configured wake word in recorded audio.
/// When detected, transcribes the rest of the utterance and sends it to chat.
class WakeWordListener {
  WakeWordListener({
    required this.onReply,
    required this.onListeningChanged,
    ValueNotifier<bool>? pauseListening,
  }) : _pauseListening = pauseListening ?? ValueNotifier(false);

  final ValueNotifier<bool>? _pauseListening;
  final void Function(String reply) onReply;
  final void Function(bool isListening) onListeningChanged;

  static const int _recordSeconds = 3;
  static const Duration _gapBetweenRecords = Duration(milliseconds: 800);

  final _recorder = AudioRecorder();
  bool _running = false;
  Future<void>? _loopTask;

  bool get isRunning => _running;

  void start() {
    if (_running) return;
    _running = true;
    onListeningChanged(true);
    _loopTask = _runLoop();
  }

  void stop() {
    _running = false;
    onListeningChanged(false);
  }

  Future<void> _runLoop() async {
    final config = AppConfig.instance;
    if (!config.isLoaded) await config.load();
    if (!config.alwaysListening) {
      _running = false;
      onListeningChanged(false);
      return;
    }

    final client = ChiliApiClient();

    while (_running) {
      await config.load();
      if (!config.alwaysListening) {
        _running = false;
        onListeningChanged(false);
        return;
      }

      if (_pauseListening?.value ?? false) {
        await Future.delayed(const Duration(milliseconds: 500));
        continue;
      }

      final hasPermission = await _recorder.hasPermission();
      if (!hasPermission) {
        await Future.delayed(const Duration(seconds: 10));
        continue;
      }

      final dir = await getTemporaryDirectory();
      final path = p.join(
        dir.path,
        'chili_wake_${DateTime.now().millisecondsSinceEpoch}.wav',
      );

      try {
        await _recorder.start(
          const RecordConfig(encoder: AudioEncoder.wav),
          path: path,
        );
        await Future.delayed(Duration(seconds: _recordSeconds));
        final stoppedPath = await _recorder.stop();
        final filePath = stoppedPath ?? path;
        final file = File(filePath);
        if (!await file.exists()) {
          await Future.delayed(_gapBetweenRecords);
          continue;
        }

        final text = await client.transcribe(file);
        try {
          if (await file.exists()) await file.delete();
        } catch (_) {}

        if (text == null || text.isEmpty) {
          await Future.delayed(_gapBetweenRecords);
          continue;
        }

        if (!config.isWakeWordMatch(text)) {
          await Future.delayed(_gapBetweenRecords);
          continue;
        }

        final command = config.stripWakeWord(text);
        if (command.isEmpty) {
          await Future.delayed(_gapBetweenRecords);
          continue;
        }

        try {
          final reply = await client.sendMessage(command);
          onReply(reply);
        } catch (_) {
          // ignore and continue
        }
      } catch (_) {
        // e.g. recorder busy; skip this window
      }

      await Future.delayed(_gapBetweenRecords);
    }
  }

  void dispose() {
    stop();
    _recorder.dispose();
  }
}
