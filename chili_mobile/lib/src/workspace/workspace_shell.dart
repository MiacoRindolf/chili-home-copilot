import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../agents/agent.dart';
import '../agents/agent_persistence.dart';
import '../agents/agent_registry.dart';
import '../agents/agent_status_service.dart';
import '../agents/agents_screen.dart';
import '../brain/brain_dispatch_screen.dart';
import '../chat/chat_screen.dart';
import '../cockpit/cockpit_screen.dart';
import '../companion/shared_chat_history.dart';
import '../dashboard/dashboard_screen.dart';
import '../intercom/intercom_screen.dart';
import '../mcp/mcp_screen.dart';
import '../network/chili_api_client.dart';
import '../notifications/notification_center.dart';
import '../notifications/notification_panel.dart';
import '../research/research_screen.dart';
import '../skills/skills_screen.dart';
import '../realtime/live_channel.dart';
import '../realtime/live_sources.dart';
import '../realtime/live_status.dart';
import '../screen/focus_controller.dart';
import '../settings/settings_screen.dart';
import 'os_window.dart';
import 'workspace_controller.dart';
import 'workspace_palette.dart';
import 'workspace_persistence.dart';
import 'workspace_taskbar.dart';

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
  bool _paletteOpen = false; // command palette (Ctrl+K) overlay

  // NC-1 — cross-window activity / notification center, fed by the real-time
  // connection state. Owned here so the dock bell + panel can show it.
  final NotificationCenter _notifications = NotificationCenter();
  bool _notifOpen = false;
  LiveStatus _prevConn = LiveStatus.idle;

  // UK-2 — inbox for ⌘K "Ask CHILI"; Chat consumes a non-null query and sends it.
  final ValueNotifier<String?> _chatAsk = ValueNotifier<String?>(null);
  Timer? _saveTimer; // debounces session saves
  bool _restoring = false; // true while replaying a saved session

  // Shared agent registry (AGT-7): owned at the workspace so the dock badge and
  // ⌘K palette see the fleet. Polled + persisted once here; the Agents window is
  // a view over it (livePolling: false).
  final AgentRegistry _agents = AgentRegistry();
  late final AgentStatusService _agentStatus =
      AgentStatusService(ChiliApiClient());
  Timer? _agentSaveTimer;

  // RT-2 — agent status now flows through the real-time layer: one LiveChannel
  // with adaptive cadence + backoff drives both the registry and the workspace
  // connection indicator, replacing the bespoke 25s poll timer.
  late final LiveChannel<Map<String, AgentLiveStatus>> _agentChannel =
      LiveChannel<Map<String, AgentLiveStatus>>(
    () => PollingLiveSource<Map<String, AgentLiveStatus>>(
      _fetchAgentStatuses,
      activeInterval: const Duration(seconds: 12),
      idleInterval: const Duration(seconds: 25),
    ),
    dedupe: false,
  );

  // Built app bodies are cached so a window keeps its State across rebuilds /
  // focus changes (and while minimized). Dropped when the window closes.
  final Map<String, Widget> _built = <String, Widget>{};

  late final Map<String, _AppDef> _apps = <String, _AppDef>{
    'dashboard': _AppDef('Dashboard', Icons.dashboard, () => const DashboardScreen()),
    'cockpit': _AppDef(
      'Cockpit',
      Icons.candlestick_chart,
      () => CockpitScreen(notifications: _notifications),
      size: const Size(900, 640),
    ),
    'chat': _AppDef(
      'Chat',
      Icons.chat,
      () => ChatScreen(
        sharedHistory: widget.sharedHistory,
        focusController: widget.focusController,
        askPrompt: _chatAsk,
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
    'agents': _AppDef(
      'Agents',
      Icons.smart_toy,
      () => AgentsScreen(registry: _agents, livePolling: false),
      size: const Size(940, 620),
    ),
    'research': _AppDef(
      'Research',
      Icons.travel_explore,
      () => ResearchScreen(onDiscuss: _onDiscussTopic),
      size: const Size(880, 640),
    ),
    'mcp': _AppDef(
      'MCP Tools',
      Icons.hub_outlined,
      () => const McpScreen(),
      size: const Size(820, 620),
    ),
    'skills': _AppDef(
      'Skills',
      Icons.school_outlined,
      () => SkillsScreen(onDiscuss: _onDiscussSkill),
      size: const Size(820, 620),
    ),
  };

  @override
  void initState() {
    super.initState();
    _ws.addListener(_scheduleSave);
    _restoreSession();
    _restoreAgents();
    _agentChannel.addListener(_onAgentChannel);
    _agentChannel.start();
  }

  @override
  void dispose() {
    _saveTimer?.cancel();
    _agentSaveTimer?.cancel();
    _agentChannel.removeListener(_onAgentChannel);
    _agentChannel.dispose();
    _notifications.dispose();
    _chatAsk.dispose();
    _ws.removeListener(_scheduleSave);
    _agents.removeListener(_scheduleAgentSave);
    _ws.dispose();
    _agents.dispose();
    super.dispose();
  }

  // ── Shared agent lifecycle (AGT-7) ───────────────────────────────────────
  Future<void> _restoreAgents() async {
    final List<Map<String, dynamic>> saved = await AgentPersistence.load();
    if (!mounted) return;
    _agents.applySaved(saved);
    _agents.addListener(_scheduleAgentSave);
  }

  void _scheduleAgentSave() {
    _agentSaveTimer?.cancel();
    _agentSaveTimer = Timer(const Duration(milliseconds: 600), () {
      AgentPersistence.save(_agents.toJson());
    });
  }

  /// LiveChannel fetch: poll backend agent status; throw when unreachable so the
  /// channel enters error/backoff (and the connection indicator shows offline).
  Future<Map<String, AgentLiveStatus>> _fetchAgentStatuses() async {
    final Map<String, AgentLiveStatus> live = await _agentStatus.poll();
    if (!_agentStatus.reachable) {
      throw Exception('agents backend unreachable');
    }
    return live;
  }

  /// Apply each live reading to the shared registry; rebuild for the indicator.
  void _onAgentChannel() {
    final Map<String, AgentLiveStatus>? live = _agentChannel.value;
    if (live != null) {
      live.forEach((String id, AgentLiveStatus s) {
        _agents.applyLiveStatus(id, s.status,
            lastRun: s.lastRun, lastResult: s.detail);
      });
    }
    _notifyConnectionTransitions();
    if (mounted) setState(() {});
  }

  /// NC-1 — emit a notification when the real-time connection drops or recovers.
  void _notifyConnectionTransitions() {
    final LiveStatus s = _agentChannel.status;
    if (s == _prevConn) return;
    final bool wasLive = _prevConn == LiveStatus.live;
    final bool wasDown =
        _prevConn == LiveStatus.error || _prevConn == LiveStatus.offline;
    final bool isDown = s == LiveStatus.error || s == LiveStatus.offline;
    if (s == LiveStatus.live && wasDown) {
      _notifications.add(NotifKind.success, 'Reconnected',
          detail: 'Live data restored.', source: 'Realtime');
    } else if (isDown && wasLive) {
      _notifications.add(NotifKind.warning, 'Connection lost',
          detail: 'Reconnecting to the backend…', source: 'Realtime');
    }
    _prevConn = s;
  }

  /// ⌘K agent command: toggle local start/stop for an agent. Local-only — never
  /// touches the broker (that path lives in the Agents window behind confirms),
  /// so it's safe to fire from the palette. Live-backed agents are excluded
  /// from the palette since their status is backend-owned.
  void _toggleAgent(String id) {
    final Agent? a = _agents.byId(id);
    if (a == null) return;
    if (a.status == AgentStatus.running) {
      _agents.stop(id);
    } else {
      _agents.start(id);
    }
  }

  void _scheduleSave() {
    if (_restoring) return; // don't thrash storage while replaying a session
    _saveTimer?.cancel();
    _saveTimer = Timer(const Duration(milliseconds: 600), () {
      WorkspacePersistence.save(_ws.toJson());
    });
  }

  Future<void> _restoreSession() async {
    final List<Map<String, dynamic>> saved = await WorkspacePersistence.load();
    if (saved.isEmpty || !mounted) return;
    _restoring = true;
    for (final Map<String, dynamic> w in saved) {
      final String? id = w['id'] as String?;
      final _AppDef? def = id == null ? null : _apps[id];
      if (id == null || def == null) continue;
      _ws.open(id, title: def.title, icon: def.icon, size: def.size);
      _ws.applyGeometry(
        id,
        position: Offset(
          (w['x'] as num?)?.toDouble() ?? 40,
          (w['y'] as num?)?.toDouble() ?? 28,
        ),
        size: Size(
          (w['w'] as num?)?.toDouble() ?? def.size.width,
          (w['h'] as num?)?.toDouble() ?? def.size.height,
        ),
        minimized: w['min'] as bool? ?? false,
        maximized: w['max'] as bool? ?? false,
      );
    }
    _restoring = false;
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

  void _togglePalette() => setState(() => _paletteOpen = !_paletteOpen);

  void _onEscape() {
    if (_paletteOpen || _notifOpen) {
      setState(() {
        _paletteOpen = false;
        _notifOpen = false;
      });
    }
  }

  List<PaletteItem> get _paletteItems => <PaletteItem>[
        for (final MapEntry<String, _AppDef> e in _apps.entries)
          PaletteItem(e.key, e.value.title, e.value.icon),
        // ⌘K agent commands (AGT-7): start/stop non-backend agents directly.
        ...agentPaletteCommands(_agents.agents),
        // UK-1: jump to any open window + workspace actions + ask CHILI.
        ...windowPaletteCommands(_ws.windows),
        ...workspaceActionCommands(),
      ];

  /// ⌘K "Ask CHILI: <query>" — open Chat and send the typed query (UK-2).
  void _onAsk(String query) {
    setState(() => _paletteOpen = false);
    _chatAsk.value = query; // staged first; Chat consumes on build or via listener
    _openApp('chat');
  }

  /// RC-1 — "Discuss" a research topic: open Chat asking about it (reuses UK-2).
  void _onDiscussTopic(String topic) {
    _chatAsk.value = 'Tell me more about: $topic';
    _openApp('chat');
  }

  /// RC-2 — "Discuss" a learned skill: open Chat asking how/when to apply it.
  void _onDiscussSkill(String skillName) {
    _chatAsk.value = 'Explain this learned skill and when to apply it: $skillName';
    _openApp('chat');
  }

  void _onPaletteOpen(String id) {
    setState(() => _paletteOpen = false);
    final String? agentId = parseAgentToggleId(id);
    if (agentId != null) {
      _toggleAgent(agentId);
      return;
    }
    final String? windowId = parseWindowFocusId(id);
    if (windowId != null) {
      _ws.focus(windowId);
      return;
    }
    switch (id) {
      case 'action:ask':
        _openApp('chat');
        return;
      case 'action:show-desktop':
        _ws.showDesktop();
        return;
      case 'action:avatar':
        widget.onBackToAvatar();
        return;
    }
    _openApp(id);
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
        const SingleActivator(LogicalKeyboardKey.keyK, control: true): _togglePalette,
        const SingleActivator(LogicalKeyboardKey.escape): _onEscape,
      },
      child: Focus(
        autofocus: true,
        child: Scaffold(
          body: Stack(
            children: <Widget>[
              Row(
                children: <Widget>[
                  _dock(context, cs),
                  VerticalDivider(width: 1, thickness: 1, color: cs.outlineVariant),
                  Expanded(child: _desktop(context, cs)),
                ],
              ),
              if (_paletteOpen)
                WorkspacePalette(
                  items: _paletteItems,
                  onOpen: _onPaletteOpen,
                  onAsk: _onAsk,
                  onClose: () => setState(() => _paletteOpen = false),
                ),
              if (_notifOpen)
                AnimatedBuilder(
                  animation: _notifications,
                  builder: (BuildContext context, _) => NotificationPanel(
                    center: _notifications,
                    onClose: () => setState(() => _notifOpen = false),
                  ),
                ),
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
              fit: StackFit.expand, // fill the desktop (the Row gives a loose height)
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
                if (_ws.snapGhost != null)
                  Positioned.fromRect(
                    rect: _ws.rectForZone(_ws.snapGhost!, deskSize),
                    child: IgnorePointer(
                      child: DecoratedBox(
                        decoration: BoxDecoration(
                          color: cs.primary.withValues(alpha: 0.18),
                          border: Border.all(color: cs.primary, width: 2),
                          borderRadius: BorderRadius.circular(8),
                        ),
                      ),
                    ),
                  ),
                WorkspaceTaskbar(
                  minimized: _ws.windows.where((WsWindow w) => w.minimized).toList(),
                  onRestore: _ws.focus,
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
              animation: Listenable.merge(<Listenable>[_ws, _agents]),
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
          const SizedBox(height: 4),
          // NC-1 — notification bell with unread badge.
          AnimatedBuilder(
            animation: _notifications,
            builder: (BuildContext context, _) => NotificationBell(
              center: _notifications,
              cs: cs,
              onTap: () => setState(() => _notifOpen = !_notifOpen),
            ),
          ),
          const SizedBox(height: 4),
          // RT-2 — live connection indicator (real-time layer status).
          _ConnectionDot(status: _agentChannel.status, cs: cs),
          const SizedBox(height: 12),
        ],
      ),
    );
  }

  Widget _dockButton(BuildContext context, ColorScheme cs, String id, _AppDef def) {
    final bool open = _ws.isOpen(id);
    final bool focused = _ws.focusedId == id;
    final int badge = id == 'agents' ? _agents.runningCount : 0;
    return _DockIcon(
      icon: def.icon,
      tooltip: badge > 0 ? '${def.title} · $badge running' : def.title,
      active: open,
      focused: focused,
      cs: cs,
      badge: badge,
      onTap: () => _openApp(id),
    );
  }
}

/// Prefix marking a palette entry as an agent start/stop command (AGT-7).
const String _agentTogglePrefix = 'agent:toggle:';

/// ⌘K command-palette entries that start/stop agents. Built-in agents whose
/// status is owned by the backend ([liveBackedAgentIds]) are excluded — those
/// are controlled from the Agents window (behind confirms). Pure + testable.
List<PaletteItem> agentPaletteCommands(List<Agent> agents) => <PaletteItem>[
      for (final Agent a in agents)
        if (!liveBackedAgentIds.contains(a.id))
          PaletteItem(
            '$_agentTogglePrefix${a.id}',
            '${a.status == AgentStatus.running ? 'Stop' : 'Start'} ${a.name}',
            a.status == AgentStatus.running ? Icons.stop : Icons.play_arrow,
          ),
    ];

/// Returns the agent id encoded in an agent-toggle palette id, or null if [id]
/// is a normal app id.
String? parseAgentToggleId(String id) =>
    id.startsWith(_agentTogglePrefix) ? id.substring(_agentTogglePrefix.length) : null;

const String _windowFocusPrefix = 'window:';

/// ⌘K "Go to <window>" commands for every open window (UK-1). Pure + testable.
List<PaletteItem> windowPaletteCommands(Iterable<WsWindow> windows) =>
    <PaletteItem>[
      for (final WsWindow w in windows)
        PaletteItem('$_windowFocusPrefix${w.id}', 'Go to ${w.title}', w.icon),
    ];

/// Returns the window id encoded in a window-focus palette id, or null.
String? parseWindowFocusId(String id) => id.startsWith(_windowFocusPrefix)
    ? id.substring(_windowFocusPrefix.length)
    : null;

/// Global workspace action commands available from ⌘K (UK-1).
List<PaletteItem> workspaceActionCommands() => const <PaletteItem>[
      PaletteItem('action:ask', 'Ask CHILI', Icons.auto_awesome),
      PaletteItem(
          'action:show-desktop', 'Show desktop', Icons.desktop_windows_outlined),
      PaletteItem('action:avatar', 'Minimize to avatar', Icons.bolt),
    ];

/// Dock footer indicator showing the real-time layer's connection status (RT-2).
class _ConnectionDot extends StatelessWidget {
  const _ConnectionDot({required this.status, required this.cs});
  final LiveStatus status;
  final ColorScheme cs;

  @override
  Widget build(BuildContext context) {
    late final Color color;
    late final IconData icon;
    switch (status) {
      case LiveStatus.live:
        color = Colors.green;
        icon = Icons.wifi;
      case LiveStatus.connecting:
      case LiveStatus.error:
        color = Colors.amber;
        icon = Icons.wifi_tethering;
      case LiveStatus.offline:
      case LiveStatus.idle:
        color = cs.onSurfaceVariant;
        icon = Icons.wifi_off;
    }
    return Tooltip(
      message: 'Realtime: ${status.label}',
      child: Icon(icon, size: 18, color: color),
    );
  }
}

class _DockIcon extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final bool active;
  final bool focused;
  final ColorScheme cs;
  final int badge;
  final VoidCallback onTap;

  const _DockIcon({
    required this.icon,
    required this.tooltip,
    required this.active,
    required this.cs,
    required this.onTap,
    this.focused = false,
    this.badge = 0,
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
          child: Stack(
            clipBehavior: Clip.none,
            children: <Widget>[
              Container(
                width: 44,
                height: 44,
                decoration: BoxDecoration(
                  color: active
                      ? Color.alphaBlend(
                          cs.primary.withValues(alpha: 0.16), cs.surface)
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
              if (badge > 0)
                Positioned(
                  top: -2,
                  right: -2,
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                    constraints: const BoxConstraints(minWidth: 16),
                    decoration: BoxDecoration(
                      color: Colors.green,
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(color: cs.surfaceContainerHighest, width: 1.5),
                    ),
                    alignment: Alignment.center,
                    child: Text(
                      badge > 99 ? '99+' : '$badge',
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
      ),
    );
  }
}
