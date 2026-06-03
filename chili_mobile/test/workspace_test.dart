import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/companion/shared_chat_history.dart';
import 'package:chili_mobile/src/screen/focus_controller.dart';
import 'package:chili_mobile/src/workspace/workspace_controller.dart';
import 'package:chili_mobile/src/workspace/workspace_palette.dart';
import 'package:chili_mobile/src/workspace/workspace_shell.dart';
import 'package:chili_mobile/src/workspace/workspace_taskbar.dart';

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

    test('zoneForRect detects edge snap zones for drag-to-edge', () {
      final WorkspaceController c = WorkspaceController();
      const Size d = Size(1000, 800);
      expect(c.zoneForRect(const Rect.fromLTWH(5, 200, 400, 300), d), 'left');
      expect(c.zoneForRect(const Rect.fromLTWH(700, 200, 295, 300), d), 'right');
      expect(c.zoneForRect(const Rect.fromLTWH(300, 5, 400, 300), d), 'max');
      expect(c.zoneForRect(const Rect.fromLTWH(300, 300, 400, 300), d), isNull);
    });

    test('rectForZone gives the expected geometry', () {
      final WorkspaceController c = WorkspaceController();
      const Size d = Size(1000, 800);
      expect(c.rectForZone('left', d), const Rect.fromLTWH(0, 0, 500, 800));
      expect(c.rectForZone('max', d), const Rect.fromLTWH(0, 0, 1000, 800));
      expect(c.rectForZone('tr', d), const Rect.fromLTWH(500, 0, 500, 400));
      expect(c.rectForZone('nope', d), Rect.zero);
    });

    test('commitGhost snaps to the ghost zone then clears it', () {
      final WorkspaceController c = WorkspaceController();
      const Size d = Size(1000, 800);
      c.open('chat', title: 'Chat', icon: Icons.chat);
      c.setGhost('right');
      expect(c.snapGhost, 'right');
      c.commitGhost('chat', d);
      expect(c.snapGhost, isNull);
      expect(c.byId('chat')!.position, const Offset(500, 0));
      expect(c.byId('chat')!.size, const Size(500, 800));
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

  group('WorkspacePalette', () {
    const List<PaletteItem> items = <PaletteItem>[
      PaletteItem('dashboard', 'Dashboard', Icons.dashboard),
      PaletteItem('chat', 'Chat', Icons.chat),
      PaletteItem('intercom', 'Intercom', Icons.mic),
      PaletteItem('settings', 'Settings', Icons.settings),
      PaletteItem('brain', 'Brain', Icons.psychology),
    ];

    test('paletteFuzzy + paletteFilter narrow by subsequence', () {
      expect(paletteFuzzy('Intercom', 'intr'), isTrue);
      expect(paletteFuzzy('Intercom', 'xyz'), isFalse);
      expect(paletteFuzzy('Chat', ''), isTrue);
      final List<PaletteItem> r = paletteFilter(items, 'set');
      expect(r.length, 1);
      expect(r.first.id, 'settings');
    });

    testWidgets('typing filters the list; tapping an item opens it', (WidgetTester tester) async {
      String? opened;
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: Stack(
            children: <Widget>[
              WorkspacePalette(
                items: items,
                onOpen: (String id) => opened = id,
                onClose: () {},
              ),
            ],
          ),
        ),
      ));
      await tester.pump();
      expect(find.text('Dashboard'), findsOneWidget);
      expect(find.text('Brain'), findsOneWidget);

      await tester.enterText(find.byType(TextField), 'brn'); // fuzzy → Brain
      await tester.pump();
      expect(find.text('Brain'), findsOneWidget);
      expect(find.text('Dashboard'), findsNothing);

      await tester.tap(find.text('Brain'));
      await tester.pump();
      expect(opened, 'brain');
    });
  });

  group('WorkspaceTaskbar', () {
    WsWindow win(String id, String title) => WsWindow(
          id: id,
          title: title,
          icon: Icons.chat,
          position: Offset.zero,
          size: const Size(100, 100),
          z: 1,
          minimized: true,
        );

    testWidgets('shows a chip per minimized window; tapping restores it', (WidgetTester tester) async {
      String? restored;
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: Stack(
            children: <Widget>[
              WorkspaceTaskbar(
                minimized: <WsWindow>[win('chat', 'Chat'), win('brain', 'Brain')],
                onRestore: (String id) => restored = id,
              ),
            ],
          ),
        ),
      ));
      await tester.pump();
      expect(find.text('Chat'), findsOneWidget);
      expect(find.text('Brain'), findsOneWidget);
      await tester.tap(find.text('Brain'));
      await tester.pump();
      expect(restored, 'brain');
    });

    testWidgets('renders nothing when no window is minimized', (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: Stack(
            children: <Widget>[
              WorkspaceTaskbar(minimized: const <WsWindow>[], onRestore: (_) {}),
            ],
          ),
        ),
      ));
      await tester.pump();
      expect(find.byType(InkWell), findsNothing);
    });
  });
}
