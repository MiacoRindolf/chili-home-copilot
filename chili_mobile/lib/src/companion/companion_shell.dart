import 'package:flutter/material.dart';
import 'package:window_manager/window_manager.dart';

import 'avatar_view.dart';
import 'shared_chat_history.dart';
import '../app_shell.dart';
import '../config/app_config.dart';
import '../screen/focus_controller.dart';
import '../voice/calibration_dialog.dart';
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
  final _sharedHistory = SharedChatHistory();
  final _focusController = FocusController();
  final _pauseWakeWord = ValueNotifier<bool>(false);
  final _wakeWordCommand = ValueNotifier<String?>(null);
  final _wakeWordReply = ValueNotifier<String?>(null);
  final _wakeWordStatus = ValueNotifier<String?>(null);
  final _wakeWordPartial = ValueNotifier<String?>(null);
  final _followUpActive = ValueNotifier<bool>(false);
  final _ttsPlaying = ValueNotifier<bool>(false);
  final _ttsInterruptRequested = ValueNotifier<bool>(false);
  final _lastTtsText = ValueNotifier<String?>(null);
  late final WakeWordListener _wakeWordListener;

  @override
  void initState() {
    super.initState();
    _wakeWordListener = WakeWordListener(
      pauseListening: _pauseWakeWord,
      ttsPlaying: _ttsPlaying,
      lastTtsText: _lastTtsText,
      focusController: _focusController,
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
      onTtsInterruptRequested: () {
        _ttsInterruptRequested.value = true;
      },
    );
    _startWakeWordIfEnabled();
  }

  Future<void> _startWakeWordIfEnabled() async {
    await AppConfig.instance.load();
    if (AppConfig.instance.alwaysListening && _mode == CompanionMode.avatar) {
      _wakeWordListener.start();
    }
    _checkFirstRunCalibration();
  }

  Future<void> _checkFirstRunCalibration() async {
    await AppConfig.instance.load();
    if (AppConfig.instance.calibrationDone) return;
    if (!AppConfig.instance.alwaysListening) return;

    // Delay slightly so the app has time to render before showing the dialog.
    await Future<void>.delayed(const Duration(seconds: 2));
    if (!mounted || _mode != CompanionMode.avatar) return;

    // Switch to full app mode so the dialog can render properly.
    await _switchToFullApp();
    if (!mounted) return;

    await CalibrationDialog.show(
      context,
      wakeWord: AppConfig.instance.wakeWord,
      pauseListening: _pauseWakeWord,
    );

    // After calibration (or skip), go back to avatar mode.
    if (mounted) await _switchToAvatar();
  }

  @override
  void dispose() {
    _wakeWordListener.dispose();
    _focusController.dispose();
    _pauseWakeWord.dispose();
    _wakeWordCommand.dispose();
    _wakeWordReply.dispose();
    _wakeWordStatus.dispose();
    _wakeWordPartial.dispose();
    _followUpActive.dispose();
    _ttsPlaying.dispose();
    _ttsInterruptRequested.dispose();
    _lastTtsText.dispose();
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
            sharedHistory: _sharedHistory,
            onOpenFullApp: _switchToFullApp,
            focusController: _focusController,
            pauseWakeWord: _pauseWakeWord,
            ttsPlaying: _ttsPlaying,
            ttsInterruptRequested: _ttsInterruptRequested,
            lastTtsText: _lastTtsText,
            wakeWordCommand: _wakeWordCommand,
            wakeWordReply: _wakeWordReply,
            wakeWordStatus: _wakeWordStatus,
            wakeWordPartial: _wakeWordPartial,
            wakeWordFollowUpActive: _followUpActive,
          )
        : AppShell(
            sharedHistory: _sharedHistory,
            onBackToAvatar: _switchToAvatar,
            pauseListening: _pauseWakeWord,
            focusController: _focusController,
          );
  }
}
