import 'package:flutter/material.dart';

import 'agent_activity_service.dart';

/// Coarse health class of a single backend run's outcome (AG-1).
enum RunOutcomeKind { good, bad, neutral }

/// Classify an [AgentRun] outcome string into a coarse health bucket. Pure +
/// tolerant: unknown / null / informational outcomes fall through to neutral so
/// they never skew the success rate. Case-insensitive substring match.
RunOutcomeKind classifyOutcome(String? outcome) {
  final String s = (outcome ?? '').toLowerCase();
  if (s.isEmpty) return RunOutcomeKind.neutral;
  // Negative signals take precedence (e.g. "ok · failed" is a failure).
  const List<String> bad = <String>[
    'fail', 'error', 'err', 'reject', 'denied', 'deny', 'abort',
    'timeout', 'timed out', 'cancel', 'exception', 'crash', 'blocked',
  ];
  for (final String t in bad) {
    if (s.contains(t)) return RunOutcomeKind.bad;
  }
  const List<String> good = <String>[
    'ok', 'success', 'succeed', 'complete', 'done', 'filled', 'fill',
    'pass', 'approved', 'win', 'profit', 'executed',
  ];
  for (final String t in good) {
    if (s.contains(t)) return RunOutcomeKind.good;
  }
  return RunOutcomeKind.neutral;
}

/// Aggregate run-health over a list of runs (AG-1). Pure.
class RunHealth {
  const RunHealth({
    required this.total,
    required this.good,
    required this.bad,
    required this.neutral,
  });

  final int total;
  final int good;
  final int bad;
  final int neutral;

  /// Runs that carry a clear good/bad signal (excludes neutral).
  int get scored => good + bad;

  /// Success rate over scored runs in 0..1, or null when nothing is scorable.
  double? get successRate => scored == 0 ? null : good / scored;

  /// "4/6 ok" style label over scored runs, or null when nothing scorable.
  String? get successLabel => scored == 0 ? null : '$good/$scored ok';

  static const RunHealth empty =
      RunHealth(total: 0, good: 0, bad: 0, neutral: 0);
}

RunHealth summarizeRuns(List<AgentRun> runs) {
  int good = 0, bad = 0, neutral = 0;
  for (final AgentRun r in runs) {
    switch (classifyOutcome(r.outcome)) {
      case RunOutcomeKind.good:
        good++;
      case RunOutcomeKind.bad:
        bad++;
      case RunOutcomeKind.neutral:
        neutral++;
    }
  }
  return RunHealth(
      total: runs.length, good: good, bad: bad, neutral: neutral);
}

Color outcomeColor(RunOutcomeKind kind, ColorScheme cs) {
  switch (kind) {
    case RunOutcomeKind.good:
      return const Color(0xFF2E9E5B); // green
    case RunOutcomeKind.bad:
      return cs.error;
    case RunOutcomeKind.neutral:
      return cs.outlineVariant;
  }
}

/// A compact horizontal strip of one colored segment per run — newest last,
/// matching the order runs arrive. At-a-glance run-health history (AG-1).
class RunOutcomeStrip extends StatelessWidget {
  const RunOutcomeStrip(this.runs, {super.key, this.height = 6});

  final List<AgentRun> runs;
  final double height;

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    if (runs.isEmpty) return const SizedBox.shrink();
    return ClipRRect(
      borderRadius: BorderRadius.circular(3),
      child: Row(
        children: <Widget>[
          for (final AgentRun r in runs)
            Expanded(
              child: Tooltip(
                message: r.outcome == null || r.outcome!.isEmpty
                    ? r.title
                    : '${r.title} — ${r.outcome}',
                child: Container(
                  height: height,
                  margin: const EdgeInsets.symmetric(horizontal: 0.5),
                  color: outcomeColor(classifyOutcome(r.outcome), cs),
                ),
              ),
            ),
        ],
      ),
    );
  }
}
