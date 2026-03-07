import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_screen_capture/flutter_screen_capture.dart';
import 'package:image/image.dart' as img;
import 'package:path_provider/path_provider.dart';

import 'focus_target.dart';

/// On-demand screen capture controller for CHILI's Focus Mode.
///
/// Unlike the old periodic "screen share", this controller stores only a
/// [FocusTarget] and captures a single fresh screenshot when [captureNow] is
/// called (i.e. when the user sends a message).
class FocusController {
  final isFocused = ValueNotifier<bool>(false);

  FocusTarget? _target;
  FocusTarget? get target => _target;

  String? _captureDir;
  String? _lastPath;
  final _screenCapture = ScreenCapture();

  /// Enter Focus Mode.  No capture happens here -- we just store the target.
  void start(FocusTarget target) {
    _target = target;
    isFocused.value = true;
  }

  /// Exit Focus Mode and clean up the last captured file.
  void stop() {
    isFocused.value = false;
    _target = null;
    _cleanupLastFile();
  }

  /// Take a single screenshot based on the stored [FocusTarget] and return the
  /// path to the temporary PNG.  Returns `null` if the capture fails.
  Future<String?> captureNow() async {
    if (_target == null) return null;
    _captureDir ??= (await getTemporaryDirectory()).path;
    _cleanupLastFile();

    try {
      CapturedScreenArea? captured;

      switch (_target!.mode) {
        case FocusMode.fullScreen:
          captured = await _screenCapture.captureEntireScreen();
          break;
        case FocusMode.region:
          if (_target!.region != null) {
            captured = await _screenCapture.captureScreenArea(_target!.region!);
          } else {
            captured = await _screenCapture.captureEntireScreen();
          }
          break;
        case FocusMode.window:
          // Window capture falls back to full-screen for now; the backend
          // still receives the window title in the system hint so the LLM
          // knows which window to focus on.
          captured = await _screenCapture.captureEntireScreen();
          break;
      }

      if (captured == null) {
        debugPrint('[Focus] Capture returned null');
        return null;
      }

      // flutter_screen_capture's toPngImage() claims RGBA on Windows but the
      // native GDI capture actually returns BGRA.  Encode with the correct
      // channel order so colours are accurate.
      final image = img.Image.fromBytes(
        width: captured.width,
        height: captured.height,
        bytes: Uint8List.fromList(captured.buffer).buffer,
        order: Platform.isWindows
            ? img.ChannelOrder.bgra
            : captured.channelOrder,
      );
      final Uint8List pngBytes = Uint8List.fromList(img.encodePng(image));
      final ts = DateTime.now().millisecondsSinceEpoch;
      final path = '$_captureDir/chili_focus_$ts.png';
      await File(path).writeAsBytes(pngBytes, flush: true);
      _lastPath = path;
      debugPrint('[Focus] Captured: $path');
      return path;
    } catch (e) {
      debugPrint('[Focus] Error: $e');
      return null;
    }
  }

  void _cleanupLastFile() {
    if (_lastPath != null) {
      try {
        final f = File(_lastPath!);
        if (f.existsSync()) f.deleteSync();
      } catch (_) {}
      _lastPath = null;
    }
  }

  /// Remove any leftover chili_focus_*.png / chili_screen_*.png from previous sessions.
  Future<void> cleanupStaleFiles() async {
    _captureDir ??= (await getTemporaryDirectory()).path;
    try {
      final dir = Directory(_captureDir!);
      if (!dir.existsSync()) return;
      for (final entity in dir.listSync()) {
        if (entity is File &&
            (entity.path.contains('chili_focus_') ||
                entity.path.contains('chili_screen_')) &&
            entity.path.endsWith('.png')) {
          try {
            entity.deleteSync();
          } catch (_) {}
        }
      }
    } catch (_) {}
  }

  void dispose() {
    stop();
    isFocused.dispose();
  }
}
