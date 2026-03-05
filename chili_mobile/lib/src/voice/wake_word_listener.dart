import 'dart:async';
import 'dart:convert';
import 'dart:ffi';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:record/record.dart';

import '../config/app_config.dart';
import '../network/chili_api_client.dart';
import 'vosk_ffi.dart';
import 'vosk_setup.dart';

/// Continuous, streaming wake-word listener powered by on-device Vosk STT.
///
/// Audio is captured via the `record` package's streaming API (PCM-16, 16 kHz,
/// mono) and fed directly to a Vosk recognizer through dart:ffi.  Each
/// finalized utterance is checked for the wake phrase ("Chili" / "Hey Chili");
/// when matched the remaining command text is sent to the CHILI backend.
class WakeWordListener {
  WakeWordListener({
    required this.onReply,
    required this.onListeningChanged,
    required this.onStatus,
    this.onFollowUpActive,
    this.onPartial,
    ValueNotifier<bool>? pauseListening,
  }) : _pauseListening = pauseListening ?? ValueNotifier<bool>(false) {
    _pauseListening?.addListener(_onPauseChanged);
  }

  final ValueNotifier<bool>? _pauseListening;

  /// Called with `(command, reply)` when a wake-word command is answered.
  final void Function(String command, String reply) onReply;
  final void Function(bool isListening) onListeningChanged;
  final void Function(String status) onStatus;
  /// Called when follow-up mode is active (user can speak without wake word).
  final void Function(bool active)? onFollowUpActive;
  /// Called with live partial transcription (what Vosk is hearing so far).
  final void Function(String partial)? onPartial;

  final _recorder = AudioRecorder();
  bool _running = false;
  bool _streamingActive = false;
  bool _handlingCommand = false;

  /// When set, the next non-wake-word utterance is treated as the command
  /// (Vosk often splits "Hey Chili, what time" into two separate results).
  DateTime? _awaitingCommandUntil;
  static const _commandWindowSeconds = 8;

  /// Continued conversation: after a reply, user can speak again without wake word for 8s.
  DateTime? _followUpUntil;
  static const _followUpSeconds = 8;

  VoskFfi? _vosk;
  Pointer<Void>? _model;
  Pointer<Void>? _recognizer;

  /// Queued finalized utterances waiting to be processed.  We use a queue so
  /// that the FFI acceptWaveform calls (synchronous) are never blocked by
  /// async HTTP calls.
  final _utteranceQueue = StreamController<String>();

  bool get isRunning => _running;

  void start() {
    if (_running) return;
    _running = true;
    onListeningChanged(true);
    _startStreamingLoop();
  }

  void stop() {
    if (!_running) return;
    _running = false;
    _clearFollowUp();
    onListeningChanged(false);
    _stopRecorder();
    onStatus('');
  }

  void dispose() {
    stop();
    _pauseListening?.removeListener(_onPauseChanged);
    _utteranceQueue.close();
    _freeVosk();
    _recorder.dispose();
  }

  // ── Vosk lifecycle ─────────────────────────────────────────────────────

  Future<void> _ensureVoskReady() async {
    if (_vosk != null && _model != null && _recognizer != null) return;

    onStatus('Preparing speech engine (one-time setup)...');

    final dllPath = await VoskSetup.ensureDll(
      onProgress: (msg) => onStatus(msg),
    );
    _vosk = VoskFfi.load(dllPath);
    _vosk!.setLogLevel(-1);

    final modelPath = await VoskSetup.ensureModel(
      onProgress: (msg) => onStatus(msg),
    );
    _model = _vosk!.createModel(modelPath);
    _recognizer = _vosk!.createRecognizer(_model!, sampleRate: 16000);

    debugPrint('[WakeWord] Vosk ready');
    onStatus('Listening for "${AppConfig.instance.wakeWord}"...');
  }

  void _freeVosk() {
    final rec = _recognizer;
    final model = _model;
    _recognizer = null;
    _model = null;
    if (rec != null) _vosk?.freeRecognizer(rec);
    if (model != null) _vosk?.freeModel(model);
  }

  // ── Streaming loop ─────────────────────────────────────────────────────

  Future<void> _startStreamingLoop() async {
    if (!_running || _streamingActive) return;
    if (_pauseListening?.value ?? false) {
      onStatus('Paused (mic in use)');
      return;
    }

    _streamingActive = true;

    final config = AppConfig.instance;
    await config.load();
    if (!config.alwaysListening) {
      _running = false;
      _streamingActive = false;
      onListeningChanged(false);
      onStatus('');
      return;
    }

    final hasPermission = await _recorder.hasPermission();
    if (!hasPermission) {
      onStatus('No mic permission');
      debugPrint('[WakeWord] No mic permission');
      _streamingActive = false;
      return;
    }

    await _stopRecorder();

    try {
      await _ensureVoskReady();
    } catch (e) {
      debugPrint('[WakeWord] Vosk init failed: $e');
      onStatus('Wake word disabled (speech engine error)');
      _running = false;
      _streamingActive = false;
      onListeningChanged(false);
      return;
    }

    onStatus('Say "Chili" or "Hey Chili"...');
    debugPrint('[WakeWord] Streaming started');

    final client = ChiliApiClient();

    // Start the utterance processor in parallel -- it awaits each HTTP call
    // sequentially so we never drop replies.
    _processUtteranceQueue(client);

    try {
      final stream = await _recorder.startStream(
        const RecordConfig(
          encoder: AudioEncoder.pcm16bits,
          sampleRate: 16000,
          numChannels: 1,
        ),
      );

      await for (final Uint8List chunk in stream) {
        if (!_running) break;
        if (_pauseListening?.value ?? false) continue;
        _feedChunk(chunk);
      }
    } catch (e) {
      debugPrint('[WakeWord] Stream error: $e');
    } finally {
      _streamingActive = false;
    }
  }

  // ── Audio → Vosk (synchronous, never blocks on HTTP) ───────────────────

  void _feedChunk(Uint8List chunk) {
    final vosk = _vosk;
    final rec = _recognizer;
    if (vosk == null || rec == null) return;

    try {
      final hasResult = vosk.acceptWaveform(rec, chunk);

      if (hasResult) {
        onPartial?.call('');
        final json = vosk.getResult(rec);
        final text = _extractText(json);
        if (text.isEmpty) return;

        debugPrint('[WakeWord] Heard: "$text"');
        onStatus('Heard: "$text"');

        if (!_utteranceQueue.isClosed) {
          _utteranceQueue.add(text);
        }
      } else {
        final partialJson = vosk.getPartialResult(rec);
        final partial = _extractPartial(partialJson);
        if (partial.isNotEmpty) onPartial?.call(partial);
      }
    } catch (e) {
      debugPrint('[WakeWord] Chunk error: $e');
    }
  }

  // ── Utterance queue processor (awaits HTTP properly) ───────────────────

  Future<void> _processUtteranceQueue(ChiliApiClient client) async {
    await for (final text in _utteranceQueue.stream) {
      if (!_running) break;
      try {
        await _handleUtterance(text, client);
      } catch (e) {
        debugPrint('[WakeWord] Utterance handler error: $e');
      }
    }
  }

  Future<void> _handleUtterance(String text, ChiliApiClient client) async {
    final config = AppConfig.instance;
    await config.load();
    final now = DateTime.now();

    // Continued conversation: within follow-up window, any non-empty utterance is a command.
    if (_followUpUntil != null) {
      if (now.isAfter(_followUpUntil!)) {
        _followUpUntil = null;
        onFollowUpActive?.call(false);
        onStatus('Listening for "${config.wakeWord}"...');
      } else if (text.trim().isNotEmpty) {
        final command = text.trim();
        _followUpUntil = now.add(const Duration(seconds: _followUpSeconds));
        onFollowUpActive?.call(true);
        await _sendCommand(command, client, config);
        return;
      }
    }

    // Check if we're in a "say your command" window from a previous wake word.
    if (_awaitingCommandUntil != null) {
      if (now.isAfter(_awaitingCommandUntil!)) {
        debugPrint('[WakeWord] Command window expired');
        _awaitingCommandUntil = null;
      } else if (text.isNotEmpty) {
        // Accept ANYTHING in the window as the command (even if it contains
        // the wake word again -- user might say "Chili, what time is it").
        final stripped = config.isWakeWordMatch(text)
            ? config.stripWakeWord(text)
            : text.trim();
        if (stripped.isNotEmpty) {
          _awaitingCommandUntil = null;
          await _sendCommand(stripped, client, config);
          return;
        }
      }
    }

    if (!config.isWakeWordMatch(text)) {
      onStatus('Listening for "${config.wakeWord}"...');
      _clearFollowUp();
      return;
    }

    final command = config.stripWakeWord(text);
    if (command.isEmpty) {
      debugPrint('[WakeWord] Wake word only -- opening ${_commandWindowSeconds}s command window');
      onStatus('Listening... say your command');
      _awaitingCommandUntil =
          now.add(const Duration(seconds: _commandWindowSeconds));
      return;
    }

    await _sendCommand(command, client, config);
  }

  Future<void> _sendCommand(
    String command,
    ChiliApiClient client,
    AppConfig config,
  ) async {
    if (_handlingCommand) {
      debugPrint('[WakeWord] Already handling a command, queued: "$command"');
      return;
    }
    _handlingCommand = true;
    debugPrint('[WakeWord] >>> Sending command: "$command"');
    onStatus('Processing: "$command"');
    try {
      final reply = await client.sendMessage(command).timeout(
        const Duration(seconds: 15),
        onTimeout: () => 'CHILI took too long to respond. Please try again.',
      );
      debugPrint('[WakeWord] <<< Reply received (${reply.length} chars)');
      onReply(command, reply);
      _followUpUntil = DateTime.now().add(const Duration(seconds: _followUpSeconds));
      onFollowUpActive?.call(true);
      onStatus('Listening... (follow-up)');
    } catch (e) {
      debugPrint('[WakeWord] !!! sendMessage error: $e');
      final errMsg =
          e.toString().replaceFirst(RegExp(r'^Exception:?\s*'), '');
      onReply(command, 'Could not reach CHILI. $errMsg');
      onStatus('Error – check server');
      _followUpUntil = null;
      onFollowUpActive?.call(false);
    } finally {
      _handlingCommand = false;
    }
  }

  void _clearFollowUp() {
    _followUpUntil = null;
    onFollowUpActive?.call(false);
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  Future<void> _stopRecorder() async {
    try {
      if (await _recorder.isRecording()) await _recorder.stop();
    } catch (_) {}
  }

  void _onPauseChanged() {
    if (!_running) return;
    if (_pauseListening?.value ?? false) {
      onStatus('Paused (mic in use)');
      _stopRecorder();
    } else {
      _startStreamingLoop();
    }
  }

  String _extractText(String json) {
    try {
      return ((jsonDecode(json) as Map)['text'] as String? ?? '').trim();
    } catch (_) {
      return '';
    }
  }

  String _extractPartial(String json) {
    try {
      return ((jsonDecode(json) as Map)['partial'] as String? ?? '').trim();
    } catch (_) {
      return '';
    }
  }
}
