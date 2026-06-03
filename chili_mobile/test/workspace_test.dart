import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/companion/shared_chat_history.dart';
import 'package:chili_mobile/src/screen/focus_controller.dart';
import 'package:chili_mobile/src/workspace/workspace_controller.dart';
import 'package:chili_mobile/src/workspace/workspace_shell.dart';

void main() {
  group('WorkspaceController', () {
    test('open adds a window, focuses it, and reports it open', () {
      final WorkspaceController c = WorkspaceController();
      c.open('chat', title: 'Chat', icon: Icons.chat);
      expect(c.windows.length, 1);
      expect(c.isOpen('chat'), isTrue);
      expect(c.focusedId, 'chat');
      expect(c.hasVisibleWindows, isTrue);
    });

    test('opening an already-open app focuses it instead of duplicating', () {
      final WorkspaceController c = WorkspaceController();
      c.open('chat', title: 'Chat', icon: Icons.chat);
      c.open('brain', title: 'Brain', icon: Icons.psychology);
      expect(c.focusedId, 'brain');
      c.open('chat', title: 'Chat', icon: Icons.chat); // re-open → focus
      expect(c.windows.length, 2);
      expect(c.focusedId, 'chat');
    });

    test('focus raises a window to the top of the z-order', () {
      final WorkspaceController c = WorkspaceController();
      c.open('a', title: 'A', icon: Icons.abc);
      c.open('b', title: 'B', icon: Icons.abc);
      expect(c.focusedId, 'b');
      c.focus('a');
      expect(c.focusedId, 'a');
    });

    test('minimize hides from focus; re-opening restores', () {
      final WorkspaceController c = WorkspaceController();
      c.open('chat', title: 'Chat', icon: Icons.chat);
      c.minimize('chat');
      expect(c.hasVisibleWindows, isFalse);
      expect(c.focusedId, isNull);
      c.open('chat', title: 'Chat', icon: Icons.chat); // restores
      expect(c.byId('chat')!.minimized, isFalse);
      expect(c.focusedId, 'chat');
    });

    test('close removes the window', () {
      final WorkspaceController c = WorkspaceController();
      c.open('chat', title: 'Chat', icon: Icons.chat);
      c.close('chat');
      expect(c.isOpen('chat'), isFalse);
      expect(c.windows, isEmpty);
    });

    test('move shifts position; resize clamps to the minimum', () {
      final WorkspaceController c = WorkspaceController();
      c.open('chat', title: 'Chat', icon: Icons.chat, size: const Size(400, 300));
      final Offset p0 = c.byId('chat')!.position;
      c.move('chat', const Offset(25, -10));
      expect(c.byId('chat')!.position, p0 + const Offset(25, -10));
      c.resize('chat', const Offset(-1000, -1000)); // way past min
      expect(c.byId('chat')!.size.width, WorkspaceController.minWinW);
      expect(c.byId('chat')!.size.height, WorkspaceController.minWinH);
    });

    test('toggleMaximize fills the desktop and restores the prior geometry', () {
      final WorkspaceController c = WorkspaceController();
      const Size desktop = Size(1200, 800);
      c.open('chat', title: 'Chat', icon: Icons.chat, size: const Size(400, 300));
      c.move('chat', const Offset(50, 60));
      final WsWindow w = c.byId('chat')!;
      final Offset prePos = w.position;
      final Size preSize = w.size;
      c.toggleMaximize('chat', desktop);
      expect(w.maximized, isTrue);
      expect(w.size, desktop);
      expect(w.position, Offset.zero);
      c.toggleMaximize('chat', desktop);
      expect(w.maximized, isFalse);
      expect(w.position, prePos);
      expect(w.size, preSize);
    });

    test('showDesktop minimizes every visible window', () {
      final WorkspaceController c = WorkspaceController();
      c.open('a', title: 'A', icon: Icons.abc);
      c.open('b', title: 'B', icon: Icons.abc);
      c.showDesktop();
      expect(c.hasVisibleWindows, isFalse);
    });

    test('snap tiles the window into halves / quarters / full', () {
      final WorkspaceController c = WorkspaceController();
      const Size d = Size(1000, 800);
      c.open('chat', title: 'Chat', icon: Icons.chat);
      c.snap('chat', 'left', d);
      expect(c.byId('chat')!.position, Offset.zero);
      expect(c.byId('chat')!.size, const Size(500, 800));
      c.snap('chat', 'right', d);
      expect(c.byId('chat')!.position, const Offset(500, 0));
      c.snap('chat', 'br', d);
      expect(c.byId('chat')!.position, const Offset(500, 400));
      expect(c.byId('chat')!.size, const Size(500, 400));
      c.snap('chat', 'max', d);
      expect(c.byId('chat')!.maximized, isTrue);
      expect(c.byId('chat')!.size, d);
    });

    test('cycleFocus moves focus to the bottom-most visible window', () {
      final WorkspaceController c = WorkspaceController();
      c.open('a', title: 'A', icon: Icons.abc);
      c.open('b', title: 'B', icon: Icons.abc);
      c.open('c', title: 'C', icon: Icons.abc);
      expect(c.focusedId, 'c'); // last opened is on top
      c.cycleFocus();
      expect(c.focusedId, 'a'); // bottom-most raised to top
    });
  });

  group('WorkspaceShell', () {
    testWidgets('renders the dock + desktop hint with no windows open', (WidgetTester tester) async {
      final SharedChatHistory history = SharedChatHistory();
      final FocusController focus = FocusController();
      addTearDown(focus.dispose);

      await tester.pumpWidget(MaterialApp(
        home: WorkspaceShell(
          onBackToAvatar: () {},
          sharedHistory: history,
          focusController: focus,
        ),
      ));
      await tester.pump();

      // The empty-desktop hint.
      expect(find.text('CHILI OS'), findsOneWidget);
      expect(find.text('Open an app from the dock'), findsOneWidget);
      // The dock has the five app icons.
      expect(find.byIcon(Icons.dashboard), findsOneWidget);
      expect(find.byIcon(Icons.chat), findsOneWidget);
      expect(find.byIcon(Icons.mic), findsOneWidget);
      expect(find.byIcon(Icons.settings), findsOneWidget);
      expect(find.byIcon(Icons.psychology), findsOneWidget);
    });
  });
}
