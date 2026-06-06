// Steam library model + pure parsers (GAME-1). Steam stores its catalog as VDF
// (Valve Data Format) text files: `libraryfolders.vdf` lists library roots and
// each `appmanifest_<appid>.acf` describes one installed game. These parsers are
// pure + tolerant so they unit-test without touching the filesystem.

class SteamGame {
  const SteamGame({
    required this.appId,
    required this.name,
    this.installDir = '',
    this.sizeOnDisk = 0,
    this.coverPath,
  });

  final String appId;
  final String name;
  final String installDir; // folder under steamapps/common
  final int sizeOnDisk; // bytes
  final String? coverPath; // local library cover image, if found

  /// The protocol URI that launches this game through Steam.
  Uri get launchUri => Uri.parse('steam://rungameid/$appId');

  SteamGame withCover(String? path) => SteamGame(
        appId: appId,
        name: name,
        installDir: installDir,
        sizeOnDisk: sizeOnDisk,
        coverPath: path,
      );
}

/// Read one VDF `"key" "value"` pair (case-insensitive key), or null.
String? _vdfField(String vdf, String key) {
  final RegExpMatch? m =
      RegExp('"$key"\\s+"([^"]*)"', caseSensitive: false).firstMatch(vdf);
  return m?.group(1);
}

/// Parse a single `appmanifest_*.acf` into a [SteamGame]. Returns null when it
/// lacks an appid or name (e.g. a partial/corrupt manifest). GAME-1.
SteamGame? parseAppManifest(String acf) {
  final String? appId = _vdfField(acf, 'appid');
  final String? name = _vdfField(acf, 'name');
  if (appId == null || appId.isEmpty || name == null || name.isEmpty) {
    return null;
  }
  return SteamGame(
    appId: appId,
    name: name,
    installDir: _vdfField(acf, 'installdir') ?? '',
    sizeOnDisk: int.tryParse(_vdfField(acf, 'SizeOnDisk') ?? '') ?? 0,
  );
}

/// Steam app ids that are tools / redistributables, not playable games.
const Set<String> kSteamNonGameAppIds = <String>{
  '228980', // Steamworks Common Redistributables
  '1070560', // Steam Linux Runtime
  '1391110', // Steam Linux Runtime - Soldier
  '1628350', // Steam Linux Runtime - Sniper
};

bool isPlayableSteamGame(SteamGame g) =>
    !kSteamNonGameAppIds.contains(g.appId);

/// Parse `libraryfolders.vdf` → the list of Steam library root paths. Steam
/// escapes backslashes (`\\`) in VDF; we unescape them to real Windows paths.
/// GAME-1.
List<String> parseLibraryFolders(String vdf) {
  return RegExp('"path"\\s+"([^"]*)"', caseSensitive: false)
      .allMatches(vdf)
      .map((RegExpMatch m) => m.group(1)!.replaceAll(r'\\', r'\'))
      .where((String p) => p.trim().isNotEmpty)
      .toList();
}

/// Human-readable install size (e.g. "12.4 GB"). GAME-1.
String formatGameSize(int bytes) {
  if (bytes <= 0) return '';
  const double gb = 1024 * 1024 * 1024;
  const double mb = 1024 * 1024;
  if (bytes >= gb) return '${(bytes / gb).toStringAsFixed(1)} GB';
  return '${(bytes / mb).toStringAsFixed(0)} MB';
}

/// Sort by name (case-insensitive) and drop duplicate app ids. GAME-1.
List<SteamGame> dedupeAndSortGames(List<SteamGame> games) {
  final Map<String, SteamGame> byId = <String, SteamGame>{};
  for (final SteamGame g in games) {
    byId.putIfAbsent(g.appId, () => g);
  }
  final List<SteamGame> out = byId.values.toList()
    ..sort((SteamGame a, SteamGame b) =>
        a.name.toLowerCase().compareTo(b.name.toLowerCase()));
  return out;
}
