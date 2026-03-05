import 'package:flutter/material.dart';
import 'package:window_manager/window_manager.dart';

import 'avatar_view.dart';
import '../app_shell.dart';
import '../config/app_config.dart';
import '../voice/wake_word_listener.dart';

enum CompanionMode { avatar, fullApp }

/// Top-level shell that switches between the small floating avatar
/// and the full desktop application window.
class CompanionShell extends StatefulWidget {
  const CompanionShell({super.key});

  @override
  State<CompanionShell> createState() => _CompanionShellState();
}

class _CompanionShellState extends State<CompanionShell> {
  CompanionMode _mode = CompanionMode.avatar;
  final _pauseWakeWord = ValueNotifier<bool>(false);
  final _wakeWordCommand = ValueNotifier<String?>(null);
  final _wakeWordReply = ValueNotifier<String?>(null);
  final _wakeWordStatus = ValueNotifier<String?>(null);
  final _wakeWordPartial = ValueNotifier<String?>(null);
  final _followUpActive = ValueNotifier<bool>(false);
  late final WakeWordListener _wakeWordListener;

  @override
  void initState() {
    super.initState();
    _wakeWordListener = WakeWordListener(
      pauseListening: _pauseWakeWord,
      onReply: (command, reply) {
        _wakeWordCommand.value = command;
        _wakeWordReply.value = reply;
      },
      onListeningChanged: (listening) {
        if (mounted) setState(() {});
      },
      onStatus: (status) {
        _wakeWordStatus.value = status.isEmpty ? null : status;
      },
      onFollowUpActive: (active) {
        _followUpActive.value = active;
      },
      onPartial: (partial) {
        _wakeWordPartial.value = partial.isEmpty ? null : partial;
      },
    );
    _startWakeWordIfEnabled();
  }

  Future<void> _startWakeWordIfEnabled() async {
    await AppConfig.instance.load();
    if (AppConfig.instance.alwaysListening && _mode == CompanionMode.avatar) {
      _wakeWordListener.start();
    }
  }

  @override
  void dispose() {
    _wakeWordListener.dispose();
    _pauseWakeWord.dispose();
    _wakeWordCommand.dispose();
    _wakeWordReply.dispose();
    _wakeWordStatus.dispose();
    _wakeWordPartial.dispose();
    _followUpActive.dispose();
    super.dispose();
  }

  Future<void> _switchToFullApp() async {
    _wakeWordListener.stop();
    await windowManager.setAlwaysOnTop(false);
    await windowManager.setTitleBarStyle(TitleBarStyle.normal);
    await windowManager.setMinimumSize(const Size(800, 600));
    await windowManager.setSize(const Size(1000, 700));
    await windowManager.center();
    if (mounted) setState(() => _mode = CompanionMode.fullApp);
  }

  Future<void> _switchToAvatar() async {
    await windowManager.setMinimumSize(const Size(160, 160));
    await windowManager.setSize(const Size(200, 200));
    await windowManager.setTitleBarStyle(TitleBarStyle.hidden);
    await windowManager.setAsFrameless();
    await windowManager.setAlwaysOnTop(true);
    if (mounted) setState(() => _mode = CompanionMode.avatar);
    if (AppConfig.instance.alwaysListening) {
      _wakeWordListener.start();
    }
  }

  @override
  Widget build(BuildContext context) {
    return _mode == CompanionMode.avatar
        ? AvatarView(
            onOpenFullApp: _switchToFullApp,
            pauseWakeWord: _pauseWakeWord,
            wakeWordCommand: _wakeWordCommand,
            wakeWordReply: _wakeWordReply,
            wakeWordStatus: _wakeWordStatus,
            wakeWordPartial: _wakeWordPartial,
            wakeWordFollowUpActive: _followUpActive,
          )
        : AppShell(onBackToAvatar: _switchToAvatar);
  }
}
