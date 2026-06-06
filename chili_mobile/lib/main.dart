import 'dart:convert';
import 'dart:io';

import 'package:desktop_multi_window/desktop_multi_window.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:window_manager/window_manager.dart';
import 'package:screen_retriever/screen_retriever.dart';

import 'src/app_theme.dart';
import 'src/companion/companion_shell.dart';
import 'src/companion/desktop_powers.dart';
import 'src/config/app_config.dart';
import 'src/config/layout_constants.dart';
import 'src/games/rs_overlay_window.dart';

Future<void> main(List<String> args) async {
  WidgetsFlutterBinding.ensureInitialized();

  // GAME-15 — a secondary engine (the floating RS price overlay) re-enters
  // main(). Ask the plugin which window this is; if it's our overlay, render
  // just that and skip the full CHILI shell. Guarded so a missing/!ready plugin
  // never blocks the normal app from launching.
  if (!kIsWeb && (Platform.isWindows || Platform.isLinux || Platform.isMacOS)) {
    String overlayArgs = '';
    try {
      final WindowController wc = await WindowController.fromCurrentEngine();
      overlayArgs = wc.arguments;
    } catch (_) {
      overlayArgs = '';
    }
    if (overlayArgs.isNotEmpty) {
      try {
        final Map<String, dynamic> m =
            jsonDecode(overlayArgs) as Map<String, dynamic>;
        if (m['kind'] == kRsPriceKind) {
          runApp(RsOverlayWindowApp(argument: m));
          return;
        }
      } catch (_) {
        // not our overlay — fall through to the normal app
      }
    }
  }

  if (!kIsWeb && (Platform.isWindows || Platform.isLinux || Platform.isMacOS)) {
    await windowManager.ensureInitialized();

    const windowOptions = WindowOptions(
      size: LayoutConstants.avatarWindowSmall,
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

  await AppConfig.instance.load();
  runApp(const ChiliDesktopCompanion());
}

class ChiliDesktopCompanion extends StatelessWidget {
  const ChiliDesktopCompanion({super.key});

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<String>(
      valueListenable: AppConfig.instance.themeModeNotifier,
      builder: (context, themeMode, _) {
        return MaterialApp(
          title: 'CHILI Companion',
          debugShowCheckedModeBanner: false,
          theme: buildChiliLightTheme(),
          darkTheme: buildChiliDarkTheme(),
          themeMode: chiliThemeModeFromString(themeMode),
          home: const CompanionShell(),
        );
      },
    );
  }
}
