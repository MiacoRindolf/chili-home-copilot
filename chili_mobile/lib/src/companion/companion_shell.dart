import 'package:flutter/material.dart';
import 'package:window_manager/window_manager.dart';

import 'avatar_view.dart';
import '../app_shell.dart';

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

  Future<void> _switchToFullApp() async {
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
  }

  @override
  Widget build(BuildContext context) {
    return _mode == CompanionMode.avatar
        ? AvatarView(onOpenFullApp: _switchToFullApp)
        : AppShell(onBackToAvatar: _switchToAvatar);
  }
}
