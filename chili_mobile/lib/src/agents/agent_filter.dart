import 'agent.dart';

/// How the agent list is ordered (AGT-6). `defaultOrder` preserves the curated
/// seed grouping (trading → brain → coding → system) and is the default.
enum AgentSort { defaultOrder, name, status, kind }

extension AgentSortLabel on AgentSort {
  String get label {
    switch (this) {
      case AgentSort.defaultOrder:
        return 'Default';
      case AgentSort.name:
        return 'Name';
      case AgentSort.status:
        return 'Status';
      case AgentSort.kind:
        return 'Kind';
    }
  }
}

/// Case-insensitive substring match over name / description / id. An empty or
/// whitespace query returns the list unchanged. Pure + testable.
List<Agent> filterAgents(List<Agent> agents, String query) {
  final String q = query.trim().toLowerCase();
  if (q.isEmpty) return agents;
  return agents.where((Agent a) {
    return a.name.toLowerCase().contains(q) ||
        a.description.toLowerCase().contains(q) ||
        a.id.toLowerCase().contains(q);
  }).toList();
}

/// Stable sort by the chosen key. `status` puts active agents first
/// (running → unknown → idle → stopped → error... by a fixed rank), then name;
/// `kind` groups by category then name; `name` is plain alphabetical. Pure.
List<Agent> sortAgents(List<Agent> agents, AgentSort sort) {
  final List<Agent> out = List<Agent>.from(agents);
  int byName(Agent a, Agent b) =>
      a.name.toLowerCase().compareTo(b.name.toLowerCase());
  switch (sort) {
    case AgentSort.defaultOrder:
      break; // keep incoming (curated seed) order
    case AgentSort.name:
      out.sort(byName);
      break;
    case AgentSort.status:
      out.sort((Agent a, Agent b) {
        final int r = _statusRank(a.status).compareTo(_statusRank(b.status));
        return r != 0 ? r : byName(a, b);
      });
      break;
    case AgentSort.kind:
      out.sort((Agent a, Agent b) {
        final int r = a.kind.index.compareTo(b.kind.index);
        return r != 0 ? r : byName(a, b);
      });
      break;
  }
  return out;
}

/// Lower rank sorts first. Running on top; errors flagged just under it so
/// they're visible; stopped/idle/unknown after.
int _statusRank(AgentStatus s) {
  switch (s) {
    case AgentStatus.running:
      return 0;
    case AgentStatus.error:
      return 1;
    case AgentStatus.idle:
      return 2;
    case AgentStatus.unknown:
      return 3;
    case AgentStatus.stopped:
      return 4;
  }
}
