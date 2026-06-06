import 'package:flutter/services.dart';

/// One pickable top-level window (GAME-5 frame picker).
class FrameWindowOption {
  const FrameWindowOption({required this.hwnd, required this.title});
  final int hwnd;
  final String title;
}

/// Parse the native `listWindows` payload into options. Pure + tolerant.
List<FrameWindowOption> parseWindowList(List<Object?> raw) => <FrameWindowOption>[
      for (final Object? w in raw)
        if (w is Map && w['hwnd'] != null && '${w['title']}'.trim().isNotEmpty)
          FrameWindowOption(
            hwnd: (w['hwnd'] as num).toInt(),
            title: '${w['title']}'.trim(),
          ),
    ];

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

  /// List visible top-level windows so the user can pick which one to frame
  /// (GAME-5). Avoids auto-grabbing the wrong window — e.g. a game launcher or
  /// anti-cheat login window that shares the game's title.
  Future<List<FrameWindowOption>> listWindows() async {
    try {
      final List<Object?>? raw =
          await _ch.invokeListMethod<Object?>('listWindows');
      return parseWindowList(raw ?? const <Object?>[]);
    } catch (_) {
      return const <FrameWindowOption>[];
    }
  }

  /// Frame the exact window the user picked, by its native handle (GAME-5).
  Future<bool> startByHandle(int hwnd, {String? name}) async {
    try {
      return (await _ch.invokeMethod<bool>('startByHandle', <String, Object?>{
            'hwnd': hwnd,
            'name': name ?? '',
          })) ??
          false;
    } catch (_) {
      return false;
    }
  }

  /// Listen for item-search queries submitted from the native in-game overlay
  /// (GAME-11). [handler] resolves a typed query to a display string; the
  /// result is pushed back to the overlay. Safe to call repeatedly.
  void bindSearch(
      Future<Map<String, Object?>> Function(String query) handler) {
    _ch.setMethodCallHandler((MethodCall call) async {
      if (call.method == 'query') {
        final Object? raw = call.arguments;
        final String q = raw is Map ? '${raw['q'] ?? ''}' : '';
        Map<String, Object?> res;
        try {
          res = await handler(q);
        } catch (_) {
          res = <String, Object?>{'state': 'error', 'message': 'Lookup failed'};
        }
        try {
          await _ch.invokeMethod<void>('searchResult', res);
        } catch (_) {
          // ignore — overlay gone / non-Windows
        }
      }
      return null;
    });
  }

  /// Remove the CHILI frame (the game window is left exactly where it is).
  Future<void> stop() async {
    try {
      await _ch.invokeMethod<void>('stop');
    } catch (_) {
      // ignore — nothing attached / non-Windows
    }
  }
}
