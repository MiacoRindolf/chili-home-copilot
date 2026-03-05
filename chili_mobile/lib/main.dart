import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:window_manager/window_manager.dart';
import 'package:screen_retriever/screen_retriever.dart';

import 'src/app_theme.dart';
import 'src/companion/companion_shell.dart';
import 'src/companion/desktop_powers.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  if (!kIsWeb && (Platform.isWindows || Platform.isLinux || Platform.isMacOS)) {
    await windowManager.ensureInitialized();

    const windowOptions = WindowOptions(
      size: Size(200, 200),
      minimumSize: Size(160, 160),
      backgroundColor: Colors.transparent,
      skipTaskbar: false,
      titleBarStyle: TitleBarStyle.hidden,
    );

    windowManager.waitUntilReadyToShow(windowOptions, () async {
      final display = await screenRetriever.getPrimaryDisplay();
      final x = display.size.width - 240.0;
      final y = display.size.height - 300.0;
      await windowManager.setBounds(
        Rect.fromLTWH(x, y, 200, 200),
      );
      await windowManager.setAsFrameless();
      await windowManager.setAlwaysOnTop(true);
      await windowManager.show();
      await windowManager.focus();
    });

    // Initialize desktop powers (tray, hotkey, notifications).
    await DesktopPowers.instance.init(
      onShowApp: () async {
        await windowManager.show();
        await windowManager.focus();
      },
      onQuit: () async {
        await DesktopPowers.instance.dispose();
        exit(0);
      },
    );
  }

  runApp(const ChiliDesktopCompanion());
}

class ChiliDesktopCompanion extends StatelessWidget {
  const ChiliDesktopCompanion({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'CHILI Companion',
      debugShowCheckedModeBanner: false,
      theme: buildChiliTheme(),
      home: const CompanionShell(),
    );
  }
}
