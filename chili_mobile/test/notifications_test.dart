import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/notifications/notification_center.dart';
import 'package:chili_mobile/src/notifications/notification_panel.dart';

void main() {
  group('NotificationCenter', () {
    test('add increments unread; newest first', () {
      int t = 1000;
      final NotificationCenter c = NotificationCenter(clock: () => t++);
      c.add(NotifKind.info, 'A');
      c.add(NotifKind.warning, 'B', detail: 'd', source: 'Realtime');
      expect(c.unreadCount, 2);
      expect(c.items.first.title, 'B'); // newest first
      expect(c.items.first.kind, NotifKind.warning);
      expect(c.items.last.title, 'A');
    });

    test('markAllRead clears unread', () {
      final NotificationCenter c = NotificationCenter();
      c.add(NotifKind.info, 'A');
      c.add(NotifKind.error, 'B');
      expect(c.unreadCount, 2);
      c.markAllRead();
      expect(c.unreadCount, 0);
      expect(c.items.every((AppNotification n) => n.read), isTrue);
    });

    test('clear empties the feed', () {
      final NotificationCenter c = NotificationCenter();
      c.add(NotifKind.info, 'A');
      expect(c.isEmpty, isFalse);
      c.clear();
      expect(c.isEmpty, isTrue);
      expect(c.unreadCount, 0);
    });

    test('dedupeKey skips a repeat of the newest', () {
      final NotificationCenter c = NotificationCenter();
      c.add(NotifKind.warning, 'Offline', dedupeKey: 'conn-down');
      c.add(NotifKind.warning, 'Offline', dedupeKey: 'conn-down'); // skipped
      expect(c.items.length, 1);
      c.add(NotifKind.success, 'Online', dedupeKey: 'conn-up');
      c.add(NotifKind.warning, 'Offline', dedupeKey: 'conn-down'); // not newest → added
      expect(c.items.length, 3);
    });

    test('caps the feed at max, keeping newest', () {
      int t = 0;
      final NotificationCenter c = NotificationCenter(max: 3, clock: () => t++);
      for (int i = 0; i < 6; i++) {
        c.add(NotifKind.info, 'n$i');
      }
      expect(c.items.length, 3);
      expect(c.items.first.title, 'n5'); // newest kept
      expect(c.items.last.title, 'n3');
    });

    test('notifies listeners on add / markRead / clear', () {
      final NotificationCenter c = NotificationCenter();
      int n = 0;
      c.addListener(() => n++);
      c.add(NotifKind.info, 'A');
      c.markAllRead();
      c.clear();
      expect(n, 3);
    });
  });

  group('NotificationPanel widget', () {
    testWidgets('lists notifications + mark-read works', (WidgetTester tester) async {
      final NotificationCenter c = NotificationCenter();
      c.add(NotifKind.warning, 'Connection lost',
          detail: 'Reconnecting…', source: 'Realtime');
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: AnimatedBuilder(
            animation: c,
            builder: (BuildContext context, _) =>
                NotificationPanel(center: c, onClose: () {}),
          ),
        ),
      ));
      await tester.pumpAndSettle();

      expect(find.text('Notifications'), findsOneWidget);
      expect(find.text('Connection lost'), findsOneWidget);
      expect(c.unreadCount, 1);
      await tester.tap(find.byTooltip('Mark all read'));
      await tester.pumpAndSettle();
      expect(c.unreadCount, 0);
    });

    testWidgets('shows empty state when no notifications', (WidgetTester tester) async {
      final NotificationCenter c = NotificationCenter();
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(body: NotificationPanel(center: c, onClose: () {})),
      ));
      await tester.pumpAndSettle();
      expect(find.text('No notifications'), findsOneWidget);
    });
  });
}
