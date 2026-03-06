import 'package:flutter/services.dart';

import 'package:audioplayers/audioplayers.dart';

import '../config/app_config.dart';

/// Optional sound/haptic feedback for wake word, reply arrival, and button presses.
/// When [AppConfig.soundEffects] is true, plays haptics; if sound assets exist,
/// plays them (add assets/sounds/pop.mp3, chime.mp3, click.mp3 and register in pubspec).
class SoundEffects {
  SoundEffects._();

  static final AudioPlayer _player = AudioPlayer();

  static bool get _enabled =>
      AppConfig.instance.isLoaded && AppConfig.instance.soundEffects;

  /// Call when wake word is detected (soft "pop" or haptic).
  static Future<void> playWakeDetected() async {
    if (!_enabled) return;
    try {
      await _player.play(AssetSource('sounds/pop.mp3'));
    } catch (_) {
      await HapticFeedback.mediumImpact();
    }
  }

  /// Call when a reply arrives (gentle chime or haptic).
  static Future<void> playReplyArrived() async {
    if (!_enabled) return;
    try {
      await _player.play(AssetSource('sounds/chime.mp3'));
    } catch (_) {
      await HapticFeedback.lightImpact();
    }
  }

  /// Call on button press (click or haptic).
  static void playButtonClick() {
    if (!_enabled) return;
    HapticFeedback.selectionClick();
  }
}
