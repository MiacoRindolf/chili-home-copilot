import 'dart:convert';
import 'dart:io';
import 'dart:ui';

/// Where a running game's window sits, plus the screen it lives on (GAME-2).
/// Read-only awareness — CHILI never moves or reparents the game window, it
/// only *reads* the geometry (like Task Manager or a screenshot tool would), so
/// there is no anti-cheat exposure.
class GameWindowInfo {
  const GameWindowInfo({required this.window, required this.screen});

  final Rect window; // game window bounds in screen pixels
  final Size screen; // primary screen size in pixels

  /// Window position as a 0..1 fraction within the screen (for a mini-map).
  Rect get normalized {
    final double sw = screen.width <= 0 ? 1 : screen.width;
    final double sh = screen.height <= 0 ? 1 : screen.height;
    return Rect.fromLTWH(
      (window.left / sw).clamp(0.0, 1.0),
      (window.top / sh).clamp(0.0, 1.0),
      (window.width / sw).clamp(0.0, 1.0),
      (window.height / sh).clamp(0.0, 1.0),
    );
  }
}

/// Parse the read-only probe output `"L,T,R,B;SW,SH"` into a [GameWindowInfo].
/// Pure + tolerant → null on any malformed / empty line. GAME-2.
GameWindowInfo? parseWindowProbe(String out) {
  final String line = out.trim();
  if (line.isEmpty) return null;
  final List<String> parts = line.split(';');
  if (parts.length != 2) return null;
  final List<int?> w =
      parts[0].split(',').map((String s) => int.tryParse(s.trim())).toList();
  final List<int?> s =
      parts[1].split(',').map((String s) => int.tryParse(s.trim())).toList();
  if (w.length != 4 || w.contains(null) || s.length != 2 || s.contains(null)) {
    return null;
  }
  final Rect rect = Rect.fromLTRB(
      w[0]!.toDouble(), w[1]!.toDouble(), w[2]!.toDouble(), w[3]!.toDouble());
  if (rect.width <= 0 || rect.height <= 0) return null;
  return GameWindowInfo(
      window: rect, screen: Size(s[0]!.toDouble(), s[1]!.toDouble()));
}

/// Reads a running game window's geometry via a **read-only** PowerShell probe
/// (`GetWindowRect` + `GetSystemMetrics`). No window is moved, hooked, or
/// reparented — purely observational. Returns null when nothing matches or on
/// any error.
class GameAwareness {
  const GameAwareness();

  /// Build the read-only probe script. Sanitises [title] so it can't break out
  /// of the string literal it's injected into.
  static String probeScript(String title) {
    final String safe =
        title.replaceAll('"', '').replaceAll('`', '').replaceAll(r'$', '');
    return '''
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class ChiliWin {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
  public struct RECT { public int L; public int T; public int R; public int B; }
}
"@
\$t = "$safe"
\$p = Get-Process | Where-Object { \$_.MainWindowHandle -ne 0 -and \$_.MainWindowTitle -like "*\$t*" } | Select-Object -First 1
if (\$p) {
  \$r = New-Object ChiliWin+RECT
  [void][ChiliWin]::GetWindowRect(\$p.MainWindowHandle, [ref]\$r)
  \$sw = [ChiliWin]::GetSystemMetrics(0)
  \$sh = [ChiliWin]::GetSystemMetrics(1)
  Write-Output ("{0},{1},{2},{3};{4},{5}" -f \$r.L,\$r.T,\$r.R,\$r.B,\$sw,\$sh)
}
''';
  }

  Future<GameWindowInfo?> probe(String title) async {
    if (title.trim().isEmpty) return null;
    try {
      // -EncodedCommand (UTF-16LE base64) avoids all shell quoting pitfalls.
      final String b64 = base64.encode(_toUtf16Le(probeScript(title)));
      final ProcessResult r = await Process.run('powershell', <String>[
        '-NoProfile',
        '-NonInteractive',
        '-EncodedCommand',
        b64,
      ]);
      return parseWindowProbe('${r.stdout}');
    } catch (_) {
      return null;
    }
  }

  static List<int> _toUtf16Le(String s) {
    final List<int> out = <int>[];
    for (final int code in s.codeUnits) {
      out.add(code & 0xFF);
      out.add((code >> 8) & 0xFF);
    }
    return out;
  }
}
