using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace Chili.Interop;

/// <summary>A top-level desktop window discovered via Win32 enumeration.</summary>
public sealed record DesktopWindow(
    IntPtr Handle, string Title, string ProcessName,
    int Left, int Top, int Width, int Height)
{
    public string Subtitle => $"{ProcessName}  ·  {Width}×{Height}";
}

/// <summary>
/// Win32 window interop — the "powerful" half of the native client.
///
/// SAFETY BOUNDARY (load-bearing, do not cross):
///   • Reading window geometry (EnumWindows / GetWindowText / GetWindowRect /
///     GetWindowThreadProcessId) is 100% passive and safe.
///   • Moving / resizing another window via <see cref="MoveResize"/> (SetWindowPos)
///     is the same class of operation as FancyZones / AutoHotkey — low risk.
///   • NEVER SetParent/reparent, inject, or read another process's memory. Those
///     are the things anti-cheat reacts to and are explicitly out of scope.
/// </summary>
public static class NativeWindows
{
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    private static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter,
        int X, int Y, int cx, int cy, uint uFlags);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    private const int GWL_EXSTYLE = -20;
    private const long WS_EX_TOOLWINDOW = 0x00000080;
    private const long WS_EX_NOACTIVATE = 0x08000000;

    private static readonly IntPtr HWND_TOP = IntPtr.Zero;
    private const uint SWP_NOZORDER = 0x0004;
    private const uint SWP_NOACTIVATE = 0x0010;

    /// <summary>Enumerate visible, titled, real top-level windows (excludes our own
    /// process, tool windows, and minimized/zero-size windows). Read-only.</summary>
    public static IReadOnlyList<DesktopWindow> ListTopLevelWindows()
    {
        var result = new List<DesktopWindow>();
        int self = Environment.ProcessId;

        EnumWindows((hWnd, _) =>
        {
            if (!IsWindowVisible(hWnd)) return true;

            int len = GetWindowTextLength(hWnd);
            if (len == 0) return true;

            var ex = (long)GetWindowLongPtr(hWnd, GWL_EXSTYLE);
            if ((ex & WS_EX_TOOLWINDOW) != 0) return true;

            if (!GetWindowRect(hWnd, out var r)) return true;
            int w = r.Right - r.Left, h = r.Bottom - r.Top;
            if (w < 120 || h < 80) return true;          // skip tiny/decorative
            if (r.Left <= -30000 || r.Top <= -30000) return true; // minimized

            GetWindowThreadProcessId(hWnd, out uint pid);
            if (pid == self) return true;                 // skip ourselves

            var sb = new StringBuilder(len + 1);
            GetWindowText(hWnd, sb, sb.Capacity);
            string title = sb.ToString();
            if (string.IsNullOrWhiteSpace(title)) return true;

            result.Add(new DesktopWindow(hWnd, title, ProcessName(pid),
                r.Left, r.Top, w, h));
            return true;
        }, IntPtr.Zero);

        return result;
    }

    /// <summary>Find the first visible window whose title contains <paramref name="substring"/>.</summary>
    public static DesktopWindow? FindByTitle(string substring)
    {
        foreach (var w in ListTopLevelWindows())
            if (w.Title.Contains(substring, StringComparison.OrdinalIgnoreCase))
                return w;
        return null;
    }

    /// <summary>Read a window's current bounds (passive). Returns false if it's gone.</summary>
    public static bool TryGetBounds(IntPtr hWnd, out int left, out int top, out int width, out int height)
    {
        left = top = width = height = 0;
        if (!GetWindowRect(hWnd, out var r)) return false;
        left = r.Left; top = r.Top; width = r.Right - r.Left; height = r.Bottom - r.Top;
        return true;
    }

    /// <summary>Move + resize a window (SetWindowPos — FancyZones-class, safe). No
    /// reparenting, no styling. Used by the CHILI frame to drive a picked window.</summary>
    public static bool MoveResize(IntPtr hWnd, int x, int y, int width, int height)
        => SetWindowPos(hWnd, HWND_TOP, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE);

    /// <summary>Bring a window to the foreground.</summary>
    public static void BringToFront(IntPtr hWnd) => SetForegroundWindow(hWnd);

    private static string ProcessName(uint pid)
    {
        try { return Process.GetProcessById((int)pid).ProcessName; }
        catch { return "?"; }
    }
}
