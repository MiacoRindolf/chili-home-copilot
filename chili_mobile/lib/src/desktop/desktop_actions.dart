import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:url_launcher/url_launcher.dart';

/// Executes client_action dicts returned by the CHILI backend.
///
/// Only runs on desktop platforms (Windows/macOS/Linux). On other platforms
/// or on web, actions are silently skipped.
class DesktopActions {
  DesktopActions._();

  static bool get _isDesktop =>
      !kIsWeb && (Platform.isWindows || Platform.isMacOS || Platform.isLinux);

  /// Dispatch a backend client_action. Returns a short result message for the UI, or null if nothing was run.
  static Future<String?> execute(Map<String, dynamic>? action) async {
    if (action == null || !_isDesktop) return null;

    final type = action['type'] as String? ?? '';
    switch (type) {
      case 'open_app':
        return _openApp(action['app_name'] as String? ?? '');
      case 'close_app':
        return _closeApp(action['app_name'] as String? ?? '');
      case 'open_url':
        return _openUrl(action['url'] as String? ?? '');
      default:
        debugPrint('[DesktopActions] Unknown action type: $type');
        return null;
    }
  }

  // ── App name → executable mapping (Windows) ───────────────────────────

  static const _appAliases = <String, String>{
    'notepad': 'notepad',
    'calculator': 'calc',
    'calc': 'calc',
    'paint': 'mspaint',
    'explorer': 'explorer',
    'file explorer': 'explorer',
    'files': 'explorer',
    'cmd': 'cmd',
    'command prompt': 'cmd',
    'terminal': 'wt',
    'windows terminal': 'wt',
    'powershell': 'powershell',
    'task manager': 'taskmgr',
    'snipping tool': 'snippingtool',
    'control panel': 'control',
    'settings': 'ms-settings:',
    'chrome': 'chrome',
    'google chrome': 'chrome',
    'firefox': 'firefox',
    'edge': 'msedge',
    'microsoft edge': 'msedge',
    'spotify': 'spotify',
    'discord': 'discord',
    'slack': 'slack',
    'teams': 'msteams',
    'microsoft teams': 'msteams',
    'code': 'code',
    'vs code': 'code',
    'visual studio code': 'code',
    'word': 'winword',
    'microsoft word': 'winword',
    'excel': 'excel',
    'microsoft excel': 'excel',
    'powerpoint': 'powerpnt',
    'outlook': 'outlook',
    'steam': 'steam',
    'vlc': 'vlc',
    'obs': 'obs64',
    'obs studio': 'obs64',
    'zoom': 'zoom',
  };

  static const _processNames = <String, String>{
    'notepad': 'notepad.exe',
    'calculator': 'CalculatorApp.exe',
    'calc': 'CalculatorApp.exe',
    'paint': 'mspaint.exe',
    'explorer': 'explorer.exe',
    'file explorer': 'explorer.exe',
    'chrome': 'chrome.exe',
    'google chrome': 'chrome.exe',
    'firefox': 'firefox.exe',
    'edge': 'msedge.exe',
    'microsoft edge': 'msedge.exe',
    'spotify': 'Spotify.exe',
    'discord': 'Discord.exe',
    'slack': 'slack.exe',
    'teams': 'ms-teams.exe',
    'microsoft teams': 'ms-teams.exe',
    'code': 'Code.exe',
    'vs code': 'Code.exe',
    'visual studio code': 'Code.exe',
    'word': 'WINWORD.EXE',
    'microsoft word': 'WINWORD.EXE',
    'excel': 'EXCEL.EXE',
    'microsoft excel': 'EXCEL.EXE',
    'powerpoint': 'POWERPNT.EXE',
    'outlook': 'OUTLOOK.EXE',
    'steam': 'steam.exe',
    'vlc': 'vlc.exe',
    'obs': 'obs64.exe',
    'obs studio': 'obs64.exe',
    'zoom': 'Zoom.exe',
    'task manager': 'Taskmgr.exe',
    'cmd': 'cmd.exe',
    'command prompt': 'cmd.exe',
    'terminal': 'WindowsTerminal.exe',
    'windows terminal': 'WindowsTerminal.exe',
    'powershell': 'powershell.exe',
  };

  static Future<String?> _openApp(String appName) async {
    if (appName.isEmpty) return null;
    final key = appName.toLowerCase().trim();
    final exe = _appAliases[key] ?? appName;
    debugPrint('[DesktopActions] Opening app: $appName -> $exe');

    try {
      if (Platform.isWindows) {
        await Process.run('cmd', ['/c', 'start', '', exe], runInShell: true);
      } else if (Platform.isMacOS) {
        await Process.run('open', ['-a', exe]);
      } else {
        await Process.run(exe, []);
      }
      return 'Opened $appName';
    } catch (e) {
      debugPrint('[DesktopActions] Failed to open $appName: $e');
      return 'Could not open $appName';
    }
  }

  static Future<String?> _closeApp(String appName) async {
    if (appName.isEmpty) return null;
    final key = appName.toLowerCase().trim();
    final proc = _processNames[key] ?? '$appName.exe';
    debugPrint('[DesktopActions] Closing app: $appName -> $proc');

    try {
      if (Platform.isWindows) {
        await Process.run('taskkill', ['/IM', proc, '/F']);
      } else if (Platform.isMacOS) {
        await Process.run('pkill', ['-f', appName]);
      } else {
        await Process.run('pkill', ['-f', appName]);
      }
      return 'Closed $appName';
    } catch (e) {
      debugPrint('[DesktopActions] Failed to close $appName: $e');
      return 'Could not close $appName';
    }
  }

  static Future<String?> _openUrl(String url) async {
    if (url.isEmpty) return null;
    debugPrint('[DesktopActions] Opening URL: $url');
    try {
      final uri = Uri.parse(url);
      final ok = await launchUrl(uri, mode: LaunchMode.externalApplication);
      return ok ? 'Opened link' : 'Could not open link';
    } catch (e) {
      debugPrint('[DesktopActions] Failed to open URL: $e');
      return 'Could not open link';
    }
  }
}
