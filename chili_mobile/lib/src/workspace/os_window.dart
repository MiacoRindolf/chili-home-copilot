import 'package:flutter/material.dart';

import 'workspace_controller.dart';

/// A draggable / resizable in-app window in the CHILI OS workspace. Hosts an
/// app surface ([child]) under a title bar with minimize / maximize / close.
class OsWindow extends StatelessWidget {
  final WsWindow data;
  final WorkspaceController controller;
  final Widget child;
  final bool focused;
  final Size desktopSize;

  const OsWindow({
    super.key,
    required this.data,
    required this.controller,
    required this.child,
    required this.focused,
    required this.desktopSize,
  });

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Positioned(
      left: data.position.dx,
      top: data.position.dy,
      width: data.size.width,
      height: data.size.height,
      child: Offstage(
        // Minimized windows stay in the tree (state preserved) but aren't shown.
        offstage: data.minimized,
        child: Listener(
          onPointerDown: (_) => controller.focus(data.id),
          child: Material(
            elevation: focused ? 14 : 4,
            color: cs.surface,
            borderRadius: BorderRadius.circular(12),
            clipBehavior: Clip.antiAlias,
            child: Stack(
              children: <Widget>[
                Column(
                  children: <Widget>[
                    _titleBar(context, cs),
                    Expanded(child: child),
                  ],
                ),
                // Resize grip (bottom-right). Hidden while maximized.
                if (!data.maximized)
                  Positioned(
                    right: 0,
                    bottom: 0,
                    child: _ResizeGrip(
                      onDrag: (Offset d) => controller.resize(data.id, d),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _titleBar(BuildContext context, ColorScheme cs) {
    final Color barColor = focused
        ? Color.alphaBlend(cs.primary.withValues(alpha: 0.10), cs.surfaceContainerHighest)
        : cs.surfaceContainerHighest;
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onPanUpdate: (DragUpdateDetails d) => controller.move(data.id, d.delta),
      onDoubleTap: () => controller.toggleMaximize(data.id, desktopSize),
      child: Container(
        height: 38,
        padding: const EdgeInsets.only(left: 12, right: 6),
        decoration: BoxDecoration(
          color: barColor,
          border: Border(bottom: BorderSide(color: cs.outlineVariant, width: 1)),
        ),
        child: Row(
          children: <Widget>[
            Icon(data.icon, size: 16, color: cs.primary),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                data.title,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                  color: cs.onSurface,
                ),
              ),
            ),
            _barBtn(context, Icons.remove, 'Minimize', () => controller.minimize(data.id)),
            _barBtn(
              context,
              data.maximized ? Icons.fullscreen_exit : Icons.crop_square,
              data.maximized ? 'Restore' : 'Maximize',
              () => controller.toggleMaximize(data.id, desktopSize),
            ),
            _barBtn(context, Icons.close, 'Close', () => controller.close(data.id), danger: true),
          ],
        ),
      ),
    );
  }

  Widget _barBtn(BuildContext context, IconData icon, String tip, VoidCallback onTap,
      {bool danger = false}) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Tooltip(
      message: tip,
      child: InkResponse(
        onTap: onTap,
        radius: 18,
        child: Container(
          width: 30,
          height: 30,
          alignment: Alignment.center,
          child: Icon(icon, size: 16, color: danger ? cs.error : cs.onSurfaceVariant),
        ),
      ),
    );
  }
}

class _ResizeGrip extends StatelessWidget {
  final ValueChanged<Offset> onDrag;
  const _ResizeGrip({required this.onDrag});

  @override
  Widget build(BuildContext context) {
    return MouseRegion(
      cursor: SystemMouseCursors.resizeDownRight,
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onPanUpdate: (DragUpdateDetails d) => onDrag(d.delta),
        child: SizedBox(
          width: 18,
          height: 18,
          child: CustomPaint(painter: _GripPainter(Theme.of(context).colorScheme.outline)),
        ),
      ),
    );
  }
}

class _GripPainter extends CustomPainter {
  final Color color;
  _GripPainter(this.color);

  @override
  void paint(Canvas canvas, Size size) {
    final Paint p = Paint()
      ..color = color
      ..strokeWidth = 1.2;
    for (int i = 1; i <= 3; i++) {
      final double o = i * 4.0;
      canvas.drawLine(Offset(size.width - o, size.height - 2), Offset(size.width - 2, size.height - o), p);
    }
  }

  @override
  bool shouldRepaint(covariant _GripPainter old) => old.color != color;
}
