import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/games/games_screen.dart';
import 'package:chili_mobile/src/games/steam_models.dart';

const String _acf = '''
"AppState"
{
	"appid"		"570"
	"name"		"Dota 2"
	"installdir"		"dota 2 beta"
	"SizeOnDisk"		"39440000000"
}
''';

const String _libraryVdf = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
	}
	"1"
	{
		"path"		"D:\\SteamLibrary"
	}
}
''';

void main() {
  group('parseAppManifest (GAME-1)', () {
    test('reads appid / name / installdir / size', () {
      final SteamGame? g = parseAppManifest(_acf);
      expect(g, isNotNull);
      expect(g!.appId, '570');
      expect(g.name, 'Dota 2');
      expect(g.installDir, 'dota 2 beta');
      expect(g.sizeOnDisk, 39440000000);
      expect(g.launchUri.toString(), 'steam://rungameid/570');
    });

    test('returns null when appid or name is missing', () {
      expect(parseAppManifest('"AppState" { "name" "X" }'), isNull);
      expect(parseAppManifest('"AppState" { "appid" "1" }'), isNull);
      expect(parseAppManifest('garbage'), isNull);
    });
  });

  group('parseLibraryFolders (GAME-1)', () {
    test('extracts and unescapes library paths', () {
      final List<String> roots = parseLibraryFolders(_libraryVdf);
      expect(roots, <String>[
        r'C:\Program Files (x86)\Steam',
        r'D:\SteamLibrary',
      ]);
    });

    test('empty vdf → empty', () {
      expect(parseLibraryFolders('"libraryfolders" {}'), isEmpty);
    });
  });

  group('helpers (GAME-1)', () {
    test('formatGameSize', () {
      expect(formatGameSize(0), '');
      expect(formatGameSize(39440000000), '36.7 GB');
      expect(formatGameSize(500 * 1024 * 1024), '500 MB');
    });

    test('isPlayableSteamGame filters redistributables', () {
      expect(
          isPlayableSteamGame(
              const SteamGame(appId: '228980', name: 'Redist')),
          isFalse);
      expect(isPlayableSteamGame(const SteamGame(appId: '570', name: 'Dota 2')),
          isTrue);
    });

    test('dedupeAndSortGames dedupes by id and sorts by name', () {
      final List<SteamGame> out = dedupeAndSortGames(<SteamGame>[
        const SteamGame(appId: '2', name: 'Zelda'),
        const SteamGame(appId: '1', name: 'Apex'),
        const SteamGame(appId: '1', name: 'Apex (dupe)'),
      ]);
      expect(out.map((SteamGame g) => g.name), <String>['Apex', 'Zelda']);
    });
  });

  group('GamesScreen widget (GAME-1)', () {
    const List<SteamGame> sample = <SteamGame>[
      SteamGame(appId: '570', name: 'Dota 2', sizeOnDisk: 39440000000),
      SteamGame(appId: '730', name: 'Counter-Strike 2'),
    ];

    testWidgets('renders installed games and launches on tap',
        (WidgetTester tester) async {
      SteamGame? launched;
      await tester.pumpWidget(MaterialApp(
        home: GamesScreen(
          fetcher: () async => sample,
          launcher: (SteamGame g) async {
            launched = g;
            return true;
          },
          runningFetcher: () async => '0',
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('Games'), findsOneWidget);
      expect(find.text('2 installed'), findsOneWidget);
      expect(find.text('Dota 2'), findsWidgets);

      await tester.tap(find.text('Dota 2').first);
      await tester.pump();
      expect(launched, isNotNull);
      expect(launched!.appId, '570');
    });

    testWidgets('shows PLAYING NOW for the running game',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: GamesScreen(
          fetcher: () async => sample,
          launcher: (SteamGame g) async => true,
          runningFetcher: () async => '730', // CS2 running
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('PLAYING NOW'), findsOneWidget);
      expect(find.textContaining('Playing: Counter-Strike 2'), findsOneWidget);
    });

    testWidgets('empty state when no games found',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: GamesScreen(
          fetcher: () async => const <SteamGame>[],
          launcher: (SteamGame g) async => true,
          runningFetcher: () async => '0',
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('No Steam games found'), findsOneWidget);
    });
  });
}
