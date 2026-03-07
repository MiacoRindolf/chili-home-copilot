import 'dart:async';
import 'dart:convert';
import 'dart:ffi';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:record/record.dart';

import '../config/app_config.dart';
import '../desktop/desktop_actions.dart';
import '../network/chili_api_client.dart';
import '../screen/focus_controller.dart';
import 'vosk_ffi.dart';
import 'vosk_setup.dart';

/// Continuous, streaming wake-word listener powered by on-device Vosk STT.
///
/// Audio is captured via the `record` package's streaming API (PCM-16, 16 kHz,
/// mono) and fed directly to a Vosk recognizer through dart:ffi.  Each
/// finalized utterance is checked for the wake phrase ("Chili" / "Hey Chili");
/// when matched the remaining command text is sent to the CHILI backend.
///
/// For command transcription accuracy, audio is buffered and sent to the
/// backend Whisper API, falling back to Vosk text when unavailable.
///
/// Echo suppression layers:
/// 1. **Barge-in**: RMS energy threshold detects user speaking over TTS.
/// 2. **Cooldown**: 1.5 s dead period after TTS ends; Vosk is reset.
/// 3. **Textual echo cancellation**: utterances matching TTS text are discarded.
class WakeWordListener {
  WakeWordListener({
    required this.onReply,
    required this.onListeningChanged,
    required this.onStatus,
    this.onFollowUpActive,
    this.onPartial,
    this.onTtsInterruptRequested,
    this.focusController,
    ValueNotifier<bool>? pauseListening,
    ValueNotifier<bool>? ttsPlaying,
    ValueNotifier<String?>? lastTtsText,
  })  : _pauseListening = pauseListening ?? ValueNotifier<bool>(false),
        _ttsPlaying = ttsPlaying,
        _lastTtsText = lastTtsText {
    _pauseListening?.addListener(_onPauseChanged);
    _ttsPlaying?.addListener(_onTtsPlayingChanged);
  }

  final ValueNotifier<bool>? _pauseListening;
  final ValueNotifier<bool>? _ttsPlaying;
  final ValueNotifier<String?>? _lastTtsText;

  final void Function(String command, String reply) onReply;
  final void Function(bool isListening) onListeningChanged;
  final void Function(String status) onStatus;
  final void Function(bool active)? onFollowUpActive;
  final void Function(String partial)? onPartial;
  /// Called when a TTS-interrupt phrase is detected during playback.
  final void Function()? onTtsInterruptRequested;

  /// When non-null and focused, a screenshot is captured and attached to every
  /// hands-free command so the LLM receives visual context.
  final FocusController? focusController;

  static const Set<String> _stopPhrases = {
    'stop', 'cancel', 'enough', 'never mind', 'nevermind',
    'chili stop', 'chile stop', 'chilly stop',
    'quiet', 'hush', 'stop talking', 'shut up',
    'okay stop', 'ok stop',
  };

  final _recorder = AudioRecorder();
  final _client = ChiliApiClient();
  bool _running = false;
  bool _streamingActive = false;
  bool _handlingCommand = false;
  bool _queueProcessorRunning = false;

  DateTime? _awaitingCommandUntil;
  static const _commandWindowSeconds = 8;

  DateTime? _followUpUntil;
  static const _followUpSeconds = 8;

  // Post-TTS cooldown: ignore utterances for this duration after TTS ends.
  DateTime? _ttsCooldownUntil;
  static const _ttsCooldownMs = 1500;

  // Track whether TTS was playing in previous listener tick (for edge detect).
  bool _wasTtsPlaying = false;

  // Barge-in: adaptive echo-baseline approach.
  //
  // During TTS the mic picks up speaker output at a roughly constant energy.
  // We track a running average of that "echo baseline" and only trigger
  // barge-in when energy spikes well above it — indicating the user is
  // speaking on top of the TTS output.  This avoids false triggers from the
  // speaker alone (which a fixed threshold cannot prevent without hardware AEC).
  int _bargeInCounter = 0;
  static const _bargeInChunksNeeded = 5;
  static const _bargeInSpikeMultiplier = 2.5;
  static const _bargeInMinAbsoluteThreshold = 3000.0;
  bool _bargeInTriggered = false;

  // Exponential moving average of RMS during TTS (echo baseline).
  double _ttsEchoBaseline = 0;
  static const _echoBaselineAlpha = 0.15; // smoothing factor
  int _ttsBaselineChunkCount = 0;
  // How many chunks to collect before allowing barge-in detection (warm-up).
  static const _echoBaselineWarmUp = 10;

  VoskFfi? _vosk;
  Pointer<Void>? _model;
  Pointer<Void>? _recognizer;

  /// Single-subscription queue; one long-lived consumer started in [start].
  final _utteranceQueue = StreamController<String>();

  // Rolling PCM buffer: accumulates audio for the current utterance (since
  // the last Vosk final).  On each Vosk final the buffer is snapshotted into
  // [_lastFinalizedPcm] so that [_sendCommand] can forward it to Whisper.
  final List<Uint8List> _currentPcmBuffer = [];
  List<Uint8List>? _lastFinalizedPcm;

  bool get isRunning => _running;

  // ── Lifecycle ──────────────────────────────────────────────────────────

  void start() {
    if (_running) return;
    _running = true;
    onListeningChanged(true);
    if (!_queueProcessorRunning) {
      _queueProcessorRunning = true;
      _processUtteranceQueue();
    }
    _startStreamingLoop();
  }

  void stop() {
    if (!_running) return;
    _running = false;
    _currentPcmBuffer.clear();
    _lastFinalizedPcm = null;
    _clearFollowUp();
    onListeningChanged(false);
    _stopRecorder();
    onStatus('');
  }

  void dispose() {
    stop();
    _pauseListening?.removeListener(_onPauseChanged);
    _ttsPlaying?.removeListener(_onTtsPlayingChanged);
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

  // ── Audio -> Vosk (synchronous, never blocks on HTTP) ──────────────────

  /// Calculate RMS energy of a PCM-16 LE mono chunk.
  static double _rms(Uint8List chunk) {
    if (chunk.length < 2) return 0;
    // Copy to a fresh buffer so the offset is always 0-aligned for Int16List.
    final aligned = Uint8List.fromList(chunk);
    final samples = aligned.buffer.asInt16List(0, aligned.length ~/ 2);
    double sum = 0;
    for (final s in samples) {
      sum += s * s;
    }
    return math.sqrt(sum / samples.length);
  }

  void _feedChunk(Uint8List chunk) {
    final vosk = _vosk;
    final rec = _recognizer;
    if (vosk == null || rec == null) return;

    // ── Layer 1: Adaptive barge-in detection during TTS ──
    //
    // Without hardware AEC the mic hears the speaker output. We track a
    // running echo-baseline (EMA of RMS) and only trigger barge-in when
    // energy exceeds baseline * multiplier — meaning the user is speaking
    // on top of the TTS audio.
    if (_ttsPlaying?.value ?? false) {
      final rms = _rms(chunk);

      // Update echo baseline via exponential moving average.
      if (_ttsBaselineChunkCount == 0) {
        _ttsEchoBaseline = rms;
      } else {
        _ttsEchoBaseline = _echoBaselineAlpha * rms +
            (1 - _echoBaselineAlpha) * _ttsEchoBaseline;
      }
      _ttsBaselineChunkCount++;

      // Wait for the baseline to stabilise before checking for spikes.
      if (_ttsBaselineChunkCount >= _echoBaselineWarmUp && !_bargeInTriggered) {
        final dynamicThreshold = _ttsEchoBaseline * _bargeInSpikeMultiplier;
        final effectiveThreshold = dynamicThreshold > _bargeInMinAbsoluteThreshold
            ? dynamicThreshold
            : _bargeInMinAbsoluteThreshold;

        if (rms > effectiveThreshold) {
          _bargeInCounter++;
          if (_bargeInCounter >= _bargeInChunksNeeded) {
            _bargeInTriggered = true;
            debugPrint('[WakeWord] Barge-in detected '
                '(RMS=${rms.toStringAsFixed(0)}, '
                'baseline=${_ttsEchoBaseline.toStringAsFixed(0)}, '
                'threshold=${effectiveThreshold.toStringAsFixed(0)})');
            onTtsInterruptRequested?.call();
            _resetVoskState();
            _awaitingCommandUntil =
                DateTime.now().add(const Duration(seconds: _commandWindowSeconds));
            onStatus('Listening... say your command');
          }
        } else {
          _bargeInCounter = 0;
        }
      }
      return;
    }

    // Normal (non-TTS) path: feed audio to Vosk.
    _bargeInCounter = 0;
    _bargeInTriggered = false;
    _ttsBaselineChunkCount = 0;
    _ttsEchoBaseline = 0;

    _currentPcmBuffer.add(Uint8List.fromList(chunk));
    int total = 0;
    for (final c in _currentPcmBuffer) {
      total += c.length;
    }
    while (total > 480000 && _currentPcmBuffer.isNotEmpty) {
      total -= _currentPcmBuffer.removeAt(0).length;
    }

    try {
      final hasResult = vosk.acceptWaveform(rec, chunk);

      if (hasResult) {
        onPartial?.call('');
        final json = vosk.getResult(rec);
        final text = _extractText(json);

        _lastFinalizedPcm = List<Uint8List>.from(_currentPcmBuffer);
        _currentPcmBuffer.clear();

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

  // ── Utterance queue processor (single long-lived consumer) ─────────────

  Future<void> _processUtteranceQueue() async {
    await for (final text in _utteranceQueue.stream) {
      if (!_running) continue;
      try {
        await _handleUtterance(text);
      } catch (e) {
        debugPrint('[WakeWord] Utterance handler error: $e');
      }
    }
    _queueProcessorRunning = false;
  }

  Future<void> _handleUtterance(String text) async {
    // During TTS playback only stop phrases are honoured (ignore echo).
    if (_ttsPlaying?.value ?? false) {
      if (_isStopPhrase(text)) {
        debugPrint('[WakeWord] Stop phrase during TTS: "$text"');
        onTtsInterruptRequested?.call();
      }
      return;
    }

    final now = DateTime.now();

    // Layer 2: Post-TTS cooldown -- discard utterances that are residual echo.
    if (_ttsCooldownUntil != null) {
      if (now.isBefore(_ttsCooldownUntil!)) {
        debugPrint('[WakeWord] Cooldown: discarding "$text"');
        return;
      }
      _ttsCooldownUntil = null;
    }

    // Layer 3: Textual echo cancellation.
    if (_isTextualEcho(text)) {
      debugPrint('[WakeWord] Textual echo: discarding "$text"');
      return;
    }

    final config = AppConfig.instance;
    await config.load();

    // Continued conversation: within follow-up window any utterance is a command.
    if (_followUpUntil != null) {
      if (now.isAfter(_followUpUntil!)) {
        _followUpUntil = null;
        onFollowUpActive?.call(false);
        onStatus('Listening for "${config.wakeWord}"...');
      } else if (text.trim().isNotEmpty) {
        final command = text.trim();
        _followUpUntil = now.add(const Duration(seconds: _followUpSeconds));
        onFollowUpActive?.call(true);
        await _sendCommand(command, config);
        return;
      }
    }

    // "Say your command" window from a previous wake-word-only utterance.
    if (_awaitingCommandUntil != null) {
      if (now.isAfter(_awaitingCommandUntil!)) {
        debugPrint('[WakeWord] Command window expired');
        _awaitingCommandUntil = null;
      } else if (text.isNotEmpty) {
        final stripped = config.isWakeWordMatch(text)
            ? config.stripWakeWord(text)
            : text.trim();
        if (stripped.isNotEmpty) {
          _awaitingCommandUntil = null;
          await _sendCommand(stripped, config);
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
      debugPrint(
          '[WakeWord] Wake word only -- opening ${_commandWindowSeconds}s command window');
      onStatus('Listening... say your command');
      _awaitingCommandUntil =
          now.add(const Duration(seconds: _commandWindowSeconds));
      return;
    }

    await _sendCommand(command, config);
  }

  // ── Send command (Whisper-first, Vosk fallback) ────────────────────────

  Future<void> _sendCommand(String voskCommand, AppConfig config) async {
    if (_handlingCommand) {
      debugPrint('[WakeWord] Already handling a command, skipped: "$voskCommand"');
      return;
    }
    _handlingCommand = true;

    String finalCommand = voskCommand;

    // Try backend Whisper for higher accuracy.
    final pcm = _lastFinalizedPcm;
    _lastFinalizedPcm = null;
    if (pcm != null && pcm.isNotEmpty) {
      final wavBytes = _buildWav(pcm);
      debugPrint(
          '[WakeWord] Sending ${wavBytes.length} bytes to Whisper...');
      onStatus('Transcribing...');
      try {
        final whisperText =
            await _client.transcribeAudioBytes(wavBytes).timeout(
                  const Duration(seconds: 10),
                );
        if (whisperText != null && whisperText.trim().isNotEmpty) {
          final stripped = config.isWakeWordMatch(whisperText)
              ? config.stripWakeWord(whisperText)
              : whisperText.trim();
          if (stripped.isNotEmpty) {
            finalCommand = stripped;
            debugPrint(
                '[WakeWord] Whisper: "$finalCommand" (Vosk had: "$voskCommand")');
          }
        }
      } catch (e) {
        debugPrint('[WakeWord] Whisper failed, using Vosk text: $e');
      }
    }

    // Capture a Focus Mode screenshot if active.
    String? focusImagePath;
    final fc = focusController;
    if (fc != null && fc.isFocused.value) {
      debugPrint('[WakeWord] Focus Mode active – capturing screenshot…');
      focusImagePath = await fc.captureNow();
    }

    final messageToSend = focusImagePath != null
        ? '[User has Focus Mode active on ${fc?.target?.label ?? 'screen'}. '
          'The attached image shows their current view.] $finalCommand'
        : finalCommand;

    debugPrint('[WakeWord] >>> Sending command: "$finalCommand"');
    onStatus(focusImagePath != null
        ? 'Analyzing screenshot…'
        : 'Processing: "$finalCommand"');
    try {
      final ChatResponse resp;
      if (focusImagePath != null) {
        resp = await _client
            .sendMessageStreamWithImages(
              messageToSend,
              imagePaths: [focusImagePath],
            )
            .timeout(
              const Duration(seconds: 90),
              onTimeout: () => ChatResponse(
                  reply: 'Vision processing took too long. Try a smaller '
                      'region or ask again.'),
            );
      } else {
        resp = await _client.sendMessage(messageToSend).timeout(
              const Duration(seconds: 15),
              onTimeout: () => ChatResponse(
                  reply: 'CHILI took too long to respond. Please try again.'),
            );
      }
      debugPrint('[WakeWord] <<< Reply received (${resp.reply.length} chars)');
      DesktopActions.execute(resp.clientAction);
      onReply(finalCommand, resp.reply);
      _followUpUntil =
          DateTime.now().add(const Duration(seconds: _followUpSeconds));
      onFollowUpActive?.call(true);
      onStatus('Listening... (follow-up)');
    } catch (e) {
      debugPrint('[WakeWord] !!! sendMessage error: $e');
      final errMsg =
          e.toString().replaceFirst(RegExp(r'^Exception:?\s*'), '');
      onReply(finalCommand, 'Could not reach CHILI. $errMsg');
      onStatus('Error -- check server');
      _followUpUntil = null;
      onFollowUpActive?.call(false);
    } finally {
      _handlingCommand = false;
    }
  }

  // ── TTS state change handler (cooldown + auto follow-up) ───────────────

  void _onTtsPlayingChanged() {
    final playing = _ttsPlaying?.value ?? false;

    if (!_wasTtsPlaying && playing) {
      // TTS just started → reset echo baseline so it adapts to this session.
      _ttsEchoBaseline = 0;
      _ttsBaselineChunkCount = 0;
      _bargeInCounter = 0;
      _bargeInTriggered = false;
    }

    if (_wasTtsPlaying && !playing) {
      // TTS just finished → start cooldown and reset Vosk.
      debugPrint('[WakeWord] TTS ended → ${_ttsCooldownMs}ms cooldown');
      _ttsCooldownUntil =
          DateTime.now().add(const Duration(milliseconds: _ttsCooldownMs));
      _resetVoskState();

      // Auto follow-up: keep listening for the user's reply without wake word.
      _followUpUntil =
          DateTime.now().add(const Duration(seconds: _followUpSeconds + _ttsCooldownMs ~/ 1000));
      onFollowUpActive?.call(true);
      onStatus('Listening... (follow-up)');
    }
    _wasTtsPlaying = playing;
  }

  /// Reset Vosk recognizer and clear all audio buffers to flush echo residue.
  void _resetVoskState() {
    final vosk = _vosk;
    final rec = _recognizer;
    if (vosk != null && rec != null) {
      vosk.resetRecognizer(rec);
    }
    _currentPcmBuffer.clear();
    _lastFinalizedPcm = null;
    onPartial?.call('');
  }

  // ── Textual echo cancellation ────────────────────────────────────────

  /// Returns true if [text] is an echo of the last TTS output (>50% word overlap).
  bool _isTextualEcho(String text) {
    final ttsText = _lastTtsText?.value;
    if (ttsText == null || ttsText.isEmpty) return false;

    final heardWords = text.toLowerCase().split(RegExp(r'\s+')).toSet();
    final ttsWords = ttsText.toLowerCase().split(RegExp(r'\s+')).toSet();
    if (heardWords.isEmpty || ttsWords.isEmpty) return false;

    final overlap = heardWords.intersection(ttsWords).length;
    final ratio = overlap / heardWords.length;
    if (ratio > 0.5) {
      debugPrint('[WakeWord] Echo ratio=${ratio.toStringAsFixed(2)} '
          '(heard=${heardWords.length}, overlap=$overlap)');
      return true;
    }
    return false;
  }

  // ── Stop-phrase detection ──────────────────────────────────────────────

  bool _isStopPhrase(String text) {
    final normalized = text.trim().toLowerCase();
    if (normalized.isEmpty) return false;
    return _stopPhrases.contains(normalized);
  }

  void _clearFollowUp() {
    _followUpUntil = null;
    onFollowUpActive?.call(false);
  }

  // ── WAV builder (16-bit, 16 kHz, mono) ─────────────────────────────────

  static Uint8List _buildWav(List<Uint8List> pcmChunks) {
    int totalLen = 0;
    for (final c in pcmChunks) {
      totalLen += c.length;
    }
    final header = ByteData(44);
    header.setUint8(0, 0x52); // R
    header.setUint8(1, 0x49); // I
    header.setUint8(2, 0x46); // F
    header.setUint8(3, 0x46); // F
    header.setUint32(4, 36 + totalLen, Endian.little);
    header.setUint8(8, 0x57);  // W
    header.setUint8(9, 0x41);  // A
    header.setUint8(10, 0x56); // V
    header.setUint8(11, 0x45); // E
    header.setUint8(12, 0x66); // f
    header.setUint8(13, 0x6D); // m
    header.setUint8(14, 0x74); // t
    header.setUint8(15, 0x20); // (space)
    header.setUint32(16, 16, Endian.little);
    header.setUint16(20, 1, Endian.little);     // PCM
    header.setUint16(22, 1, Endian.little);     // mono
    header.setUint32(24, 16000, Endian.little); // sample rate
    header.setUint32(28, 32000, Endian.little); // byte rate
    header.setUint16(32, 2, Endian.little);     // block align
    header.setUint16(34, 16, Endian.little);    // bits per sample
    header.setUint8(36, 0x64); // d
    header.setUint8(37, 0x61); // a
    header.setUint8(38, 0x74); // t
    header.setUint8(39, 0x61); // a
    header.setUint32(40, totalLen, Endian.little);

    final wav = BytesBuilder();
    wav.add(header.buffer.asUint8List());
    for (final c in pcmChunks) {
      wav.add(c);
    }
    return wav.toBytes();
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
