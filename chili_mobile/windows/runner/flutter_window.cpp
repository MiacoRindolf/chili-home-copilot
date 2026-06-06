#include "flutter_window.h"

#include <windowsx.h>

#include <algorithm>
#include <optional>
#include <string>

#include "flutter/generated_plugin_registrant.h"

namespace {

constexpr wchar_t kFrameClass[] = L"ChiliGameFrameBar";
constexpr int kTitleH = 32;  // CHILI title bar height
constexpr int kBorder = 2;   // side / bottom border thickness
constexpr int kGrip = 16;    // bottom-right resize grip

// The game name shown on the CHILI title bar (one frame at a time).
std::wstring g_frame_name;

// The window that owns the active frame, so the bar's close button can ask it
// to tear the frame down (one frame at a time).
FlutterWindow* g_frame_owner = nullptr;

std::wstring ToLower(std::wstring s) {
  std::transform(s.begin(), s.end(), s.begin(),
                 [](wchar_t c) { return static_cast<wchar_t>(::towlower(c)); });
  return s;
}

std::wstring Utf8ToWide(const std::string& utf8) {
  if (utf8.empty()) return std::wstring();
  int len = ::MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(),
                                  static_cast<int>(utf8.size()), nullptr, 0);
  std::wstring out(len, L'\0');
  ::MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(),
                        static_cast<int>(utf8.size()), &out[0], len);
  return out;
}

struct FindContext {
  std::wstring needle;
  HWND host = nullptr;
  HWND found = nullptr;
};

BOOL CALLBACK FindWindowByTitle(HWND hwnd, LPARAM lparam) {
  auto* ctx = reinterpret_cast<FindContext*>(lparam);
  if (hwnd == ctx->host) return TRUE;
  if (!::IsWindowVisible(hwnd)) return TRUE;
  LONG_PTR ex = ::GetWindowLongPtr(hwnd, GWL_EXSTYLE);
  if (ex & WS_EX_TOOLWINDOW) return TRUE;
  int len = ::GetWindowTextLengthW(hwnd);
  if (len <= 0) return TRUE;
  std::wstring title(len + 1, L'\0');
  ::GetWindowTextW(hwnd, &title[0], len + 1);
  title.resize(len);
  if (ToLower(title).find(ctx->needle) != std::wstring::npos) {
    ctx->found = hwnd;
    return FALSE;
  }
  return TRUE;
}

HWND FindGameWindow(const std::wstring& title, HWND host) {
  FindContext ctx;
  ctx.needle = ToLower(title);
  ctx.host = host;
  if (ctx.needle.empty()) return nullptr;
  ::EnumWindows(FindWindowByTitle, reinterpret_cast<LPARAM>(&ctx));
  return ctx.found;
}

std::string GetString(const flutter::EncodableMap& m, const char* key) {
  auto it = m.find(flutter::EncodableValue(std::string(key)));
  if (it == m.end()) return std::string();
  if (auto p = std::get_if<std::string>(&it->second)) return *p;
  return std::string();
}

int64_t GetInt64(const flutter::EncodableMap& m, const char* key) {
  auto it = m.find(flutter::EncodableValue(std::string(key)));
  if (it == m.end()) return 0;
  if (auto p = std::get_if<int64_t>(&it->second)) return *p;
  if (auto p = std::get_if<int>(&it->second)) return *p;
  return 0;
}

std::string WideToUtf8(const std::wstring& w) {
  if (w.empty()) return std::string();
  int len = ::WideCharToMultiByte(CP_UTF8, 0, w.c_str(),
                                  static_cast<int>(w.size()), nullptr, 0,
                                  nullptr, nullptr);
  std::string out(len, '\0');
  ::WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()),
                        &out[0], len, nullptr, nullptr);
  return out;
}

struct WinList {
  HWND host = nullptr;
  flutter::EncodableList items;
};

BOOL CALLBACK CollectWindows(HWND h, LPARAM l) {
  auto* wl = reinterpret_cast<WinList*>(l);
  if (h == wl->host) return TRUE;
  if (!::IsWindowVisible(h)) return TRUE;
  LONG_PTR ex = ::GetWindowLongPtr(h, GWL_EXSTYLE);
  if (ex & WS_EX_TOOLWINDOW) return TRUE;     // overlays/tooltips
  if (::GetWindow(h, GW_OWNER) != nullptr) return TRUE;  // owned dialogs
  int len = ::GetWindowTextLengthW(h);
  if (len <= 0) return TRUE;
  std::wstring t(len + 1, L'\0');
  ::GetWindowTextW(h, &t[0], len + 1);
  t.resize(len);
  flutter::EncodableMap item;
  item[flutter::EncodableValue("hwnd")] = flutter::EncodableValue(
      static_cast<int64_t>(reinterpret_cast<intptr_t>(h)));
  item[flutter::EncodableValue("title")] =
      flutter::EncodableValue(WideToUtf8(t));
  wl->items.push_back(flutter::EncodableValue(item));
  return TRUE;
}

// Read-only list of visible top-level app windows (for the frame picker).
flutter::EncodableList EnumerateAppWindows(HWND host) {
  WinList wl;
  wl.host = host;
  ::EnumWindows(CollectWindows, reinterpret_cast<LPARAM>(&wl));
  return wl.items;
}

// Pin the framed game (HWND in the frame's USERDATA) into the frame's client
// area — below the CHILI title bar, inside the borders — and keep it just
// above the frame in z-order. Plain window move/size, never a reparent.
void FitGameToFrame(HWND frame) {
  HWND game = reinterpret_cast<HWND>(::GetWindowLongPtrW(frame, GWLP_USERDATA));
  if (!game || !::IsWindow(game)) return;
  RECT fr;
  ::GetWindowRect(frame, &fr);
  int gx = fr.left + kBorder;
  int gy = fr.top + kTitleH;
  int gw = (fr.right - fr.left) - 2 * kBorder;
  int gh = (fr.bottom - fr.top) - kTitleH - kBorder;
  if (gw < 50) gw = 50;
  if (gh < 50) gh = 50;
  ::SetWindowPos(game, frame, gx, gy, gw, gh, SWP_NOACTIVATE);
}

// Make the frame a hollow "picture frame": only the title bar + borders are
// part of the window; the middle is a hole the game shows through. This means
// the frame can never cover the game, whatever the z-order, and clicks in the
// middle go straight to the game.
void ApplyFrameRegion(HWND frame) {
  RECT wr;
  ::GetWindowRect(frame, &wr);
  int w = wr.right - wr.left;
  int h = wr.bottom - wr.top;
  if (w <= 2 * kBorder || h <= kTitleH + kBorder) return;
  HRGN outer = ::CreateRectRgn(0, 0, w, h);
  HRGN hole =
      ::CreateRectRgn(kBorder, kTitleH, w - kBorder, h - kBorder);
  ::CombineRgn(outer, outer, hole, RGN_DIFF);
  ::DeleteObject(hole);
  ::SetWindowRgn(frame, outer, TRUE);  // window takes ownership of outer
}

// Window proc for the CHILI frame chrome: the title bar drags the whole thing
// (HTCAPTION) and the edges / corner resize it (HTRIGHT/HTBOTTOM/...). As the
// frame moves or resizes, the game is fitted to its client area.
LRESULT CALLBACK FrameBarProc(HWND h, UINT m, WPARAM w, LPARAM l) {
  switch (m) {
    case WM_NCHITTEST: {
      RECT wr;
      ::GetWindowRect(h, &wr);
      int x = GET_X_LPARAM(l) - wr.left;
      int y = GET_Y_LPARAM(l) - wr.top;
      int ww = wr.right - wr.left;
      int wh = wr.bottom - wr.top;
      // Close button (top-right square) must receive clicks, so HTCLIENT.
      if (y < kTitleH && x >= ww - kTitleH) return HTCLIENT;
      if (x >= ww - kGrip && y >= wh - kGrip) return HTBOTTOMRIGHT;
      if (x <= kBorder) return HTLEFT;
      if (x >= ww - kBorder) return HTRIGHT;
      if (y >= wh - kBorder) return HTBOTTOM;
      if (y < kTitleH) return HTCAPTION;  // title bar = drag
      return HTCLIENT;
    }
    case WM_LBUTTONDOWN: {
      RECT rc;
      ::GetClientRect(h, &rc);
      int x = GET_X_LPARAM(l);
      int y = GET_Y_LPARAM(l);
      if (y < kTitleH && x >= rc.right - kTitleH) {
        if (g_frame_owner) g_frame_owner->DismissFrame();
        return 0;
      }
      return 0;
    }
    case WM_MOVE:
      FitGameToFrame(h);
      return 0;
    case WM_SIZE:
      ApplyFrameRegion(h);  // keep the hollow shape in sync
      FitGameToFrame(h);
      return 0;
    case WM_GETMINMAXINFO: {
      auto* mmi = reinterpret_cast<MINMAXINFO*>(l);
      mmi->ptMinTrackSize.x = 220;
      mmi->ptMinTrackSize.y = kTitleH + 120;
      return 0;
    }
    case WM_PAINT: {
      PAINTSTRUCT ps;
      HDC hdc = ::BeginPaint(h, &ps);
      RECT rc;
      ::GetClientRect(h, &rc);
      HBRUSH bg = ::CreateSolidBrush(RGB(24, 28, 34));
      ::FillRect(hdc, &rc, bg);
      ::DeleteObject(bg);
      // CHILI accent square (drawn, not a glyph — avoids encoding issues).
      RECT acc = {12, (kTitleH - 12) / 2, 24, (kTitleH - 12) / 2 + 12};
      HBRUSH ab = ::CreateSolidBrush(RGB(46, 158, 91));
      ::FillRect(hdc, &acc, ab);
      ::DeleteObject(ab);
      ::SetBkMode(hdc, TRANSPARENT);
      ::SetTextColor(hdc, RGB(236, 239, 241));
      RECT tr = {34, 0, rc.right - kTitleH - 8, kTitleH};
      std::wstring label = L"CHILI  -  " + g_frame_name;
      ::DrawTextW(hdc, label.c_str(), -1, &tr,
                  DT_SINGLELINE | DT_VCENTER | DT_LEFT | DT_END_ELLIPSIS);
      // Close (X) button in the top-right square.
      int cx0 = rc.right - kTitleH;
      HPEN pen = ::CreatePen(PS_SOLID, 1, RGB(205, 210, 214));
      HGDIOBJ oldp = ::SelectObject(hdc, pen);
      int pad = 11;
      ::MoveToEx(hdc, cx0 + pad, pad, nullptr);
      ::LineTo(hdc, rc.right - pad, kTitleH - pad);
      ::MoveToEx(hdc, rc.right - pad, pad, nullptr);
      ::LineTo(hdc, cx0 + pad, kTitleH - pad);
      ::SelectObject(hdc, oldp);
      ::DeleteObject(pen);
      ::EndPaint(h, &ps);
      return 0;
    }
  }
  return ::DefWindowProcW(h, m, w, l);
}

void EnsureFrameClass() {
  static bool registered = false;
  if (registered) return;
  WNDCLASSW wc = {};
  wc.lpfnWndProc = FrameBarProc;
  wc.hInstance = ::GetModuleHandleW(nullptr);
  wc.lpszClassName = kFrameClass;
  wc.hCursor = ::LoadCursor(nullptr, IDC_ARROW);
  wc.style = CS_HREDRAW | CS_VREDRAW;
  ::RegisterClassW(&wc);
  registered = true;
}

}  // namespace

FlutterWindow::FlutterWindow(const flutter::DartProject& project)
    : project_(project) {}

FlutterWindow::~FlutterWindow() {}

bool FlutterWindow::OnCreate() {
  if (!Win32Window::OnCreate()) {
    return false;
  }

  RECT frame = GetClientArea();

  // The size here must match the window dimensions to avoid unnecessary surface
  // creation / destruction in the startup path.
  flutter_controller_ = std::make_unique<flutter::FlutterViewController>(
      frame.right - frame.left, frame.bottom - frame.top, project_);
  // Ensure that basic setup of the controller was successful.
  if (!flutter_controller_->engine() || !flutter_controller_->view()) {
    return false;
  }
  RegisterPlugins(flutter_controller_->engine());
  SetupFrameChannel();
  SetChildContent(flutter_controller_->view()->GetNativeWindow());

  flutter_controller_->engine()->SetNextFrameCallback([&]() {
    this->Show();
  });

  // Flutter can complete the first frame before the "show window" callback is
  // registered. The following call ensures a frame is pending to ensure the
  // window is shown. It is a no-op if the first frame hasn't completed yet.
  flutter_controller_->ForceRedraw();

  return true;
}

void FlutterWindow::OnDestroy() {
  StopFrame();
  if (flutter_controller_) {
    flutter_controller_ = nullptr;
  }

  Win32Window::OnDestroy();
}

void FlutterWindow::StopFrame() {
  // Give the game back its own title bar / border.
  if (framed_game_ && ::IsWindow(framed_game_) && framed_orig_style_) {
    ::SetWindowLongPtrW(framed_game_, GWL_STYLE, framed_orig_style_);
    ::SetWindowPos(framed_game_, nullptr, 0, 0, 0, 0,
                   SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED);
  }
  if (frame_bar_ && ::IsWindow(frame_bar_)) {
    ::DestroyWindow(frame_bar_);
  }
  frame_bar_ = nullptr;
  framed_game_ = nullptr;
  framed_orig_style_ = 0;
  g_frame_owner = nullptr;
}

bool FlutterWindow::StartFrame(const std::wstring& title,
                              const std::wstring& name) {
  HWND game = FindGameWindow(title, GetHandle());
  if (!game) return false;
  return FrameWindow(game, name);
}

bool FlutterWindow::FrameWindow(HWND game, const std::wstring& name) {
  if (!game || !::IsWindow(game) || game == GetHandle()) return false;
  StopFrame();
  EnsureFrameClass();
  g_frame_name = name.empty() ? L"Game" : name;

  RECT r0;
  ::GetWindowRect(game, &r0);
  int gw = r0.right - r0.left;
  int gh = r0.bottom - r0.top;

  // Make the game borderless so only the CHILI frame shows. Reversible — the
  // original style is restored on Stop. (Style change only; no reparenting.)
  framed_orig_style_ = ::GetWindowLongPtrW(game, GWL_STYLE);
  LONG_PTR ns = framed_orig_style_;
  ns &= ~(WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX |
          WS_SYSMENU | WS_BORDER | WS_DLGFRAME);
  ns |= WS_VISIBLE;
  ::SetWindowLongPtrW(game, GWL_STYLE, ns);
  ::SetWindowPos(game, nullptr, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED);

  int fw = gw + 2 * kBorder;
  int fh = gh + kTitleH + kBorder;
  int fx = r0.left - kBorder;
  int fy = r0.top - kTitleH;
  if (fy < 0) fy = 0;
  frame_bar_ = ::CreateWindowExW(WS_EX_TOOLWINDOW, kFrameClass, L"CHILI Frame",
                                 WS_POPUP | WS_VISIBLE, fx, fy, fw, fh, nullptr,
                                 nullptr, ::GetModuleHandleW(nullptr), nullptr);
  if (!frame_bar_) {
    // Restore the game's chrome on failure.
    ::SetWindowLongPtrW(game, GWL_STYLE, framed_orig_style_);
    ::SetWindowPos(game, nullptr, 0, 0, 0, 0,
                   SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED);
    framed_orig_style_ = 0;
    return false;
  }
  ::SetWindowLongPtrW(frame_bar_, GWLP_USERDATA,
                      reinterpret_cast<LONG_PTR>(game));
  framed_game_ = game;
  g_frame_owner = this;
  ApplyFrameRegion(frame_bar_);  // hollow picture-frame shape
  ::ShowWindow(frame_bar_, SW_SHOWNOACTIVATE);
  ::UpdateWindow(frame_bar_);
  // Bring the frame + game in front of the CHILI shell so they're actually
  // visible (the CHILI window otherwise covers the game after the picker).
  ::SetWindowPos(frame_bar_, HWND_TOP, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
  FitGameToFrame(frame_bar_);  // place the game in the hole, above the frame
  ::SetForegroundWindow(game);
  return true;
}

void FlutterWindow::DismissFrame() { StopFrame(); }

void FlutterWindow::SetupFrameChannel() {
  frame_channel_ =
      std::make_unique<flutter::MethodChannel<flutter::EncodableValue>>(
          flutter_controller_->engine()->messenger(), "chili/game_frame",
          &flutter::StandardMethodCodec::GetInstance());

  frame_channel_->SetMethodCallHandler(
      [this](const flutter::MethodCall<flutter::EncodableValue>& call,
             std::unique_ptr<flutter::MethodResult<flutter::EncodableValue>>
                 result) {
        const auto* args =
            std::get_if<flutter::EncodableMap>(call.arguments());
        if (call.method_name() == "start") {
          std::string title = args ? GetString(*args, "title") : std::string();
          std::string name = args ? GetString(*args, "name") : std::string();
          if (name.empty()) name = title;
          bool ok = StartFrame(Utf8ToWide(title), Utf8ToWide(name));
          result->Success(flutter::EncodableValue(ok));
          return;
        }
        if (call.method_name() == "listWindows") {
          result->Success(
              flutter::EncodableValue(EnumerateAppWindows(GetHandle())));
          return;
        }
        if (call.method_name() == "startByHandle") {
          int64_t raw = args ? GetInt64(*args, "hwnd") : 0;
          std::string name = args ? GetString(*args, "name") : std::string();
          HWND game = reinterpret_cast<HWND>(static_cast<intptr_t>(raw));
          bool ok = FrameWindow(game, Utf8ToWide(name));
          result->Success(flutter::EncodableValue(ok));
          return;
        }
        if (call.method_name() == "stop") {
          StopFrame();
          result->Success(flutter::EncodableValue(true));
          return;
        }
        result->NotImplemented();
      });
}

LRESULT
FlutterWindow::MessageHandler(HWND hwnd, UINT const message,
                              WPARAM const wparam,
                              LPARAM const lparam) noexcept {
  // Give Flutter, including plugins, an opportunity to handle window messages.
  if (flutter_controller_) {
    std::optional<LRESULT> result =
        flutter_controller_->HandleTopLevelWindowProc(hwnd, message, wparam,
                                                      lparam);
    if (result) {
      return *result;
    }
  }

  switch (message) {
    case WM_FONTCHANGE:
      flutter_controller_->engine()->ReloadSystemFonts();
      break;
  }

  return Win32Window::MessageHandler(hwnd, message, wparam, lparam);
}
