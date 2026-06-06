#ifndef RUNNER_FLUTTER_WINDOW_H_
#define RUNNER_FLUTTER_WINDOW_H_

#include <flutter/dart_project.h>
#include <flutter/flutter_view_controller.h>
#include <flutter/method_channel.h>
#include <flutter/standard_method_codec.h>

#include <memory>
#include <string>

#include "win32_window.h"

// A window that does nothing but host a Flutter view.
class FlutterWindow : public Win32Window {
 public:
  // Creates a new FlutterWindow hosting a Flutter view running |project|.
  explicit FlutterWindow(const flutter::DartProject& project);
  virtual ~FlutterWindow();

 protected:
  // Win32Window:
  bool OnCreate() override;
  void OnDestroy() override;
  LRESULT MessageHandler(HWND window, UINT const message, WPARAM const wparam,
                         LPARAM const lparam) noexcept override;

 private:
  // The project to run.
  flutter::DartProject project_;

  // The Flutter instance hosted by this window.
  std::unique_ptr<flutter::FlutterViewController> flutter_controller_;

  // GAME-3 — "CHILI frame" over a running game. A small always-on-top bar that,
  // when dragged, moves the game window to follow (SetWindowPos only — no
  // reparenting, no injection; the same windowing API FancyZones/AutoHotkey
  // use). Opt-in and best-effort.
  std::unique_ptr<flutter::MethodChannel<flutter::EncodableValue>>
      frame_channel_;
  HWND frame_bar_ = nullptr;     // the CHILI bar window
  HWND framed_game_ = nullptr;   // the game it controls

  void SetupFrameChannel();
  bool StartFrame(const std::wstring& title);
  void StopFrame();
};

#endif  // RUNNER_FLUTTER_WINDOW_H_
