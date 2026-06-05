/// Connection lifecycle of a [LiveChannel] (RT-1).
enum LiveStatus {
  /// Not started yet.
  idle,

  /// Opening the source / first fetch in flight.
  connecting,

  /// Receiving fresh values.
  live,

  /// The last attempt failed; the source is retrying with backoff.
  error,

  /// The source completed / disconnected and is not retrying.
  offline,
}

extension LiveStatusLabel on LiveStatus {
  String get label {
    switch (this) {
      case LiveStatus.idle:
        return 'Idle';
      case LiveStatus.connecting:
        return 'Connecting…';
      case LiveStatus.live:
        return 'Live';
      case LiveStatus.error:
        return 'Reconnecting…';
      case LiveStatus.offline:
        return 'Offline';
    }
  }

  bool get isHealthy => this == LiveStatus.live;
}
