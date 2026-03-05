import 'package:flutter/material.dart';

import 'chat/chat_screen.dart';
import 'dashboard/dashboard_screen.dart';
import 'intercom/intercom_screen.dart';
import 'settings/settings_screen.dart';

/// Full application window with a navigation rail.
class AppShell extends StatefulWidget {
  final VoidCallback onBackToAvatar;
  const AppShell({super.key, required this.onBackToAvatar});

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  int _selectedIndex = 0;

  static const _labels = ['Dashboard', 'Chat', 'Intercom', 'Settings'];

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
            ],
          ),
          const VerticalDivider(thickness: 1, width: 1),
          Expanded(child: _pages[_selectedIndex]),
        ],
      ),
    );
  }

  List<Widget> get _pages => const [
        DashboardScreen(),
        ChatScreen(),
        IntercomScreen(),
        SettingsScreen(),
      ];
}
