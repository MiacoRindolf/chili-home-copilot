import 'dart:math' as math;

import 'package:flutter/material.dart';

enum AvatarState { idle, listening, thinking, speaking }

/// Animated chili mascot avatar with state-specific animations.
/// Uses the chili_mascot.png image with Flutter animations.
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
      height: 120,
      width: 120,
      child: AnimatedBuilder(
        animation: Listenable.merge([
          _bobController,
          _pulseController,
          _ringController,
          _dotsController,
          _breathController,
        ]),
        builder: (context, child) {
          final bob = Tween<double>(begin: 0, end: 6)
              .animate(CurvedAnimation(parent: _bobController, curve: Curves.easeInOut));
          final pulse = Tween<double>(begin: 0.92, end: 1.08)
              .animate(CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut));
          final ring = Tween<double>(begin: 0.8, end: 1.3)
              .animate(CurvedAnimation(parent: _ringController, curve: Curves.easeInOut));
          final breath = Tween<double>(begin: 0.95, end: 1.08)
              .animate(CurvedAnimation(parent: _breathController, curve: Curves.easeInOut));

          double scale = 1.0;
          double translateY = 0.0;
          bool showRing = false;
          bool showDots = false;
          double? glowOpacity;

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

          return Stack(
            alignment: Alignment.center,
            clipBehavior: Clip.none,
            children: [
              if (glowOpacity != null)
                Container(
                  width: 100,
                  height: 100,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    boxShadow: [
                      BoxShadow(
                        color: const Color(0xFFEF5350).withOpacity(glowOpacity),
                        blurRadius: 24,
                        spreadRadius: 2,
                      ),
                    ],
                  ),
                ),
              if (showRing)
                Container(
                  width: 100 * ring.value,
                  height: 100 * ring.value,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: const Color(0xFFEF5350).withOpacity(0.6),
                      width: 3,
                    ),
                  ),
                ),
              if (showDots) _buildOrbitingDots(),
              Transform.translate(
                offset: Offset(0, translateY),
                child: Transform.scale(
                  scale: scale,
                  child: Image.asset(
                    'assets/chili_mascot.png',
                    width: 100,
                    height: 100,
                    fit: BoxFit.contain,
                    errorBuilder: (_, __, ___) => Icon(
                      Icons.local_fire_department,
                      size: 80,
                      color: Colors.red.shade400,
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
    return LayoutBuilder(
      builder: (context, constraints) {
          return CustomPaint(
          size: const Size(120, 120),
          painter: _OrbitDotsPainter(
            progress: angle,
            dotCount: dotCount,
            color: const Color(0xFFEF5350),
          ),
        );
      },
    );
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
    const radius = 52.0;
    const dotRadius = 4.0;

    for (var i = 0; i < dotCount; i++) {
      final angle = progress + (2 * math.pi * i / dotCount);
      final x = center.dx + radius * math.cos(angle);
      final y = center.dy + radius * math.sin(angle);
      canvas.drawCircle(
        Offset(x, y),
        dotRadius,
        Paint()..color = color.withOpacity(0.8),
      );
    }
  }

  @override
  bool shouldRepaint(covariant _OrbitDotsPainter oldDelegate) {
    return oldDelegate.progress != progress;
  }
}
