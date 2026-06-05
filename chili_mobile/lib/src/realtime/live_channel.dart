import 'dart:async';

import 'package:flutter/foundation.dart';

import 'live_status.dart';

/// A transport that produces a stream of [T] values for a [LiveChannel].
/// Implementations (polling, SSE, WebSocket) own their own retry/backoff and
/// simply keep the stream alive; the channel reflects whatever they emit.
abstract class LiveSource<T> {
  /// Begin producing values. Errors are surfaced on the stream (the channel
  /// moves to [LiveStatus.error]); completion means the source gave up
  /// ([LiveStatus.offline]).
  Stream<T> open();

  /// Request an immediate refresh (no-op for push transports).
  void poke() {}

  /// Stop and release resources.
  Future<void> close();
}

/// Observable holder for a single live value (RT-1). A [ChangeNotifier] so any
/// widget can `AnimatedBuilder`/`ListenableBuilder` over it. Owns the source
/// subscription and exposes the current value, last error, connection [status]
/// and freshness.
class LiveChannel<T> extends ChangeNotifier {
  LiveChannel(this._sourceFactory, {bool dedupe = true, T? initial})
      : _dedupe = dedupe,
        _value = initial;

  final LiveSource<T> Function() _sourceFactory;
  final bool _dedupe;

  LiveSource<T>? _source;
  StreamSubscription<T>? _sub;

  T? _value;
  Object? _error;
  LiveStatus _status = LiveStatus.idle;
  int? _lastUpdatedMs; // epoch ms of the last value

  T? get value => _value;
  Object? get error => _error;
  LiveStatus get status => _status;
  int? get lastUpdatedMs => _lastUpdatedMs;
  bool get isActive => _sub != null;

  /// Open the source and start receiving values. Idempotent.
  void start() {
    if (_sub != null) return;
    _setStatus(LiveStatus.connecting);
    _source = _sourceFactory();
    _sub = _source!.open().listen(
          _onData,
          onError: _onError,
          onDone: _onDone,
          cancelOnError: false,
        );
  }

  /// Request an immediate refresh from the source (polling transports).
  void refresh() => _source?.poke();

  /// Cancel the subscription and close the source.
  Future<void> stop() async {
    final StreamSubscription<T>? sub = _sub;
    final LiveSource<T>? src = _source;
    _sub = null;
    _source = null;
    await sub?.cancel();
    await src?.close();
    _setStatus(LiveStatus.idle);
  }

  void _onData(T v) {
    if (_dedupe && _status == LiveStatus.live && v == _value) {
      // Value unchanged → keep status/freshness; no notification churn.
      return;
    }
    _value = v;
    _error = null;
    _lastUpdatedMs = _nowMs();
    _setStatus(LiveStatus.live);
  }

  void _onError(Object e, [StackTrace? _]) {
    _error = e;
    _setStatus(LiveStatus.error);
  }

  void _onDone() => _setStatus(LiveStatus.offline);

  void _setStatus(LiveStatus s) {
    if (_status == s) {
      notifyListeners(); // value/error may still have changed
      return;
    }
    _status = s;
    notifyListeners();
  }

  static int _nowMs() => DateTime.now().millisecondsSinceEpoch;

  @override
  void dispose() {
    _sub?.cancel();
    _source?.close();
    _sub = null;
    _source = null;
    super.dispose();
  }
}
