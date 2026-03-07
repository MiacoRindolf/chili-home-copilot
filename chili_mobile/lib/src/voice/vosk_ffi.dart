import 'dart:ffi';
import 'dart:typed_data';

import 'package:ffi/ffi.dart';
import 'package:path/path.dart' as p;

/// Thin dart:ffi wrapper around the Vosk C API (libvosk.dll).
///
/// Only exposes the subset needed for streaming wake-word detection:
/// model load/free, recognizer create/free, accept_waveform, result,
/// partial_result, and set_log_level.
class VoskFfi {
  VoskFfi._(this._lib);

  final DynamicLibrary _lib;
  static VoskFfi? _instance;

  /// Load libvosk from [dllPath]. Returns a cached singleton after the first
  /// successful call.
  ///
  /// Before opening libvosk we call Win32 `SetDllDirectoryW` so that Windows
  /// searches the same folder for transitive dependencies (libgcc, libstdc++,
  /// libwinpthread).
  static VoskFfi load(String dllPath) {
    if (_instance != null) return _instance!;

    // Tell Windows to search the vosk folder for dependency DLLs.
    final kernel32 = DynamicLibrary.open('kernel32.dll');
    final setDllDir = kernel32.lookupFunction<
        Int32 Function(Pointer<Utf16>),
        int Function(Pointer<Utf16>)>('SetDllDirectoryW');

    final dirPath = p.dirname(dllPath);
    final dirPtr = dirPath.toNativeUtf16();
    setDllDir(dirPtr);
    malloc.free(dirPtr);

    final lib = DynamicLibrary.open(dllPath);
    _instance = VoskFfi._(lib);
    return _instance!;
  }

  // ── Model ──────────────────────────────────────────────────────────────

  late final _modelNew = _lib.lookupFunction<
      Pointer<Void> Function(Pointer<Utf8>),
      Pointer<Void> Function(Pointer<Utf8>)>('vosk_model_new');

  late final _modelFree = _lib.lookupFunction<
      Void Function(Pointer<Void>),
      void Function(Pointer<Void>)>('vosk_model_free');

  Pointer<Void> createModel(String modelPath) {
    final pathPtr = modelPath.toNativeUtf8();
    try {
      final model = _modelNew(pathPtr);
      if (model == nullptr) {
        throw StateError('Failed to load Vosk model at "$modelPath"');
      }
      return model;
    } finally {
      malloc.free(pathPtr);
    }
  }

  void freeModel(Pointer<Void> model) => _modelFree(model);

  // ── Recognizer ─────────────────────────────────────────────────────────

  late final _recNew = _lib.lookupFunction<
      Pointer<Void> Function(Pointer<Void>, Float),
      Pointer<Void> Function(Pointer<Void>, double)>('vosk_recognizer_new');

  late final _recFree = _lib.lookupFunction<
      Void Function(Pointer<Void>),
      void Function(Pointer<Void>)>('vosk_recognizer_free');

  late final _recReset = _lib.lookupFunction<
      Void Function(Pointer<Void>),
      void Function(Pointer<Void>)>('vosk_recognizer_reset');

  late final _accept = _lib.lookupFunction<
      Int32 Function(Pointer<Void>, Pointer<Uint8>, Int32),
      int Function(
          Pointer<Void>, Pointer<Uint8>, int)>('vosk_recognizer_accept_waveform');

  late final _result = _lib.lookupFunction<
      Pointer<Utf8> Function(Pointer<Void>),
      Pointer<Utf8> Function(Pointer<Void>)>('vosk_recognizer_result');

  late final _partial = _lib.lookupFunction<
      Pointer<Utf8> Function(Pointer<Void>),
      Pointer<Utf8> Function(Pointer<Void>)>('vosk_recognizer_partial_result');

  Pointer<Void> createRecognizer(
    Pointer<Void> model, {
    double sampleRate = 16000,
  }) {
    final rec = _recNew(model, sampleRate);
    if (rec == nullptr) {
      throw StateError('Failed to create Vosk recognizer');
    }
    return rec;
  }

  void freeRecognizer(Pointer<Void> rec) => _recFree(rec);

  /// Flush the recognizer's internal buffers, discarding any accumulated
  /// partial result.  Useful after TTS playback to prevent echo.
  void resetRecognizer(Pointer<Void> rec) => _recReset(rec);

  /// Feed raw PCM-16 LE mono audio bytes to the recognizer.
  /// Returns `true` when Vosk considers the current utterance complete
  /// (call [getResult] to retrieve the text).
  bool acceptWaveform(Pointer<Void> rec, Uint8List data) {
    final ptr = malloc<Uint8>(data.length);
    try {
      ptr.asTypedList(data.length).setAll(0, data);
      return _accept(rec, ptr, data.length) != 0;
    } finally {
      malloc.free(ptr);
    }
  }

  /// JSON string with `{"text": "..."}`. The returned Dart string is a copy;
  /// the native pointer is owned by Vosk and must NOT be freed.
  String getResult(Pointer<Void> rec) => _result(rec).toDartString();

  /// JSON string with `{"partial": "..."}`.
  String getPartialResult(Pointer<Void> rec) => _partial(rec).toDartString();

  // ── Log level ──────────────────────────────────────────────────────────

  late final _setLogLevel = _lib.lookupFunction<
      Void Function(Int32), void Function(int)>('vosk_set_log_level');

  /// -1 = silent, 0 = errors, 1 = warnings, 2+ = verbose.
  void setLogLevel(int level) => _setLogLevel(level);
}
