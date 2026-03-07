import 'dart:convert';
import 'dart:ffi';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:record/record.dart';

import 'vosk_ffi.dart';
import 'vosk_setup.dart';

/// Result of a full calibration session.
class CalibrationResult {
  const CalibrationResult({
    required this.learnedVariants,
    required this.ambientRms,
    required this.verificationPassed,
  });

  final List<String> learnedVariants;
  final double ambientRms;
  final bool verificationPassed;
}

/// Self-contained calibration engine that records audio via [AudioRecorder],
/// feeds it through Vosk via FFI, and produces learned wake-word variants
/// plus an ambient noise baseline.
///
/// Operates independently of [WakeWordListener] so the two never contend
/// for the mic simultaneously (caller pauses the listener first).
class WakeWordCalibrator {
  WakeWordCalibrator({this.onStatus});

  final void Function(String status)? onStatus;

  final _recorder = AudioRecorder();
  VoskFfi? _vosk;
  Pointer<Void>? _model;
  Pointer<Void>? _recognizer;

  bool _cancelled = false;

  // ── Public API ──────────────────────────────────────────────────────────

  /// Measure ambient RMS over [seconds] of silence.
  Future<double> calibrateAmbient({int seconds = 3}) async {
    _cancelled = false;
    await _ensureVosk();
    await _stopRecorder();

    onStatus?.call('Measuring ambient noise...');

    final stream = await _recorder.startStream(
      const RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: 16000,
        numChannels: 1,
      ),
    );

    final deadline = DateTime.now().add(Duration(seconds: seconds));
    double rmsSum = 0;
    int rmsCount = 0;

    await for (final Uint8List chunk in stream) {
      if (_cancelled || DateTime.now().isAfter(deadline)) break;
      final rms = _rms(chunk);
      rmsSum += rms;
      rmsCount++;
    }

    await _stopRecorder();

    final avg = rmsCount > 0 ? rmsSum / rmsCount : 200.0;
    debugPrint('[Calibrator] Ambient RMS: ${avg.toStringAsFixed(1)} '
        '($rmsCount chunks)');
    return avg;
  }

  /// Record one wake-word sample. Returns the text Vosk transcribed,
  /// or empty string on timeout / silence.
  ///
  /// Listens for up to [timeoutSeconds]. A Vosk "final" result triggers
  /// early return.
  Future<String> recordSample({
    int timeoutSeconds = 5,
    void Function(String partial)? onPartial,
  }) async {
    _cancelled = false;
    await _ensureVosk();
    final vosk = _vosk!;
    final rec = _recognizer!;
    vosk.resetRecognizer(rec);

    await _stopRecorder();

    final stream = await _recorder.startStream(
      const RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: 16000,
        numChannels: 1,
      ),
    );

    final deadline =
        DateTime.now().add(Duration(seconds: timeoutSeconds));
    String result = '';

    await for (final Uint8List chunk in stream) {
      if (_cancelled || DateTime.now().isAfter(deadline)) break;

      final hasResult = vosk.acceptWaveform(rec, chunk);
      if (hasResult) {
        final json = vosk.getResult(rec);
        result = _extractText(json);
        if (result.isNotEmpty) break;
      } else {
        final partialJson = vosk.getPartialResult(rec);
        final partial = _extractPartial(partialJson);
        if (partial.isNotEmpty) onPartial?.call(partial);
      }
    }

    // If we timed out without a final, grab whatever Vosk has.
    if (result.isEmpty) {
      final json = vosk.getResult(rec);
      result = _extractText(json);
    }

    await _stopRecorder();
    debugPrint('[Calibrator] Sample: "$result"');
    return result;
  }

  /// Cancel an in-progress recording.
  void cancel() {
    _cancelled = true;
    _stopRecorder();
  }

  void dispose() {
    cancel();
    _freeVosk();
    _recorder.dispose();
  }

  // ── Vosk lifecycle ────────────────────────────────────────────────────

  Future<void> _ensureVosk() async {
    if (_vosk != null && _model != null && _recognizer != null) return;

    onStatus?.call('Preparing speech engine...');

    final dllPath = await VoskSetup.ensureDll(
      onProgress: (msg) => onStatus?.call(msg),
    );
    _vosk = VoskFfi.load(dllPath);
    _vosk!.setLogLevel(-1);

    final modelPath = await VoskSetup.ensureModel(
      onProgress: (msg) => onStatus?.call(msg),
    );
    _model = _vosk!.createModel(modelPath);
    _recognizer = _vosk!.createRecognizer(_model!, sampleRate: 16000);

    debugPrint('[Calibrator] Vosk ready');
  }

  void _freeVosk() {
    final rec = _recognizer;
    final model = _model;
    _recognizer = null;
    _model = null;
    if (rec != null) _vosk?.freeRecognizer(rec);
    if (model != null) _vosk?.freeModel(model);
  }

  // ── Helpers ──────────────────────────────────────────────────────────

  Future<void> _stopRecorder() async {
    try {
      if (await _recorder.isRecording()) await _recorder.stop();
    } catch (_) {}
  }

  static double _rms(Uint8List chunk) {
    if (chunk.length < 2) return 0;
    final aligned = Uint8List.fromList(chunk);
    final samples = aligned.buffer.asInt16List(0, aligned.length ~/ 2);
    double sum = 0;
    for (final s in samples) {
      sum += s * s;
    }
    return math.sqrt(sum / samples.length);
  }

  static String _extractText(String json) {
    try {
      return ((jsonDecode(json) as Map)['text'] as String? ?? '').trim();
    } catch (_) {
      return '';
    }
  }

  static String _extractPartial(String json) {
    try {
      return ((jsonDecode(json) as Map)['partial'] as String? ?? '').trim();
    } catch (_) {
      return '';
    }
  }
}
