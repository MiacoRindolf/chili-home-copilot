import 'package:flutter/material.dart';

class OnboardingOverlay extends StatelessWidget {
  const OnboardingOverlay({
    super.key,
    required this.step,
    required this.onNext,
  });

  final int step;
  final VoidCallback onNext;

  static const _steps = [
    "Say 'Chili' to start a conversation",
    "Drag me to move",
    "Tap to chat, double-tap for full app",
  ];

  @override
  Widget build(BuildContext context) {
    final clampedIndex = step.clamp(0, _steps.length - 1);
    final text = _steps[clampedIndex];
    return Positioned.fill(
      child: Material(
        color: Colors.black54,
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          text,
                          textAlign: TextAlign.center,
                          style: const TextStyle(fontSize: 13, height: 1.4),
                        ),
                        const SizedBox(height: 12),
                        FilledButton(
                          onPressed: onNext,
                          child: const Text('Got it'),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

