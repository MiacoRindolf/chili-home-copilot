#include "flutter_window.h"

#include <algorithm>
#include <optional>
#include <string>

#include "flutter/generated_plugin_registrant.h"

namespace {

constexpr wchar_t kFrameClass[] = L"ChiliGameFrameBar";
constexpr int kBarHeight = 34;

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

// Window proc for the CHILI frame bar. The whole bar acts as a drag handle
// (HTCAPTION); as it moves we pin the game (stored in USERDATA) just below it
// via SetWindowPos — a plain window move, never a reparent.
LRESULT CALLBACK FrameBarProc(HWND h, UINT m, WPARAM w, LPARAM l) {
  switch (m) {
    case WM_NCHITTEST:
      return HTCAPTION;  // drag anywhere on the bar
    case WM_MOVE: {
      HWND game = reinterpret_cast<HWND>(::GetWindowLongPtrW(h, GWLP_USERDATA));
      if (game && ::IsWindow(game)) {
        RECT br, gr;
        ::GetWindowRect(h, &br);
        ::GetWindowRect(game, &gr);
        int gw = gr.right - gr.left;
        int gh = gr.bottom - gr.top;
        ::SetWindowPos(game, nullptr, br.left, br.bottom, gw, gh,
                       SWP_NOZORDER | SWP_NOACTIVATE);
      }
      return 0;
    }
    case WM_PAINT: {
      PAINTSTRUCT ps;
      HDC hdc = ::BeginPaint(h, &ps);
      RECT rc;
      ::GetClientRect(h, &rc);
      HBRUSH bg = ::CreateSolidBrush(RGB(18, 22, 27));
      ::FillRect(hdc, &rc, bg);
      ::DeleteObject(bg);
      ::SetBkMode(hdc, TRANSPARENT);
      ::SetTextColor(hdc, RGB(120, 230, 170));
      RECT tr = rc;
      tr.left += 12;
      ::DrawTextW(hdc, L"▣  CHILI — drag to move the game", -1, &tr,
                  DT_SINGLELINE | DT_VCENTER | DT_LEFT);
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
  wc.hCursor = ::LoadCursor(nullptr, IDC_SIZEALL);
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
  if (frame_bar_ && ::IsWindow(frame_bar_)) {
    ::DestroyWindow(frame_bar_);
  }
  frame_bar_ = nullptr;
  framed_game_ = nullptr;
}

bool FlutterWindow::StartFrame(const std::wstring& title) {
  HWND game = FindGameWindow(title, GetHandle());
  if (!game) return false;
  StopFrame();
  EnsureFrameClass();
  RECT gr;
  ::GetWindowRect(game, &gr);
  int x = gr.left;
  int y = gr.top - kBarHeight;
  if (y < 0) y = 0;
  int w = gr.right - gr.left;
  frame_bar_ = ::CreateWindowExW(
      WS_EX_TOPMOST | WS_EX_TOOLWINDOW, kFrameClass, L"CHILI", WS_POPUP, x, y,
      w, kBarHeight, nullptr, nullptr, ::GetModuleHandleW(nullptr), nullptr);
  if (!frame_bar_) return false;
  ::SetWindowLongPtrW(frame_bar_, GWLP_USERDATA,
                      reinterpret_cast<LONG_PTR>(game));
  framed_game_ = game;
  ::ShowWindow(frame_bar_, SW_SHOWNOACTIVATE);
  // Force it visible and above the game even if the game grabbed foreground.
  ::SetWindowPos(frame_bar_, HWND_TOPMOST, x, y, w, kBarHeight,
                 SWP_SHOWWINDOW | SWP_NOACTIVATE);
  ::UpdateWindow(frame_bar_);
  return true;
}

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
          bool ok = StartFrame(Utf8ToWide(title));
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
