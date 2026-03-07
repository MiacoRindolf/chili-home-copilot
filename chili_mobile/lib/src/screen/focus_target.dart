import 'dart:ui';

/// The kind of screen area the user wants Chili to focus on.
enum FocusMode { fullScreen, region, window }

/// Describes what part of the screen Chili should capture when the user sends a
/// message in Focus Mode.
class FocusTarget {
  const FocusTarget.fullScreen()
      : mode = FocusMode.fullScreen,
        region = null,
        windowTitle = null;

  const FocusTarget.region(this.region)
      : mode = FocusMode.region,
        windowTitle = null;

  const FocusTarget.window(this.windowTitle)
      : mode = FocusMode.window,
        region = null;

  final FocusMode mode;
  final Rect? region;
  final String? windowTitle;

  String get label {
    switch (mode) {
      case FocusMode.fullScreen:
        return 'Full Screen';
      case FocusMode.region:
        return 'Selected Region';
      case FocusMode.window:
        return windowTitle ?? 'Window';
    }
  }
}
