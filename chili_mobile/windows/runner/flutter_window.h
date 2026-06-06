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

  // Called by the CHILI frame bar's own close button (GAME-6).
  void DismissFrame();

  // Called by the search overlay's edit box when the user submits a query
  // (GAME-11); routes the query to Dart for an RS3 GE price lookup.
  void OnSearchSubmit(const std::wstring& text);

  // Keep the search overlay pinned to the game's top-left as it moves (GAME-11).
  void RepositionSearchOverlay();

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
  HWND frame_bar_ = nullptr;       // the CHILI chrome window
  HWND framed_game_ = nullptr;     // the game it controls
  LONG_PTR framed_orig_style_ = 0; // game's style before we made it borderless
  HWND search_overlay_ = nullptr;  // GAME-11 item-price search overlay
  HWND search_edit_ = nullptr;     // its text box

  void SetupFrameChannel();
  bool StartFrame(const std::wstring& title, const std::wstring& name);
  bool FrameWindow(HWND game, const std::wstring& name);
  void StopFrame();
  void CreateSearchOverlay(HWND game, int gx, int gy);
  void DestroySearchOverlay();
};

#endif  // RUNNER_FLUTTER_WINDOW_H_
