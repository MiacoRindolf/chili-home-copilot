import 'package:flutter/material.dart';

/// One open window in the CHILI OS desktop workspace.
class WsWindow {
  final String id; // app key — 'dashboard', 'chat', 'intercom', 'settings', 'brain'
  final String title;
  final IconData icon;
  Offset position;
  Size size;
  int z;
  bool minimized;
  bool maximized;
  Rect? restoreRect; // geometry to return to when un-maximizing

  WsWindow({
    required this.id,
    required this.title,
    required this.icon,
    required this.position,
    required this.size,
    required this.z,
    this.minimized = false,
    this.maximized = false,
  });
}

/// Manages the set of open workspace windows: open / focus / close / move /
/// resize / minimize / maximize. A lightweight [ChangeNotifier] so it slots in
/// alongside the app's existing ChangeNotifier + setState style (no new deps).
class WorkspaceController extends ChangeNotifier {
  final List<WsWindow> _windows = <WsWindow>[];
  int _zTop = 10;
  int _opened = 0;

  static const double minWinW = 320;
  static const double minWinH = 220;

  List<WsWindow> get windows => List<WsWindow>.unmodifiable(_windows);

  /// True when at least one window is visible (not minimized).
  bool get hasVisibleWindows => _windows.any((WsWindow w) => !w.minimized);

  WsWindow? byId(String id) {
    for (final WsWindow w in _windows) {
      if (w.id == id) return w;
    }
    return null;
  }

  bool isOpen(String id) => byId(id) != null;

  /// The id of the top-most non-minimized window (the focused one), or null.
  String? get focusedId {
    WsWindow? top;
    for (final WsWindow w in _windows) {
      if (w.minimized) continue;
      if (top == null || w.z > top.z) top = w;
    }
    return top?.id;
  }

  /// Open the app's window, or focus/restore it if already open.
  void open(
    String id, {
    required String title,
    required IconData icon,
    Size size = const Size(720, 520),
  }) {
    final WsWindow? existing = byId(id);
    if (existing != null) {
      existing.minimized = false;
      focus(id);
      return;
    }
    final int n = _opened++;
    _windows.add(WsWindow(
      id: id,
      title: title,
      icon: icon,
      position: Offset(40 + (n % 6) * 36.0, 28 + (n % 6) * 30.0),
      size: size,
      z: ++_zTop,
    ));
    notifyListeners();
  }

  void close(String id) {
    final int before = _windows.length;
    _windows.removeWhere((WsWindow w) => w.id == id);
    if (_windows.length != before) notifyListeners();
  }

  void focus(String id) {
    final WsWindow? w = byId(id);
    if (w == null) return;
    w.minimized = false;
    if (w.z != _zTop) w.z = ++_zTop;
    notifyListeners();
  }

  void minimize(String id) {
    final WsWindow? w = byId(id);
    if (w == null || w.minimized) return;
    w.minimized = true;
    notifyListeners();
  }

  void move(String id, Offset delta) {
    final WsWindow? w = byId(id);
    if (w == null || w.maximized) return;
    w.position += delta;
    notifyListeners();
  }

  void resize(String id, Offset delta) {
    final WsWindow? w = byId(id);
    if (w == null || w.maximized) return;
    w.size = Size(
      (w.size.width + delta.dx).clamp(minWinW, 6000.0),
      (w.size.height + delta.dy).clamp(minWinH, 6000.0),
    );
    notifyListeners();
  }

  /// Toggle maximize against the given desktop size.
  void toggleMaximize(String id, Size desktop) {
    final WsWindow? w = byId(id);
    if (w == null) return;
    if (w.maximized) {
      final Rect? r = w.restoreRect;
      if (r != null) {
        w.position = r.topLeft;
        w.size = r.size;
      }
      w.maximized = false;
    } else {
      w.restoreRect = Rect.fromLTWH(w.position.dx, w.position.dy, w.size.width, w.size.height);
      w.position = Offset.zero;
      w.size = desktop;
      w.maximized = true;
    }
    focus(id);
  }

  /// Minimize every visible window to reveal the desktop home.
  void showDesktop() {
    bool any = false;
    for (final WsWindow w in _windows) {
      if (!w.minimized) {
        w.minimized = true;
        any = true;
      }
    }
    if (any) notifyListeners();
  }

  /// Tile the window into a half / quarter / full zone of the desktop.
  /// Zones: 'left', 'right', 'max', 'tl', 'tr', 'bl', 'br'.
  void snap(String id, String zone, Size desktop) {
    final WsWindow? w = byId(id);
    if (w == null) return;
    final double hw = desktop.width / 2;
    final double hh = desktop.height / 2;
    Rect r;
    switch (zone) {
      case 'left':
        r = Rect.fromLTWH(0, 0, hw, desktop.height);
        break;
      case 'right':
        r = Rect.fromLTWH(hw, 0, hw, desktop.height);
        break;
      case 'max':
        r = Rect.fromLTWH(0, 0, desktop.width, desktop.height);
        break;
      case 'tl':
        r = Rect.fromLTWH(0, 0, hw, hh);
        break;
      case 'tr':
        r = Rect.fromLTWH(hw, 0, hw, hh);
        break;
      case 'bl':
        r = Rect.fromLTWH(0, hh, hw, hh);
        break;
      case 'br':
        r = Rect.fromLTWH(hw, hh, hw, hh);
        break;
      default:
        return;
    }
    w.position = r.topLeft;
    w.size = r.size;
    w.maximized = zone == 'max';
    if (zone != 'max') w.restoreRect = null; // a half/quarter becomes the new geometry
    focus(id);
  }

  /// Cycle focus to the bottom-most visible window (mirrors the web OS ⌘`).
  void cycleFocus() {
    final List<WsWindow> visible = _windows.where((WsWindow w) => !w.minimized).toList()
      ..sort((WsWindow a, WsWindow b) => a.z.compareTo(b.z));
    if (visible.length < 2) return;
    focus(visible.first.id);
  }
}
