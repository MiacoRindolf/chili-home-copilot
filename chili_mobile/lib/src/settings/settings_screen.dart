import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late TextEditingController _urlController;

  @override
  void initState() {
    super.initState();
    _urlController = TextEditingController(text: ChiliApiClient.baseUrl);
  }

  @override
  void dispose() {
    _urlController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Settings',
              style: Theme.of(context).textTheme.headlineMedium),
          const SizedBox(height: 24),

          // Server connection
          Card(
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Server Connection',
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _urlController,
                    decoration: const InputDecoration(
                      labelText: 'Backend URL',
                      hintText: 'http://localhost:8000',
                      helperText: 'The URL of your CHILI FastAPI server',
                    ),
                  ),
                  const SizedBox(height: 12),
                  ElevatedButton.icon(
                    onPressed: () {
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(
                            content:
                                Text('Server URL saved (restart to apply)')),
                      );
                    },
                    icon: const Icon(Icons.save),
                    label: const Text('Save'),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),

          // Keyboard shortcuts reference
          Card(
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Shortcuts',
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 12),
                  _shortcutRow('Single-click avatar', 'Toggle chat bubble'),
                  _shortcutRow('Double-click avatar', 'Open full app'),
                  _shortcutRow('Drag avatar', 'Move companion on screen'),
                  _shortcutRow('Chili button (nav rail)', 'Minimize to avatar'),
                  _shortcutRow('Ctrl+Shift+C', 'Global hotkey: summon CHILI'),
                  _shortcutRow('Long-press mic button', 'Record voice message'),
                  _shortcutRow('Drop files on avatar', 'Send files to CHILI'),
                  _shortcutRow('System tray icon', 'Click to show, right-click for menu'),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),

          // About
          Card(
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('About',
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 12),
                  const Text('CHILI Desktop Companion v0.1.0'),
                  const SizedBox(height: 4),
                  Text(
                    'Conversational Home Interface & Life Intelligence',
                    style: TextStyle(color: Colors.grey.shade600),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _shortcutRow(String action, String desc) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(children: [
        SizedBox(
          width: 200,
          child: Text(action,
              style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
        ),
        Expanded(
            child: Text(desc,
                style: TextStyle(fontSize: 13, color: Colors.grey.shade600))),
      ]),
    );
  }
}
