import 'package:flutter/foundation.dart';

import 'agent.dart';

/// In-memory, observable collection of [Agent]s. Pure logic (no I/O) so it is
/// unit-testable like [WorkspaceController]; the screen wires it to
/// [AgentPersistence] for save/restore. AGT-1 controls (start/stop/run) move
/// local state only — real backend wiring lands in AGT-2/AGT-3.
class AgentRegistry extends ChangeNotifier {
  final List<Agent> _agents;

  AgentRegistry({List<Agent>? seed})
      : _agents = List<Agent>.from(seed ?? defaultAgents());

  List<Agent> get agents => List<Agent>.unmodifiable(_agents);

  /// Agents of [kind], or all when [kind] is null.
  List<Agent> byKind(AgentKind? kind) => kind == null
      ? agents
      : _agents.where((Agent a) => a.kind == kind).toList();

  int get runningCount =>
      _agents.where((Agent a) => a.status == AgentStatus.running).length;

  Agent? byId(String id) {
    for (final Agent a in _agents) {
      if (a.id == id) return a;
    }
    return null;
  }

  /// Mark the agent running and stamp [lastRun]. Honours [Agent.canStart] and
  /// [Agent.enabled].
  void start(String id) {
    _mutate(id, (Agent a) {
      if (!a.canStart || !a.enabled) return a;
      return a.copyWith(status: AgentStatus.running, lastRun: _nowIso());
    });
  }

  void stop(String id) {
    _mutate(id, (Agent a) {
      if (!a.canStop) return a;
      return a.copyWith(status: AgentStatus.stopped);
    });
  }

  /// Trigger a single (local) run — records [lastRun]/[lastResult] without
  /// leaving the agent in a running state.
  void runOnce(String id) {
    _mutate(id, (Agent a) {
      if (!a.canRunOnce || !a.enabled) return a;
      return a.copyWith(lastRun: _nowIso(), lastResult: 'Ran once (local)');
    });
  }

  void setStatus(String id, AgentStatus status) =>
      _mutate(id, (Agent a) => a.copyWith(status: status));

  /// Apply a backend-derived live reading (AGT-2). No-ops when nothing changed
  /// so periodic polling doesn't churn listeners / storage.
  void applyLiveStatus(String id, AgentStatus status,
      {String? lastRun, String? lastResult}) {
    _mutate(id, (Agent a) {
      final String? nextRun = lastRun ?? a.lastRun;
      final String? nextResult = lastResult ?? a.lastResult;
      if (a.status == status &&
          a.lastRun == nextRun &&
          a.lastResult == nextResult) {
        return a; // unchanged → _mutate skips notify
      }
      return a.copyWith(
        status: status,
        lastRun: nextRun,
        lastResult: nextResult,
      );
    });
  }

  /// Toggle whether the agent may run. Disabling also stops it.
  void setEnabled(String id, bool enabled) {
    _mutate(id, (Agent a) {
      if (enabled) return a.copyWith(enabled: true);
      return a.copyWith(
        enabled: false,
        status: a.status == AgentStatus.running ? AgentStatus.stopped : a.status,
      );
    });
  }

  void setConfig(String id, String key, String value) {
    _mutate(id, (Agent a) {
      final Map<String, String> next = Map<String, String>.from(a.config);
      next[key] = value;
      return a.copyWith(config: next);
    });
  }

  /// Add a user-defined agent (AGT-4). No-op if the id already exists.
  void addCustom(Agent agent) {
    if (byId(agent.id) != null) return;
    _agents.add(agent.copyWith(builtin: false));
    notifyListeners();
  }

  /// Remove an agent. Built-in agents are protected.
  void remove(String id) {
    final int i = _agents.indexWhere((Agent a) => a.id == id);
    if (i < 0 || _agents[i].builtin) return;
    _agents.removeAt(i);
    notifyListeners();
  }

  List<Map<String, dynamic>> toJson() =>
      _agents.map((Agent a) => a.toJson()).toList();

  /// Overlay persisted user state onto the live seed: matching ids keep the
  /// fresh built-in definition (description/schedule) but adopt the saved
  /// status/enabled/config/last-run; unknown ids are restored as custom agents.
  void applySaved(List<Map<String, dynamic>> saved) {
    if (saved.isEmpty) return;
    for (final Map<String, dynamic> raw in saved) {
      final Agent restored = Agent.fromJson(raw);
      final int i = _agents.indexWhere((Agent a) => a.id == restored.id);
      if (i >= 0) {
        _agents[i] = _agents[i].copyWith(
          status: restored.status,
          enabled: restored.enabled,
          config: restored.config,
          lastRun: restored.lastRun,
          lastResult: restored.lastResult,
        );
      } else {
        _agents.add(restored.copyWith(builtin: false));
      }
    }
    notifyListeners();
  }

  void _mutate(String id, Agent Function(Agent) f) {
    final int i = _agents.indexWhere((Agent a) => a.id == id);
    if (i < 0) return;
    final Agent next = f(_agents[i]);
    if (identical(next, _agents[i])) return;
    _agents[i] = next;
    notifyListeners();
  }

  static String _nowIso() => DateTime.now().toIso8601String();
}

/// The seeded built-in fleet — a curated slice of CHILI's real autonomous
/// agents across trading, brain/neural, coding-autonomy and system/infra.
/// Names, cadences, config knobs and kill-switches mirror the backend
/// (app/services/trading_scheduler.py, scripts/brain_worker.py, the code-brain
/// loop). Status starts `unknown` — honest until AGT-2 reads real backend state.
List<Agent> defaultAgents() => <Agent>[
      // ── Trading ──────────────────────────────────────────────────────────
      const Agent(
        id: 'auto-trader',
        name: 'Auto-Trader',
        kind: AgentKind.trading,
        description:
            'Real-time entry decisions on breakout alerts; places live broker orders.',
        schedule: 'every 10s',
        config: <String, String>{
          'tick_interval_s': '10',
          'max_instances': '1',
          'tick_timeout_s': '15',
        },
        killSwitch: 'kill switch + circuit breaker',
      ),
      const Agent(
        id: 'position-monitor',
        name: 'Position Monitor',
        kind: AgentKind.trading,
        description:
            'Sweeps open positions for exits, cancels stuck orders, handles day-trade exits.',
        schedule: 'every 30s',
        config: <String, String>{'interval_s': '30'},
      ),
      const Agent(
        id: 'bracket-reconciler',
        name: 'Bracket Reconciler',
        kind: AgentKind.trading,
        description:
            'Verifies stop/target bracket orders against broker truth; heals orphaned stops.',
        schedule: 'every 60s',
        config: <String, String>{'mode': 'shadow', 'interval_s': '60'},
        killSwitch: 'brain_live_brackets_mode = off',
      ),
      const Agent(
        id: 'crypto-stop-monitor',
        name: 'Crypto Stop Monitor',
        kind: AgentKind.trading,
        description: '24/7 crypto stop-loss enforcement on open positions.',
        schedule: 'every 2min',
        config: <String, String>{'interval_s': '120'},
      ),
      const Agent(
        id: 'momentum-live-runner',
        name: 'Momentum Live Runner',
        kind: AgentKind.trading,
        description: 'Advances live Coinbase momentum automation sessions (real orders).',
        schedule: 'every 2min',
        enabled: false,
        config: <String, String>{'interval_min': '2'},
        killSwitch: 'chili_momentum_live_runner_enabled = false',
      ),
      const Agent(
        id: 'fast-path-scalper',
        name: 'Fast-Path Scalper',
        kind: AgentKind.trading,
        description:
            'Streams Coinbase WebSocket data for crypto scalping (1m bars + L2 depth).',
        schedule: 'continuous',
        enabled: false,
        config: <String, String>{'mode': 'paper'},
        killSwitch: 'CHILI_FAST_PATH_ENABLED = 0',
      ),
      // ── Brain / neural ───────────────────────────────────────────────────
      const Agent(
        id: 'learning-cycle',
        name: 'Learning Cycle',
        kind: AgentKind.brain,
        description:
            'Mines patterns, backtests candidates, auto-promotes/demotes on realized edge.',
        schedule: 'every 5min',
        config: <String, String>{'interval_min': '5'},
        killSwitch: 'TRADING_BRAIN_NEURAL_MESH_ENABLED = 0',
      ),
      const Agent(
        id: 'fast-backtest',
        name: 'Fast Backtest',
        kind: AgentKind.brain,
        description: 'Drains the backtest queue independently of the learning cycle.',
        schedule: 'every 60s',
        config: <String, String>{'interval_s': '60'},
      ),
      const Agent(
        id: 'neural-mesh',
        name: 'Neural Mesh',
        kind: AgentKind.brain,
        description:
            'Drains the neural-mesh activation queue (stop-eval, pattern-health signals).',
        schedule: 'every 30s',
        config: <String, String>{'interval_s': '30', 'max_events': '32'},
      ),
      const Agent(
        id: 'drift-monitor',
        name: 'Drift Monitor',
        kind: AgentKind.brain,
        description:
            'Daily realized-drift sweep; queues degraded patterns for recertification.',
        schedule: '5:30 AM PT daily',
        config: <String, String>{'mode': 'shadow', 'lookback_days': '30'},
      ),
      const Agent(
        id: 'macro-regime',
        name: 'Macro Regime',
        kind: AgentKind.brain,
        description:
            'Daily macro-regime snapshot (VIX, yield curve) for regime classification.',
        schedule: '6:30 AM PT daily',
        config: <String, String>{'mode': 'shadow'},
      ),
      // ── Coding autonomy ──────────────────────────────────────────────────
      const Agent(
        id: 'coding-autopilot',
        name: 'Coding Autopilot',
        kind: AgentKind.coding,
        description:
            'Routes code tasks through template → local model → premium LLM within budget.',
        schedule: 'every 30s',
        config: <String, String>{'mode': 'reactive', 'process_s': '30'},
        killSwitch: 'code_brain mode = paused',
      ),
      const Agent(
        id: 'task-watcher',
        name: 'Task Watcher',
        kind: AgentKind.coding,
        description:
            'Detects new ready tasks and validation failures; enqueues code-brain events.',
        schedule: 'every 30s',
        config: <String, String>{'watch_s': '30'},
        killSwitch: 'code_brain mode = paused',
      ),
      const Agent(
        id: 'template-miner',
        name: 'Template Miner',
        kind: AgentKind.coding,
        description:
            'Mines deterministic templates from past LLM calls to cut LLM dependency.',
        schedule: 'every 6h',
        config: <String, String>{'mine_hours': '6'},
      ),
      // ── System / infra ───────────────────────────────────────────────────
      const Agent(
        id: 'scheduler',
        name: 'Scheduler',
        kind: AgentKind.system,
        description:
            'Hosts all cron-driven jobs (daily scans, prescreen, retention, snapshots).',
        schedule: 'orchestrator',
        config: <String, String>{'role': 'all'},
      ),
      const Agent(
        id: 'broker-sync',
        name: 'Broker Sync',
        kind: AgentKind.system,
        description:
            'Reconciles broker orders/positions into the DB — the source of broker truth.',
        schedule: 'ET 8am–8pm, every 2min',
        config: <String, String>{'role': 'broker_sync_only'},
      ),
      const Agent(
        id: 'market-snapshot',
        name: 'Market Snapshots',
        kind: AgentKind.system,
        description:
            'Persists intraday + daily OHLCV snapshots for sizing and pattern mining.',
        schedule: 'every 15min',
        config: <String, String>{'interval_min': '15'},
        killSwitch: 'brain_market_snapshot_scheduler_enabled = false',
      ),
    ];
