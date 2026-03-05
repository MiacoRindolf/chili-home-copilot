import 'package:flutter/material.dart';
import 'package:lottie/lottie.dart';

enum AvatarState { idle, listening, thinking, speaking }

class ChiliAvatar extends StatelessWidget {
  const ChiliAvatar({super.key, required this.state});

  final AvatarState state;

  String get _asset {
    switch (state) {
      case AvatarState.listening:
        return 'assets/animations/chili_listening.json';
      case AvatarState.thinking:
        return 'assets/animations/chili_thinking.json';
      case AvatarState.speaking:
        return 'assets/animations/chili_speaking.json';
      case AvatarState.idle:
      default:
        return 'assets/animations/chili_idle.json';
    }
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 140,
      child: Lottie.asset(
        _asset,
        repeat: true,
        fit: BoxFit.contain,
      ),
    );
  }
}

