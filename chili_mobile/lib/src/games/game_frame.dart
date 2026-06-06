import 'package:flutter/services.dart';

/// Bridge to the native "CHILI frame" channel (GAME-3). Shows a small
/// always-on-top CHILI bar above a running game; dragging the bar moves the
/// game window to follow (native `SetWindowPos` only — a plain window move, the
/// same windowing API FancyZones/AutoHotkey use; no reparenting, no injection).
/// All calls are tolerant: a platform error resolves to a safe default.
class GameFrame {
  const GameFrame();

  static const MethodChannel _ch = MethodChannel('chili/game_frame');

  /// Attach the CHILI frame to the window whose title contains [title]. [name]
  /// is shown on the CHILI title bar. Returns true once the game is found and
  /// framed.
  Future<bool> start(String title, {String? name}) async {
    try {
      return (await _ch.invokeMethod<bool>('start', <String, Object?>{
            'title': title,
            'name': name ?? title,
          })) ??
          false;
    } catch (_) {
      return false;
    }
  }

  /// Remove the CHILI frame bar (the game window is left exactly where it is).
  Future<void> stop() async {
    try {
      await _ch.invokeMethod<void>('stop');
    } catch (_) {
      // ignore — nothing attached / non-Windows
    }
  }
}
