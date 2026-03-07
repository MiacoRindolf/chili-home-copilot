import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_screen_capture/flutter_screen_capture.dart';
import 'package:screen_retriever/screen_retriever.dart';
import 'package:window_manager/window_manager.dart';

import 'focus_target.dart';

/// Fullscreen overlay that lets the user choose a focus target:
///
///  - **Full Screen** -- captures the entire primary display.
///  - **Draw Region** -- user drags a rectangle on a frozen screenshot.
///  - **Window** -- user picks an open application window.
///
/// Returns a [FocusTarget] via `Navigator.pop`, or `null` on cancel.
class FocusSelectorScreen extends StatefulWidget {
  const FocusSelectorScreen({super.key});

  @override
  State<FocusSelectorScreen> createState() => _FocusSelectorScreenState();
}

class _FocusSelectorScreenState extends State<FocusSelectorScreen> {
  ui.Image? _backgroundImage;
  bool _loading = true;

  Offset? _dragStart;
  Offset? _dragEnd;

  Rect _savedBounds = Rect.zero;
  bool _wasAlwaysOnTop = true;

  bool _showWindowPicker = false;
  List<String> _windowTitles = [];

  final _focusNode = FocusNode();

  @override
  void initState() {
    super.initState();
    _init();
  }

  Future<void> _init() async {
    _wasAlwaysOnTop = await windowManager.isAlwaysOnTop();
    _savedBounds = await windowManager.getBounds();

    await windowManager.setAlwaysOnTop(false);
    await windowManager.hide();
    await Future<void>.delayed(const Duration(milliseconds: 350));

    final captured = await ScreenCapture().captureEntireScreen();

    if (captured == null) {
      await _restore();
      if (mounted) Navigator.pop(context);
      return;
    }

    final pngBytes = captured.toPngImage();
    final decoded = await _decodeImage(pngBytes);

    final display = await screenRetriever.getPrimaryDisplay();
    final screenW = display.size.width;
    final screenH = display.size.height;

    await windowManager.setMinimumSize(Size.zero);
    await windowManager.setTitleBarStyle(TitleBarStyle.hidden);
    await windowManager.setBounds(Rect.fromLTWH(0, 0, screenW, screenH));
    await windowManager.setAlwaysOnTop(true);
    await windowManager.show();
    await windowManager.focus();

    await Future<void>.delayed(const Duration(milliseconds: 150));

    if (mounted) {
      setState(() {
        _backgroundImage = decoded;
        _loading = false;
      });
      _focusNode.requestFocus();
    }
  }

  Future<ui.Image> _decodeImage(Uint8List pngBytes) async {
    final codec = await ui.instantiateImageCodec(pngBytes);
    final frame = await codec.getNextFrame();
    return frame.image;
  }

  Future<void> _restore() async {
    await windowManager.setMinimumSize(const Size(160, 160));
    await windowManager.setBounds(_savedBounds);
    await windowManager.setTitleBarStyle(TitleBarStyle.hidden);
    await windowManager.setAsFrameless();
    await windowManager.setAlwaysOnTop(_wasAlwaysOnTop);
  }

  Rect? get _selectionRect {
    if (_dragStart == null || _dragEnd == null) return null;
    return Rect.fromPoints(_dragStart!, _dragEnd!);
  }

  Rect? _toScreenRect(Size displaySize) {
    final sel = _selectionRect;
    if (sel == null || _backgroundImage == null) return null;

    final imgW = _backgroundImage!.width.toDouble();
    final imgH = _backgroundImage!.height.toDouble();

    final scaleX = imgW / displaySize.width;
    final scaleY = imgH / displaySize.height;

    return Rect.fromLTRB(
      sel.left * scaleX,
      sel.top * scaleY,
      sel.right * scaleX,
      sel.bottom * scaleY,
    );
  }

  void _confirmRegion() async {
    final size = MediaQuery.of(context).size;
    final screenRect = _toScreenRect(size);
    await _restore();
    if (mounted) {
      Navigator.pop(context, FocusTarget.region(screenRect));
    }
  }

  void _selectFullScreen() async {
    await _restore();
    if (mounted) Navigator.pop(context, const FocusTarget.fullScreen());
  }

  void _selectWindow(String title) async {
    await _restore();
    if (mounted) Navigator.pop(context, FocusTarget.window(title));
  }

  void _cancel() async {
    await _restore();
    if (mounted) Navigator.pop(context, null);
  }

  Future<void> _loadWindowList() async {
    try {
      final result = await Process.run('powershell', [
        '-NoProfile',
        '-Command',
        r"Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | "
            r"Select-Object -ExpandProperty MainWindowTitle | Sort-Object",
      ]);
      if (result.exitCode == 0) {
        final titles = (result.stdout as String)
            .split('\n')
            .map((l) => l.trim())
            .where((l) => l.isNotEmpty)
            .toList();
        if (mounted) setState(() => _windowTitles = titles);
      }
    } catch (_) {}
  }

  @override
  void dispose() {
    _backgroundImage?.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(
        backgroundColor: Colors.black87,
        body: Center(child: CircularProgressIndicator()),
      );
    }

    return KeyboardListener(
      focusNode: _focusNode,
      autofocus: true,
      onKeyEvent: (event) {
        if (event is KeyDownEvent &&
            event.logicalKey == LogicalKeyboardKey.escape) {
          if (_showWindowPicker) {
            setState(() => _showWindowPicker = false);
          } else {
            _cancel();
          }
        }
      },
      child: Scaffold(
        backgroundColor: Colors.transparent,
        body: Stack(
          fit: StackFit.expand,
          clipBehavior: Clip.none,
          children: [
            if (_backgroundImage != null)
              RawImage(image: _backgroundImage, fit: BoxFit.cover),

            if (!_showWindowPicker) _buildOverlayWithCutout(context),

            if (_showWindowPicker) _buildWindowPickerOverlay(),

            Positioned(
              top: 24,
              left: 0,
              right: 0,
              child: Center(child: _buildToolbar()),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildOverlayWithCutout(BuildContext context) {
    return GestureDetector(
      onPanStart: (d) => setState(() {
        _dragStart = d.localPosition;
        _dragEnd = d.localPosition;
      }),
      onPanUpdate: (d) => setState(() => _dragEnd = d.localPosition),
      onPanEnd: (_) {},
      child: CustomPaint(
        painter: _OverlayPainter(selection: _selectionRect),
        size: Size.infinite,
      ),
    );
  }

  Widget _buildWindowPickerOverlay() {
    return Container(
      color: Colors.black54,
      child: Center(
        child: Container(
          width: 420,
          constraints: const BoxConstraints(maxHeight: 500),
          decoration: BoxDecoration(
            color: const Color(0xFF1E1E1E),
            borderRadius: BorderRadius.circular(16),
            boxShadow: const [BoxShadow(color: Colors.black45, blurRadius: 16)],
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 16, 20, 8),
                child: Row(
                  children: [
                    const Icon(Icons.window, color: Colors.white70, size: 20),
                    const SizedBox(width: 10),
                    const Expanded(
                      child: Text(
                        'Select a window to focus on',
                        style: TextStyle(color: Colors.white, fontSize: 15, fontWeight: FontWeight.w500),
                      ),
                    ),
                    IconButton(
                      icon: const Icon(Icons.close, color: Colors.white54, size: 18),
                      onPressed: () => setState(() => _showWindowPicker = false),
                      padding: EdgeInsets.zero,
                      constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
                    ),
                  ],
                ),
              ),
              const Divider(color: Colors.white24, height: 1),
              if (_windowTitles.isEmpty)
                const Padding(
                  padding: EdgeInsets.all(24),
                  child: Text('No windows found', style: TextStyle(color: Colors.white54)),
                ),
              if (_windowTitles.isNotEmpty)
                Flexible(
                  child: ListView.builder(
                    shrinkWrap: true,
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    itemCount: _windowTitles.length,
                    itemBuilder: (_, i) => ListTile(
                      leading: const Icon(Icons.web_asset, color: Colors.white54, size: 18),
                      title: Text(
                        _windowTitles[i],
                        style: const TextStyle(color: Colors.white, fontSize: 13),
                        overflow: TextOverflow.ellipsis,
                      ),
                      dense: true,
                      hoverColor: Colors.white12,
                      onTap: () => _selectWindow(_windowTitles[i]),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildToolbar() {
    final hasSelection = _selectionRect != null &&
        _selectionRect!.width.abs() > 10 &&
        _selectionRect!.height.abs() > 10;

    return Container(
      constraints: const BoxConstraints(maxWidth: 720),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      decoration: BoxDecoration(
        color: Colors.black87,
        borderRadius: BorderRadius.circular(12),
        boxShadow: const [BoxShadow(color: Colors.black45, blurRadius: 12)],
      ),
      child: Wrap(
        alignment: WrapAlignment.center,
        crossAxisAlignment: WrapCrossAlignment.center,
        spacing: 8,
        runSpacing: 6,
        children: [
          const Icon(Icons.center_focus_strong, color: Colors.white70, size: 20),
          Text(
            _showWindowPicker ? 'Pick a window' : 'Drag a region or choose:',
            style: const TextStyle(color: Colors.white70, fontSize: 14),
          ),
          TextButton.icon(
            onPressed: _selectFullScreen,
            icon: const Icon(Icons.fullscreen, size: 20),
            label: const Text('Full Screen'),
            style: TextButton.styleFrom(foregroundColor: Colors.white),
          ),
          TextButton.icon(
            onPressed: () {
              _loadWindowList();
              setState(() => _showWindowPicker = !_showWindowPicker);
            },
            icon: const Icon(Icons.window, size: 20),
            label: const Text('Window'),
            style: TextButton.styleFrom(
              foregroundColor: _showWindowPicker ? const Color(0xFF42A5F5) : Colors.white,
            ),
          ),
          if (hasSelection && !_showWindowPicker)
            FilledButton.icon(
              onPressed: _confirmRegion,
              icon: const Icon(Icons.check, size: 18),
              label: const Text('Confirm'),
            ),
          TextButton(
            onPressed: _cancel,
            style: TextButton.styleFrom(foregroundColor: Colors.white70),
            child: const Text('Cancel'),
          ),
        ],
      ),
    );
  }
}

class _OverlayPainter extends CustomPainter {
  _OverlayPainter({this.selection});
  final Rect? selection;

  @override
  void paint(Canvas canvas, Size size) {
    final dimPaint = Paint()..color = const Color(0x88000000);

    if (selection == null ||
        selection!.width.abs() < 2 ||
        selection!.height.abs() < 2) {
      canvas.drawRect(Offset.zero & size, dimPaint);
      return;
    }

    final sel = Rect.fromPoints(
      Offset(selection!.left.clamp(0, size.width),
          selection!.top.clamp(0, size.height)),
      Offset(selection!.right.clamp(0, size.width),
          selection!.bottom.clamp(0, size.height)),
    );

    final path = Path()
      ..addRect(Offset.zero & size)
      ..addRect(sel)
      ..fillType = PathFillType.evenOdd;
    canvas.drawPath(path, dimPaint);

    final borderPaint = Paint()
      ..color = const Color(0xFF42A5F5)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.0;
    canvas.drawRect(sel, borderPaint);

    const handleSize = 8.0;
    final handlePaint = Paint()..color = const Color(0xFF42A5F5);
    for (final corner in [
      sel.topLeft,
      sel.topRight,
      sel.bottomLeft,
      sel.bottomRight,
    ]) {
      canvas.drawRect(
        Rect.fromCenter(center: corner, width: handleSize, height: handleSize),
        handlePaint,
      );
    }

    final w = sel.width.round();
    final h = sel.height.round();
    final textSpan = TextSpan(
      text: '$w x $h',
      style: const TextStyle(
        color: Colors.white,
        fontSize: 12,
        backgroundColor: Color(0xCC000000),
      ),
    );
    final tp = TextPainter(text: textSpan, textDirection: TextDirection.ltr)
      ..layout();
    tp.paint(canvas, Offset(sel.left + 4, sel.bottom + 6));
  }

  @override
  bool shouldRepaint(_OverlayPainter old) => old.selection != selection;
}
