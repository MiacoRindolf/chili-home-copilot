import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:hotkey_manager/hotkey_manager.dart';
import 'package:local_notifier/local_notifier.dart';
import 'package:tray_manager/tray_manager.dart';

/// Initializes desktop-specific powers: system tray, global hotkey,
/// and the local notification system.
///
/// Call [init] once at startup from main.dart.
class DesktopPowers {
  static final DesktopPowers instance = DesktopPowers._();
  DesktopPowers._();

  VoidCallback? onShowApp;
  VoidCallback? onQuit;

  Future<void> init({
    VoidCallback? onShowApp,
    VoidCallback? onQuit,
  }) async {
    this.onShowApp = onShowApp;
    this.onQuit = onQuit;

    await _initNotifications();
    await _initTray();
    await _initHotkey();
  }

  // ── Notifications ──

  Future<void> _initNotifications() async {
    try {
      await localNotifier.setup(appName: 'CHILI Companion');
    } catch (e) {
      debugPrint('local_notifier init failed: $e');
    }
  }

  /// Show a desktop toast notification.
  Future<void> notify({
    required String title,
    String? body,
  }) async {
    try {
      final notification = LocalNotification(
        title: title,
        body: body ?? '',
      );
      await notification.show();
    } catch (e) {
      debugPrint('notify failed: $e');
    }
  }

  // ── System Tray ──

  Future<void> _initTray() async {
    try {
      await trayManager.setIcon(
        Platform.isWindows
            ? 'assets/animations/chili_idle.json' // fallback; no .ico bundled yet
            : 'assets/animations/chili_idle.json',
      );
      await trayManager.setToolTip('CHILI Companion');
      final menu = Menu(items: [
        MenuItem(key: 'show', label: 'Show CHILI'),
        MenuItem.separator(),
        MenuItem(key: 'quit', label: 'Quit'),
      ]);
      await trayManager.setContextMenu(menu);
      trayManager.addListener(_TrayListener(this));
    } catch (e) {
      debugPrint('tray init failed: $e');
    }
  }

  void _onTrayMenuItemClick(MenuItem item) {
    switch (item.key) {
      case 'show':
        onShowApp?.call();
        break;
      case 'quit':
        onQuit?.call();
        break;
    }
  }

  // ── Global Hotkey (Ctrl+Shift+C) ──

  Future<void> _initHotkey() async {
    try {
      final hotKey = HotKey(
        key: PhysicalKeyboardKey.keyC,
        modifiers: [HotKeyModifier.control, HotKeyModifier.shift],
        scope: HotKeyScope.system,
      );
      await hotKeyManager.register(hotKey, keyDownHandler: (_) {
        onShowApp?.call();
      });
    } catch (e) {
      debugPrint('hotkey init failed: $e');
    }
  }

  Future<void> dispose() async {
    try {
      await hotKeyManager.unregisterAll();
    } catch (_) {}
  }
}

class _TrayListener extends TrayListener {
  final DesktopPowers _powers;
  _TrayListener(this._powers);

  @override
  void onTrayIconMouseDown() {
    _powers.onShowApp?.call();
  }

  @override
  void onTrayIconRightMouseDown() {
    trayManager.popUpContextMenu();
  }

  @override
  void onTrayMenuItemClick(MenuItem menuItem) {
    _powers._onTrayMenuItemClick(menuItem);
  }
}
