import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import '../config/chili_colors.dart';
import 'chili_painters.dart';

enum AvatarState {
  idle,
  listening,
  thinking,
  speaking,
  muted,
  reading,
  happy,
  confused,
  error,
  wakeDetected,
  actionPerforming,
}

/// Expression drawn on the mascot face (eyes, mouth, eyebrows).
enum AvatarExpression {
  neutral,
  attentive,
  sleepy,
  thinking,
  happy,
  confused,
  error,
  talking,
}

/// Maps [AvatarState] to the face [AvatarExpression] to draw.
AvatarExpression expressionForState(AvatarState state) {
  switch (state) {
    case AvatarState.idle:
      return AvatarExpression.neutral;
    case AvatarState.listening:
    case AvatarState.wakeDetected:
      return AvatarExpression.attentive;
    case AvatarState.muted:
      return AvatarExpression.sleepy;
    case AvatarState.reading:
      return AvatarExpression.thinking;
    case AvatarState.thinking:
      return AvatarExpression.thinking;
    case AvatarState.speaking:
      return AvatarExpression.talking;
    case AvatarState.happy:
      return AvatarExpression.happy;
    case AvatarState.confused:
      return AvatarExpression.confused;
    case AvatarState.error:
      return AvatarExpression.error;
    case AvatarState.actionPerforming:
      return AvatarExpression.attentive;
  }
}

/// Animated chili mascot avatar with state-specific animations.
/// Uses a CustomPainter-drawn chili pepper for guaranteed transparency.
/// When [reduceMotion] is true, animations are disabled.
class ChiliAvatar extends StatefulWidget {
  const ChiliAvatar({super.key, required this.state, this.reduceMotion = false});

  final AvatarState state;
  final bool reduceMotion;

  @override
  State<ChiliAvatar> createState() => _ChiliAvatarState();
}

enum _AuraType { none, glow, flames }

class _ChiliAvatarState extends State<ChiliAvatar>
    with TickerProviderStateMixin {
  late AnimationController _bobController;
  late AnimationController _pulseController;
  late AnimationController _ringController;
  late AnimationController _dotsController;
  late AnimationController _breathController;
  late AnimationController _mouthController;

  DateTime? _idleSleepyUntil;
  Offset? _idlePupilOffset;
  Timer? _idleMicroTimer;

  void _scheduleIdleMicro() {
    _idleMicroTimer?.cancel();
    if (widget.state != AvatarState.idle || widget.reduceMotion) return;
    final sec = 8 + math.Random().nextInt(5);
    _idleMicroTimer = Timer(Duration(seconds: sec), () {
      if (!mounted || widget.state != AvatarState.idle || widget.reduceMotion) return;
      final r = math.Random().nextInt(3);
      if (r == 0) {
        setState(() => _idleSleepyUntil = DateTime.now().add(const Duration(milliseconds: 220)));
        Future.delayed(const Duration(milliseconds: 250), () {
          if (mounted) setState(() => _idleSleepyUntil = null);
        });
      } else if (r == 1) {
        setState(() => _idleSleepyUntil = DateTime.now().add(const Duration(milliseconds: 1000)));
        Future.delayed(const Duration(milliseconds: 1100), () {
          if (mounted) setState(() => _idleSleepyUntil = null);
        });
      } else {
        final dx = (math.Random().nextDouble() - 0.5) * 12;
        final dy = (math.Random().nextDouble() - 0.5) * 8;
        setState(() => _idlePupilOffset = Offset(dx, dy));
        Future.delayed(const Duration(seconds: 2), () {
          if (mounted) setState(() => _idlePupilOffset = null);
        });
      }
      _scheduleIdleMicro();
    });
  }

  @override
  void initState() {
    super.initState();
    _bobController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2000),
    );
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1500),
    );
    _ringController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 800),
    );
    _dotsController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2000),
    );
    _breathController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    );
    _mouthController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 200),
    );
    if (!widget.reduceMotion) {
      _bobController.repeat(reverse: true);
      _pulseController.repeat(reverse: true);
      _ringController.repeat();
      _dotsController.repeat();
      _breathController.repeat(reverse: true);
      if (widget.state == AvatarState.speaking) {
        _mouthController.repeat(reverse: true);
      }
    } else {
      _bobController.value = 0.5;
      _pulseController.value = 0.5;
      _ringController.value = 1.0;
      _dotsController.value = 0.0;
      _breathController.value = 0.5;
      _mouthController.value = 0.0;
    }
    if (widget.state == AvatarState.idle && !widget.reduceMotion) {
      _scheduleIdleMicro();
    }
  }

  @override
  void didUpdateWidget(ChiliAvatar oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.reduceMotion != widget.reduceMotion) {
      if (widget.reduceMotion) {
        _bobController.stop();
        _pulseController.stop();
        _ringController.stop();
        _dotsController.stop();
        _breathController.stop();
        _mouthController.stop();
        _bobController.value = 0.5;
        _pulseController.value = 0.5;
        _ringController.value = 1.0;
        _dotsController.value = 0.0;
        _breathController.value = 0.5;
        _mouthController.value = 0.0;
      } else {
        _bobController.repeat(reverse: true);
        _pulseController.repeat(reverse: true);
        _ringController.repeat();
        _dotsController.repeat();
        _breathController.repeat(reverse: true);
      }
    }
    if (oldWidget.state != widget.state) {
      if (widget.state == AvatarState.speaking && !widget.reduceMotion) {
        _mouthController.repeat(reverse: true);
      } else {
        _mouthController.stop();
        _mouthController.value = 0.0;
      }
      if (widget.state != AvatarState.idle) {
        _idleMicroTimer?.cancel();
        _idleMicroTimer = null;
        if (_idleSleepyUntil != null || _idlePupilOffset != null) {
          setState(() {
            _idleSleepyUntil = null;
            _idlePupilOffset = null;
          });
        }
      } else if (!widget.reduceMotion) {
        _scheduleIdleMicro();
      }
    }
  }

  @override
  void dispose() {
    _idleMicroTimer?.cancel();
    _bobController.dispose();
    _pulseController.dispose();
    _ringController.dispose();
    _dotsController.dispose();
    _breathController.dispose();
    _mouthController.dispose();
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
          _mouthController,
        ]),
        builder: (context, child) {
          final noMotion = widget.reduceMotion;
          final baseExpression = expressionForState(widget.state);
          final expression = (_idleSleepyUntil != null && DateTime.now().isBefore(_idleSleepyUntil!))
              ? AvatarExpression.sleepy
              : baseExpression;
          final mouthOpen = widget.state == AvatarState.speaking && !noMotion
              ? _mouthController.value
              : 0.0;
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
          final ringValue = noMotion ? 1.0 : ring.value;

          switch (widget.state) {
            case AvatarState.idle:
              translateY = noMotion ? 0 : bob.value;
              scale = noMotion ? 1.0 : pulse.value;
              glowOpacity = noMotion ? 0.2 : (0.15 + 0.1 * _pulseController.value);
              break;
            case AvatarState.listening:
            case AvatarState.wakeDetected:
              translateY = noMotion ? 0 : bob.value * 0.5;
              scale = widget.state == AvatarState.wakeDetected && !noMotion ? 1.05 : 1.0;
              showRing = true;
              glowOpacity = widget.state == AvatarState.wakeDetected ? 0.4 : 0.25;
              break;
            case AvatarState.thinking:
            case AvatarState.reading:
              translateY = 0;
              scale = 1.0;
              showDots = !noMotion;
              glowOpacity = 0.2;
              break;
            case AvatarState.speaking:
              translateY = 0;
              scale = noMotion ? 1.0 : breath.value;
              glowOpacity = 0.2;
              break;
            case AvatarState.muted:
              translateY = 0;
              scale = 1.0;
              glowOpacity = 0.1;
              break;
            case AvatarState.happy:
              translateY = noMotion ? 0 : bob.value * 0.3;
              scale = noMotion ? 1.0 : pulse.value;
              glowOpacity = 0.25;
              break;
            case AvatarState.confused:
              translateY = 0;
              scale = 1.0;
              glowOpacity = 0.2;
              break;
            case AvatarState.error:
              translateY = 0;
              scale = 1.0;
              glowOpacity = 0.3;
              break;
            case AvatarState.actionPerforming:
              translateY = 0;
              scale = 1.0;
              showRing = true;
              glowOpacity = 0.22;
              break;
          }

          Color tintColor = const Color(0xFFEF5350);
          if (widget.state == AvatarState.listening || widget.state == AvatarState.wakeDetected) {
            tintColor = const Color(0xFF4CAF50);
          } else if (widget.state == AvatarState.thinking || widget.state == AvatarState.reading) {
            tintColor = const Color(0xFFFFC107);
          } else if (widget.state == AvatarState.speaking) {
            tintColor = const Color(0xFF2196F3);
          } else if (widget.state == AvatarState.muted) {
            tintColor = const Color(0xFF9E9E9E);
          } else if (widget.state == AvatarState.happy) {
            tintColor = const Color(0xFFFF9800);
          } else if (widget.state == AvatarState.confused) {
            tintColor = const Color(0xFFFFC107);
          } else if (widget.state == AvatarState.error) {
            tintColor = const Color(0xFFD32F2F);
          } else if (widget.state == AvatarState.actionPerforming) {
            tintColor = const Color(0xFF5C6BC0);
          }

          _AuraType auraType = _AuraType.glow;
          if (widget.state == AvatarState.muted) {
            auraType = _AuraType.none;
          } else if (widget.state == AvatarState.error) {
            auraType = _AuraType.flames;
          }

          return Stack(
            alignment: Alignment.center,
            clipBehavior: Clip.none,
            children: [
              if (auraType == _AuraType.flames)
                CustomPaint(
                  size: const Size(160, 160),
                  painter: FlameAuraPainter(color: tintColor),
                ),
              if (glowOpacity > 0 && auraType != _AuraType.none)
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
                  width: 120 * ringValue,
                  height: 120 * ringValue,
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
                    painter: ChiliMascotPainter(
                      expression: expression,
                      mouthOpen: mouthOpen,
                      tintColor: widget.state != AvatarState.idle
                          ? tintColor
                          : null,
                      pupilOffset: _idlePupilOffset,
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
        color: ChiliColors.primaryRed,
      ),
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
