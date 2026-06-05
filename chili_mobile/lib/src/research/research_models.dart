// Parsed shape of the research digest returned by
// GET /api/brain/reasoning/research/report?format=json (RS-1). Pure data +
// tolerant parser so it's unit-testable without the network.

class ResearchSource {
  const ResearchSource({required this.title, required this.url});
  final String title;
  final String url;

  /// Bare host for compact display (www-stripped).
  String get host {
    final Uri? u = Uri.tryParse(url);
    final String h = u?.host ?? '';
    return h.startsWith('www.') ? h.substring(4) : h;
  }
}

class ResearchTopic {
  const ResearchTopic({
    required this.topic,
    required this.summary,
    required this.relevance,
  });
  final String topic;
  final String summary;
  final double relevance; // 0..1-ish
}

class ResearchDigest {
  const ResearchDigest({
    required this.title,
    required this.topicCount,
    required this.topics,
    required this.sources,
  });
  final String title;
  final int topicCount;
  final List<ResearchTopic> topics;
  final List<ResearchSource> sources;

  bool get isEmpty => topics.isEmpty;

  static const ResearchDigest empty = ResearchDigest(
    title: 'Research Digest',
    topicCount: 0,
    topics: <ResearchTopic>[],
    sources: <ResearchSource>[],
  );
}

/// How research-digest topics are ordered (RS-4).
enum ResearchTopicSort { relevance, title }

String researchSortLabel(ResearchTopicSort s) {
  switch (s) {
    case ResearchTopicSort.relevance:
      return 'Relevance';
    case ResearchTopicSort.title:
      return 'Title';
  }
}

/// Return a NEW list ordered by [sort] (RS-4). Pure — never mutates [topics].
/// Relevance sorts highest-first (ties fall back to title A→Z for stability);
/// title sorts A→Z case-insensitively.
List<ResearchTopic> sortResearchTopics(
    List<ResearchTopic> topics, ResearchTopicSort sort) {
  final List<ResearchTopic> out = List<ResearchTopic>.of(topics);
  int byTitle(ResearchTopic a, ResearchTopic b) =>
      a.topic.toLowerCase().compareTo(b.topic.toLowerCase());
  switch (sort) {
    case ResearchTopicSort.relevance:
      out.sort((ResearchTopic a, ResearchTopic b) {
        final int c = b.relevance.compareTo(a.relevance);
        return c != 0 ? c : byTitle(a, b);
      });
    case ResearchTopicSort.title:
      out.sort(byTitle);
  }
  return out;
}

/// Case-insensitive filter over topic + summary (SF-1). Empty query → unchanged.
List<ResearchTopic> filterResearchTopics(
    List<ResearchTopic> topics, String query) {
  final String q = query.trim().toLowerCase();
  if (q.isEmpty) return topics;
  return topics
      .where((ResearchTopic t) =>
          t.topic.toLowerCase().contains(q) ||
          t.summary.toLowerCase().contains(q))
      .toList();
}

ResearchDigest parseResearchDigest(Map<String, dynamic> json) {
  final List<ResearchTopic> topics = <ResearchTopic>[
    for (final Object? t in (json['topics'] as List? ?? const <Object?>[]))
      if (t is Map)
        ResearchTopic(
          topic: _str(t['topic']),
          summary: _str(t['summary']),
          relevance: _dbl(t['relevance_score']),
        ),
  ];
  final List<ResearchSource> sources = <ResearchSource>[
    for (final Object? s in (json['sources'] as List? ?? const <Object?>[]))
      if (s is Map && _str(s['url']).isNotEmpty)
        ResearchSource(
          title: _str(s['title']).isEmpty ? _str(s['url']) : _str(s['title']),
          url: _str(s['url']),
        ),
  ];
  return ResearchDigest(
    title: _str(json['title']).isEmpty ? 'Research Digest' : _str(json['title']),
    topicCount: (json['topic_count'] as num?)?.toInt() ?? topics.length,
    topics: topics,
    sources: sources,
  );
}

String _str(Object? v) => v?.toString() ?? '';

double _dbl(Object? v) {
  if (v is num) return v.toDouble();
  if (v is String) return double.tryParse(v) ?? 0;
  return 0;
}
