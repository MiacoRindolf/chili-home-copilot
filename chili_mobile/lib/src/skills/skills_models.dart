// Parsed shape of a teacher-written reusable skill (SK-1), from
// GET /api/brain/teacher/skills. Pure + tolerant — skill dicts vary, so we
// extract a name, a description, and an ordered list of steps best-effort.

class Skill {
  const Skill({
    required this.name,
    required this.description,
    required this.steps,
    required this.savedAtMs,
  });

  final String name;
  final String description;
  final List<String> steps;
  final int savedAtMs; // epoch ms, 0 if unknown

  bool get hasSteps => steps.isNotEmpty;
}

/// How the Skills viewer orders learned skills (SK-2).
enum SkillSort { recent, name }

String skillSortLabel(SkillSort s) {
  switch (s) {
    case SkillSort.recent:
      return 'Recent';
    case SkillSort.name:
      return 'Name';
  }
}

/// Return a NEW list ordered by [sort] (SK-2). Pure — never mutates [skills].
/// Recent sorts newest-first by savedAtMs (ties / unknown timestamps fall back
/// to name A→Z for stability); name sorts A→Z case-insensitively.
List<Skill> sortSkills(List<Skill> skills, SkillSort sort) {
  final List<Skill> out = List<Skill>.of(skills);
  int byName(Skill a, Skill b) =>
      a.name.toLowerCase().compareTo(b.name.toLowerCase());
  switch (sort) {
    case SkillSort.recent:
      out.sort((Skill a, Skill b) {
        final int c = b.savedAtMs.compareTo(a.savedAtMs);
        return c != 0 ? c : byName(a, b);
      });
    case SkillSort.name:
      out.sort(byName);
  }
  return out;
}

/// Case-insensitive filter over name + description + steps (SF-1). Empty → all.
List<Skill> filterSkills(List<Skill> skills, String query) {
  final String q = query.trim().toLowerCase();
  if (q.isEmpty) return skills;
  return skills
      .where((Skill s) =>
          s.name.toLowerCase().contains(q) ||
          s.description.toLowerCase().contains(q) ||
          s.steps.any((String st) => st.toLowerCase().contains(q)))
      .toList();
}

List<Skill> parseSkills(List<Map<String, dynamic>> raw) => <Skill>[
      for (final Map<String, dynamic> s in raw)
        if (_str(s['name']).isNotEmpty) _skill(s),
    ];

Skill _skill(Map<String, dynamic> s) => Skill(
      name: _str(s['name']),
      description: _firstStr(s, const <String>[
        'description',
        'when_to_use',
        'when',
        'summary',
        'goal',
      ]),
      steps: _steps(s['steps'] ?? s['procedure'] ?? s['plan']),
      savedAtMs: _savedAtMs(s['saved_at']),
    );

List<String> _steps(Object? v) {
  if (v is List) {
    return v
        .map((Object? e) {
          if (e is Map) {
            return _firstStr(Map<String, dynamic>.from(e),
                const <String>['step', 'action', 'text', 'description']);
          }
          return e?.toString().trim() ?? '';
        })
        .where((String s) => s.isNotEmpty)
        .toList();
  }
  if (v is String && v.trim().isNotEmpty) {
    // newline / numbered text → split into steps.
    return v
        .split(RegExp(r'\n+'))
        .map((String s) => s.trim())
        .where((String s) => s.isNotEmpty)
        .toList();
  }
  return <String>[];
}

int _savedAtMs(Object? v) {
  // saved_at is unix seconds in the store.
  if (v is num) return (v * 1000).round();
  if (v is String) {
    final int? n = int.tryParse(v);
    if (n != null) return n * 1000;
  }
  return 0;
}

String _firstStr(Map<String, dynamic> m, List<String> keys) {
  for (final String k in keys) {
    final String s = _str(m[k]);
    if (s.isNotEmpty) return s;
  }
  return '';
}

String _str(Object? v) => v?.toString().trim() ?? '';
