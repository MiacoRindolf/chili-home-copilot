import 'package:flutter/material.dart';

import 'workspace_controller.dart';

/// Bottom strip of chips for minimized windows — tap a chip to restore it.
/// Hidden (zero-size) when nothing is minimized, so it only appears on demand.
class WorkspaceTaskbar extends StatelessWidget {
  final List<WsWindow> minimized;
  final void Function(String id) onRestore;

  const WorkspaceTaskbar({
    super.key,
    required this.minimized,
    required this.onRestore,
  });

  @override
  Widget build(BuildContext context) {
    if (minimized.isEmpty) return const SizedBox.shrink();
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Positioned(
      left: 0,
      right: 0,
      bottom: 0,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: Color.alphaBlend(
            cs.surfaceContainerHighest.withValues(alpha: 0.92),
            cs.surface,
          ),
          border: Border(top: BorderSide(color: cs.outlineVariant, width: 1)),
        ),
        child: SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: <Widget>[
              for (final WsWindow w in minimized) _chip(context, cs, w),
            ],
          ),
        ),
      ),
    );
  }

  Widget _chip(BuildContext context, ColorScheme cs, WsWindow w) {
    return Padding(
      padding: const EdgeInsets.only(right: 8),
      child: Material(
        color: cs.surface,
        borderRadius: BorderRadius.circular(8),
        child: InkWell(
          onTap: () => onRestore(w.id),
          borderRadius: BorderRadius.circular(8),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Icon(w.icon, size: 14, color: cs.primary),
                const SizedBox(width: 6),
                Text(
                  w.title,
                  style: TextStyle(fontSize: 12, color: cs.onSurface),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
