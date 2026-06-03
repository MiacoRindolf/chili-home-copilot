import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../brain/brain_dispatch_screen.dart';
import '../chat/chat_screen.dart';
import '../companion/shared_chat_history.dart';
import '../dashboard/dashboard_screen.dart';
import '../intercom/intercom_screen.dart';
import '../screen/focus_controller.dart';
import '../settings/settings_screen.dart';
import 'os_window.dart';
import 'workspace_controller.dart';

/// Definition of a dockable app surface.
class _AppDef {
  final String title;
  final IconData icon;
  final Size size;
  final Widget Function() build;
  const _AppDef(this.title, this.icon, this.build, {this.size = const Size(760, 540)});
}

/// CHILI OS desktop workspace — replaces the NavigationRail [AppShell] with a
/// windowed desktop: a dock rail opens each surface as a draggable window over
/// a desktop home. Same constructor as AppShell so it drops into CompanionShell.
class WorkspaceShell extends StatefulWidget {
  final VoidCallback onBackToAvatar;
  final SharedChatHistory sharedHistory;
  final ValueNotifier<bool>? pauseListening;
  final FocusController focusController;

  const WorkspaceShell({
    super.key,
    required this.onBackToAvatar,
    required this.sharedHistory,
    required this.focusController,
    this.pauseListening,
  });

  @override
  State<WorkspaceShell> createState() => _WorkspaceShellState();
}

class _WorkspaceShellState extends State<WorkspaceShell> {
  final WorkspaceController _ws = WorkspaceController();
  Size _deskSize = Size.zero; // latest desktop size, for keyboard tiling

  // Built app bodies are cached so a window keeps its State across rebuilds /
  // focus changes (and while minimized). Dropped when the window closes.
  final Map<String, Widget> _built = <String, Widget>{};

  late final Map<String, _AppDef> _apps = <String, _AppDef>{
    'dashboard': _AppDef('Dashboard', Icons.dashboard, () => const DashboardScreen()),
    'chat': _AppDef(
      'Chat',
      Icons.chat,
      () => ChatScreen(
        sharedHistory: widget.sharedHistory,
        focusController: widget.focusController,
      ),
    ),
    'intercom': _AppDef('Intercom', Icons.mic, () => const IntercomScreen()),
    'settings': _AppDef(
      'Settings',
      Icons.settings,
      () => SettingsScreen(
        sharedHistory: widget.sharedHistory,
        pauseListening: widget.pauseListening,
      ),
    ),
    'brain': _AppDef(
      'Brain',
      Icons.psychology,
      () => BrainDispatchScreen(onOpenSettings: () => _openApp('settings')),
      size: const Size(900, 600),
    ),
  };

  @override
  void dispose() {
    _ws.dispose();
    super.dispose();
  }

  void _openApp(String id) {
    final _AppDef? a = _apps[id];
    if (a == null) return;
    _ws.open(id, title: a.title, icon: a.icon, size: a.size);
  }

  // ── Keyboard window management on the focused window (mirrors the web OS). ──
  void _snapFocused(String zone) {
    final String? id = _ws.focusedId;
    if (id != null) _ws.snap(id, zone, _deskSize);
  }

  void _minimizeFocused() {
    final String? id = _ws.focusedId;
    if (id != null) _ws.minimize(id);
  }

  void _closeFocused() {
    final String? id = _ws.focusedId;
    if (id != null) _ws.close(id);
  }

  Widget _bodyFor(String id) {
    return _built.putIfAbsent(id, () => _apps[id]!.build());
  }

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return CallbackShortcuts(
      bindings: <ShortcutActivator, VoidCallback>{
        const SingleActivator(LogicalKeyboardKey.arrowLeft, control: true, alt: true): () => _snapFocused('left'),
        const SingleActivator(LogicalKeyboardKey.arrowRight, control: true, alt: true): () => _snapFocused('right'),
        const SingleActivator(LogicalKeyboardKey.arrowUp, control: true, alt: true): () => _snapFocused('max'),
        const SingleActivator(LogicalKeyboardKey.arrowDown, control: true, alt: true): _minimizeFocused,
        const SingleActivator(LogicalKeyboardKey.keyW, control: true, alt: true): _closeFocused,
        const SingleActivator(LogicalKeyboardKey.backquote, control: true): _ws.cycleFocus,
      },
      child: Focus(
        autofocus: true,
        child: Scaffold(
          body: Row(
            children: <Widget>[
              _dock(context, cs),
              VerticalDivider(width: 1, thickness: 1, color: cs.outlineVariant),
              Expanded(child: _desktop(context, cs)),
            ],
          ),
        ),
      ),
    );
  }

  Widget _desktop(BuildContext context, ColorScheme cs) {
    return LayoutBuilder(
      builder: (BuildContext context, BoxConstraints constraints) {
        final Size deskSize = Size(constraints.maxWidth, constraints.maxHeight);
        _deskSize = deskSize; // remembered for keyboard tiling shortcuts
        return AnimatedBuilder(
          animation: _ws,
          builder: (BuildContext context, _) {
            // Drop cached bodies for windows that have been closed.
            _built.removeWhere((String id, _) => !_ws.isOpen(id));
            final List<WsWindow> wins = _ws.windows.toList()
              ..sort((WsWindow a, WsWindow b) => a.z.compareTo(b.z)); // bottom → top
            final String? focusedId = _ws.focusedId;
            return Stack(
              children: <Widget>[
                _desktopBackground(context, cs),
                for (final WsWindow w in wins)
                  OsWindow(
                    key: ValueKey<String>(w.id),
                    data: w,
                    controller: _ws,
                    focused: w.id == focusedId,
                    desktopSize: deskSize,
                    child: _bodyFor(w.id),
                  ),
              ],
            );
          },
        );
      },
    );
  }

  Widget _desktopBackground(BuildContext context, ColorScheme cs) {
    return Positioned.fill(
      child: DecoratedBox(
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: <Color>[
              cs.surface,
              Color.alphaBlend(cs.primary.withValues(alpha: 0.05), cs.surface),
            ],
          ),
        ),
        child: AnimatedBuilder(
          animation: _ws,
          builder: (BuildContext context, _) {
            if (_ws.hasVisibleWindows) return const SizedBox.shrink();
            return Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  const Text('\u{1F336}', style: TextStyle(fontSize: 56)),
                  const SizedBox(height: 10),
                  Text(
                    'CHILI OS',
                    style: TextStyle(
                      fontSize: 22,
                      fontWeight: FontWeight.w700,
                      color: cs.onSurface.withValues(alpha: 0.85),
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    'Open an app from the dock',
                    style: TextStyle(fontSize: 13, color: cs.onSurfaceVariant),
                  ),
                ],
              ),
            );
          },
        ),
      ),
    );
  }

  Widget _dock(BuildContext context, ColorScheme cs) {
    return Container(
      width: 64,
      color: cs.surfaceContainerHighest,
      child: Column(
        children: <Widget>[
          const SizedBox(height: 12),
          // Back-to-avatar (CHILI logo).
          Tooltip(
            message: 'Minimize to avatar',
            child: InkResponse(
              onTap: widget.onBackToAvatar,
              radius: 26,
              child: Container(
                width: 44,
                height: 44,
                decoration: BoxDecoration(
                  color: cs.primary,
                  borderRadius: BorderRadius.circular(12),
                ),
                alignment: Alignment.center,
                child: const Text('\u{1F336}', style: TextStyle(fontSize: 20)),
              ),
            ),
          ),
          const SizedBox(height: 10),
          Divider(height: 8, indent: 14, endIndent: 14, color: cs.outlineVariant),
          const SizedBox(height: 4),
          Expanded(
            child: AnimatedBuilder(
              animation: _ws,
              builder: (BuildContext context, _) {
                return ListView(
                  padding: const EdgeInsets.symmetric(vertical: 4),
                  children: <Widget>[
                    for (final MapEntry<String, _AppDef> e in _apps.entries)
                      _dockButton(context, cs, e.key, e.value),
                  ],
                );
              },
            ),
          ),
          // Show desktop.
          _DockIcon(
            icon: Icons.desktop_windows_outlined,
            tooltip: 'Show desktop',
            active: false,
            cs: cs,
            onTap: _ws.showDesktop,
          ),
          const SizedBox(height: 12),
        ],
      ),
    );
  }

  Widget _dockButton(BuildContext context, ColorScheme cs, String id, _AppDef def) {
    final bool open = _ws.isOpen(id);
    final bool focused = _ws.focusedId == id;
    return _DockIcon(
      icon: def.icon,
      tooltip: def.title,
      active: open,
      focused: focused,
      cs: cs,
      onTap: () => _openApp(id),
    );
  }
}

class _DockIcon extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final bool active;
  final bool focused;
  final ColorScheme cs;
  final VoidCallback onTap;

  const _DockIcon({
    required this.icon,
    required this.tooltip,
    required this.active,
    required this.cs,
    required this.onTap,
    this.focused = false,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Tooltip(
        message: tooltip,
        child: InkResponse(
          onTap: onTap,
          radius: 26,
          child: Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              color: active
                  ? Color.alphaBlend(cs.primary.withValues(alpha: 0.16), cs.surface)
                  : Colors.transparent,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(
                color: focused ? cs.primary : Colors.transparent,
                width: 1.5,
              ),
            ),
            alignment: Alignment.center,
            child: Icon(
              icon,
              size: 22,
              color: active ? cs.primary : cs.onSurfaceVariant,
            ),
          ),
        ),
      ),
    );
  }
}
