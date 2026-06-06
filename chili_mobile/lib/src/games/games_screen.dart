import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';

import '../ui/app_ui.dart';
import 'steam_models.dart';
import 'steam_service.dart';

/// Fetches the installed Steam games — injectable for tests.
typedef GamesFetcher = Future<List<SteamGame>> Function();

/// Launches a game; returns true on success — injectable for tests.
typedef GameLauncher = Future<bool> Function(SteamGame game);

/// Returns the currently-running Steam app id ('0' if none) — injectable.
typedef RunningAppIdFetcher = Future<String> Function();

/// CHILI Games (GAME-1) — discovers the Steam games installed on this PC,
/// launches them through Steam, and stays aware in real time of which game is
/// running (the "PLAYING NOW" badge). An encapsulated launcher inside the OS.
class GamesScreen extends StatefulWidget {
  const GamesScreen({
    super.key,
    GamesFetcher? fetcher,
    GameLauncher? launcher,
    RunningAppIdFetcher? runningFetcher,
  })  : _fetcher = fetcher,
        _launcher = launcher,
        _runningFetcher = runningFetcher;

  final GamesFetcher? _fetcher;
  final GameLauncher? _launcher;
  final RunningAppIdFetcher? _runningFetcher;

  @override
  State<GamesScreen> createState() => _GamesScreenState();
}

class _GamesScreenState extends State<GamesScreen> {
  late final GamesFetcher _fetcher;
  late final GameLauncher _launcher;
  late final RunningAppIdFetcher _runningFetcher;

  List<SteamGame>? _games;
  bool _loading = true;
  String? _error;
  String _runningAppId = '0';
  String? _launchingAppId;
  Timer? _poll;
  Timer? _postLaunch;

  @override
  void initState() {
    super.initState();
    const SteamService svc = SteamService();
    _fetcher = widget._fetcher ?? svc.installedGames;
    _launcher = widget._launcher ?? svc.launch;
    _runningFetcher = widget._runningFetcher ?? svc.runningAppId;
    _load();
  }

  @override
  void dispose() {
    _poll?.cancel();
    _postLaunch?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final List<SteamGame> games = await _fetcher();
      if (!mounted) return;
      setState(() {
        _games = games;
        _loading = false;
      });
      _refreshRunning();
      _poll?.cancel();
      // Real-time gaming awareness — poll which game Steam reports as running.
      _poll = Timer.periodic(
          const Duration(seconds: 5), (_) => _refreshRunning());
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
  }

  Future<void> _refreshRunning() async {
    try {
      final String id = await _runningFetcher();
      if (mounted && id != _runningAppId) {
        setState(() => _runningAppId = id);
      }
    } catch (_) {
      // ignore — awareness is best-effort
    }
  }

  Future<void> _launch(SteamGame game) async {
    setState(() => _launchingAppId = game.appId);
    final bool ok = await _launcher(game);
    if (!mounted) return;
    setState(() => _launchingAppId = null);
    if (!ok) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Couldn’t launch ${game.name}')),
      );
    } else {
      // Give Steam a moment, then re-check what's running.
      _postLaunch?.cancel();
      _postLaunch = Timer(const Duration(seconds: 3), _refreshRunning);
    }
  }

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Scaffold(
      backgroundColor: cs.surface,
      body: Column(
        children: <Widget>[
          _header(cs),
          const Divider(height: 1),
          Expanded(child: _body(cs)),
        ],
      ),
    );
  }

  Widget _header(ColorScheme cs) {
    final int count = _games?.length ?? 0;
    final SteamGame? playing = _runningGame;
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 14, 12, 12),
      child: Row(
        children: <Widget>[
          Icon(Icons.sports_esports, color: cs.primary),
          const SizedBox(width: 10),
          Text('Games',
              style: Theme.of(context)
                  .textTheme
                  .headlineSmall
                  ?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(width: 12),
          if (count > 0)
            ApStatusPill('$count installed', color: cs.secondary),
          if (playing != null) ...<Widget>[
            const SizedBox(width: 8),
            ApStatusPill('Playing: ${playing.name}',
                color: const Color(0xFF2E9E5B), icon: Icons.videogame_asset),
          ],
          const Spacer(),
          IconButton(
            tooltip: 'Rescan library',
            icon: const Icon(Icons.refresh, size: 20),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
    );
  }

  SteamGame? get _runningGame {
    if (_runningAppId == '0') return null;
    for (final SteamGame g in _games ?? const <SteamGame>[]) {
      if (g.appId == _runningAppId) return g;
    }
    return null;
  }

  Widget _body(ColorScheme cs) {
    if (_loading) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            CircularProgressIndicator(),
            SizedBox(height: 12),
            Text('Scanning your Steam library…'),
          ],
        ),
      );
    }
    if (_error != null) {
      return ApEmptyState(
        icon: Icons.error_outline,
        message: 'Couldn’t read the Steam library',
        detail: _error,
        action: FilledButton.icon(
          onPressed: _load,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Retry'),
        ),
      );
    }
    final List<SteamGame> games = _games ?? const <SteamGame>[];
    if (games.isEmpty) {
      return const ApEmptyState(
        icon: Icons.sports_esports_outlined,
        message: 'No Steam games found',
        detail:
            'Make sure Steam is installed and you have at least one game downloaded, then rescan.',
      );
    }
    return GridView.builder(
      padding: const EdgeInsets.all(20),
      gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
        maxCrossAxisExtent: 190,
        childAspectRatio: 0.62,
        crossAxisSpacing: 16,
        mainAxisSpacing: 16,
      ),
      itemCount: games.length,
      itemBuilder: (BuildContext _, int i) => _gameCard(cs, games[i]),
    );
  }

  Widget _gameCard(ColorScheme cs, SteamGame game) {
    final bool isPlaying = game.appId == _runningAppId && _runningAppId != '0';
    final bool isLaunching = game.appId == _launchingAppId;
    return Tooltip(
      message: game.name,
      child: Material(
        color: cs.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(12),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: isLaunching ? null : () => _launch(game),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              Expanded(
                child: Stack(
                  fit: StackFit.expand,
                  children: <Widget>[
                    _cover(cs, game),
                    if (isPlaying)
                      Positioned(
                        top: 8,
                        left: 8,
                        child: _badge(
                            'PLAYING NOW', const Color(0xFF2E9E5B)),
                      ),
                    // Launch affordance overlay.
                    Positioned(
                      right: 8,
                      bottom: 8,
                      child: CircleAvatar(
                        radius: 16,
                        backgroundColor: cs.primary,
                        child: isLaunching
                            ? const SizedBox(
                                width: 16,
                                height: 16,
                                child: CircularProgressIndicator(
                                    strokeWidth: 2, color: Colors.white),
                              )
                            : const Icon(Icons.play_arrow,
                                size: 20, color: Colors.white),
                      ),
                    ),
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(10, 8, 10, 4),
                child: Text(
                  game.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      fontWeight: FontWeight.w600, color: cs.onSurface),
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(10, 0, 10, 10),
                child: Text(
                  formatGameSize(game.sizeOnDisk),
                  style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _cover(ColorScheme cs, SteamGame game) {
    final String? path = game.coverPath;
    if (path != null) {
      return Image.file(
        File(path),
        fit: BoxFit.cover,
        errorBuilder: (_, __, ___) => _coverPlaceholder(cs, game),
      );
    }
    return _coverPlaceholder(cs, game);
  }

  Widget _coverPlaceholder(ColorScheme cs, SteamGame game) {
    return Container(
      color: cs.primary.withValues(alpha: 0.10),
      alignment: Alignment.center,
      padding: const EdgeInsets.all(12),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Icon(Icons.videogame_asset_outlined,
              size: 34, color: cs.primary.withValues(alpha: 0.7)),
          const SizedBox(height: 8),
          Text(game.name,
              maxLines: 3,
              textAlign: TextAlign.center,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                  color: cs.onSurface)),
        ],
      ),
    );
  }

  Widget _badge(String text, Color color) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.circular(6),
      ),
      child: Text(text,
          style: const TextStyle(
              color: Colors.white,
              fontSize: 9,
              fontWeight: FontWeight.w800,
              letterSpacing: 0.5)),
    );
  }
}
