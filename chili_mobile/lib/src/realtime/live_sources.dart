import 'dart:async';
import 'dart:io';

import 'live_channel.dart';
import 'realtime_schedule.dart';

/// Adaptive-cadence polling transport: calls [fetch] on a timer, fast while
/// [isActive] returns true and slow when idle, with exponential backoff after
/// failures. The default real-time transport until a push endpoint exists.
class PollingLiveSource<T> implements LiveSource<T> {
  PollingLiveSource(
    this._fetch, {
    this.isActive,
    this.activeInterval = const Duration(seconds: 5),
    this.idleInterval = const Duration(seconds: 30),
  });

  final Future<T> Function() _fetch;
  final bool Function()? isActive;
  final Duration activeInterval;
  final Duration idleInterval;

  StreamController<T>? _ctrl;
  Timer? _timer;
  bool _closed = false;
  int _failures = 0;

  @override
  Stream<T> open() {
    _ctrl = StreamController<T>(onCancel: close);
    scheduleMicrotask(_tick);
    return _ctrl!.stream;
  }

  Future<void> _tick() async {
    if (_closed) return;
    try {
      final T v = await _fetch();
      _failures = 0;
      if (!_closed) _ctrl?.add(v);
    } catch (e) {
      _failures++;
      if (!_closed) _ctrl?.addError(e);
    }
    if (_closed) return;
    final Duration delay = _failures > 0
        ? backoffDelay(_failures)
        : adaptiveInterval(
            active: isActive?.call() ?? true,
            activeInterval: activeInterval,
            idleInterval: idleInterval,
          );
    _timer = Timer(delay, _tick);
  }

  @override
  void poke() {
    if (_closed) return;
    _timer?.cancel();
    scheduleMicrotask(_tick);
  }

  @override
  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    _timer?.cancel();
    await _ctrl?.close();
  }
}

/// WebSocket push transport (dart:io — desktop). Emits each text frame as it
/// arrives and auto-reconnects with exponential backoff if the socket drops.
/// No external dependency.
class WebSocketLiveSource implements LiveSource<String> {
  WebSocketLiveSource(this.url, {this.protocols, this.pingInterval});

  final String url;
  final Iterable<String>? protocols;
  final Duration? pingInterval;

  StreamController<String>? _ctrl;
  WebSocket? _socket;
  StreamSubscription<dynamic>? _socketSub;
  Timer? _retryTimer;
  bool _closed = false;
  int _failures = 0;

  @override
  Stream<String> open() {
    _ctrl = StreamController<String>(onCancel: close);
    _connect();
    return _ctrl!.stream;
  }

  Future<void> _connect() async {
    if (_closed) return;
    try {
      final WebSocket socket = await WebSocket.connect(url, protocols: protocols);
      if (_closed) {
        await socket.close();
        return;
      }
      _socket = socket;
      if (pingInterval != null) socket.pingInterval = pingInterval;
      _failures = 0;
      _socketSub = socket.listen(
        (dynamic frame) {
          if (!_closed) _ctrl?.add(frame.toString());
        },
        onError: (Object e) {
          if (!_closed) _ctrl?.addError(e);
          _scheduleReconnect();
        },
        onDone: _scheduleReconnect,
        cancelOnError: false,
      );
    } catch (e) {
      if (!_closed) _ctrl?.addError(e);
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    if (_closed) return;
    _socketSub?.cancel();
    _socketSub = null;
    _socket = null;
    _failures++;
    _retryTimer?.cancel();
    _retryTimer = Timer(backoffDelay(_failures), _connect);
  }

  /// Send a text frame to the server (e.g. a subscribe message).
  @override
  void poke() {
    // No client-pull semantics for a push socket; left as a no-op.
  }

  void send(String message) => _socket?.add(message);

  @override
  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    _retryTimer?.cancel();
    await _socketSub?.cancel();
    await _socket?.close();
    await _ctrl?.close();
  }
}
