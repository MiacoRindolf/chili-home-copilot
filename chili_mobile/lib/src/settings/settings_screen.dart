import 'package:flutter/material.dart';

import '../config/app_config.dart';
import '../network/chili_api_client.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late TextEditingController _urlController;
  late TextEditingController _wakeWordController;
  bool _alwaysListening = true;

  @override
  void initState() {
    super.initState();
    _urlController = TextEditingController(text: ChiliApiClient.baseUrl);
    _wakeWordController = TextEditingController(text: 'chili');
    _loadConfig();
  }

  Future<void> _loadConfig() async {
    await AppConfig.instance.load();
    if (mounted) {
      setState(() {
        _wakeWordController.text = AppConfig.instance.wakeWord;
        _alwaysListening = AppConfig.instance.alwaysListening;
      });
    }
  }

  @override
  void dispose() {
    _urlController.dispose();
    _wakeWordController.dispose();
    super.dispose();
  }

  Future<void> _saveWakeWord() async {
    final word = _wakeWordController.text.trim();
    if (word.isEmpty) return;
    await AppConfig.instance.setWakeWord(word);
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Wake word saved')),
      );
    }
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

          // Wake word
          Card(
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Wake Word',
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(height: 12),
                  TextField(
                    controller: _wakeWordController,
                    decoration: const InputDecoration(
                      labelText: 'Wake word',
                      hintText: 'Chili',
                      helperText:
                          'Say "Chili" or "Hey Chili" then your question (e.g. "Chili, what\'s the weather?")',
                    ),
                    textCapitalization: TextCapitalization.words,
                    onSubmitted: (_) => _saveWakeWord(),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          'Always listening for wake word',
                          style: TextStyle(
                            fontSize: 14,
                            color: Colors.grey.shade700,
                          ),
                        ),
                      ),
                      Switch(
                        value: _alwaysListening,
                        onChanged: (value) async {
                          await AppConfig.instance.setAlwaysListening(value);
                          if (mounted) setState(() => _alwaysListening = value);
                        },
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  ElevatedButton.icon(
                    onPressed: _saveWakeWord,
                    icon: const Icon(Icons.save),
                    label: const Text('Save wake word'),
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
                  _shortcutRow('Wake word', 'Say "Chili" or "Hey Chili" then your question for hands-free'),
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
