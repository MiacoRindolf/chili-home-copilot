import 'live_channel.dart';

/// Owns the app's [LiveChannel]s by id so screens share one live source of
/// truth instead of each spinning up its own poll timer (RT-1). Register a
/// channel once; subscribers listen to it; the service starts/stops them all.
class RealtimeService {
  RealtimeService();

  final Map<String, LiveChannel<dynamic>> _channels =
      <String, LiveChannel<dynamic>>{};

  /// Register (or return the existing) channel under [id].
  LiveChannel<T> channel<T>(String id, LiveChannel<T> Function() create) {
    final LiveChannel<dynamic>? existing = _channels[id];
    if (existing is LiveChannel<T>) return existing;
    final LiveChannel<T> ch = create();
    _channels[id] = ch;
    return ch;
  }

  LiveChannel<dynamic>? byId(String id) => _channels[id];

  Iterable<String> get ids => _channels.keys;

  /// Start every registered channel.
  void startAll() {
    for (final LiveChannel<dynamic> c in _channels.values) {
      c.start();
    }
  }

  /// Stop every registered channel (e.g. when the app is backgrounded).
  Future<void> stopAll() async {
    for (final LiveChannel<dynamic> c in _channels.values) {
      await c.stop();
    }
  }

  Future<void> dispose() async {
    for (final LiveChannel<dynamic> c in _channels.values) {
      c.dispose();
    }
    _channels.clear();
  }
}
