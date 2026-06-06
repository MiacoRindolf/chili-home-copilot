import 'package:flutter/foundation.dart';

/// Severity / accent of a notification (NC-1).
enum NotifKind { info, success, warning, error }

@immutable
class AppNotification {
  const AppNotification({
    required this.id,
    required this.kind,
    required this.title,
    required this.detail,
    required this.source,
    required this.timestampMs,
    required this.read,
    this.appId,
  });

  final String id;
  final NotifKind kind;
  final String title;
  final String detail;
  final String source;
  final int timestampMs;
  final bool read;

  /// NC-3 — optional target app id; when set, tapping the notification opens
  /// that app (e.g. a kill-switch alert → 'cockpit').
  final String? appId;

  AppNotification copyWith({bool? read}) => AppNotification(
        id: id,
        kind: kind,
        title: title,
        detail: detail,
        source: source,
        timestampMs: timestampMs,
        read: read ?? this.read,
        appId: appId,
      );
}

/// Cross-window activity / notification store (NC-1). Owned at the workspace so
/// any surface can push events and the dock can show an unread badge + panel.
/// Pure logic (no I/O) → unit-testable.
class NotificationCenter extends ChangeNotifier {
  NotificationCenter({int max = 50, int Function()? clock})
      : _max = max,
        _clock = clock ?? (() => DateTime.now().millisecondsSinceEpoch);

  final int _max;
  final int Function() _clock;
  final List<AppNotification> _items = <AppNotification>[]; // oldest → newest

  /// Newest first.
  List<AppNotification> get items =>
      List<AppNotification>.unmodifiable(_items.reversed);

  int get unreadCount => _items.where((AppNotification n) => !n.read).length;
  bool get isEmpty => _items.isEmpty;

  /// Append a notification. If [dedupeKey] equals the newest item's id, the add
  /// is skipped (so a repeating condition doesn't spam the feed).
  void add(
    NotifKind kind,
    String title, {
    String detail = '',
    String source = '',
    String? dedupeKey,
    String? appId,
  }) {
    if (dedupeKey != null &&
        _items.isNotEmpty &&
        _items.last.id == dedupeKey) {
      return;
    }
    _items.add(AppNotification(
      id: dedupeKey ?? '${kind.name}:$title:${_clock()}',
      kind: kind,
      title: title,
      detail: detail,
      source: source,
      timestampMs: _clock(),
      read: false,
      appId: appId,
    ));
    if (_items.length > _max) {
      _items.removeRange(0, _items.length - _max);
    }
    notifyListeners();
  }

  void markAllRead() {
    if (unreadCount == 0) return;
    for (int i = 0; i < _items.length; i++) {
      if (!_items[i].read) _items[i] = _items[i].copyWith(read: true);
    }
    notifyListeners();
  }

  void clear() {
    if (_items.isEmpty) return;
    _items.clear();
    notifyListeners();
  }
}
