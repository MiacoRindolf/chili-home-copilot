import 'package:flutter/material.dart';

import 'brain/brain_dispatch_screen.dart';
import 'chat/chat_screen.dart';
import 'companion/shared_chat_history.dart';
import 'dashboard/dashboard_screen.dart';
import 'intercom/intercom_screen.dart';
import 'screen/focus_controller.dart';
import 'settings/settings_screen.dart';

/// Full application window with a navigation rail.
class AppShell extends StatefulWidget {
  final VoidCallback onBackToAvatar;
  final SharedChatHistory sharedHistory;
  final ValueNotifier<bool>? pauseListening;
  final FocusController focusController;
  const AppShell({
    super.key,
    required this.onBackToAvatar,
    required this.sharedHistory,
    required this.focusController,
    this.pauseListening,
  });

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  int _selectedIndex = 0;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Row(
        children: [
          NavigationRail(
            selectedIndex: _selectedIndex,
            onDestinationSelected: (i) => setState(() => _selectedIndex = i),
            labelType: NavigationRailLabelType.all,
            leading: Padding(
              padding: const EdgeInsets.symmetric(vertical: 12),
              child: Column(
                children: [
                  FloatingActionButton.small(
                    heroTag: 'avatar_btn',
                    onPressed: widget.onBackToAvatar,
                    tooltip: 'Minimize to avatar',
                    backgroundColor: const Color(0xFFEF5350),
                    child: const Text(
                      '\u{1F336}',
                      style: TextStyle(fontSize: 18),
                    ),
                  ),
                  const SizedBox(height: 4),
                  const Text(
                    'CHILI',
                    style: TextStyle(
                      fontSize: 11,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ],
              ),
            ),
            destinations: const [
              NavigationRailDestination(
                icon: Icon(Icons.dashboard_outlined),
                selectedIcon: Icon(Icons.dashboard),
                label: Text('Dashboard'),
              ),
              NavigationRailDestination(
                icon: Icon(Icons.chat_outlined),
                selectedIcon: Icon(Icons.chat),
                label: Text('Chat'),
              ),
              NavigationRailDestination(
                icon: Icon(Icons.mic_none),
                selectedIcon: Icon(Icons.mic),
                label: Text('Intercom'),
              ),
              NavigationRailDestination(
                icon: Icon(Icons.settings_outlined),
                selectedIcon: Icon(Icons.settings),
                label: Text('Settings'),
              ),
              NavigationRailDestination(
                icon: Icon(Icons.psychology_outlined),
                selectedIcon: Icon(Icons.psychology),
                label: Text('Brain'),
              ),
            ],
          ),
          const VerticalDivider(thickness: 1, width: 1),
          Expanded(child: _pageAt(_selectedIndex)),
        ],
      ),
    );
  }

  Widget _pageAt(int index) {
    switch (index) {
      case 0:
        return const DashboardScreen();
      case 1:
        return ChatScreen(
          sharedHistory: widget.sharedHistory,
          focusController: widget.focusController,
        );
      case 2:
        return const IntercomScreen();
      case 3:
        return SettingsScreen(
          sharedHistory: widget.sharedHistory,
          pauseListening: widget.pauseListening,
        );
      case 4:
        return BrainDispatchScreen(
          onOpenSettings: () => setState(() => _selectedIndex = 3),
        );
      default:
        return const SizedBox.shrink();
    }
  }
}
