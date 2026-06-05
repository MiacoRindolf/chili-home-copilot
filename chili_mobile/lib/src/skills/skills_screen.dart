import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';
import '../ui/app_ui.dart';
import 'skills_models.dart';

/// Fetches teacher skills — injectable for tests.
typedef SkillsFetcher = Future<List<Map<String, dynamic>>> Function();

/// Skills viewer (SK-1): the reusable skills CHILI's teacher-escalation loop
/// (salvaged from Odysseus) has learned from past failures. Read-only.
class SkillsScreen extends StatefulWidget {
  const SkillsScreen({super.key, SkillsFetcher? fetcher})
      : _injectedFetcher = fetcher;

  final SkillsFetcher? _injectedFetcher;

  @override
  State<SkillsScreen> createState() => _SkillsScreenState();
}

class _SkillsScreenState extends State<SkillsScreen> {
  late final SkillsFetcher _fetcher;
  final TextEditingController _search = TextEditingController(); // SF-1
  List<Skill>? _skills;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _fetcher = widget._injectedFetcher ?? ChiliApiClient().getTeacherSkills;
    _load();
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final List<Map<String, dynamic>> raw = await _fetcher();
      if (!mounted) return;
      setState(() {
        _skills = parseSkills(raw);
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

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Scaffold(
      backgroundColor: cs.surface,
      body: Column(
        children: <Widget>[
          _header(cs),
          if ((_skills?.length ?? 0) > 0) _searchBar(cs), // SF-1
          const Divider(height: 1),
          Expanded(child: _body(cs)),
        ],
      ),
    );
  }

  Widget _searchBar(ColorScheme cs) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 0, 20, 12),
      child: TextField(
        controller: _search,
        onChanged: (_) => setState(() {}),
        decoration: InputDecoration(
          isDense: true,
          hintText: 'Search skills…',
          prefixIcon: const Icon(Icons.search, size: 18),
          suffixIcon: _search.text.isEmpty
              ? null
              : IconButton(
                  icon: const Icon(Icons.close, size: 16),
                  onPressed: () => setState(() => _search.clear()),
                ),
          border:
              OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
        ),
      ),
    );
  }

  Widget _header(ColorScheme cs) {
    final List<Skill>? s = _skills;
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 14, 12, 12),
      child: Row(
        children: <Widget>[
          Icon(Icons.school_outlined, color: cs.primary),
          const SizedBox(width: 10),
          Text('Skills',
              style: Theme.of(context)
                  .textTheme
                  .headlineSmall
                  ?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(width: 12),
          if (s != null && s.isNotEmpty)
            ApStatusPill('${s.length} learned', color: cs.secondary),
          const Spacer(),
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
            Text('Loading skills…'),
          ],
        ),
      );
    }
    if (_error != null) {
      return ApEmptyState(
        icon: Icons.cloud_off,
        message: 'Couldn’t load skills',
        detail: _error,
        action: FilledButton.icon(
          onPressed: _load,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Retry'),
        ),
      );
    }
    final List<Skill> all = _skills ?? const <Skill>[];
    if (all.isEmpty) {
      return const ApEmptyState(
        icon: Icons.school_outlined,
        message: 'No skills learned yet',
        detail:
            'The teacher loop distills a reusable skill when a strong model recovers from a failure.',
      );
    }
    // SF-1 — filter by the search box.
    final List<Skill> skills = filterSkills(all, _search.text);
    if (skills.isEmpty) {
      return ApEmptyState(
        icon: Icons.search_off,
        message: 'No skills match “${_search.text.trim()}”',
      );
    }
    return ListView(
      padding: const EdgeInsets.all(20),
      children: <Widget>[
        for (final Skill s in skills) _skillCard(cs, s),
        const SizedBox(height: 24),
      ],
    );
  }

  Widget _skillCard(ColorScheme cs, Skill s) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: ApPanel(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Icon(Icons.lightbulb_outline, size: 18, color: cs.primary),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(s.name,
                      style: TextStyle(
                          fontSize: 15,
                          fontWeight: FontWeight.w700,
                          color: cs.onSurface)),
                ),
                if (s.savedAtMs > 0)
                  Text(_date(s.savedAtMs),
                      style: TextStyle(
                          fontSize: 11, color: cs.onSurfaceVariant)),
              ],
            ),
            if (s.description.isNotEmpty) ...<Widget>[
              const SizedBox(height: 8),
              Text(s.description,
                  style: TextStyle(
                      fontSize: 13, height: 1.35, color: cs.onSurface)),
            ],
            if (s.hasSteps) ...<Widget>[
              const SizedBox(height: 12),
              const ApSectionHeader('Steps'),
              const SizedBox(height: 4),
              for (int i = 0; i < s.steps.length; i++)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 2),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      SizedBox(
                        width: 22,
                        child: Text('${i + 1}.',
                            style: TextStyle(
                                fontSize: 13,
                                fontWeight: FontWeight.w600,
                                color: cs.onSurfaceVariant)),
                      ),
                      Expanded(
                        child: Text(s.steps[i],
                            style:
                                TextStyle(fontSize: 13, color: cs.onSurface)),
                      ),
                    ],
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }

  static String _date(int ms) {
    final DateTime dt = DateTime.fromMillisecondsSinceEpoch(ms);
    return '${dt.year}-${dt.month.toString().padLeft(2, '0')}-${dt.day.toString().padLeft(2, '0')}';
  }
}
