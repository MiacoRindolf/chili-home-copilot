import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

enum AvatarState { idle, listening, thinking, speaking }

/// Animated chili mascot avatar with state-specific animations.
/// Uses a CustomPainter-drawn chili pepper for guaranteed transparency.
class ChiliAvatar extends StatefulWidget {
  const ChiliAvatar({super.key, required this.state});

  final AvatarState state;

  @override
  State<ChiliAvatar> createState() => _ChiliAvatarState();
}

class _ChiliAvatarState extends State<ChiliAvatar>
    with TickerProviderStateMixin {
  late AnimationController _bobController;
  late AnimationController _pulseController;
  late AnimationController _ringController;
  late AnimationController _dotsController;
  late AnimationController _breathController;

  @override
  void initState() {
    super.initState();
    _bobController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2000),
    )..repeat(reverse: true);

    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1500),
    )..repeat(reverse: true);

    _ringController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 800),
    )..repeat();

    _dotsController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2000),
    )..repeat();

    _breathController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..repeat(reverse: true);
  }

  @override
  void didUpdateWidget(ChiliAvatar oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.state != widget.state) {
      setState(() {});
    }
  }

  @override
  void dispose() {
    _bobController.dispose();
    _pulseController.dispose();
    _ringController.dispose();
    _dotsController.dispose();
    _breathController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 160,
      width: 160,
      child: AnimatedBuilder(
        animation: Listenable.merge([
          _bobController,
          _pulseController,
          _ringController,
          _dotsController,
          _breathController,
        ]),
        builder: (context, child) {
          final bob = Tween<double>(begin: 0, end: 6).animate(
            CurvedAnimation(parent: _bobController, curve: Curves.easeInOut),
          );
          final pulse = Tween<double>(begin: 0.92, end: 1.08).animate(
            CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
          );
          final ring = Tween<double>(begin: 0.8, end: 1.3).animate(
            CurvedAnimation(parent: _ringController, curve: Curves.easeInOut),
          );
          final breath = Tween<double>(begin: 0.88, end: 1.15).animate(
            CurvedAnimation(
                parent: _breathController, curve: Curves.easeInOut),
          );

          double scale = 1.0;
          double translateY = 0.0;
          bool showRing = false;
          bool showDots = false;
          double glowOpacity = 0.0;

          switch (widget.state) {
            case AvatarState.idle:
              translateY = bob.value;
              scale = pulse.value;
              glowOpacity = 0.15 + 0.1 * _pulseController.value;
              break;
            case AvatarState.listening:
              translateY = bob.value * 0.5;
              scale = 1.0;
              showRing = true;
              glowOpacity = 0.25;
              break;
            case AvatarState.thinking:
              translateY = 0;
              scale = 1.0;
              showDots = true;
              glowOpacity = 0.2;
              break;
            case AvatarState.speaking:
              translateY = 0;
              scale = breath.value;
              glowOpacity = 0.2;
              break;
          }

          Color tintColor = const Color(0xFFEF5350);
          if (widget.state == AvatarState.listening) {
            tintColor = const Color(0xFF4CAF50);
          } else if (widget.state == AvatarState.thinking) {
            tintColor = const Color(0xFFFFC107);
          } else if (widget.state == AvatarState.speaking) {
            tintColor = const Color(0xFF2196F3);
          }

          return Stack(
            alignment: Alignment.center,
            clipBehavior: Clip.none,
            children: [
              if (glowOpacity > 0)
                Container(
                  width: 120,
                  height: 120,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    boxShadow: [
                      BoxShadow(
                        color: tintColor.withValues(alpha: glowOpacity),
                        blurRadius: 24,
                        spreadRadius: 2,
                      ),
                    ],
                  ),
                ),
              if (showRing)
                Container(
                  width: 120 * ring.value,
                  height: 120 * ring.value,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: tintColor.withValues(alpha: 0.85),
                      width: 4,
                    ),
                  ),
                ),
              if (showDots) _buildOrbitingDots(),
              Transform.translate(
                offset: Offset(0, translateY),
                child: Transform.scale(
                  scale: scale,
                  child: CustomPaint(
                    size: const Size(100, 120),
                    painter: _ChiliMascotPainter(
                      tintColor: widget.state != AvatarState.idle
                          ? tintColor
                          : null,
                    ),
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }

  Widget _buildOrbitingDots() {
    const dotCount = 3;
    final angle = _dotsController.value * 2 * math.pi;
    return CustomPaint(
      size: const Size(160, 160),
      painter: _OrbitDotsPainter(
        progress: angle,
        dotCount: dotCount,
        color: const Color(0xFFEF5350),
      ),
    );
  }
}

/// Draws a cute chili pepper mascot entirely in code -- no PNG needed.
class _ChiliMascotPainter extends CustomPainter {
  _ChiliMascotPainter({this.tintColor});

  final Color? tintColor;

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
    const bodyColor = Color(0xFFE53935);
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

    // Eyes - white circles with dark pupils
    final eyeWhitePaint = Paint()
      ..color = Colors.white
      ..style = PaintingStyle.fill;
    final pupilPaint = Paint()
      ..color = const Color(0xFF3E2723)
      ..style = PaintingStyle.fill;
    final eyeGlintPaint = Paint()
      ..color = Colors.white
      ..style = PaintingStyle.fill;

    // Left eye
    canvas.drawCircle(Offset(w * 0.37, h * 0.42), w * 0.09, eyeWhitePaint);
    canvas.drawCircle(Offset(w * 0.38, h * 0.43), w * 0.055, pupilPaint);
    canvas.drawCircle(Offset(w * 0.36, h * 0.41), w * 0.025, eyeGlintPaint);

    // Right eye
    canvas.drawCircle(Offset(w * 0.57, h * 0.38), w * 0.09, eyeWhitePaint);
    canvas.drawCircle(Offset(w * 0.58, h * 0.39), w * 0.055, pupilPaint);
    canvas.drawCircle(Offset(w * 0.56, h * 0.37), w * 0.025, eyeGlintPaint);

    // Rosy cheeks
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

    // Smile
    final smilePaint = Paint()
      ..color = const Color(0xFF3E2723)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.5
      ..strokeCap = StrokeCap.round;
    final smilePath = Path()
      ..moveTo(w * 0.38, h * 0.53)
      ..cubicTo(w * 0.42, h * 0.60, w * 0.52, h * 0.60, w * 0.56, h * 0.52);
    canvas.drawPath(smilePath, smilePaint);

    // Tint overlay for non-idle states
    if (tintColor != null) {
      final tintPaint = Paint()
        ..color = tintColor!.withValues(alpha: 0.18)
        ..style = PaintingStyle.fill;
      canvas.drawPath(bodyPath, tintPaint);
    }
  }

  @override
  bool shouldRepaint(covariant _ChiliMascotPainter oldDelegate) {
    return oldDelegate.tintColor != tintColor;
  }
}

class _OrbitDotsPainter extends CustomPainter {
  _OrbitDotsPainter({
    required this.progress,
    required this.dotCount,
    required this.color,
  });

  final double progress;
  final int dotCount;
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    const radius = 65.0;
    const dotRadius = 5.0;

    for (var i = 0; i < dotCount; i++) {
      final angle = progress + (2 * math.pi * i / dotCount);
      final x = center.dx + radius * math.cos(angle);
      final y = center.dy + radius * math.sin(angle);
      canvas.drawCircle(
        Offset(x, y),
        dotRadius,
        Paint()..color = color.withValues(alpha: 0.8),
      );
    }
  }

  @override
  bool shouldRepaint(covariant _OrbitDotsPainter oldDelegate) {
    return oldDelegate.progress != progress;
  }
}
