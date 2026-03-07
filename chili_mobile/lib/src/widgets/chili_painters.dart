import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import '../config/chili_colors.dart';
import 'chili_avatar.dart';

/// Draws a cute chili pepper mascot entirely in code -- no PNG needed.
/// [expression] and [mouthOpen] control face (eyes, eyebrows, mouth).
/// [pupilOffset] optionally shifts both pupils (for idle look-around).
class ChiliMascotPainter extends CustomPainter {
  ChiliMascotPainter({
    required this.expression,
    this.mouthOpen = 0.0,
    this.tintColor,
    this.pupilOffset,
  });

  final AvatarExpression expression;
  final double mouthOpen;
  final Color? tintColor;
  final Offset? pupilOffset;

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height;

    // Stem
    final stemPaint = Paint()
      ..color = const Color(0xFF4CAF50)
      ..style = PaintingStyle.fill;
    final stemPath = Path()
      ..moveTo(w * 0.42, h * 0.18)
      ..cubicTo(w * 0.40, h * 0.08, w * 0.48, h * 0.0, w * 0.55, h * 0.02)
      ..cubicTo(w * 0.62, h * 0.04, w * 0.58, h * 0.12, w * 0.55, h * 0.18)
      ..close();
    canvas.drawPath(stemPath, stemPaint);

    // Small leaf on the stem
    final leafPaint = Paint()
      ..color = const Color(0xFF66BB6A)
      ..style = PaintingStyle.fill;
    final leafPath = Path()
      ..moveTo(w * 0.52, h * 0.10)
      ..cubicTo(w * 0.62, h * 0.04, w * 0.72, h * 0.06, w * 0.68, h * 0.12)
      ..cubicTo(w * 0.64, h * 0.16, w * 0.56, h * 0.14, w * 0.52, h * 0.10)
      ..close();
    canvas.drawPath(leafPath, leafPaint);

    // Body: a plump curved chili pepper shape
    const bodyColor = ChiliColors.avatarBodyRed;
    final bodyPaint = Paint()
      ..color = bodyColor
      ..style = PaintingStyle.fill;

    final bodyPath = Path()
      ..moveTo(w * 0.35, h * 0.20)
      ..cubicTo(w * 0.15, h * 0.30, w * 0.08, h * 0.55, w * 0.22, h * 0.78)
      ..cubicTo(w * 0.30, h * 0.88, w * 0.38, h * 0.95, w * 0.48, h * 0.98)
      ..cubicTo(w * 0.55, h * 1.0, w * 0.58, h * 0.96, w * 0.55, h * 0.90)
      ..cubicTo(w * 0.80, h * 0.70, w * 0.85, h * 0.42, w * 0.65, h * 0.22)
      ..cubicTo(w * 0.58, h * 0.16, w * 0.45, h * 0.15, w * 0.35, h * 0.20)
      ..close();
    canvas.drawPath(bodyPath, bodyPaint);

    // Highlight on body for glossy look
    final highlightPaint = Paint()
      ..shader = ui.Gradient.linear(
        Offset(w * 0.30, h * 0.25),
        Offset(w * 0.55, h * 0.60),
        [
          Colors.white.withValues(alpha: 0.35),
          Colors.white.withValues(alpha: 0.0),
        ],
      )
      ..style = PaintingStyle.fill;
    final highlightPath = Path()
      ..moveTo(w * 0.38, h * 0.24)
      ..cubicTo(w * 0.25, h * 0.32, w * 0.20, h * 0.50, w * 0.30, h * 0.62)
      ..cubicTo(w * 0.35, h * 0.55, w * 0.35, h * 0.40, w * 0.38, h * 0.24)
      ..close();
    canvas.drawPath(highlightPath, highlightPaint);

    _drawEyebrows(canvas, w, h);
    _drawEyes(canvas, w, h);
    _drawCheeks(canvas, w, h);
    _drawMouth(canvas, w, h);

    // Tint overlay for non-idle states
    if (tintColor != null) {
      final tintPaint = Paint()
        ..color = tintColor!.withValues(alpha: 0.18)
        ..style = PaintingStyle.fill;
      canvas.drawPath(bodyPath, tintPaint);
    }
  }

  void _drawEyebrows(Canvas canvas, double w, double h) {
    final paint = Paint()
      ..color = ChiliColors.avatarFaceBrown
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.2
      ..strokeCap = StrokeCap.round;
    final leftCx = w * 0.37;
    final rightCx = w * 0.57;
    final y = h * 0.34;
    final span = w * 0.08;
    switch (expression) {
      case AvatarExpression.neutral:
        // Subtle arcs
        canvas.drawArc(
          Rect.fromCenter(center: Offset(leftCx, y), width: span, height: h * 0.04),
          -0.2 * math.pi,
          0.4 * math.pi,
          false,
          paint,
        );
        canvas.drawArc(
          Rect.fromCenter(center: Offset(rightCx, y), width: span, height: h * 0.04),
          -0.2 * math.pi,
          0.4 * math.pi,
          false,
          paint,
        );
        break;
      case AvatarExpression.attentive:
        // Slightly raised
        canvas.drawArc(
          Rect.fromCenter(center: Offset(leftCx, y - 2), width: span, height: h * 0.04),
          -0.15 * math.pi,
          0.35 * math.pi,
          false,
          paint,
        );
        canvas.drawArc(
          Rect.fromCenter(center: Offset(rightCx, y - 2), width: span, height: h * 0.04),
          -0.15 * math.pi,
          0.35 * math.pi,
          false,
          paint,
        );
        break;
      case AvatarExpression.sleepy:
        // Flat sleepy eyebrows
        canvas.drawLine(Offset(leftCx - span * 0.6, h * 0.36), Offset(leftCx + span * 0.6, h * 0.36), paint);
        canvas.drawLine(Offset(rightCx - span * 0.6, h * 0.33), Offset(rightCx + span * 0.6, h * 0.33), paint);
        break;
      case AvatarExpression.thinking:
        // Raised, curved
        canvas.drawArc(
          Rect.fromCenter(center: Offset(leftCx, y - 3), width: span * 1.1, height: h * 0.05),
          -0.25 * math.pi,
          0.5 * math.pi,
          false,
          paint,
        );
        canvas.drawArc(
          Rect.fromCenter(center: Offset(rightCx, y - 3), width: span * 1.1, height: h * 0.05),
          -0.25 * math.pi,
          0.5 * math.pi,
          false,
          paint,
        );
        break;
      case AvatarExpression.happy:
        // Curved down (squint)
        canvas.drawArc(
          Rect.fromCenter(center: Offset(leftCx, y + 4), width: span, height: h * 0.04),
          0.1 * math.pi,
          0.4 * math.pi,
          false,
          paint,
        );
        canvas.drawArc(
          Rect.fromCenter(center: Offset(rightCx, y + 4), width: span, height: h * 0.04),
          0.1 * math.pi,
          0.4 * math.pi,
          false,
          paint,
        );
        break;
      case AvatarExpression.confused:
        // Left normal, right raised
        canvas.drawArc(
          Rect.fromCenter(center: Offset(leftCx, y), width: span, height: h * 0.04),
          -0.2 * math.pi,
          0.4 * math.pi,
          false,
          paint,
        );
        canvas.drawArc(
          Rect.fromCenter(center: Offset(rightCx, y - 5), width: span * 1.2, height: h * 0.05),
          -0.3 * math.pi,
          0.5 * math.pi,
          false,
          paint,
        );
        break;
      case AvatarExpression.error:
        // Furrowed (angled down toward center)
        canvas.drawLine(Offset(leftCx - span * 0.5, y + 2), Offset(leftCx + span * 0.5, y - 2), paint);
        canvas.drawLine(Offset(rightCx - span * 0.5, y - 2), Offset(rightCx + span * 0.5, y + 2), paint);
        break;
      case AvatarExpression.talking:
        // Neutral/engaged
        canvas.drawArc(
          Rect.fromCenter(center: Offset(leftCx, y), width: span, height: h * 0.04),
          -0.2 * math.pi,
          0.4 * math.pi,
          false,
          paint,
        );
        canvas.drawArc(
          Rect.fromCenter(center: Offset(rightCx, y), width: span, height: h * 0.04),
          -0.2 * math.pi,
          0.4 * math.pi,
          false,
          paint,
        );
        break;
    }
  }

  void _drawEyes(Canvas canvas, double w, double h) {
    final eyeWhitePaint = Paint()
      ..color = Colors.white
      ..style = PaintingStyle.fill;
    final pupilPaint = Paint()
      ..color = ChiliColors.avatarFaceBrown
      ..style = PaintingStyle.fill;
    final eyeGlintPaint = Paint()
      ..color = Colors.white
      ..style = PaintingStyle.fill;

    final leftCenter = Offset(w * 0.37, h * 0.42);
    final rightCenter = Offset(w * 0.57, h * 0.38);
    final radius = w * 0.09;
    final pupilRadius = w * 0.055;
    final glintRadius = w * 0.025;

    if (expression == AvatarExpression.sleepy) {
      // Draw thin lines for sleepy eyes
      final linePaint = Paint()
        ..color = const Color(0xFF3E2723)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.5
        ..strokeCap = StrokeCap.round;
      canvas.drawLine(
        Offset(leftCenter.dx - radius * 0.7, leftCenter.dy),
        Offset(leftCenter.dx + radius * 0.7, leftCenter.dy),
        linePaint,
      );
      canvas.drawLine(
        Offset(rightCenter.dx - radius * 0.7, rightCenter.dy),
        Offset(rightCenter.dx + radius * 0.7, rightCenter.dy),
        linePaint,
      );
      return;
    }

    if (expression == AvatarExpression.happy) {
      // Squinted: curved lines
      final linePaint = Paint()
        ..color = const Color(0xFF3E2723)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.2
        ..strokeCap = StrokeCap.round;
      canvas.drawArc(
        Rect.fromCenter(center: leftCenter, width: radius * 2, height: h * 0.06),
        0.15 * math.pi,
        0.7 * math.pi,
        false,
        linePaint,
      );
      canvas.drawArc(
        Rect.fromCenter(center: rightCenter, width: radius * 2, height: h * 0.06),
        0.15 * math.pi,
        0.7 * math.pi,
        false,
        linePaint,
      );
      return;
    }

    // Pupil offset for thinking (look up) or idle look-around
    double pupilDx = 0.01 * w;
    double pupilDy = 0.01 * h;
    if (expression == AvatarExpression.thinking) {
      pupilDy -= h * 0.03;
    }
    if (pupilOffset != null) {
      pupilDx += pupilOffset!.dx;
      pupilDy += pupilOffset!.dy;
    }

    final effectivePupilRadius = expression == AvatarExpression.attentive
        ? pupilRadius * 1.15
        : pupilRadius;
    canvas.drawCircle(leftCenter, radius, eyeWhitePaint);
    canvas.drawCircle(leftCenter + Offset(pupilDx, pupilDy), effectivePupilRadius, pupilPaint);
    canvas.drawCircle(leftCenter + Offset(-0.01 * w, -0.01 * h), glintRadius, eyeGlintPaint);

    canvas.drawCircle(rightCenter, radius, eyeWhitePaint);
    canvas.drawCircle(rightCenter + Offset(pupilDx, pupilDy), effectivePupilRadius, pupilPaint);
    canvas.drawCircle(rightCenter + Offset(-0.01 * w, -0.01 * h), glintRadius, eyeGlintPaint);
  }

  void _drawCheeks(Canvas canvas, double w, double h) {
    final cheekPaint = Paint()
      ..color = const Color(0xFFFF8A80).withValues(alpha: 0.5)
      ..style = PaintingStyle.fill;
    canvas.drawOval(
      Rect.fromCenter(
          center: Offset(w * 0.28, h * 0.52), width: w * 0.12, height: h * 0.06),
      cheekPaint,
    );
    canvas.drawOval(
      Rect.fromCenter(
          center: Offset(w * 0.64, h * 0.48), width: w * 0.12, height: h * 0.06),
      cheekPaint,
    );
  }

  void _drawMouth(Canvas canvas, double w, double h) {
    final strokePaint = Paint()
      ..color = ChiliColors.avatarFaceBrown
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.5
      ..strokeCap = StrokeCap.round;
    final fillPaint = Paint()
      ..color = const Color(0xFF3E2723)
      ..style = PaintingStyle.fill;

    final cx = w * 0.47;
    final mouthY = h * 0.56;

    switch (expression) {
      case AvatarExpression.neutral:
      case AvatarExpression.attentive:
      case AvatarExpression.thinking:
      case AvatarExpression.talking:
        if (expression == AvatarExpression.talking && mouthOpen > 0.05) {
          final openH = h * 0.06 * (0.3 + mouthOpen * 0.7);
          canvas.drawOval(
            Rect.fromCenter(center: Offset(cx, mouthY), width: w * 0.12, height: openH),
            fillPaint,
          );
        } else if (expression == AvatarExpression.attentive) {
          canvas.drawOval(
            Rect.fromCenter(center: Offset(cx, mouthY), width: w * 0.06, height: h * 0.04),
            strokePaint,
          );
        } else {
          final smilePath = Path()
            ..moveTo(w * 0.38, h * 0.53)
            ..cubicTo(w * 0.42, h * 0.60, w * 0.52, h * 0.60, w * 0.56, h * 0.52);
          canvas.drawPath(smilePath, strokePaint);
        }
        break;
      case AvatarExpression.sleepy:
        canvas.drawLine(
          Offset(cx - w * 0.06, mouthY),
          Offset(cx + w * 0.06, mouthY),
          strokePaint,
        );
        break;
      case AvatarExpression.happy:
        final wideSmile = Path()
          ..moveTo(w * 0.35, h * 0.54)
          ..cubicTo(w * 0.47, h * 0.64, w * 0.59, h * 0.64, w * 0.59, h * 0.54);
        strokePaint.strokeWidth = 3;
        canvas.drawPath(wideSmile, strokePaint);
        break;
      case AvatarExpression.confused:
        canvas.drawOval(
          Rect.fromCenter(center: Offset(cx, mouthY), width: w * 0.05, height: h * 0.03),
          strokePaint,
        );
        break;
      case AvatarExpression.error:
        final frownPath = Path()
          ..moveTo(w * 0.38, h * 0.58)
          ..cubicTo(w * 0.42, h * 0.52, w * 0.52, h * 0.52, w * 0.56, h * 0.58);
        canvas.drawPath(frownPath, strokePaint);
        break;
    }
  }

  @override
  bool shouldRepaint(covariant ChiliMascotPainter oldDelegate) {
    return oldDelegate.tintColor != tintColor ||
        oldDelegate.expression != expression ||
        oldDelegate.mouthOpen != mouthOpen ||
        oldDelegate.pupilOffset != pupilOffset;
  }
}

/// Simple flame/radial aura behind the mascot for error state.
class FlameAuraPainter extends CustomPainter {
  FlameAuraPainter({required this.color});

  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final paint = Paint()..style = PaintingStyle.fill;
    for (var i = 0; i < 8; i++) {
      final angle = (i / 8) * 2 * math.pi + 0.1;
      final dx = 45 * math.cos(angle);
      final dy = 45 * math.sin(angle);
      paint.color = color.withValues(alpha: 0.15 + 0.1 * (i % 3));
      canvas.drawOval(
        Rect.fromCenter(
          center: center + Offset(dx, dy),
          width: 28,
          height: 40,
        ),
        paint,
      );
    }
    paint.color = color.withValues(alpha: 0.12);
    canvas.drawCircle(center, 55, paint);
  }

  @override
  bool shouldRepaint(covariant FlameAuraPainter oldDelegate) =>
      oldDelegate.color != color;
}

