import 'package:flutter/material.dart';

import '../ui/app_ui.dart';
import 'notification_center.dart';

/// Dock bell with an unread badge (NC-1).
class NotificationBell extends StatelessWidget {
  const NotificationBell({
    super.key,
    required this.center,
    required this.onTap,
    required this.cs,
  });

  final NotificationCenter center;
  final VoidCallback onTap;
  final ColorScheme cs;

  @override
  Widget build(BuildContext context) {
    final int unread = center.unreadCount;
    return Tooltip(
      message: unread > 0 ? 'Notifications ($unread)' : 'Notifications',
      child: InkResponse(
        onTap: onTap,
        radius: 26,
        child: Stack(
          clipBehavior: Clip.none,
          alignment: Alignment.center,
          children: <Widget>[
            SizedBox(
              width: 44,
              height: 44,
              child: Icon(
                unread > 0 ? Icons.notifications_active : Icons.notifications_none,
                size: 22,
                color: unread > 0 ? cs.primary : cs.onSurfaceVariant,
              ),
            ),
            if (unread > 0)
              Positioned(
                top: 4,
                right: 4,
                child: Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                  constraints: const BoxConstraints(minWidth: 16),
                  decoration: BoxDecoration(
                    color: cs.error,
                    borderRadius: BorderRadius.circular(8),
                    border:
                        Border.all(color: cs.surfaceContainerHighest, width: 1.5),
                  ),
                  alignment: Alignment.center,
                  child: Text(
                    unread > 99 ? '99+' : '$unread',
                    style: const TextStyle(
                      fontSize: 9,
                      height: 1.1,
                      fontWeight: FontWeight.w800,
                      color: Colors.white,
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// Slide-over notification panel (NC-1), anchored top-left next to the dock.
/// Rendered in the workspace Stack when open.
class NotificationPanel extends StatelessWidget {
  const NotificationPanel({
    super.key,
    required this.center,
    required this.onClose,
    this.onOpenApp,
  });

  final NotificationCenter center;
  final VoidCallback onClose;

  /// NC-3 — open the app a notification points at (its appId). When null,
  /// notifications are not tappable.
  final void Function(String appId)? onOpenApp;

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Stack(
      children: <Widget>[
        // Scrim.
        Positioned.fill(
          child: GestureDetector(
            onTap: onClose,
            child: ColoredBox(color: Colors.black.withValues(alpha: 0.18)),
          ),
        ),
        Positioned(
          top: 12,
          bottom: 12,
          left: 12,
          child: Material(
            elevation: 8,
            borderRadius: BorderRadius.circular(14),
            color: cs.surface,
            child: SizedBox(
              width: 360,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: <Widget>[
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 12, 6, 8),
                    child: Row(
                      children: <Widget>[
                        Icon(Icons.notifications_none, color: cs.primary),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text('Notifications',
                              overflow: TextOverflow.ellipsis,
                              style: Theme.of(context)
                                  .textTheme
                                  .titleMedium
                                  ?.copyWith(fontWeight: FontWeight.w700)),
                        ),
                        IconButton(
                          tooltip: 'Mark all read',
                          visualDensity: VisualDensity.compact,
                          icon: const Icon(Icons.done_all, size: 20),
                          onPressed: center.unreadCount > 0
                              ? center.markAllRead
                              : null,
                        ),
                        IconButton(
                          tooltip: 'Clear all',
                          visualDensity: VisualDensity.compact,
                          icon: const Icon(Icons.clear_all, size: 20),
                          onPressed: center.isEmpty ? null : center.clear,
                        ),
                        IconButton(
                          tooltip: 'Close',
                          visualDensity: VisualDensity.compact,
                          icon: const Icon(Icons.close, size: 20),
                          onPressed: onClose,
                        ),
                      ],
                    ),
                  ),
                  const Divider(height: 1),
                  Expanded(
                    child: center.isEmpty
                        ? const ApEmptyState(
                            icon: Icons.notifications_off_outlined,
                            message: 'No notifications',
                            detail: 'Live events from agents and the desk appear here.',
                          )
                        : ListView.separated(
                            padding: const EdgeInsets.symmetric(vertical: 4),
                            itemCount: center.items.length,
                            separatorBuilder: (_, __) =>
                                Divider(height: 1, color: cs.outlineVariant),
                            itemBuilder: (BuildContext context, int i) =>
                                _row(cs, center.items[i]),
                          ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ],
    );
  }

  Widget _row(ColorScheme cs, AppNotification n) {
    final Color c = _kindColor(n.kind, cs);
    // NC-3 — tappable when it targets an app and a handler is wired.
    final bool tappable =
        onOpenApp != null && (n.appId?.isNotEmpty ?? false);
    final Widget body = Container(
      color: n.read ? null : c.withValues(alpha: 0.05),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Icon(_kindIcon(n.kind), size: 18, color: c),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Row(
                  children: <Widget>[
                    Expanded(
                      child: Text(n.title,
                          style: TextStyle(
                              fontWeight: FontWeight.w600,
                              color: cs.onSurface)),
                    ),
                    Text(_time(n.timestampMs),
                        style: TextStyle(
                            fontSize: 11, color: cs.onSurfaceVariant)),
                  ],
                ),
                if (n.detail.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(n.detail,
                        style: TextStyle(
                            fontSize: 12, color: cs.onSurfaceVariant)),
                  ),
                if (n.source.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(n.source,
                        style: TextStyle(
                            fontSize: 10,
                            color: cs.onSurfaceVariant,
                            fontWeight: FontWeight.w600)),
                  ),
              ],
            ),
          ),
          if (tappable) ...<Widget>[
            const SizedBox(width: 6),
            Icon(Icons.chevron_right, size: 18, color: cs.onSurfaceVariant),
          ],
        ],
      ),
    );
    if (!tappable) return body;
    return InkWell(
      onTap: () {
        onClose();
        onOpenApp!(n.appId!);
      },
      child: body,
    );
  }

  static String _time(int ms) {
    final DateTime dt = DateTime.fromMillisecondsSinceEpoch(ms);
    return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
  }

  static IconData _kindIcon(NotifKind k) {
    switch (k) {
      case NotifKind.success:
        return Icons.check_circle_outline;
      case NotifKind.warning:
        return Icons.warning_amber_rounded;
      case NotifKind.error:
        return Icons.error_outline;
      case NotifKind.info:
        return Icons.info_outline;
    }
  }

  static Color _kindColor(NotifKind k, ColorScheme cs) {
    switch (k) {
      case NotifKind.success:
        return Colors.green;
      case NotifKind.warning:
        return Colors.amber.shade700;
      case NotifKind.error:
        return cs.error;
      case NotifKind.info:
        return cs.primary;
    }
  }
}
