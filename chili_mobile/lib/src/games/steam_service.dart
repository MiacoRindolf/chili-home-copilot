import 'dart:io';

import 'package:url_launcher/url_launcher.dart';

import 'steam_models.dart';

/// Discovers and launches installed Steam games on the local machine (GAME-1).
/// Everything is best-effort and wrapped in try/catch: a missing Steam install
/// or unreadable file yields an empty list, never an exception to the UI.
class SteamService {
  const SteamService();

  /// Locate the Steam install root. Tries the registry (`HKCU\Software\Valve\
  /// Steam\SteamPath`), then common default locations. Returns null if absent.
  Future<String?> steamRoot() async {
    // Registry is the source of truth for a custom install location.
    try {
      final ProcessResult r = await Process.run('reg', <String>[
        'query',
        r'HKCU\Software\Valve\Steam',
        '/v',
        'SteamPath',
      ]);
      final String out = '${r.stdout}';
      final RegExpMatch? m =
          RegExp(r'SteamPath\s+REG_SZ\s+(.+)').firstMatch(out);
      if (m != null) {
        final String path = m.group(1)!.trim().replaceAll('/', r'\');
        if (await Directory(path).exists()) return path;
      }
    } catch (_) {
      // reg missing / non-Windows — fall through to defaults.
    }
    for (final String candidate in <String>[
      r'C:\Program Files (x86)\Steam',
      r'C:\Program Files\Steam',
    ]) {
      if (await Directory(candidate).exists()) return candidate;
    }
    return null;
  }

  /// All installed, playable Steam games across every library folder, with a
  /// local cover image attached when one exists. Sorted by name.
  Future<List<SteamGame>> installedGames() async {
    final String? root = await steamRoot();
    if (root == null) return const <SteamGame>[];

    final List<String> libraries = await _libraryRoots(root);
    final List<SteamGame> games = <SteamGame>[];
    for (final String lib in libraries) {
      final Directory steamapps = Directory('$lib${Platform.pathSeparator}steamapps');
      if (!await steamapps.exists()) continue;
      try {
        await for (final FileSystemEntity e in steamapps.list()) {
          if (e is! File) continue;
          final String fname = e.uri.pathSegments.last;
          if (!fname.startsWith('appmanifest_') || !fname.endsWith('.acf')) {
            continue;
          }
          try {
            final SteamGame? g = parseAppManifest(await e.readAsString());
            if (g != null && isPlayableSteamGame(g)) {
              games.add(g.withCover(await _coverFor(root, g.appId)));
            }
          } catch (_) {
            // skip an unreadable manifest
          }
        }
      } catch (_) {
        // skip an unreadable library
      }
    }
    return dedupeAndSortGames(games);
  }

  /// Library roots: the main install plus every path in libraryfolders.vdf.
  Future<List<String>> _libraryRoots(String steamRoot) async {
    final Set<String> roots = <String>{steamRoot};
    final File vdf = File(
        '$steamRoot${Platform.pathSeparator}steamapps${Platform.pathSeparator}libraryfolders.vdf');
    try {
      if (await vdf.exists()) {
        roots.addAll(parseLibraryFolders(await vdf.readAsString()));
      }
    } catch (_) {
      // ignore — main root alone is still useful
    }
    return roots.toList();
  }

  /// First existing local cover image for [appId], or null. Steam caches these
  /// under appcache/librarycache in a couple of layouts across versions.
  Future<String?> _coverFor(String steamRoot, String appId) async {
    final String cache =
        '$steamRoot${Platform.pathSeparator}appcache${Platform.pathSeparator}librarycache';
    final List<String> candidates = <String>[
      '$cache${Platform.pathSeparator}${appId}_library_600x900.jpg',
      '$cache${Platform.pathSeparator}$appId${Platform.pathSeparator}library_600x900.jpg',
      '$cache${Platform.pathSeparator}${appId}_library_600x900.png',
    ];
    for (final String c in candidates) {
      try {
        if (await File(c).exists()) return c;
      } catch (_) {
        // ignore
      }
    }
    return null;
  }

  /// The app id Steam reports as currently running ('0' when nothing is). This
  /// is CHILI's real-time gaming-awareness signal. GAME-1.
  Future<String> runningAppId() async {
    try {
      final ProcessResult r = await Process.run('reg', <String>[
        'query',
        r'HKCU\Software\Valve\Steam',
        '/v',
        'RunningAppID',
      ]);
      final RegExpMatch? m =
          RegExp(r'RunningAppID\s+REG_DWORD\s+0x([0-9a-fA-F]+)')
              .firstMatch('${r.stdout}');
      if (m != null) return int.parse(m.group(1)!, radix: 16).toString();
    } catch (_) {
      // not running / non-Windows
    }
    return '0';
  }

  /// Launch a game through Steam. Returns true on success.
  Future<bool> launch(SteamGame game) async {
    try {
      return await launchUrl(game.launchUri,
          mode: LaunchMode.externalApplication);
    } catch (_) {
      return false;
    }
  }
}
