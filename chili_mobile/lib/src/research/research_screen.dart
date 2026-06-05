import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../network/chili_api_client.dart';
import '../ui/app_ui.dart';
import 'research_models.dart';
import 'research_report.dart';

/// Fetches the research digest JSON — injectable for tests.
typedef ResearchDigestFetcher = Future<Map<String, dynamic>> Function();

/// Opens the visual report; returns true on success — injectable for tests.
typedef ResearchReportOpener = Future<bool> Function();

/// Runs on-demand research on a topic; returns {ok, stored, ...} — injectable.
typedef ResearchRunner = Future<Map<String, dynamic>> Function(String topic);

/// Opens a source URL in the browser; returns true on success — injectable (RS-5).
typedef ResearchUrlOpener = Future<bool> Function(String url);

/// CHILI Research — surfaces the salvaged Odysseus research power: browse the
/// digest + open the visual report (RS-1), and run research on demand (RS-2).
class ResearchScreen extends StatefulWidget {
  const ResearchScreen({
    super.key,
    ResearchDigestFetcher? fetcher,
    ResearchReportOpener? reportOpener,
    ResearchRunner? runner,
    ResearchUrlOpener? urlOpener,
    this.onDiscuss,
  })  : _injectedFetcher = fetcher,
        _injectedOpener = reportOpener,
        _injectedRunner = runner,
        _injectedUrlOpener = urlOpener;

  final ResearchDigestFetcher? _injectedFetcher;
  final ResearchReportOpener? _injectedOpener;
  final ResearchRunner? _injectedRunner;
  final ResearchUrlOpener? _injectedUrlOpener;

  /// RC-1 — pivot a research topic into Chat ("Discuss"). Wired by the workspace
  /// to the shared ⌘K ask-inbox so Chat opens and asks about the topic.
  final void Function(String topic)? onDiscuss;

  @override
  State<ResearchScreen> createState() => _ResearchScreenState();
}

class _ResearchScreenState extends State<ResearchScreen> {
  late final ResearchDigestFetcher _fetcher;
  late final ResearchReportOpener _opener;
  late final ResearchRunner _runner;
  late final ResearchUrlOpener _urlOpener; // RS-5
  final TextEditingController _topicCtrl = TextEditingController();
  ChiliApiClient? _api;

  ResearchDigest? _digest;
  bool _loading = true;
  bool _openingReport = false;
  bool _running = false;
  String? _error;

  // RS-4 — topic sort order (default: most relevant first).
  ResearchTopicSort _sort = ResearchTopicSort.relevance;

  @override
  void initState() {
    super.initState();
    final bool needClient = widget._injectedFetcher == null ||
        widget._injectedOpener == null ||
        widget._injectedRunner == null;
    _api = needClient ? ChiliApiClient() : null;
    _fetcher = widget._injectedFetcher ?? _api!.getResearchDigest;
    _opener = widget._injectedOpener ??
        () async => (await openResearchReport(_api!)) != null;
    _runner = widget._injectedRunner ?? _api!.runResearch;
    _urlOpener = widget._injectedUrlOpener ?? openExternalUrl;
    _load();
  }

  @override
  void dispose() {
    _topicCtrl.dispose();
    super.dispose();
  }

  Future<void> _runResearch() async {
    final String topic = _topicCtrl.text.trim();
    if (topic.isEmpty || _running) return;
    setState(() => _running = true);
    Map<String, dynamic> res;
    try {
      res = await _runner(topic);
    } catch (_) {
      res = const <String, dynamic>{};
    }
    if (!mounted) return;
    setState(() => _running = false);
    final bool stored = res['stored'] == true;
    if (stored) {
      _topicCtrl.clear();
      _snack('Researched “$topic” — added to your digest');
      _load(); // refresh to show the new topic
    } else {
      final String note = (res['error'] ?? res['note'] ?? '').toString();
      _snack(note.isEmpty
          ? 'Couldn’t research that topic'
          : note);
    }
  }

  void _snack(String msg) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(msg),
      behavior: SnackBarBehavior.floating,
      duration: const Duration(seconds: 3),
    ));
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final Map<String, dynamic> json = await _fetcher();
      if (!mounted) return;
      setState(() {
        _digest = parseResearchDigest(json);
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
  }

  Future<void> _openReport() async {
    setState(() => _openingReport = true);
    bool ok = false;
    try {
      ok = await _opener();
    } catch (_) {
      ok = false;
    }
    if (!mounted) return;
    setState(() => _openingReport = false);
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(ok ? 'Opened research report in your browser' : 'No report to open yet'),
      behavior: SnackBarBehavior.floating,
      duration: const Duration(seconds: 3),
    ));
  }

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Scaffold(
      backgroundColor: cs.surface,
      body: Column(
        children: <Widget>[
          _header(cs),
          _runBar(cs),
          const Divider(height: 1),
          Expanded(child: _body(cs)),
        ],
      ),
    );
  }

  // RS-2 — on-demand research: type a topic, CHILI researches it now.
  Widget _runBar(ColorScheme cs) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 0, 16, 12),
      child: Row(
        children: <Widget>[
          Expanded(
            child: TextField(
              controller: _topicCtrl,
              enabled: !_running,
              textInputAction: TextInputAction.search,
              // SF-1 — filter shown topics as you type; submit researches it.
              onChanged: (_) => setState(() {}),
              onSubmitted: (_) => _runResearch(),
              decoration: InputDecoration(
                isDense: true,
                hintText: 'Filter topics — or research a new one…',
                prefixIcon: const Icon(Icons.search, size: 18),
                border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12)),
              ),
            ),
          ),
          const SizedBox(width: 10),
          FilledButton.icon(
            onPressed: _running ? null : _runResearch,
            icon: _running
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.bolt, size: 18),
            label: Text(_running ? 'Researching…' : 'Research'),
            style: FilledButton.styleFrom(minimumSize: const Size(0, 44)),
          ),
        ],
      ),
    );
  }

  Widget _header(ColorScheme cs) {
    final ResearchDigest? d = _digest;
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 14, 12, 12),
      child: Row(
        children: <Widget>[
          Icon(Icons.travel_explore, color: cs.primary),
          const SizedBox(width: 10),
          Text('Research',
              style: Theme.of(context)
                  .textTheme
                  .headlineSmall
                  ?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(width: 12),
          if (d != null && !d.isEmpty)
            ApStatusPill('${d.topicCount} topics', color: cs.secondary),
          const Spacer(),
          OutlinedButton.icon(
            onPressed: (_openingReport || (_digest?.isEmpty ?? true))
                ? null
                : _openReport,
            icon: _openingReport
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.open_in_new, size: 18),
            label: const Text('Open report'),
          ),
          const SizedBox(width: 4),
          IconButton(
            tooltip: 'Refresh',
            icon: const Icon(Icons.refresh, size: 20),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
    );
  }

  Widget _body(ColorScheme cs) {
    if (_loading) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            CircularProgressIndicator(),
            SizedBox(height: 12),
            Text('Loading research…'),
          ],
        ),
      );
    }
    if (_error != null) {
      return ApEmptyState(
        icon: Icons.cloud_off,
        message: 'Couldn’t load research',
        detail: _error,
        action: FilledButton.icon(
          onPressed: _load,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Retry'),
        ),
      );
    }
    final ResearchDigest d = _digest ?? ResearchDigest.empty;
    if (d.isEmpty) {
      return const ApEmptyState(
        icon: Icons.travel_explore,
        message: 'No research yet',
        detail:
            'CHILI researches topics you show interest in. Results will appear here.',
      );
    }
    // SF-1 — filter the shown topics by the run-bar text; RS-4 — then sort.
    final String q = _topicCtrl.text.trim();
    final List<ResearchTopic> topics =
        sortResearchTopics(filterResearchTopics(d.topics, q), _sort);
    if (topics.isEmpty) {
      return ApEmptyState(
        icon: Icons.search_off,
        message: 'No topics match “$q”',
        detail: 'Press Research to look it up on the web.',
        action: FilledButton.icon(
          onPressed: _running ? null : _runResearch,
          icon: const Icon(Icons.bolt, size: 18),
          label: Text('Research “$q”'),
        ),
      );
    }
    return ListView(
      padding: const EdgeInsets.all(20),
      children: <Widget>[
        if (topics.length > 1) ...<Widget>[
          _sortToggle(cs),
          const SizedBox(height: 8),
        ],
        for (final ResearchTopic t in topics) _topicCard(cs, t),
        if (q.isEmpty && d.sources.isNotEmpty) ...<Widget>[
          const SizedBox(height: 8),
          ApSectionHeader('Sources · ${d.sources.length}', icon: Icons.link),
          const SizedBox(height: 6),
          for (final ResearchSource s in d.sources) _sourceRow(cs, s),
        ],
        const SizedBox(height: 24),
      ],
    );
  }

  // RS-4 — segmented sort toggle for the topic list.
  Widget _sortToggle(ColorScheme cs) {
    return Row(
      children: <Widget>[
        Icon(Icons.sort, size: 14, color: cs.onSurfaceVariant),
        const SizedBox(width: 6),
        Text('Sort',
            style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
        const SizedBox(width: 10),
        for (final ResearchTopicSort s in ResearchTopicSort.values)
          Padding(
            padding: const EdgeInsets.only(right: 6),
            child: ChoiceChip(
              label: Text(researchSortLabel(s)),
              selected: _sort == s,
              onSelected: (_) => setState(() => _sort = s),
              visualDensity: VisualDensity.compact,
            ),
          ),
      ],
    );
  }

  Widget _topicCard(ColorScheme cs, ResearchTopic t) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: ApPanel(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Expanded(
                  child: Text(
                    t.topic,
                    style: TextStyle(
                      fontSize: 15,
                      fontWeight: FontWeight.w700,
                      color: cs.onSurface,
                    ),
                  ),
                ),
                if (t.relevance > 0)
                  ApStatusPill('${(t.relevance * 100).round()}%',
                      color: cs.primary),
                if (widget.onDiscuss != null) ...<Widget>[
                  const SizedBox(width: 4),
                  // RC-1 — pivot this research topic into a Chat conversation.
                  IconButton(
                    tooltip: 'Discuss in Chat',
                    visualDensity: VisualDensity.compact,
                    icon: const Icon(Icons.forum_outlined, size: 18),
                    onPressed: () => widget.onDiscuss!(t.topic),
                  ),
                ],
              ],
            ),
            if (t.summary.trim().isNotEmpty) ...<Widget>[
              const SizedBox(height: 8),
              MarkdownBody(
                data: t.summary,
                shrinkWrap: true,
                styleSheet: MarkdownStyleSheet(
                  p: TextStyle(
                      fontSize: 13, height: 1.4, color: cs.onSurface),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  // RS-5 — open a source in the OS browser; toast on failure / unopenable.
  Future<void> _openSource(ResearchSource s) async {
    if (!s.isOpenable) return;
    final bool ok = await _urlOpener(s.url);
    if (!ok && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Couldn’t open ${s.host}')),
      );
    }
  }

  Widget _sourceRow(ColorScheme cs, ResearchSource s) {
    final bool openable = s.isOpenable; // RS-5
    return InkWell(
      onTap: openable ? () => _openSource(s) : null,
      borderRadius: BorderRadius.circular(6),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 5, horizontal: 2),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Icon(Icons.link, size: 14, color: cs.secondary),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text(s.title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                          fontSize: 13,
                          color: openable ? cs.primary : cs.onSurface)),
                  if (s.host.isNotEmpty)
                    Text(s.host,
                        style: TextStyle(
                            fontSize: 11, color: cs.onSurfaceVariant)),
                ],
              ),
            ),
            if (openable) ...<Widget>[
              const SizedBox(width: 8),
              Icon(Icons.open_in_new, size: 14, color: cs.onSurfaceVariant),
            ],
          ],
        ),
      ),
    );
  }
}
