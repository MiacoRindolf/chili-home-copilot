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
  String? snapGhost; // drag-to-edge preview zone, set while a title bar is dragged

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

  /// The pixel rect a snap zone occupies on [d].
  /// Zones: 'left', 'right', 'max', 'tl', 'tr', 'bl', 'br' (else Rect.zero).
  Rect rectForZone(String zone, Size d) {
    final double hw = d.width / 2;
    final double hh = d.height / 2;
    switch (zone) {
      case 'left':
        return Rect.fromLTWH(0, 0, hw, d.height);
      case 'right':
        return Rect.fromLTWH(hw, 0, hw, d.height);
      case 'max':
        return Rect.fromLTWH(0, 0, d.width, d.height);
      case 'tl':
        return Rect.fromLTWH(0, 0, hw, hh);
      case 'tr':
        return Rect.fromLTWH(hw, 0, hw, hh);
      case 'bl':
        return Rect.fromLTWH(0, hh, hw, hh);
      case 'br':
        return Rect.fromLTWH(hw, hh, hw, hh);
      default:
        return Rect.zero;
    }
  }

  /// Tile the window into a snap zone of the desktop.
  void snap(String id, String zone, Size desktop) {
    final WsWindow? w = byId(id);
    if (w == null) return;
    final Rect r = rectForZone(zone, desktop);
    if (r == Rect.zero) return;
    w.position = r.topLeft;
    w.size = r.size;
    w.maximized = zone == 'max';
    if (zone != 'max') w.restoreRect = null; // a half/quarter becomes the new geometry
    focus(id);
  }

  // ── Drag-to-edge snapping: while a title bar is dragged, the shell shows a
  //    translucent [snapGhost] preview; releasing in a zone snaps the window. ──

  /// The snap zone a window rect is hovering (top→max, left→left-half,
  /// right→right-half), or null. Drives the ghost preview during a drag.
  String? zoneForRect(Rect r, Size desktop) {
    const double edge = 28;
    if (r.top <= edge) return 'max';
    if (r.left <= edge) return 'left';
    if (r.right >= desktop.width - edge) return 'right';
    return null;
  }

  void setGhost(String? zone) {
    if (snapGhost != zone) {
      snapGhost = zone;
      notifyListeners();
    }
  }

  /// Commit the current drag: snap to [snapGhost] if one is set, then clear it.
  void commitGhost(String id, Size desktop) {
    final String? z = snapGhost;
    snapGhost = null;
    if (z != null) {
      snap(id, z, desktop); // snap() focuses + notifies
    } else {
      notifyListeners();
    }
  }

  /// Cycle focus to the bottom-most visible window (mirrors the web OS ⌘`).
  void cycleFocus() {
    final List<WsWindow> visible = _windows.where((WsWindow w) => !w.minimized).toList()
      ..sort((WsWindow a, WsWindow b) => a.z.compareTo(b.z));
    if (visible.length < 2) return;
    focus(visible.first.id);
  }
}
