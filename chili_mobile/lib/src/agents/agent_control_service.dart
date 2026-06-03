import '../network/chili_api_client.dart';

/// A control action a user can invoke on an agent.
enum AgentAction { start, stop, runOnce }

/// Agents whose Start/Stop/Run act on the REAL backend (AGT-3). Everything not
/// here keeps AGT-1 local-only behaviour (or AGT-2 read-only for live agents
/// without a control endpoint, e.g. momentum-live-runner, position-monitor).
const Set<String> controlBackedAgentIds = <String>{
  'coding-autopilot',
  'task-watcher',
  'learning-cycle',
  'auto-trader',
};

/// Of the control-backed agents, which support a one-shot "Run once".
const Set<String> runOnceBackedAgentIds = <String>{
  'learning-cycle',
};

/// Control-backed agents whose actions touch live trading — every action MUST
/// be confirmed by the user before it fires (never one-click).
const Set<String> tradingConfirmAgentIds = <String>{
  'auto-trader',
};

/// Maps (agentId, action) → the matching existing backend endpoint. Throws on
/// an unsupported pair or on a backend failure (caller surfaces it + re-polls).
class AgentControlService {
  AgentControlService(this._api);
  final ChiliApiClient _api;

  Future<void> invoke(String id, AgentAction action) async {
    switch (id) {
      case 'coding-autopilot':
      case 'task-watcher':
        // Code-brain mode: reactive = running, paused = halted.
        switch (action) {
          case AgentAction.start:
            return _api.setCodeBrainMode('reactive');
          case AgentAction.stop:
            return _api.setCodeBrainMode('paused');
          case AgentAction.runOnce:
            throw UnsupportedError('Run once is not supported for $id');
        }
      case 'learning-cycle':
        switch (action) {
          case AgentAction.start:
            return _api.setContextLearning(true);
          case AgentAction.stop:
            return _api.setContextLearning(false);
          case AgentAction.runOnce:
            return _api.triggerLearnCycle();
        }
      case 'auto-trader':
        switch (action) {
          case AgentAction.start:
            return _api.setAutotraderPaused(false); // resumes LIVE trading
          case AgentAction.stop:
            return _api.setAutotraderPaused(true);
          case AgentAction.runOnce:
            throw UnsupportedError('Run once is not supported for $id');
        }
      default:
        throw UnsupportedError('No backend control for agent "$id"');
    }
  }
}
