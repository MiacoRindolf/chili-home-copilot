import 'dart:math' as math;

import 'package:flutter/material.dart';

/// Map a series of values to points within [size] for a sparkline (TC-2). Pure +
/// testable. Returns [] for fewer than 2 values; a flat series sits mid-height.
List<Offset> sparklinePoints(List<double> values, Size size) {
  if (values.length < 2 || size.width <= 0 || size.height <= 0) {
    return const <Offset>[];
  }
  double min = values.first;
  double max = values.first;
  for (final double v in values) {
    if (v < min) min = v;
    if (v > max) max = v;
  }
  final double range = max - min;
  final double dx = size.width / (values.length - 1);
  return <Offset>[
    for (int i = 0; i < values.length; i++)
      Offset(
        i * dx,
        range == 0
            ? size.height / 2
            : size.height - ((values[i] - min) / range) * size.height,
      ),
  ];
}

/// A compact line chart of [values] with a soft gradient fill. Colour defaults
/// to a trend tint (green if the series ends above where it started, else red).
class Sparkline extends StatelessWidget {
  const Sparkline({
    super.key,
    required this.values,
    this.color,
    this.height = 44,
    this.strokeWidth = 2,
  });

  final List<double> values;
  final Color? color;
  final double height;
  final double strokeWidth;

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    final Color c = color ??
        (values.length >= 2 && values.last >= values.first
            ? Colors.green
            : cs.error);
    return SizedBox(
      height: height,
      width: double.infinity,
      child: CustomPaint(
        painter: _SparklinePainter(values, c, strokeWidth),
      ),
    );
  }
}

class _SparklinePainter extends CustomPainter {
  _SparklinePainter(this.values, this.color, this.strokeWidth);

  final List<double> values;
  final Color color;
  final double strokeWidth;

  @override
  void paint(Canvas canvas, Size size) {
    final List<Offset> pts = sparklinePoints(values, size);
    if (pts.isEmpty) return;

    final Path line = Path()..moveTo(pts.first.dx, pts.first.dy);
    for (int i = 1; i < pts.length; i++) {
      line.lineTo(pts[i].dx, pts[i].dy);
    }

    // Soft fill under the line.
    final Path fill = Path.from(line)
      ..lineTo(pts.last.dx, size.height)
      ..lineTo(pts.first.dx, size.height)
      ..close();
    canvas.drawPath(
      fill,
      Paint()
        ..shader = LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: <Color>[
            color.withValues(alpha: 0.22),
            color.withValues(alpha: 0.0),
          ],
        ).createShader(Offset.zero & size),
    );

    canvas.drawPath(
      line,
      Paint()
        ..color = color
        ..style = PaintingStyle.stroke
        ..strokeWidth = strokeWidth
        ..strokeJoin = StrokeJoin.round
        ..strokeCap = StrokeCap.round,
    );

    // Dot on the latest point.
    canvas.drawCircle(
        pts.last, math.max(2.0, strokeWidth), Paint()..color = color);
  }

  @override
  bool shouldRepaint(_SparklinePainter old) =>
      old.values != values || old.color != color;
}
