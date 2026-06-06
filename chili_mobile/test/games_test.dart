import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/games/game_awareness.dart';
import 'package:chili_mobile/src/games/game_frame.dart';
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

  group('game awareness (GAME-2)', () {
    test('parseWindowProbe reads window rect + screen size', () {
      final GameWindowInfo? info = parseWindowProbe('10,20,1290,740;1920,1080');
      expect(info, isNotNull);
      expect(info!.window, const Rect.fromLTRB(10, 20, 1290, 740));
      expect(info.screen, const Size(1920, 1080));
    });

    test('parseWindowProbe is tolerant of junk / empty', () {
      expect(parseWindowProbe(''), isNull);
      expect(parseWindowProbe('nope'), isNull);
      expect(parseWindowProbe('1,2,3;4,5'), isNull); // wrong arity
      expect(parseWindowProbe('0,0,0,0;1920,1080'), isNull); // zero-size window
    });

    test('normalized maps the window into 0..1 of the screen', () {
      const GameWindowInfo info = GameWindowInfo(
        window: Rect.fromLTWH(960, 0, 960, 540),
        screen: Size(1920, 1080),
      );
      final Rect n = info.normalized;
      expect(n.left, closeTo(0.5, 1e-9));
      expect(n.top, closeTo(0.0, 1e-9));
      expect(n.width, closeTo(0.5, 1e-9));
      expect(n.height, closeTo(0.5, 1e-9));
    });

    test('probeScript sanitises the title (no quote/backtick/\$ injection)', () {
      final String s = GameAwareness.probeScript('a"b`c\$d');
      expect(s.contains('abcd'), isTrue); // quote/backtick/\$ stripped out
      expect(s.contains('a"b'), isFalse);
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

    testWidgets('GAME-2: shows the Now Playing banner with session + position',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: GamesScreen(
          fetcher: () async => sample,
          launcher: (SteamGame g) async => true,
          runningFetcher: () async => '730', // CS2 running
          clock: () => DateTime(2026, 1, 1), // fixed → '00:00:00'
          probe: (String title) async => const GameWindowInfo(
            window: Rect.fromLTWH(0, 0, 1920, 1080),
            screen: Size(1920, 1080),
          ),
        ),
      ));
      // Don't pumpAndSettle — a 1s session ticker runs forever; drain with pumps.
      for (int i = 0; i < 5; i++) {
        await tester.pump(const Duration(milliseconds: 30));
      }
      expect(find.text('NOW PLAYING'), findsOneWidget);
      expect(find.textContaining('Session 00:00:00'), findsOneWidget);
      expect(find.textContaining('1920×1080'), findsOneWidget);
      // The header still flags the running game.
      expect(find.textContaining('Playing: Counter-Strike 2'), findsOneWidget);
      // Dispose to cancel the session timers cleanly.
      await tester.pumpWidget(const SizedBox());
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

    testWidgets('GAME-3: frame toggle is present and switchable',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: GamesScreen(
          fetcher: () async => sample,
          launcher: (SteamGame g) async => true,
          runningFetcher: () async => '0',
        ),
      ));
      await tester.pumpAndSettle();
      final Finder chip = find.widgetWithText(FilterChip, 'Frame (beta)');
      expect(chip, findsOneWidget);
      // On by default now.
      expect(tester.widget<FilterChip>(chip).selected, isTrue);
      await tester.tap(chip);
      await tester.pumpAndSettle();
      expect(tester.widget<FilterChip>(chip).selected, isFalse);
    });
  });

  group('GameFrame (GAME-3)', () {
    const MethodChannel ch = MethodChannel('chili/game_frame');
    final List<MethodCall> calls = <MethodCall>[];

    setUp(() {
      calls.clear();
      TestWidgetsFlutterBinding.ensureInitialized();
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(ch, (MethodCall call) async {
        calls.add(call);
        return true;
      });
    });

    tearDown(() {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(ch, null);
    });

    test('start sends the title and returns the native result', () async {
      const GameFrame f = GameFrame();
      final bool ok = await f.start('Dota 2');
      expect(ok, isTrue);
      expect(calls.single.method, 'start');
      expect((calls.single.arguments as Map)['title'], 'Dota 2');
    });

    test('tolerates a missing native handler', () async {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(ch, null);
      const GameFrame f = GameFrame();
      expect(await f.start('x'), isFalse);
      await f.stop(); // must not throw
    });
  });
}
