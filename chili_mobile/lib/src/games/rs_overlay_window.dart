import 'dart:convert';

import 'package:desktop_multi_window/desktop_multi_window.dart';
import 'package:flutter/material.dart';
import 'package:window_manager/window_manager.dart';

import 'rs_item_overlay.dart';

/// Argument kind that marks a window as the floating RS price overlay (GAME-15).
const String kRsPriceKind = 'rs_price';

const Size _kOverlaySize = Size(380, 360);

/// Open the price card as a floating, frameless, always-on-top window near
/// [gameRect]'s top-left (logical px). The new engine re-enters main(), reads
/// these arguments, and renders [RsOverlayWindowApp]. GAME-15.
Future<void> openRsPriceWindow({Rect? gameRect}) async {
  final double l = gameRect?.left ?? 80;
  final double t = gameRect?.top ?? 80;
  final WindowController c = await WindowController.create(
    WindowConfiguration(
      hiddenAtLaunch: true,
      arguments: jsonEncode(<String, Object?>{'kind': kRsPriceKind, 'l': l, 't': t}),
    ),
  );
  await c.show();
}

/// The app rendered inside the floating overlay window. It positions + styles
/// its own window (frameless, always-on-top) via window_manager, then hosts the
/// self-contained [RsItemOverlay].
class RsOverlayWindowApp extends StatefulWidget {
  const RsOverlayWindowApp({super.key, required this.argument});

  final Map<String, dynamic> argument;

  @override
  State<RsOverlayWindowApp> createState() => _RsOverlayWindowAppState();
}

class _RsOverlayWindowAppState extends State<RsOverlayWindowApp> {
  @override
  void initState() {
    super.initState();
    _setupWindow();
  }

  Future<void> _setupWindow() async {
    try {
      await windowManager.ensureInitialized();
      final double l = (widget.argument['l'] as num?)?.toDouble() ?? 80;
      final double t = (widget.argument['t'] as num?)?.toDouble() ?? 80;
      await windowManager.setAsFrameless();
      await windowManager.setAlwaysOnTop(true);
      await windowManager.setBackgroundColor(Colors.transparent);
      await windowManager.setBounds(
          Rect.fromLTWH(l, t, _kOverlaySize.width, _kOverlaySize.height));
      await windowManager.show();
    } catch (_) {
      // best-effort window styling
    }
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark(useMaterial3: true),
      home: Scaffold(
        backgroundColor: Colors.transparent,
        body: Padding(
          padding: const EdgeInsets.all(8),
          child: Align(
            alignment: Alignment.topCenter,
            child: RsItemOverlay(
              autofocus: true,
              onClose: () => windowManager.close(),
            ),
          ),
        ),
      ),
    );
  }
}
