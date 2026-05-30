import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:desktop_drop/desktop_drop.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:pasteboard/pasteboard.dart';

import '../network/chili_api_client.dart';
import '../network/network_error_message.dart';
import '../screen/focus_controller.dart';
import '../screen/focus_target.dart';
import 'autonomy_run_presenter.dart';
import 'device_auth_store.dart';

/// Dispatch monitor: status, queue task, run history.
class BrainDispatchScreen extends StatefulWidget {
  const BrainDispatchScreen({super.key, required this.onOpenSettings});

  final VoidCallback onOpenSettings;

  @override
  State<BrainDispatchScreen> createState() => _BrainDispatchScreenState();
}

class _BrainDispatchScreenState extends State<BrainDispatchScreen>
    with SingleTickerProviderStateMixin {
  late TabController _tabs;
  final ChiliApiClient _api = ChiliApiClient();
  Timer? _statusTimer;

  bool _loadingBoot = true;
  bool _paired = false;

  Map<String, dynamic>? _status;
  String? _statusError;

  final TextEditingController _titleCtrl = TextEditingController();
  final TextEditingController _descCtrl = TextEditingController();
  // Phase E.2 — optional dynamic source for the Queue tab. The backend
  // resolver accepts: local Windows path, container path, GitHub URL,
  // USER/REPO shorthand, or a bare repo name.
  final TextEditingController _sourceCtrl = TextEditingController();
  List<Map<String, dynamic>> _projects = [];
  int? _projectId;
  bool _queueBusy = false;
  String? _queueError;

  List<Map<String, dynamic>> _runs = [];
  String? _runsError;
  bool _runsLoading = false;

  final TextEditingController _autopilotPromptCtrl = TextEditingController();
  final FocusNode _autopilotPromptFocusNode =
      FocusNode(debugLabel: 'autopilotPrompt');
  final ScrollController _autopilotChatScroll = ScrollController();
  final ScrollController _autonomyAgentBenchScroll = ScrollController();
  final FocusController _autopilotFocus = FocusController();
  final List<String> _autopilotPendingImages = [];
  static const _autopilotImageExts = {
    '.jpg',
    '.jpeg',
    '.png',
    '.gif',
    '.webp',
    '.bmp',
  };
  static const _autopilotMimeByExt = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
  };
  static const _autopilotMaxPendingImages = 10;
  static const _autopilotImagePreviewSize = 82.0;
  static const _autopilotBubbleMaxWidth = 720.0;
  static const _autopilotMessagePreviewLimit = 1400;
  static const _autopilotAgentPromptPreviewLimit = 900;
  static const _autopilotChatFollowThreshold = 96.0;
  static const _autopilotPastedImagePrefix = 'chili_autopilot_paste';
  static const _autopilotExecutionModePlanApproval =
      ChiliApiClient.projectAutonomyPlanApprovalMode;
  static const _autopilotStatusAwaitingApproval = 'awaiting_approval';
  static const _autopilotStatusAwaitingClarification = 'awaiting_clarification';
  static const _autopilotStatusChatting = 'chatting';
  static const _autopilotAgentStatusActive = 'active';
  static const _autopilotAgentStatusPaused = 'paused';
  static const _autopilotAgentStatusBlocked = 'blocked';
  static const _autopilotAgentTierMacro = 'macro';
  static const _autopilotAgentTierMicro = 'micro';
  static const _autopilotAgentTierSpecialist = 'specialist';
  static const _autopilotAgentBenchFilterAll = 'all';
  static const _autopilotAgentBenchFilterMacro = _autopilotAgentTierMacro;
  static const _autopilotAgentBenchFilterMicro = _autopilotAgentTierMicro;
  static const _autopilotAgentBenchFilterSpecialist =
      _autopilotAgentTierSpecialist;
  static const _autopilotAgentBenchFilterCodex = 'codex';
  static const _autopilotAgentBenchFilterActive = 'active';
  static const _autopilotAgentBenchFilterNeedsInput = 'needs_input';
  static const _autopilotAgentBenchFilters = [
    _autopilotAgentBenchFilterAll,
    _autopilotAgentBenchFilterMacro,
    _autopilotAgentBenchFilterMicro,
    _autopilotAgentBenchFilterSpecialist,
    _autopilotAgentBenchFilterCodex,
    _autopilotAgentBenchFilterActive,
    _autopilotAgentBenchFilterNeedsInput,
  ];
  static const _autopilotAgentBenchListMinHeight = 280.0;
  static const _autopilotAgentBenchListMaxHeight = 440.0;
  static const _autopilotAgentBenchViewportRatio = 0.36;
  static const _autopilotCodexSourceStatusActive = 'ACTIVE';
  static const _autopilotPermissionObserve = 'observe';
  static const _autopilotPermissionResearch = 'research';
  static const _autopilotPermissionPlan = 'plan';
  static const _autopilotPermissionWorktree = 'worktree';
  static const _autopilotPermissionMerge = 'merge';
  static const _autopilotArchiveReasonOperatorClear = 'operator_clear';
  static const _autopilotAttachmentKindImage = 'image';
  static const _autopilotArtifactPromptImage = 'prompt_image';
  static const _autopilotStartPlanLabel = 'Start plan';
  static const _autopilotAgentSourceCodexAutomation = 'codex_automation';
  static const _autopilotAgentSourceDesktopCustom = 'desktop_custom';
  static const _autopilotPromptFreshnessCurrent = 'current';
  static const _autopilotPromptFreshnessStale = 'stale';
  static const _autopilotPromptFreshnessCustom = 'custom_override';
  static const _autopilotPromptFreshnessMissingSource = 'missing_source';
  static const _autopilotPromptFreshnessMissingProfile = 'missing_profile';
  static const _autopilotModelPolicyLocalFirst = 'local_first';
  static const _autopilotModelPolicyLocalOnly = 'local_only';
  static const _autopilotModelPolicyCurrent = 'chili_coder_current';
  static const _autopilotModelPolicyValues = [
    _autopilotModelPolicyLocalFirst,
    _autopilotModelPolicyLocalOnly,
    _autopilotModelPolicyCurrent,
  ];
  static const _autopilotScheduleCadenceManual = 'manual';
  static const _autopilotScheduleCadenceTwoMinutes = 'two_minutes';
  static const _autopilotScheduleCadenceFiveMinutes = 'five_minutes';
  static const _autopilotScheduleCadenceTenMinutes = 'ten_minutes';
  static const _autopilotScheduleCadenceHourly = 'hourly';
  static const _autopilotScheduleCadenceAlwaysOn = 'always_on';
  static const _autopilotScheduleRuntimeModeKey = 'runtime_mode';
  static const _autopilotScheduleRestUntilKey = 'rest_until';
  static const _autopilotScheduleWorkStartedAtKey = 'work_started_at';
  static const _autopilotScheduleWorkWindowMinutesKey = 'work_window_minutes';
  static const _autopilotScheduleRestMinutesKey = 'rest_minutes';
  static const _autopilotSchedulerLastPollAt = 'last_poll_at';
  static const _autopilotSchedulerNextPollAt = 'next_poll_at';
  static const _autopilotSchedulerLastResult = 'last_result';
  static const _autopilotSchedulerLastError = 'last_error';
  static const _autopilotSchedulerActiveWorkers = 'active_workers';
  static const _autopilotSchedulerMaxWorkers = 'max_workers';
  static const _autopilotSchedulerWorkerStarted = 'worker_started';
  static const _autopilotSchedulerWorkerDeferredCount = 'worker_deferred_count';
  static const _autopilotSchedulerResultSource = 'source';
  static const _autopilotSchedulerResultSourceManual = 'manual_wake';
  static const _autopilotScheduleRrulePrefix = 'RRULE:';
  static const _autopilotScheduleDefaultWorkWindowMinutes = 4 * 60;
  static const _autopilotScheduleDefaultRestMinutes = 5;
  static const _autopilotScheduleRruleTwoMinutes = 'FREQ=MINUTELY;INTERVAL=2';
  static const _autopilotScheduleRruleFiveMinutes = 'FREQ=MINUTELY;INTERVAL=5';
  static const _autopilotScheduleRruleTenMinutes = 'FREQ=MINUTELY;INTERVAL=10';
  static const _autopilotScheduleRruleHourly = 'FREQ=HOURLY;INTERVAL=1';
  static const _autopilotScheduleDefaultMaxMinutes = 20;
  static const _autopilotScheduleDefaultMaxChildRuns = 0;
  static const _autopilotCodexParityPreviewLimit = 10;
  static const _autopilotTeamPreviewLimit = 6;
  static const _autopilotTeamChildPreviewLimit = 4;
  static const _autopilotContractPathTailSegments = 3;
  static const _autopilotContractDetailPreviewLimit = 3;
  static const _autopilotContractWorkspace = 'workspace';
  static const _autopilotContractInbox = 'inbox';
  static const _autopilotContractOutput = 'output';
  static const _autopilotContractState = 'state';
  static const _autopilotContractKeyCommands = 'key_commands';
  static const _autopilotContractSafetyBoundaries = 'safety_boundaries';
  static const _autopilotContractDDriveAligned = 'd_drive_aligned';
  static const _autopilotContractUsesMailboxProtocol = 'uses_mailbox_protocol';
  static const _autopilotContractUsesRunLock = 'uses_run_lock';
  static const _autopilotContractRequiresOutReport = 'requires_out_report';
  static const _autopilotContractUsesPrReviewFlow = 'uses_pr_review_flow';
  static const _autopilotQualityScorecard = 'quality_scorecard';
  static const _autopilotQualityArchitectReviews = 'architect_reviews';
  static const _autopilotQualityScheduled = 'scheduled_quality';
  static const _autopilotQualityValidation = 'validation';
  static const _autopilotQualityProblems = 'problems';
  static const _autopilotQualityDefaultWindowDays = 7;
  static const _autopilotRuntimeQueue = 'runtime_queue';
  static const _autopilotRuntimeQueueProblems = 'problems';
  static const _autopilotOperatorInbox = 'operator_inbox';
  static const _autopilotOperatorInboxItems = 'items';
  static const _autopilotOperatorInboxNextAction = 'next_action';
  static const _autopilotOperatorInboxNextActionLabel = 'next_action_label';
  static const _autopilotOperatorInboxNextActionDetail = 'next_action_detail';
  static const _autopilotOperatorInboxNextActionKind = 'next_action_kind';
  static const _autopilotOperatorInboxNextActionRunId = 'next_action_run_id';
  static const _autopilotOperatorInboxNextActionAgent = 'next_action_agent';
  static const _autopilotOperatorInboxKindApproval = 'approval';
  static const _autopilotOperatorInboxKindClarification = 'clarification';
  static const _autopilotOperatorInboxKindQuestion = 'question';
  static const _autopilotOperatorInboxKindBlocker = 'blocker';
  static const _autopilotOperatorInboxKindReply = 'user_reply';
  static const _autopilotOperatorInboxActionKeepMonitoring = 'keep_monitoring';
  static const _autopilotOperatorInboxActionAnswer = 'Answer';
  static const _autopilotOperatorInboxActionOpen = 'Open';
  static const _autopilotOperatorInboxActionReview = 'Review';
  static const _autopilotOperatorInboxOpenedPrompt =
      'Opened the agent chat. Answer in the composer.';
  static const _autopilotAgentOperatingState = 'operating_state';
  static const _autopilotAgentOperatingStateNeedsInput = 'needs_input';
  static const _autopilotAgentOperatingStateNeedsSync = 'needs_sync';
  static const _autopilotAgentOperatingStateCustomPrompt = 'custom_prompt';
  static const _autopilotAgentOperatingStateRunning = 'running';
  static const _autopilotAgentOperatingStatePausedSourceActive =
      'paused_source_active';
  static const _autopilotAgentOperatingStatePaused = 'paused';
  static const _autopilotAgentOperatingStateManualReady = 'manual_ready';
  static const _autopilotAgentOperatingStateScheduled = 'scheduled';
  static const _autopilotAgentOperatingStateReady = 'ready';
  static const _autopilotAgentOperatingSafetyPlanOnly = 'plan_only';
  static const _autopilotAgentOperatingSafetyPatchCapable = 'patch_capable';
  static const _autopilotAgentOperatingSafetyMergeCapable = 'merge_capable';
  static const _autopilotReadinessPassed = 'passed';
  static const _autopilotReadinessFailed = 'failed';
  static const _autopilotCodexBench = 'codex_bench';
  static const _autopilotAgentQualityMonitor = 'agent_quality_monitor';
  static const _autopilotAgentCapabilityAudit = 'agent_os_capability_audit';
  static const _autopilotAgentCapabilityItems = 'capabilities';
  static const _autopilotAgentCapabilityGaps = 'gaps';
  static const _autopilotAgentCapabilityNextActionLabel = 'next_action_label';
  static const _autopilotAgentCapabilityNextActionDetail = 'next_action_detail';
  static const _autopilotAgentCapabilityNextAction = 'next_action';
  static const _autopilotAgentCapabilityActionEnableAlwaysOn =
      'enable_always_on';
  static const _autopilotAgentQualityDimensions = 'dimensions';
  static const _autopilotAgentQualityNextActionLabel = 'next_action_label';
  static const _autopilotAgentQualityNextActionDetail = 'next_action_detail';
  static const _autopilotCodexAlignment = 'codex_alignment';
  static const _autopilotCodexAlignmentDimensions = 'dimensions';
  static const _autopilotCodexAlignmentGaps = 'gaps';
  static const _autopilotCodexAlignmentPassingScore = 85;
  static const _autopilotDefaultRepoPath = r'D:\dev\chili-home-copilot';
  static const _autopilotSlashHelp = '/help';
  static const _autopilotSlashStatus = '/status';
  static const _autopilotSlashAgents = '/agents';
  static const _autopilotSlashPlan = '/plan';
  static const _autopilotSlashModel = '/model';
  static const _autopilotSlashQuestions = '/questions';
  static const _autopilotSlashDoctor = '/doctor';
  static const _autopilotSlashQuality = '/quality';
  static const _autopilotSlashReference = '/reference ';
  static const _autopilotSlashScheduleCodex = '/schedule codex';
  static const _autopilotSlashScheduleCodexActive = '/schedule codex-active';
  static const _autopilotSlashScheduleCodexAlwaysOn =
      '/schedule codex-always-on';
  static const _autopilotSlashScheduleCodexPause = '/schedule codex-pause';
  static const _autopilotCommandQuickActions = [
    {
      'label': 'Help',
      'command': _autopilotSlashHelp,
      'tooltip': 'Show Autopilot chat commands',
    },
    {
      'label': 'Status',
      'command': _autopilotSlashStatus,
      'tooltip': 'Summarize this agent run',
    },
    {
      'label': 'Agents',
      'command': _autopilotSlashAgents,
      'tooltip': 'List repo agents',
    },
    {
      'label': 'Plan',
      'command': _autopilotSlashPlan,
      'tooltip': 'Start approval-first planning',
    },
    {
      'label': 'Model',
      'command': _autopilotSlashModel,
      'tooltip': 'Show model and prompt settings',
    },
    {
      'label': 'Doctor',
      'command': _autopilotSlashDoctor,
      'tooltip': 'Audit Agent OS readiness',
    },
    {
      'label': 'Quality',
      'command': _autopilotSlashQuality,
      'tooltip': 'Explain local-model guardrails',
    },
    {
      'label': 'Reference',
      'command': _autopilotSlashReference,
      'tooltip': 'Clean-room scan a local reference folder',
    },
    {
      'label': 'Codex schedules',
      'command': _autopilotSlashScheduleCodex,
      'tooltip': 'Show imported Codex schedule mirror',
    },
    {
      'label': 'Questions',
      'command': _autopilotSlashQuestions,
      'tooltip': 'Show pending operator questions',
    },
  ];
  static const _autopilotAgentCustomPromptLabel = 'Desktop custom';
  static const _autopilotAttachedImagePromptLabel =
      'Describe the attached image(s)';
  static const int _statusPollIntervalSeconds = 30;
  static const int _autopilotPollIntervalSeconds = 5;
  static const _autopilotSseTypeSnapshot = 'snapshot';
  static const _autopilotSseTypeEvents = 'events';
  static const _autopilotSseTypeState = 'state';
  static const _autopilotSseTypeComplete = 'complete';
  static const _autopilotEventListKeys = [
    'messages',
    'steps',
    'artifacts',
  ];
  List<Map<String, dynamic>> _codeRepos = [];
  int? _autonomyRepoId;
  int? _autonomyHistoryRepoId;
  int? _autonomyAgentProfileId;
  List<Map<String, dynamic>> _autonomyRuns = [];
  List<Map<String, dynamic>> _autonomyAgents = [];
  Map<String, dynamic> _autonomyAgentScheduler = {};
  Map<String, dynamic> _autonomyAgentReadiness = {};
  String _autonomyAgentBenchFilter = _autopilotAgentBenchFilterAll;
  Map<String, dynamic>? _activeAutonomyRun;
  bool _autonomyLoading = false;
  bool _autonomyAgentsLoading = false;
  bool _autonomyBusy = false;
  bool _autonomyShowArchived = false;
  bool _autonomyListInFlight = false;
  bool _autonomyRefreshInFlight = false;
  bool _statusRefreshInFlight = false;
  bool _autopilotDropActive = false;
  bool _autonomyDraftOpen = false;
  String? _lastAutopilotChatSignature;
  String? _autonomyError;
  String? _autonomyLiveSyncNotice;
  Timer? _autonomyTimer;
  StreamSubscription<Map<String, dynamic>>? _autonomyEventsSub;
  String? _autonomyEventsRunId;
  bool _autonomySseFallbackQueued = false;
  int _autonomySseRetryAttempt = 0;

  // Phase F — Context Brain tab state
  Map<String, dynamic>? _ctxStatus;
  List<Map<String, dynamic>> _ctxAssemblies = [];
  List<Map<String, dynamic>> _ctxSources = [];
  bool _ctxLoading = false;
  String? _ctxError;

  // History tab filter state
  bool _hideRouterEscalate = true; // hide noisy auto-escalations by default
  bool _onlyOperatorReview = false; // toggle to show ONLY notify_user=true
  int? _watchTaskId;

  final TextEditingController _pairEmailCtrl = TextEditingController();
  final TextEditingController _pairCodeCtrl = TextEditingController();
  final TextEditingController _pairLabelCtrl = TextEditingController();
  bool _pairBusy = false;
  String? _pairError;

  /// After a successful "Send code" request; mirrors chat.html pairing steps.
  bool _pairShowCodeStep = false;
  String? _pairCodeMessage;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 5, vsync: this);
    _tabs.addListener(_onTabChanged);
    unawaited(_boot());
  }

  void _onTabChanged() {
    if (_tabs.indexIsChanging) return;
    if (_tabs.index == 1) {
      unawaited(_loadAutonomyAgentProfiles());
      unawaited(_loadAutonomyAgentSchedulerStatus());
      unawaited(_loadAutonomyAgentReadiness());
      unawaited(_loadAutonomyRuns());
      _syncAutonomyEventStream();
    } else if (_tabs.index == 3) {
      _cancelAutonomyEventStream();
      unawaited(_loadRuns());
    } else if (_tabs.index == 4) {
      _cancelAutonomyEventStream();
      unawaited(_loadContextBrain());
    } else {
      _cancelAutonomyEventStream();
    }
  }

  Future<void> _loadContextBrain() async {
    if (!mounted) return;
    setState(() {
      _ctxLoading = true;
      _ctxError = null;
    });
    try {
      final status = await _api.getContextBrainStatus();
      final assemblies = await _api.getContextBrainAssemblies(limit: 25);
      final sources = await _api.getContextBrainSources();
      if (!mounted) return;
      setState(() {
        _ctxStatus = status;
        _ctxAssemblies = assemblies;
        _ctxSources = sources;
        _ctxLoading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _ctxError = '$e';
        _ctxLoading = false;
      });
    }
  }

  Future<void> _boot() async {
    await _api.initFromStore();
    final token = await DeviceAuthStore.getToken();
    if (!mounted) return;
    if (token == null || token.isEmpty) {
      setState(() {
        _paired = false;
        _loadingBoot = false;
      });
      return;
    }
    await _startPairedSession();
    if (mounted) setState(() => _loadingBoot = false);
  }

  Future<void> _startPairedSession() async {
    setState(() => _paired = true);
    await _loadProjectsPicker();
    await _loadCodeRepos();
    await _loadAutonomyAgentProfiles();
    await _loadAutonomyAgentSchedulerStatus();
    await _loadAutonomyAgentReadiness();
    await _refreshStatus();
    await _loadAutonomyRuns(silent: true);
    _statusTimer?.cancel();
    _statusTimer = Timer.periodic(
        const Duration(seconds: _statusPollIntervalSeconds), (_) {
      if (mounted && _paired) unawaited(_refreshStatus());
    });
    _autonomyTimer?.cancel();
    _autonomyTimer = Timer.periodic(
        const Duration(seconds: _autopilotPollIntervalSeconds), (_) {
      if (mounted && _paired && _tabs.index == 1) {
        unawaited(_refreshActiveAutonomyRun());
      }
    });
    _syncAutonomyEventStream();
  }

  Future<void> _requestPairCode() async {
    final email = _pairEmailCtrl.text.trim();
    if (email.isEmpty) {
      setState(() => _pairError = 'Enter your email.');
      return;
    }
    setState(() {
      _pairBusy = true;
      _pairError = null;
    });
    try {
      final data = await _api.pairRequest(email: email);
      if (!mounted) return;
      final ok = data['ok'] == true;
      if (!ok) {
        setState(() {
          _pairError = '${data['error'] ?? 'Request failed'}';
          _pairBusy = false;
        });
        return;
      }
      final devCode = data['dev_code']?.toString();
      final msg =
          data['message']?.toString() ?? 'Check your email for the code.';
      if (devCode != null && devCode.isNotEmpty) {
        _pairCodeCtrl.text = devCode;
      }
      setState(() {
        _pairShowCodeStep = true;
        _pairCodeMessage = msg;
        _pairBusy = false;
      });
    } catch (e) {
      if (mounted) {
        setState(() {
          _pairError = userVisibleNetworkError(e);
          _pairBusy = false;
        });
      }
    }
  }

  void _pairBackToEmail() {
    setState(() {
      _pairShowCodeStep = false;
      _pairCodeMessage = null;
      _pairError = null;
    });
  }

  Future<void> _verifyPairCode() async {
    final code = _pairCodeCtrl.text.trim();
    if (code.isEmpty) {
      setState(() => _pairError = 'Enter the code.');
      return;
    }
    setState(() {
      _pairBusy = true;
      _pairError = null;
    });
    try {
      final data = await _api.pairVerify(
        code: code,
        label: _pairLabelCtrl.text.trim().isEmpty
            ? 'CHILI Desktop Companion'
            : _pairLabelCtrl.text.trim(),
      );
      if (!mounted) return;
      if (data['ok'] != true) {
        setState(() {
          _pairError = '${data['error'] ?? 'Verification failed'}';
          _pairBusy = false;
        });
        return;
      }
      final token = data['token'] as String?;
      if (token == null || token.isEmpty) {
        setState(() {
          _pairError = 'Server did not return a token.';
          _pairBusy = false;
        });
        return;
      }
      await DeviceAuthStore.setToken(token);
      _api.token = token;
      _pairCodeCtrl.clear();
      setState(() {
        _pairBusy = false;
        _pairError = null;
        _pairShowCodeStep = false;
        _pairCodeMessage = null;
      });
      await _startPairedSession();
    } catch (e) {
      if (mounted) {
        setState(() {
          _pairError = userVisibleNetworkError(e);
          _pairBusy = false;
        });
      }
    }
  }

  Future<void> _loadProjectsPicker() async {
    try {
      final list = await _api.listProjects();
      final last = await DeviceAuthStore.getLastProjectId();
      int? pick;
      if (last != null) {
        final has = list.any((p) => p['id'] == last);
        if (has) pick = last;
      }
      pick ??= list.isNotEmpty ? (list.first['id'] as int?) : null;
      if (mounted) {
        setState(() {
          _projects = list;
          _projectId = pick;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() => _queueError = 'Could not load projects: $e');
      }
    }
  }

  Future<void> _loadCodeRepos() async {
    try {
      final list = await _api.getCodeBrainRepos();
      int? pick = _autonomyRepoId;
      if (pick != null && !list.any((repo) => _asInt(repo['id']) == pick)) {
        pick = null;
      }
      int? historyPick = _autonomyHistoryRepoId;
      if (historyPick != null &&
          !list.any((repo) => _asInt(repo['id']) == historyPick)) {
        historyPick = null;
      }
      pick ??= _preferredAutonomyRepoId(list);
      if (mounted) {
        setState(() {
          _codeRepos = list;
          _autonomyRepoId = pick;
          _autonomyHistoryRepoId = historyPick;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() => _autonomyError =
            'Could not load local repos: ${userVisibleNetworkError(e)}');
      }
    }
  }

  int? _preferredAutonomyRepoId(List<Map<String, dynamic>> repos) {
    int? firstReachable;
    for (final repo in repos) {
      final id = _asInt(repo['id']);
      if (id == null) continue;
      if (repo['preferred_for_autopilot'] == true) return id;
      if (repo['is_current_workspace'] == true) {
        firstReachable ??= id;
      }
      if (firstReachable == null &&
          repo['reachable_in_current_runtime'] == true) {
        firstReachable = id;
      }
    }
    return firstReachable ??
        (repos.isNotEmpty ? _asInt(repos.first['id']) : null);
  }

  int? get _autonomyAgentRepoId => _autonomyHistoryRepoId ?? _autonomyRepoId;

  Future<void> _loadAutonomyAgentProfiles({
    bool bootstrapIfEmpty = true,
  }) async {
    final repoId = _autonomyAgentRepoId;
    if (repoId == null) {
      if (mounted) {
        setState(() {
          _autonomyAgents = [];
          _autonomyAgentProfileId = null;
        });
      }
      return;
    }
    if (mounted) setState(() => _autonomyAgentsLoading = true);
    try {
      var agents = await _api.getProjectAutonomyAgentProfiles(repoId: repoId);
      if (agents.isEmpty && bootstrapIfEmpty) {
        agents =
            await _api.bootstrapProjectAutonomyAgentProfiles(repoId: repoId);
      }
      int? selectedAgentId = _autonomyAgentProfileId;
      if (selectedAgentId != null &&
          !agents.any((agent) => _asInt(agent['id']) == selectedAgentId)) {
        selectedAgentId = null;
      }
      if (!mounted) return;
      setState(() {
        _autonomyAgents = agents;
        _autonomyAgentProfileId = selectedAgentId;
        _autonomyAgentsLoading = false;
      });
      unawaited(_loadAutonomyAgentReadiness());
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _autonomyAgentsLoading = false;
        _autonomyError =
            'Could not load Autopilot agents: ${userVisibleNetworkError(e)}';
      });
    }
  }

  Future<void> _loadAutonomyAgentSchedulerStatus() async {
    try {
      final scheduler = await _api.getProjectAutonomyAgentScheduler();
      if (!mounted) return;
      setState(() => _autonomyAgentScheduler = scheduler);
    } catch (_) {
      if (!mounted) return;
      setState(() => _autonomyAgentScheduler = {});
    }
  }

  Future<void> _loadAutonomyAgentReadiness() async {
    final repoId = _autonomyAgentRepoId;
    if (repoId == null) {
      if (mounted) setState(() => _autonomyAgentReadiness = {});
      return;
    }
    try {
      final readiness =
          await _api.getProjectAutonomyAgentReadiness(repoId: repoId);
      if (!mounted) return;
      setState(() => _autonomyAgentReadiness = readiness);
    } catch (_) {
      if (!mounted) return;
      setState(() => _autonomyAgentReadiness = {});
    }
  }

  Future<void> _refreshStatus() async {
    if (_statusRefreshInFlight) return;
    _statusRefreshInFlight = true;
    try {
      final data = await _api.getDispatchStatus();
      if (mounted) {
        setState(() {
          _status = data;
          _statusError = null;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() => _statusError = '$e');
      }
    } finally {
      _statusRefreshInFlight = false;
    }
  }

  Future<void> _loadRuns() async {
    setState(() {
      _runsLoading = true;
      _runsError = null;
    });
    try {
      final list = await _api.getDispatchRuns(limit: 20, taskId: _watchTaskId);
      if (mounted) {
        setState(() {
          _runs = list;
          _runsLoading = false;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _runsError = '$e';
          _runsLoading = false;
        });
      }
    }
  }

  bool _autonomyTerminal(Map<String, dynamic>? run) {
    final status = run?['status']?.toString() ?? '';
    return const {'merged', 'completed', 'blocked', 'failed', 'cancelled'}
        .contains(status);
  }

  bool _runMatchesAutonomyHistoryRepo(Map<String, dynamic>? run) {
    final historyRepoId = _autonomyHistoryRepoId;
    if (historyRepoId == null) return true;
    return _asInt(run?['repo_id']) == historyRepoId;
  }

  bool _runMatchesAutonomyAgent(Map<String, dynamic>? run) {
    final agentId = _autonomyAgentProfileId;
    if (agentId == null) return true;
    return _asInt(run?['agent_profile_id']) == agentId;
  }

  bool _runMatchesAutonomyFilters(Map<String, dynamic>? run) {
    return _runMatchesAutonomyHistoryRepo(run) && _runMatchesAutonomyAgent(run);
  }

  bool _agentIsCodexAutomation(Map<String, dynamic> profile) {
    return _asMap(profile['prompt_setting'])['source'] ==
        _autopilotAgentSourceCodexAutomation;
  }

  bool _agentIsSourceActive(Map<String, dynamic> profile) {
    return (_asMap(profile['schedule'])['source_status']?.toString() ?? '')
            .toUpperCase() ==
        _autopilotCodexSourceStatusActive;
  }

  bool _agentNeedsOperatorInput(Map<String, dynamic> profile) {
    if ((_asInt(profile['pending_question_count']) ?? 0) > 0) return true;
    if (profile['status']?.toString() == _autopilotAgentStatusBlocked) {
      return true;
    }
    return _asMap(profile[_autopilotAgentOperatingState])['state']
            ?.toString() ==
        _autopilotAgentOperatingStateNeedsInput;
  }

  bool _agentIsOperationallyActive(Map<String, dynamic> profile) {
    if (profile['status']?.toString() == _autopilotAgentStatusActive) {
      return true;
    }
    if (profile['schedule_enabled'] == true) return true;
    return (_asInt(profile['open_run_count']) ?? 0) > 0;
  }

  bool _agentMatchesBenchFilter(Map<String, dynamic> profile, String filter) {
    switch (filter) {
      case _autopilotAgentBenchFilterMacro:
      case _autopilotAgentBenchFilterMicro:
      case _autopilotAgentBenchFilterSpecialist:
        return profile['tier']?.toString() == filter;
      case _autopilotAgentBenchFilterCodex:
        return _agentIsCodexAutomation(profile);
      case _autopilotAgentBenchFilterActive:
        return _agentIsOperationallyActive(profile);
      case _autopilotAgentBenchFilterNeedsInput:
        return _agentNeedsOperatorInput(profile);
      case _autopilotAgentBenchFilterAll:
      default:
        return true;
    }
  }

  List<Map<String, dynamic>> _filteredAutonomyBenchAgents() {
    final filter = _autonomyAgentBenchFilter;
    return [
      for (final agent in _autonomyAgents)
        if (_agentMatchesBenchFilter(agent, filter)) agent,
    ];
  }

  int _agentBenchFilterCount(String filter) {
    return _autonomyAgents
        .where((agent) => _agentMatchesBenchFilter(agent, filter))
        .length;
  }

  String _agentBenchFilterLabel(String filter) {
    switch (filter) {
      case _autopilotAgentBenchFilterMacro:
        return 'Macro';
      case _autopilotAgentBenchFilterMicro:
        return 'Micro';
      case _autopilotAgentBenchFilterSpecialist:
        return 'Specialists';
      case _autopilotAgentBenchFilterCodex:
        return 'Codex';
      case _autopilotAgentBenchFilterActive:
        return 'Active';
      case _autopilotAgentBenchFilterNeedsInput:
        return 'Needs input';
      case _autopilotAgentBenchFilterAll:
      default:
        return 'All';
    }
  }

  Color _agentBenchFilterColor(String filter) {
    switch (filter) {
      case _autopilotAgentBenchFilterMacro:
        return Colors.indigo;
      case _autopilotAgentBenchFilterMicro:
        return Colors.teal;
      case _autopilotAgentBenchFilterSpecialist:
        return Colors.deepPurple;
      case _autopilotAgentBenchFilterCodex:
        return Colors.cyan.shade800;
      case _autopilotAgentBenchFilterActive:
        return _autonomyStatusColor('running');
      case _autopilotAgentBenchFilterNeedsInput:
        return Colors.orange;
      case _autopilotAgentBenchFilterAll:
      default:
        return Theme.of(context).colorScheme.primary;
    }
  }

  String _repoDisplayName(int? repoId) {
    if (repoId == null) return 'All repos';
    for (final repo in _codeRepos) {
      if (_asInt(repo['id']) == repoId) {
        final name = repo['name']?.toString().trim();
        return name?.isNotEmpty == true ? name! : 'repo $repoId';
      }
    }
    return 'repo $repoId';
  }

  String _repoPathLabel(Map<String, dynamic> repo) {
    for (final key in [
      'resolved_path',
      'host_path',
      'path',
      'container_path'
    ]) {
      final value = repo[key]?.toString().trim() ?? '';
      if (value.isNotEmpty) return value;
    }
    return 'path unavailable';
  }

  String _contractPathTail(String raw) {
    final clean = raw.trim().replaceAll('\\', '/');
    if (clean.isEmpty) return '';
    final parts = clean.split('/').where((part) => part.trim().isNotEmpty);
    final tail = parts.length <= _autopilotContractPathTailSegments
        ? parts
        : parts.skip(parts.length - _autopilotContractPathTailSegments);
    return tail.join('/');
  }

  Map<String, dynamic>? _selectedAutonomyRepo() {
    final repoId = _autonomyRepoId;
    if (repoId == null) return null;
    for (final repo in _codeRepos) {
      if (_asInt(repo['id']) == repoId) return repo;
    }
    return null;
  }

  bool _isProtectedAutonomyRepo(Map<String, dynamic>? repo) {
    if (repo == null) return true;
    return repo['preferred_for_autopilot'] == true ||
        repo['is_current_workspace'] == true;
  }

  Map<String, dynamic> _agentProfileById(int? profileId) {
    if (profileId == null) return const <String, dynamic>{};
    for (final agent in _autonomyAgents) {
      if (_asInt(agent['id']) == profileId) return agent;
    }
    return const <String, dynamic>{};
  }

  Map<String, dynamic> _agentProfileForRun(Map<String, dynamic>? run) {
    final embedded = _asMap(run?['agent_profile']);
    if (embedded.isNotEmpty) return embedded;
    return _agentProfileById(_asInt(run?['agent_profile_id']));
  }

  Map<String, dynamic> _selectedAutonomyAgentProfile(
      [Map<String, dynamic>? run]) {
    final active = _agentProfileForRun(run);
    if (active.isNotEmpty) return active;
    return _agentProfileById(_autonomyAgentProfileId);
  }

  String _agentDisplayName(Map<String, dynamic> profile) {
    final name = profile['name']?.toString().trim() ?? '';
    if (name.isNotEmpty) return name;
    final role = profile['role']?.toString().trim() ?? '';
    if (role.isNotEmpty) return role;
    final key = profile['profile_key']?.toString().trim() ?? '';
    return key.isNotEmpty ? key : 'Autopilot agent';
  }

  String _agentRoleLabel(Map<String, dynamic> profile) {
    final role = profile['role']?.toString().trim() ?? '';
    if (role.isEmpty) return _agentDisplayName(profile);
    return role.replaceAll('_', ' ');
  }

  String _agentTierLabel(String tier) {
    switch (tier) {
      case _autopilotAgentTierMacro:
        return 'macro manager';
      case _autopilotAgentTierMicro:
        return 'micro IC';
      case _autopilotAgentTierSpecialist:
        return 'specialist';
      default:
        return tier.replaceAll('_', ' ');
    }
  }

  String _agentStatusLabel(String status) {
    switch (status) {
      case _autopilotAgentStatusActive:
        return 'active';
      case _autopilotAgentStatusPaused:
        return 'paused';
      case _autopilotAgentStatusBlocked:
        return 'needs input';
      default:
        return status.replaceAll('_', ' ');
    }
  }

  Color _agentStatusColor(String status) {
    switch (status) {
      case _autopilotAgentStatusActive:
        return _autonomyStatusColor('running');
      case _autopilotAgentStatusBlocked:
        return Colors.orange;
      case _autopilotAgentStatusPaused:
        return Colors.blueGrey;
      default:
        return Theme.of(context).colorScheme.primary;
    }
  }

  List<String> _agentEnabledPermissionLabels(Map<String, dynamic> profile) {
    final permissions = _asMap(profile['permissions']);
    const labels = {
      _autopilotPermissionObserve: 'observe',
      _autopilotPermissionResearch: 'research',
      _autopilotPermissionPlan: 'plan',
      _autopilotPermissionWorktree: 'patch',
      _autopilotPermissionMerge: 'merge',
    };
    return [
      for (final entry in labels.entries)
        if (permissions[entry.key] == true) entry.value,
    ];
  }

  String _agentScheduleSummary(Map<String, dynamic> profile) {
    if (profile['schedule_enabled'] != true) return 'schedule disabled';
    final schedule = _asMap(profile['schedule']);
    if (schedule[_autopilotScheduleRuntimeModeKey]?.toString() ==
        _autopilotScheduleCadenceAlwaysOn) {
      final restUntil =
          schedule[_autopilotScheduleRestUntilKey]?.toString().trim() ?? '';
      return restUntil.isEmpty ? 'always-on queue' : 'resting until $restUntil';
    }
    final cadence = schedule['cadence']?.toString().trim() ?? '';
    if (cadence.isNotEmpty) return _agentScheduleCadenceLabel(cadence);
    final rrule =
        _normalizeAgentScheduleRrule(schedule['rrule']?.toString() ?? '');
    if (rrule.isEmpty) return 'schedule enabled';
    return _agentScheduleRruleLabel(rrule);
  }

  String _agentScheduleSelection(Map<String, dynamic> profile) {
    if (profile['schedule_enabled'] != true) {
      return _autopilotScheduleCadenceManual;
    }
    final schedule = _asMap(profile['schedule']);
    if (schedule[_autopilotScheduleRuntimeModeKey]?.toString() ==
        _autopilotScheduleCadenceAlwaysOn) {
      return _autopilotScheduleCadenceAlwaysOn;
    }
    final cadence = schedule['cadence']?.toString().trim() ?? '';
    if (_agentScheduleCadenceValues.contains(cadence)) return cadence;
    final rrule =
        _normalizeAgentScheduleRrule(schedule['rrule']?.toString() ?? '');
    switch (rrule) {
      case _autopilotScheduleRruleTwoMinutes:
        return _autopilotScheduleCadenceTwoMinutes;
      case _autopilotScheduleRruleFiveMinutes:
        return _autopilotScheduleCadenceFiveMinutes;
      case _autopilotScheduleRruleTenMinutes:
        return _autopilotScheduleCadenceTenMinutes;
      case _autopilotScheduleRruleHourly:
        return _autopilotScheduleCadenceHourly;
      default:
        return _autopilotScheduleCadenceManual;
    }
  }

  List<String> get _agentScheduleCadenceValues => const [
        _autopilotScheduleCadenceManual,
        _autopilotScheduleCadenceTwoMinutes,
        _autopilotScheduleCadenceFiveMinutes,
        _autopilotScheduleCadenceTenMinutes,
        _autopilotScheduleCadenceHourly,
        _autopilotScheduleCadenceAlwaysOn,
      ];

  String _normalizeAgentScheduleRrule(String rrule) {
    final trimmed = rrule.trim();
    if (trimmed.toUpperCase().startsWith(_autopilotScheduleRrulePrefix)) {
      return trimmed.substring(_autopilotScheduleRrulePrefix.length).trim();
    }
    return trimmed;
  }

  String _agentScheduleCadenceLabel(String cadence) {
    switch (cadence) {
      case _autopilotScheduleCadenceManual:
        return 'manual';
      case _autopilotScheduleCadenceTwoMinutes:
        return 'every 2 min';
      case _autopilotScheduleCadenceFiveMinutes:
        return 'every 5 min';
      case _autopilotScheduleCadenceTenMinutes:
        return 'every 10 min';
      case _autopilotScheduleCadenceHourly:
        return 'hourly';
      case _autopilotScheduleCadenceAlwaysOn:
        return 'always-on queue';
      default:
        return cadence.replaceAll('_', ' ');
    }
  }

  String _agentScheduleRruleLabel(String rrule) {
    switch (_normalizeAgentScheduleRrule(rrule)) {
      case _autopilotScheduleRruleTwoMinutes:
        return _agentScheduleCadenceLabel(_autopilotScheduleCadenceTwoMinutes);
      case _autopilotScheduleRruleFiveMinutes:
        return _agentScheduleCadenceLabel(_autopilotScheduleCadenceFiveMinutes);
      case _autopilotScheduleRruleTenMinutes:
        return _agentScheduleCadenceLabel(_autopilotScheduleCadenceTenMinutes);
      case _autopilotScheduleRruleHourly:
        return _agentScheduleCadenceLabel(_autopilotScheduleCadenceHourly);
      default:
        return 'custom schedule';
    }
  }

  Map<String, dynamic> _agentScheduleBudget(Map<String, dynamic> profile) {
    final existing = _asMap(_asMap(profile['schedule'])['budget']);
    if (existing.isNotEmpty) return existing;
    return const {
      'max_minutes': _autopilotScheduleDefaultMaxMinutes,
      'max_child_runs': _autopilotScheduleDefaultMaxChildRuns,
    };
  }

  Map<String, dynamic> _agentSchedulePatch(
    Map<String, dynamic> profile,
    String cadence,
  ) {
    final budget = _agentScheduleBudget(profile);
    switch (cadence) {
      case _autopilotScheduleCadenceTwoMinutes:
        return {
          'cadence': cadence,
          'rrule': _autopilotScheduleRruleTwoMinutes,
          _autopilotScheduleRuntimeModeKey: 'scheduled',
          'budget': budget,
        };
      case _autopilotScheduleCadenceFiveMinutes:
        return {
          'cadence': cadence,
          'rrule': _autopilotScheduleRruleFiveMinutes,
          _autopilotScheduleRuntimeModeKey: 'scheduled',
          'budget': budget,
        };
      case _autopilotScheduleCadenceTenMinutes:
        return {
          'cadence': cadence,
          'rrule': _autopilotScheduleRruleTenMinutes,
          _autopilotScheduleRuntimeModeKey: 'scheduled',
          'budget': budget,
        };
      case _autopilotScheduleCadenceHourly:
        return {
          'cadence': cadence,
          'rrule': _autopilotScheduleRruleHourly,
          _autopilotScheduleRuntimeModeKey: 'scheduled',
          'budget': budget,
        };
      case _autopilotScheduleCadenceAlwaysOn:
        return {
          'cadence': cadence,
          'rrule': null,
          _autopilotScheduleRuntimeModeKey: _autopilotScheduleCadenceAlwaysOn,
          _autopilotScheduleWorkWindowMinutesKey:
              _autopilotScheduleDefaultWorkWindowMinutes,
          _autopilotScheduleRestMinutesKey:
              _autopilotScheduleDefaultRestMinutes,
          _autopilotScheduleWorkStartedAtKey: null,
          _autopilotScheduleRestUntilKey: null,
          'budget': budget,
        };
      default:
        return {
          'cadence': _autopilotScheduleCadenceManual,
          'rrule': null,
          _autopilotScheduleRuntimeModeKey: 'scheduled',
          'budget': budget,
        };
    }
  }

  String _agentSystemPromptPreview(String prompt) {
    final trimmed = prompt.trim();
    if (trimmed.length <= _autopilotAgentPromptPreviewLimit) return trimmed;
    return '${trimmed.substring(0, _autopilotAgentPromptPreviewLimit).trimRight()}...';
  }

  bool _isCodexAutomationAgent(Map<String, dynamic> promptSetting) {
    return promptSetting['source']?.toString() ==
        _autopilotAgentSourceCodexAutomation;
  }

  String _agentModelPolicyLabel(String policy) {
    switch (policy) {
      case _autopilotModelPolicyLocalOnly:
        return 'local only';
      case _autopilotModelPolicyCurrent:
        return 'current coder';
      case _autopilotModelPolicyLocalFirst:
        return 'local first';
      default:
        return policy.trim().isEmpty ? 'local first' : policy;
    }
  }

  String _agentPromptSourceLabel(Map<String, dynamic> promptSetting) {
    final source = promptSetting['source']?.toString().trim() ?? '';
    if (source == _autopilotAgentSourceCodexAutomation) {
      return 'Codex seed';
    }
    if (source == _autopilotAgentSourceDesktopCustom) {
      return _autopilotAgentCustomPromptLabel;
    }
    return source.isEmpty ? 'Generated prompt' : source;
  }

  String _agentPromptFreshnessLabel(Map<String, dynamic> profile) {
    final freshness = _asMap(profile['prompt_freshness']);
    final status = freshness['status']?.toString().trim() ?? '';
    switch (status) {
      case _autopilotPromptFreshnessCurrent:
        return 'prompt current';
      case _autopilotPromptFreshnessStale:
        return 'prompt stale';
      case _autopilotPromptFreshnessCustom:
        return 'custom override';
      case _autopilotPromptFreshnessMissingSource:
        return 'source missing';
      default:
        return '';
    }
  }

  Color _agentPromptFreshnessColor(Map<String, dynamic> profile) {
    final freshness = _asMap(profile['prompt_freshness']);
    switch (freshness['status']?.toString().trim() ?? '') {
      case _autopilotPromptFreshnessCurrent:
        return _autonomyStatusColor('completed');
      case _autopilotPromptFreshnessCustom:
        return Colors.indigo;
      case _autopilotPromptFreshnessStale:
      case _autopilotPromptFreshnessMissingSource:
        return Colors.orange;
      default:
        return Colors.blueGrey;
    }
  }

  bool _isAutopilotImageFile(String path) {
    final lower = path.toLowerCase().split('?').first;
    return _autopilotImageExts.any(lower.endsWith);
  }

  String _autopilotFileName(String path) {
    final normalized = path.replaceAll('\\', '/');
    final parts = normalized.split('/');
    return parts.isEmpty || parts.last.trim().isEmpty ? 'image' : parts.last;
  }

  void _addAutopilotPendingImage(String path) {
    if (path.trim().isEmpty || !_isAutopilotImageFile(path)) {
      setState(
          () => _autonomyError = 'Attach a PNG, JPG, GIF, WebP, or BMP image.');
      return;
    }
    if (_autopilotPendingImages.contains(path)) return;
    if (_autopilotPendingImages.length >= _autopilotMaxPendingImages) {
      setState(() => _autonomyError =
          'Autopilot supports up to $_autopilotMaxPendingImages images per message.');
      return;
    }
    setState(() {
      _autopilotPendingImages.add(path);
      _autonomyError = null;
    });
  }

  void _removeAutopilotPendingImage(int index) {
    if (index < 0 || index >= _autopilotPendingImages.length) return;
    setState(() => _autopilotPendingImages.removeAt(index));
  }

  Future<void> _pickAutopilotImages() async {
    final result = await FilePicker.platform.pickFiles(
      type: FileType.image,
      allowMultiple: true,
    );
    if (result == null) return;
    for (final file in result.files) {
      final path = file.path;
      if (path != null) _addAutopilotPendingImage(path);
    }
  }

  Future<void> _pasteAutopilotImage() async {
    try {
      final imageBytes = await Pasteboard.image;
      if (imageBytes == null || imageBytes.isEmpty) {
        setState(() => _autonomyError = 'Clipboard does not contain an image.');
        return;
      }
      final ts = DateTime.now().millisecondsSinceEpoch;
      final tempFile = File(
          '${Directory.systemTemp.path}/${_autopilotPastedImagePrefix}_$ts.png');
      await tempFile.writeAsBytes(imageBytes);
      _addAutopilotPendingImage(tempFile.path);
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    }
  }

  void _handleAutopilotImageDrop(DropDoneDetails details) {
    final seen = _autopilotPendingImages.toSet();
    final imagePaths = <String>[];
    var sawUnsupportedFile = false;
    for (final file in details.files) {
      final path = file.path.trim();
      if (path.isEmpty) continue;
      if (!_isAutopilotImageFile(path)) {
        sawUnsupportedFile = true;
        continue;
      }
      if (seen.add(path)) imagePaths.add(path);
    }

    final slots = _autopilotMaxPendingImages - _autopilotPendingImages.length;
    final accepted = slots <= 0 ? <String>[] : imagePaths.take(slots).toList();
    final hitLimit = imagePaths.length > accepted.length;
    setState(() {
      _autopilotDropActive = false;
      if (accepted.isNotEmpty) {
        _autopilotPendingImages.addAll(accepted);
      }
      if (accepted.isEmpty) {
        _autonomyError = sawUnsupportedFile
            ? 'Drop a PNG, JPG, GIF, WebP, or BMP image.'
            : null;
      } else if (hitLimit) {
        _autonomyError =
            'Added ${accepted.length} image(s). Autopilot supports up to $_autopilotMaxPendingImages images per message.';
      } else {
        _autonomyError = null;
      }
    });
  }

  List<Map<String, dynamic>> _autopilotAttachmentPayloads() {
    return [
      for (final path in _autopilotPendingImages)
        {
          'kind': _autopilotAttachmentKindImage,
          'path': path,
          'name': _autopilotFileName(path),
          'mime_type': _autopilotMimeType(path),
        },
    ];
  }

  String _autopilotMimeType(String path) {
    final lower = path.toLowerCase().split('?').first;
    for (final entry in _autopilotMimeByExt.entries) {
      if (lower.endsWith(entry.key)) return entry.value;
    }
    return _autopilotMimeByExt['.png']!;
  }

  String _autopilotChatSignature(Map<String, dynamic>? run) {
    if (run == null) return '';
    final runId = run['run_id']?.toString() ?? '';
    final updated = run['updated_at']?.toString() ?? '';
    final stage = run['current_stage']?.toString() ?? '';
    final status = run['status']?.toString() ?? '';
    final messageCount = _asMapList(run['messages']).length;
    final artifactCount = _asMapList(run['artifacts']).length;
    return '$runId|$status|$stage|$updated|$messageCount|$artifactCount';
  }

  bool _autopilotChatIsNearBottom() {
    if (!_autopilotChatScroll.hasClients) return true;
    final position = _autopilotChatScroll.position;
    final distance = position.maxScrollExtent - position.pixels;
    return distance <= _autopilotChatFollowThreshold;
  }

  void _scheduleAutopilotChatFollow({
    required String signature,
    bool force = false,
    bool wasNearBottom = true,
  }) {
    if (!force && !wasNearBottom) {
      _lastAutopilotChatSignature = signature;
      return;
    }
    if (_lastAutopilotChatSignature == signature && !force) return;
    _lastAutopilotChatSignature = signature;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_autopilotChatScroll.hasClients) return;
      final max = _autopilotChatScroll.position.maxScrollExtent;
      _autopilotChatScroll.animateTo(
        max,
        duration: const Duration(milliseconds: 180),
        curve: Curves.easeOutCubic,
      );
    });
  }

  void _syncAutonomyRunListState(Map<String, dynamic>? run) {
    if (run == null) return;
    final runId = run['run_id']?.toString() ?? '';
    if (runId.isEmpty) return;
    final index =
        _autonomyRuns.indexWhere((item) => item['run_id']?.toString() == runId);
    if (!_runMatchesAutonomyFilters(run) ||
        (run['archived'] == true && !_autonomyShowArchived)) {
      if (index >= 0) {
        final updated = List<Map<String, dynamic>>.from(_autonomyRuns)
          ..removeAt(index);
        _autonomyRuns = updated;
      }
      return;
    }
    if (index < 0) {
      _autonomyRuns = [run, ..._autonomyRuns];
      return;
    }
    final updated = List<Map<String, dynamic>>.from(_autonomyRuns);
    updated[index] = {...updated[index], ...run};
    _autonomyRuns = updated;
  }

  bool _autonomyLiveStreamEligible(Map<String, dynamic>? run) {
    if (!_paired || _tabs.index != 1 || run == null) return false;
    final runId = run['run_id']?.toString() ?? '';
    return runId.isNotEmpty && !_autonomyTerminal(run);
  }

  void _syncAutonomyEventStream() {
    if (!mounted || !_autonomyLiveStreamEligible(_activeAutonomyRun)) {
      _cancelAutonomyEventStream();
      return;
    }
    final runId = _activeAutonomyRun?['run_id']?.toString() ?? '';
    if (runId.isEmpty || _autonomyEventsRunId == runId) return;
    if (_autonomyEventsSub != null || _autonomyEventsRunId != null) {
      _cancelAutonomyEventStream();
    }
    _autonomyEventsRunId = runId;
    late final StreamSubscription<Map<String, dynamic>> sub;
    sub = _api.streamProjectAutonomyEvents(runId).listen(
      _handleAutonomyStreamEvent,
      onError: (Object error) {
        if (_autonomyEventsRunId != runId || _autonomyEventsSub != sub) return;
        if (error is ProjectAutonomySseException && !error.retriable) {
          _autonomyEventsSub = null;
          _autonomyEventsRunId = null;
          _autonomySseRetryAttempt = 0;
          if (mounted) {
            setState(() {
              _autonomyError = userVisibleNetworkError(error);
              _autonomyLiveSyncNotice = null;
            });
          }
          return;
        }
        _queueAutonomySseFallbackRefresh(error);
      },
      onDone: () {
        if (_autonomyEventsRunId != runId || _autonomyEventsSub != sub) return;
        _autonomyEventsSub = null;
        _autonomyEventsRunId = null;
        if (_autonomyLiveStreamEligible(_activeAutonomyRun)) {
          _queueAutonomySseFallbackRefresh(
            StateError('Autopilot live stream closed before completion.'),
          );
        }
      },
      cancelOnError: true,
    );
    _autonomyEventsSub = sub;
  }

  void _cancelAutonomyEventStream() {
    final sub = _autonomyEventsSub;
    _autonomyEventsSub = null;
    _autonomyEventsRunId = null;
    if (sub != null) unawaited(sub.cancel());
  }

  void _handleAutonomyStreamEvent(Map<String, dynamic> event) {
    if (!mounted) return;
    final run = _asMap(event['run']);
    final type = event['type']?.toString() ?? '';
    if (run.isNotEmpty &&
        {
          _autopilotSseTypeSnapshot,
          _autopilotSseTypeEvents,
          _autopilotSseTypeState,
          _autopilotSseTypeComplete,
        }.contains(type)) {
      _applyAutonomyLiveRun(run,
          event: event, forceFollow: type == _autopilotSseTypeSnapshot);
    } else if (type == _autopilotSseTypeEvents) {
      unawaited(_refreshActiveAutonomyRun(silent: true, force: true));
    }
    if (event['done'] == true || type == _autopilotSseTypeComplete) {
      _cancelAutonomyEventStream();
    }
  }

  void _applyAutonomyLiveRun(
    Map<String, dynamic> run, {
    Map<String, dynamic>? event,
    bool forceFollow = false,
  }) {
    final incomingRunId = run['run_id']?.toString() ?? '';
    final activeRunId = _activeAutonomyRun?['run_id']?.toString() ?? '';
    final streamRunId = _autonomyEventsRunId ?? '';
    if (incomingRunId.isEmpty) return;
    if (activeRunId.isEmpty && streamRunId != incomingRunId) return;
    if (activeRunId.isNotEmpty && activeRunId != incomingRunId) return;
    final wasNearBottom = _autopilotChatIsNearBottom();
    final merged = _mergeAutonomyLiveRun(_activeAutonomyRun, run, event);
    final signature = _autopilotChatSignature(merged);
    setState(() {
      _activeAutonomyRun = merged;
      _autonomyDraftOpen = false;
      _syncAutonomyRunListState(merged);
      _autonomyError = null;
      _autonomyLiveSyncNotice = null;
      _autonomySseRetryAttempt = 0;
    });
    _scheduleAutopilotChatFollow(
      signature: signature,
      force: forceFollow || activeRunId != incomingRunId,
      wasNearBottom: wasNearBottom,
    );
    _syncAutonomyEventStream();
  }

  Map<String, dynamic> _mergeAutonomyLiveRun(
    Map<String, dynamic>? existing,
    Map<String, dynamic> incoming,
    Map<String, dynamic>? event,
  ) {
    final merged = <String, dynamic>{
      ...?existing,
      ...incoming,
    };
    final deltas = event ?? const <String, dynamic>{};
    for (final key in _autopilotEventListKeys) {
      if (!deltas.containsKey(key)) continue;
      merged[key] = _mergeAutonomyEventItems(merged[key], deltas[key]);
    }
    return merged;
  }

  List<Map<String, dynamic>> _mergeAutonomyEventItems(
    dynamic existing,
    dynamic incoming,
  ) {
    final merged = _asMapList(existing);
    final indexById = <String, int>{};
    for (var index = 0; index < merged.length; index += 1) {
      final id = merged[index]['id']?.toString() ?? '';
      if (id.isNotEmpty) indexById[id] = index;
    }
    for (final item in _asMapList(incoming)) {
      final id = item['id']?.toString() ?? '';
      if (id.isEmpty) {
        merged.add(item);
      } else if (!indexById.containsKey(id)) {
        indexById[id] = merged.length;
        merged.add(item);
      } else {
        merged[indexById[id]!] = {
          ...merged[indexById[id]!],
          ...item,
        };
      }
    }
    return merged;
  }

  void _queueAutonomySseFallbackRefresh(Object _) {
    if (!mounted || _autonomySseFallbackQueued) return;
    _autonomySseFallbackQueued = true;
    _autonomyEventsSub = null;
    _autonomyEventsRunId = null;
    if (ProjectAutonomySseRetryPolicy.shouldShowDegradedNotice(
      _autonomySseRetryAttempt,
    )) {
      setState(() {
        _autonomyLiveSyncNotice =
            'Live Autopilot updates are reconnecting. This run may be a few seconds behind.';
      });
    }
    final delay =
        ProjectAutonomySseRetryPolicy.delayForAttempt(_autonomySseRetryAttempt);
    _autonomySseRetryAttempt += 1;
    unawaited(Future<void>.delayed(delay, () async {
      _autonomySseFallbackQueued = false;
      if (!mounted) return;
      await _refreshActiveAutonomyRun(silent: true, force: true);
      _syncAutonomyEventStream();
    }));
  }

  Future<void> _retryAutonomyLiveSync() async {
    if (_autonomyBusy) return;
    setState(() {
      _autonomyError = null;
      _autonomyLiveSyncNotice = null;
      _autonomySseRetryAttempt = 0;
    });
    await _loadCodeRepos();
    await _loadAutonomyAgentProfiles();
    await _loadAutonomyAgentSchedulerStatus();
    await _loadAutonomyRuns();
    await _refreshActiveAutonomyRun(silent: false, force: true);
    _syncAutonomyEventStream();
  }

  void _startNewAutonomyDraft() {
    _resetAutonomyDraft();
  }

  void _resetAutonomyDraft({String prompt = '', int? repoId}) {
    _cancelAutonomyEventStream();
    _autopilotPromptCtrl.text = prompt;
    _autopilotPromptCtrl.selection = TextSelection.collapsed(
      offset: _autopilotPromptCtrl.text.length,
    );
    setState(() {
      _activeAutonomyRun = null;
      _autonomyDraftOpen = true;
      _autopilotPendingImages.clear();
      if (repoId != null) _autonomyRepoId = repoId;
      _autonomyError = null;
      _autonomyLiveSyncNotice = null;
      _autonomySseRetryAttempt = 0;
      _autonomySseFallbackQueued = false;
      _lastAutopilotChatSignature = null;
    });
  }

  Future<void> _loadAutonomyRuns({bool silent = false}) async {
    if (!mounted) return;
    if (_autonomyListInFlight) return;
    _autonomyListInFlight = true;
    final requestedActiveId = _activeAutonomyRun?['run_id']?.toString();
    final requestedDraftOpen = _autonomyDraftOpen;
    if (!silent) {
      setState(() {
        _autonomyLoading = true;
        _autonomyError = null;
      });
    }
    try {
      final list = await _api.getProjectAutonomyRuns(
        limit: 20,
        repoId: _autonomyHistoryRepoId,
        agentProfileId: _autonomyAgentProfileId,
        includeArchived: _autonomyShowArchived,
      );
      Map<String, dynamic>? active = _activeAutonomyRun;
      final activeId = active?['run_id']?.toString();
      final selectionChangedWhileLoading = requestedActiveId != activeId;
      final draftStillOpen =
          requestedDraftOpen && _autonomyDraftOpen && activeId == null;
      if (list.isNotEmpty) {
        Map<String, dynamic>? matchingActive;
        for (final item in list) {
          if (item['run_id']?.toString() == activeId) {
            matchingActive = item;
            break;
          }
        }
        if (matchingActive != null) {
          active = {
            ...matchingActive,
            ...?active,
            'status': matchingActive['status'],
            'current_stage': matchingActive['current_stage'],
            'plan_status': matchingActive['plan_status'],
            'merge_status': matchingActive['merge_status'],
            'updated_at': matchingActive['updated_at'],
          };
        } else if (!selectionChangedWhileLoading && !draftStillOpen) {
          active = list.first;
        }
      } else if (!selectionChangedWhileLoading &&
          !draftStillOpen &&
          (!_runMatchesAutonomyFilters(active) ||
              (active?['archived'] == true && !_autonomyShowArchived))) {
        active = null;
      }
      if (!mounted) return;
      setState(() {
        _autonomyRuns = list;
        _activeAutonomyRun = active;
        if (active != null) _autonomyDraftOpen = false;
        _autonomyLoading = false;
        _autonomyError = null;
        _autonomyLiveSyncNotice = null;
      });
      await _refreshActiveAutonomyRun(silent: true, force: true);
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _autonomyError = userVisibleNetworkError(e);
        _autonomyLoading = false;
      });
    } finally {
      _autonomyListInFlight = false;
    }
  }

  Future<void> _refreshActiveAutonomyRun({
    bool silent = true,
    bool force = false,
  }) async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    if (_autonomyTerminal(_activeAutonomyRun) && silent && !force) {
      _cancelAutonomyEventStream();
      return;
    }
    if (_autonomyRefreshInFlight) return;
    _autonomyRefreshInFlight = true;
    try {
      final run = await _api.getProjectAutonomyRun(runId);
      if (!mounted) return;
      final oldRunId = _activeAutonomyRun?['run_id']?.toString();
      if (oldRunId != runId) return;
      final newRunId = run['run_id']?.toString();
      final follow = force ||
          oldRunId != newRunId ||
          _lastAutopilotChatSignature == null ||
          _autopilotChatIsNearBottom();
      final signature = _autopilotChatSignature(run);
      setState(() {
        _activeAutonomyRun = run;
        _autonomyDraftOpen = false;
        _syncAutonomyRunListState(run);
        _autonomyError = null;
        _autonomyLiveSyncNotice = null;
        _autonomySseRetryAttempt = 0;
      });
      _scheduleAutopilotChatFollow(
        signature: signature,
        force: force || oldRunId != newRunId,
        wasNearBottom: follow,
      );
      _syncAutonomyEventStream();
    } catch (e) {
      if (!mounted || silent) return;
      setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      _autonomyRefreshInFlight = false;
    }
  }

  Future<void> _openAutonomyRunById(String runId) async {
    final id = runId.trim();
    if (id.isEmpty || _autonomyBusy) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.getProjectAutonomyRun(id);
      if (!mounted) return;
      final signature = _autopilotChatSignature(run);
      setState(() {
        _activeAutonomyRun = run;
        _autonomyDraftOpen = false;
        _syncAutonomyRunListState(run);
        _autonomyError = null;
      });
      _scheduleAutopilotChatFollow(signature: signature, force: true);
      _syncAutonomyEventStream();
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _openAutonomyInboxItem(Map<String, dynamic> item) async {
    final runId = item['run_id']?.toString().trim() ?? '';
    if (runId.isEmpty) return;
    final kind = item['kind']?.toString() ?? '';
    await _openAutonomyRunById(runId);
    if (!mounted) return;
    if ((_activeAutonomyRun?['run_id']?.toString() ?? '') != runId) return;
    final needsAnswer = kind == _autopilotOperatorInboxKindQuestion ||
        kind == _autopilotOperatorInboxKindClarification ||
        kind == _autopilotOperatorInboxKindReply;
    if (needsAnswer) {
      _focusAutopilotComposer();
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(_autopilotOperatorInboxOpenedPrompt),
        ),
      );
    }
  }

  void _focusAutopilotComposer() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _autopilotPromptFocusNode.requestFocus();
    });
  }

  Future<void> _startAutopilot({
    String? promptOverride,
    bool clearComposer = true,
  }) async {
    final prompt = (promptOverride ?? _autopilotPromptCtrl.text).trim();
    final attachments = promptOverride == null
        ? _autopilotAttachmentPayloads()
        : const <Map<String, dynamic>>[];
    if (prompt.isEmpty && attachments.isEmpty) {
      setState(() => _autonomyError =
          'Enter a message or attach an image for Project Autopilot.');
      return;
    }
    final runId = _activeAutonomyRun?['run_id']?.toString() ?? '';
    final canContinueChat = runId.isNotEmpty &&
        !_autonomyTerminal(_activeAutonomyRun) &&
        _activeAutonomyRun?['status']?.toString() != 'merged';
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = canContinueChat
          ? await _api.sendProjectAutonomyMessage(
              runId: runId,
              content: prompt,
              attachments: attachments,
            )
          : await _api.createProjectAutonomyRun(
              prompt: prompt,
              repoId: _autonomyRepoId,
              agentProfileId: _autonomyAgentProfileId,
              executionMode: _autopilotExecutionModePlanApproval,
              startPlanning: false,
              attachments: attachments,
            );
      if (!mounted) return;
      setState(() {
        _activeAutonomyRun = run;
        _autonomyDraftOpen = false;
        _syncAutonomyRunListState(run);
        if (clearComposer) {
          _autopilotPromptCtrl.clear();
          _autopilotPendingImages.clear();
        }
      });
      await _loadAutonomyRuns(silent: true);
      await _refreshActiveAutonomyRun(silent: true, force: true);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(canContinueChat
                ? 'Message sent to ${run['run_id']}'
                : 'Autopilot chat started: ${run['run_id']}'),
          ),
        );
      }
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  void _submitAutopilotComposer() {
    if (_autonomyBusy || _codeRepos.isEmpty) return;
    if (_autopilotPromptCtrl.text.trim().isEmpty &&
        _autopilotPendingImages.isEmpty) {
      return;
    }
    unawaited(_startAutopilot());
  }

  void _submitAutopilotCommand(String command) {
    if (_autonomyBusy || _codeRepos.isEmpty || !_canSendAutopilotCommand) {
      return;
    }
    unawaited(_startAutopilot(
      promptOverride: command,
      clearComposer: false,
    ));
  }

  bool get _canSendAutopilotCommand {
    final runId = _activeAutonomyRun?['run_id']?.toString() ?? '';
    return runId.isNotEmpty &&
        !_autonomyTerminal(_activeAutonomyRun) &&
        _activeAutonomyRun?['status']?.toString() != 'merged';
  }

  TextEditingValue _formatAutopilotComposerInput(
    TextEditingValue oldValue,
    TextEditingValue newValue,
  ) {
    final oldBreaks = '\n'.allMatches(oldValue.text).length;
    final newBreaks = '\n'.allMatches(newValue.text).length;
    final enterPressed = HardwareKeyboard.instance
            .isLogicalKeyPressed(LogicalKeyboardKey.enter) ||
        HardwareKeyboard.instance
            .isLogicalKeyPressed(LogicalKeyboardKey.numpadEnter);
    if (newBreaks > oldBreaks &&
        enterPressed &&
        !HardwareKeyboard.instance.isShiftPressed) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _submitAutopilotComposer();
      });
      return oldValue;
    }
    return newValue;
  }

  Future<void> _approveAutopilotPlan() async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.approveProjectAutonomyPlan(runId);
      if (mounted) {
        setState(() {
          _activeAutonomyRun = run;
          _autonomyDraftOpen = false;
          _syncAutonomyRunListState(run);
        });
      }
      await _loadAutonomyRuns(silent: true);
      await _refreshActiveAutonomyRun(silent: true, force: true);
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _startAutopilotPlan() async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.startProjectAutonomyPlan(runId);
      if (mounted) {
        setState(() {
          _activeAutonomyRun = run;
          _autonomyDraftOpen = false;
          _syncAutonomyRunListState(run);
        });
      }
      await _loadAutonomyRuns(silent: true);
      await _refreshActiveAutonomyRun(silent: true, force: true);
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _attachAutopilotScreenshot() async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      _autopilotFocus.start(const FocusTarget.fullScreen());
      final path = await _autopilotFocus.captureNow();
      _autopilotFocus.stop(deleteLastFile: path == null);
      final run = await _api.recordProjectAutonomyVisualValidation(
        runId: runId,
        kind: 'screenshot',
        path: path,
        note: 'Desktop screenshot captured from the Autopilot cockpit.',
      );
      if (mounted) {
        setState(() {
          _activeAutonomyRun = run;
          _autonomyDraftOpen = false;
          _syncAutonomyRunListState(run);
        });
        _syncAutonomyEventStream();
      }
    } catch (e) {
      _autopilotFocus.stop();
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _requestAutopilotVideoValidation() async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.recordProjectAutonomyVisualValidation(
        runId: runId,
        kind: 'video',
        note: 'Video validation requested from the desktop cockpit.',
      );
      if (mounted) {
        setState(() {
          _activeAutonomyRun = run;
          _autonomyDraftOpen = false;
          _syncAutonomyRunListState(run);
        });
        _syncAutonomyEventStream();
      }
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  void _prefillAutopilotRerun(Map<String, dynamic> run) {
    final prompt = run['prompt']?.toString() ?? '';
    final repoId = _asInt(run['repo_id']);
    _resetAutonomyDraft(prompt: prompt, repoId: repoId);
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(
        content: Text(
          'Prompt restored in a fresh draft. Press Start to run it safely.',
        ),
      ),
    );
  }

  Future<void> _cancelAutopilot() async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.cancelProjectAutonomyRun(runId);
      if (mounted) {
        setState(() {
          _activeAutonomyRun = run;
          _autonomyDraftOpen = false;
          _syncAutonomyRunListState(run);
        });
      }
      await _refreshActiveAutonomyRun(silent: true, force: true);
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _mergeAutopilot() async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.mergeProjectAutonomyRun(runId);
      if (mounted) {
        setState(() {
          _activeAutonomyRun = run;
          _autonomyDraftOpen = false;
          _syncAutonomyRunListState(run);
        });
        _syncAutonomyEventStream();
      }
      await _loadAutonomyRuns(silent: true);
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _copyAutopilotRunSummary(Map<String, dynamic> run) async {
    final summary = _autopilotRunSummary(run);
    await Clipboard.setData(ClipboardData(text: summary));
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Autopilot summary copied.')),
    );
  }

  String _autopilotRunSummary(Map<String, dynamic> run) {
    final runId = run['run_id']?.toString() ?? 'Autopilot run';
    final status = _autonomyStatusLabel(run['status']?.toString() ?? '');
    final stage = _autonomyStageLabel(run['current_stage']?.toString() ?? '');
    final merge =
        _autonomyMergeStatusLabel(run['merge_status']?.toString() ?? '');
    final prompt = run['prompt']?.toString().trim() ?? '';
    final planBody = AutonomyRunPresenter.planBody(_asMap(run['plan']));
    final reviewBody = AutonomyRunPresenter.architectReviewBody(
        _asMap(run['architect_review']));
    final validation = AutonomyRunPresenter.validationBody(
      _asMapList(run['validation']),
    );
    final files = _asStringList(run['files']);
    final branch = run['integration_branch']?.toString() ?? '';
    final worktree = run['worktree_path']?.toString() ?? '';
    final issue = AutonomyRunPresenter.blockedRunMessage(run);
    final blocked = {'blocked', 'failed', 'cancelled'}
        .contains(run['status']?.toString() ?? '');
    final lines = <String>[
      runId,
      'Status: $status',
      if (stage.trim().isNotEmpty) 'Stage: $stage',
      if (merge.trim().isNotEmpty && merge != 'merge unknown') 'Merge: $merge',
      if (prompt.isNotEmpty) 'Prompt: $prompt',
      if (planBody.isNotEmpty) 'Plan:\n$planBody',
      if (reviewBody.isNotEmpty) 'Architect quality:\n$reviewBody',
      if (files.isNotEmpty) 'Files: ${files.take(8).join(', ')}',
      if (validation.isNotEmpty) 'Validation:\n$validation',
      if (branch.isNotEmpty) 'Branch: $branch',
      if (worktree.isNotEmpty) 'Worktree: $worktree',
      if (blocked && issue.isNotEmpty) 'Operator next step:\n$issue',
    ];
    return lines.join('\n\n');
  }

  Future<void> _submitQueue() async {
    final pid = _projectId;
    if (pid == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Select a project')),
      );
      return;
    }
    setState(() {
      _queueBusy = true;
      _queueError = null;
    });
    try {
      final res = await _api.queueDispatchTask(
        title: _titleCtrl.text.trim(),
        description: _descCtrl.text.trim(),
        projectId: pid,
        sourceInput:
            _sourceCtrl.text.trim().isEmpty ? null : _sourceCtrl.text.trim(),
      );
      await DeviceAuthStore.setLastProjectId(pid);
      if (!mounted) return;
      final tid = res['task_id'];
      final watchId = tid is int ? tid : int.tryParse('$tid');
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('Queued task #$tid'),
          action: SnackBarAction(
            label: 'Watch this task',
            onPressed: () {
              setState(() => _watchTaskId = watchId);
              _tabs.animateTo(2);
              unawaited(_loadRuns());
            },
          ),
        ),
      );
      _titleCtrl.clear();
      _descCtrl.clear();
      _sourceCtrl.clear();
    } catch (e) {
      if (mounted) setState(() => _queueError = '$e');
    } finally {
      if (mounted) setState(() => _queueBusy = false);
    }
  }

  Future<void> _confirmKillSwitch(bool targetActive) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title:
            Text(targetActive ? 'Enable kill switch?' : 'Disable kill switch?'),
        content: Text(
          targetActive
              ? 'Code dispatch will stop until you turn this off.'
              : 'Resume normal dispatch?',
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('Confirm')),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    try {
      await _api.toggleKillSwitch(
        active: targetActive,
        reason: targetActive ? 'brain_ui' : null,
      );
      await _refreshStatus();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Kill switch updated')),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Kill switch failed: $e')),
        );
      }
    }
  }

  String _agoLabel(String? iso) {
    if (iso == null || iso.isEmpty) return 'No activity recorded';
    final t = DateTime.tryParse(iso);
    if (t == null) return iso;
    final s = DateTime.now().difference(t).inSeconds;
    if (s < 60) return '$s seconds ago';
    if (s < 3600) return '${s ~/ 60} minutes ago';
    return '${s ~/ 3600} hours ago';
  }

  String _snapshotPreview(dynamic snap) {
    if (snap == null) return '—';
    try {
      final s = const JsonEncoder.withIndent('  ').convert(snap);
      if (s.length > 800) return '${s.substring(0, 800)}…';
      return s;
    } catch (_) {
      return snap.toString();
    }
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _autonomyTimer?.cancel();
    _cancelAutonomyEventStream();
    _tabs.removeListener(_onTabChanged);
    _tabs.dispose();
    _titleCtrl.dispose();
    _descCtrl.dispose();
    _sourceCtrl.dispose();
    _autopilotPromptCtrl.dispose();
    _autopilotPromptFocusNode.dispose();
    _autopilotChatScroll.dispose();
    _autonomyAgentBenchScroll.dispose();
    _autopilotFocus.dispose();
    _pairEmailCtrl.dispose();
    _pairCodeCtrl.dispose();
    _pairLabelCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_loadingBoot) {
      return const Center(child: CircularProgressIndicator());
    }
    if (!_paired) {
      return Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 440),
          child: Card(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    'Pair Your Device',
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                  const SizedBox(height: 8),
                  if (_pairError != null)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 12),
                      child: Material(
                        color: Colors.red.shade50,
                        borderRadius: BorderRadius.circular(8),
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Row(
                            children: [
                              Icon(Icons.error_outline,
                                  color: Colors.red.shade800, size: 20),
                              const SizedBox(width: 8),
                              Expanded(
                                child: Text(
                                  _pairError!,
                                  style: TextStyle(
                                      color: Colors.red.shade900, fontSize: 13),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  if (!_pairShowCodeStep) ...[
                    Text(
                      'Enter the email your admin registered for you. '
                      "We'll send a 6-digit verification code.",
                      textAlign: TextAlign.center,
                      style:
                          TextStyle(color: Colors.grey.shade700, fontSize: 13),
                    ),
                    const SizedBox(height: 20),
                    TextField(
                      controller: _pairEmailCtrl,
                      keyboardType: TextInputType.emailAddress,
                      autocorrect: false,
                      decoration: const InputDecoration(
                        hintText: 'your-email@example.com',
                        border: OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: 12),
                    FilledButton(
                      onPressed: _pairBusy ? null : _requestPairCode,
                      child: _pairBusy
                          ? const SizedBox(
                              height: 22,
                              width: 22,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Text('Send Code'),
                    ),
                  ] else ...[
                    if (_pairCodeMessage != null)
                      Text(
                        _pairCodeMessage!,
                        textAlign: TextAlign.center,
                        style: TextStyle(
                            color: Colors.grey.shade700, fontSize: 13),
                      ),
                    const SizedBox(height: 16),
                    TextField(
                      controller: _pairCodeCtrl,
                      keyboardType: TextInputType.text,
                      decoration: const InputDecoration(
                        hintText: '6-digit code',
                        border: OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: 12),
                    TextField(
                      controller: _pairLabelCtrl,
                      decoration: const InputDecoration(
                        hintText: 'Device name (e.g. My Phone)',
                        border: OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: 12),
                    FilledButton(
                      onPressed: _pairBusy ? null : _verifyPairCode,
                      child: _pairBusy
                          ? const SizedBox(
                              height: 22,
                              width: 22,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Text('Verify & Pair'),
                    ),
                    TextButton(
                      onPressed: _pairBusy ? null : _pairBackToEmail,
                      child: const Text('Use a different email'),
                    ),
                  ],
                  const SizedBox(height: 20),
                  OutlinedButton.icon(
                    onPressed: widget.onOpenSettings,
                    icon: const Icon(Icons.settings, size: 18),
                    label: const Text('Open Settings'),
                  ),
                ],
              ),
            ),
          ),
        ),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(24, 16, 24, 0),
          child:
              Text('Brain', style: Theme.of(context).textTheme.headlineMedium),
        ),
        TabBar(
          controller: _tabs,
          tabs: const [
            Tab(text: 'Status'),
            Tab(text: 'Autopilot'),
            Tab(text: 'Queue'),
            Tab(text: 'History'),
            Tab(text: 'Context'),
          ],
        ),
        Expanded(
          child: TabBarView(
            controller: _tabs,
            children: [
              _buildStatusTab(),
              _buildAutopilotTab(),
              _buildQueueTab(),
              _buildHistoryTab(),
              _buildContextTab(),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildStatusTab() {
    if (_statusError != null) {
      return ListView(
        padding: const EdgeInsets.all(24),
        children: [
          Card(
            color: Colors.red.shade50,
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Text(_statusError!,
                  style: TextStyle(color: Colors.red.shade900)),
            ),
          ),
          const SizedBox(height: 12),
          FilledButton(onPressed: _refreshStatus, child: const Text('Retry')),
        ],
      );
    }
    final s = _status;
    if (s == null) {
      return const Center(child: CircularProgressIndicator());
    }
    final killRaw = s['kill_switch'];
    final kill = killRaw is Map
        ? Map<String, dynamic>.from(killRaw)
        : <String, dynamic>{};
    final active = kill['active'] == true;
    final reason = kill['reason']?.toString() ?? '';
    final counters = (s['counters_5min'] as Map?) ?? {};
    final spendToday = (s['spend_today'] as List?) ?? [];
    final lastIso = s['last_dispatch_activity_at'] as String?;

    return ListView(
      padding: const EdgeInsets.all(24),
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Kill switch',
                    style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 8),
                Text(
                  active ? 'ON${reason.isNotEmpty ? ': $reason' : ''}' : 'Off',
                  style: TextStyle(
                    fontWeight: FontWeight.w600,
                    color: active ? Colors.red.shade700 : Colors.green.shade700,
                  ),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    if (!active)
                      FilledButton(
                        onPressed: () => _confirmKillSwitch(true),
                        child: const Text('Enable'),
                      )
                    else
                      OutlinedButton(
                        onPressed: () => _confirmKillSwitch(false),
                        child: const Text('Disable'),
                      ),
                  ],
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 12),
        Text('Last activity: ${_agoLabel(lastIso)}',
            style: TextStyle(color: Colors.grey.shade700)),
        const SizedBox(height: 16),
        Text('Counters (5 min)',
            style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: counters.entries.map((e) {
            return Chip(label: Text('${e.key}: ${e.value}'));
          }).toList(),
        ),
        if (counters.isEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text('No runs in the last 5 minutes',
                style: TextStyle(color: Colors.grey.shade600)),
          ),
        const SizedBox(height: 20),
        Text('Spend today', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        if (spendToday.isEmpty)
          Text('No LLM spend recorded today',
              style: TextStyle(color: Colors.grey.shade600))
        else
          ...spendToday.map((row) {
            final m = Map<String, dynamic>.from(row as Map<dynamic, dynamic>);
            final prov = m['provider'] ?? '';
            final calls = m['calls'] ?? 0;
            final usd = (m['spend_usd'] is num)
                ? (m['spend_usd'] as num).toDouble()
                : 0.0;
            return ListTile(
              dense: true,
              title: Text('$prov'),
              subtitle: Text('$calls calls · \$${usd.toStringAsFixed(4)}'),
            );
          }),
        if (spendToday.isNotEmpty) ...[
          const Divider(),
          Builder(
            builder: (context) {
              double total = 0;
              for (final row in spendToday) {
                final m =
                    Map<String, dynamic>.from(row as Map<dynamic, dynamic>);
                final u = m['spend_usd'];
                if (u is num) total += u.toDouble();
              }
              return Text('Total today: \$${total.toStringAsFixed(4)}',
                  style: const TextStyle(fontWeight: FontWeight.bold));
            },
          ),
        ],
      ],
    );
  }

  Map<String, dynamic> _asMap(dynamic raw) {
    if (raw is Map<String, dynamic>) return raw;
    if (raw is Map) return Map<String, dynamic>.from(raw);
    return <String, dynamic>{};
  }

  Map<String, dynamic> _jsonTextAsMap(String raw) {
    final text = raw.trim();
    if (text.isEmpty) return <String, dynamic>{};
    try {
      final decoded = jsonDecode(text);
      return _asMap(decoded);
    } catch (_) {
      return <String, dynamic>{};
    }
  }

  List<Map<String, dynamic>> _asMapList(dynamic raw) {
    if (raw is! List) return const [];
    return raw
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  List<String> _asStringList(dynamic raw) {
    if (raw is! List) return const [];
    return raw
        .map((item) => item?.toString() ?? '')
        .where((item) => item.trim().isNotEmpty)
        .toList();
  }

  int? _asInt(dynamic raw) {
    if (raw is int) return raw;
    return int.tryParse(raw?.toString() ?? '');
  }

  String _shortStamp(dynamic raw) {
    final value = raw?.toString() ?? '';
    if (value.length >= 19) {
      return value.substring(0, 19).replaceFirst('T', ' ');
    }
    return value;
  }

  Color _autonomyStatusColor(String status) {
    final dark = Theme.of(context).brightness == Brightness.dark;
    switch (status) {
      case 'merged':
      case 'completed':
        return dark ? Colors.green.shade300 : Colors.green.shade700;
      case 'blocked':
        return dark ? Colors.orange.shade300 : Colors.orange.shade800;
      case 'failed':
      case 'cancelled':
        return dark ? Colors.red.shade300 : Colors.red.shade700;
      case 'validating':
      case 'merging':
        return dark ? Colors.indigo.shade200 : Colors.indigo.shade700;
      case _autopilotStatusAwaitingApproval:
        return dark ? Colors.teal.shade200 : Colors.teal.shade700;
      case _autopilotStatusAwaitingClarification:
        return dark ? Colors.amber.shade200 : Colors.amber.shade800;
      case _autopilotStatusChatting:
        return dark ? Colors.cyan.shade200 : Colors.cyan.shade700;
      case 'running':
        return dark ? Colors.blue.shade300 : Colors.blue.shade700;
      default:
        return dark ? Colors.grey.shade400 : Colors.grey.shade700;
    }
  }

  String _autonomyStatusLabel(String status) {
    switch (status) {
      case _autopilotStatusAwaitingApproval:
        return 'awaiting approval';
      case _autopilotStatusAwaitingClarification:
        return 'needs clarification';
      case _autopilotStatusChatting:
        return 'chatting';
      case 'queued':
        return 'queued';
      case 'running':
        return 'running';
      case 'validating':
        return 'validating';
      case 'merging':
        return 'merging';
      case 'merged':
        return 'merged';
      case 'completed':
        return 'completed';
      case 'blocked':
        return 'blocked';
      case 'failed':
        return 'failed';
      case 'cancelled':
        return 'cancelled';
      case 'ready':
        return 'ready';
      default:
        return status.replaceAll('_', ' ').trim().isEmpty
            ? 'unknown'
            : status.replaceAll('_', ' ');
    }
  }

  String _autonomyStageLabel(String stage) {
    switch (stage) {
      case 'queued':
        return 'queued';
      case 'chat':
        return 'chat';
      case 'classify':
        return 'reading request';
      case 'repo_scan':
        return 'scanning repo';
      case 'plan':
        return 'planning';
      case 'assign_roles':
        return 'assigning lanes';
      case 'architect_review':
        return 'architect review';
      case 'implement':
        return 'implementing';
      case 'integrate':
        return 'integrating';
      case 'validate':
        return 'validating';
      case 'repair':
        return 'repairing';
      case 'merge':
        return 'merge check';
      case 'learn':
        return 'learning';
      default:
        return stage.replaceAll('_', ' ');
    }
  }

  String _autonomyPlanStatusLabel(String status) {
    switch (status) {
      case 'drafting':
        return 'plan drafting';
      case 'revising':
        return 'plan revising';
      case _autopilotStatusAwaitingApproval:
        return 'plan awaiting approval';
      case _autopilotStatusAwaitingClarification:
        return 'plan needs clarification';
      case 'approved':
        return 'plan approved';
      case 'implemented':
        return 'plan implemented';
      case _autopilotStatusChatting:
        return 'brainstorming';
      default:
        return status.replaceAll('_', ' ');
    }
  }

  String _autonomyMergeStatusLabel(String status) {
    switch (status) {
      case 'pending':
        return 'merge pending';
      case 'blocked':
        return 'merge blocked';
      case 'merged':
        return 'merged safely';
      case 'clean':
        return 'merge clean';
      default:
        return status.replaceAll('_', ' ').trim().isEmpty
            ? 'merge unknown'
            : 'merge ${status.replaceAll('_', ' ')}';
    }
  }

  Color _autonomyPanelColor() => Theme.of(context).colorScheme.surface;

  Color _autonomySidebarColor() {
    final scheme = Theme.of(context).colorScheme;
    final tint = Theme.of(context).brightness == Brightness.dark ? 0.08 : 0.035;
    return Color.alphaBlend(
      scheme.primary.withValues(alpha: tint),
      scheme.surface,
    );
  }

  Color _autonomyDividerColor() => Theme.of(context).dividerColor;

  Color _mutedTextColor() => Theme.of(context).colorScheme.onSurfaceVariant;

  Color _autonomyBubbleBackground(Color color, {double alpha = 0.10}) {
    final scheme = Theme.of(context).colorScheme;
    final dark = Theme.of(context).brightness == Brightness.dark;
    return Color.alphaBlend(
      color.withValues(alpha: dark ? alpha + 0.10 : alpha),
      scheme.surface,
    );
  }

  Widget _buildAutopilotTab() {
    return LayoutBuilder(
      builder: (context, constraints) {
        if (constraints.maxWidth < 980) {
          return _buildAutonomyStackedCockpit();
        }
        final rightWidth = constraints.maxWidth >= 1320 ? 380.0 : 330.0;
        return Column(
          children: [
            if (_autonomyError != null) _buildAutonomyErrorBanner(),
            if (_autonomyLiveSyncNotice != null) _buildAutonomyLiveSyncNotice(),
            Expanded(
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  SizedBox(width: 280, child: _buildAutonomyThreadSidebar()),
                  VerticalDivider(width: 1, color: _autonomyDividerColor()),
                  Expanded(child: _buildAutonomyConversationPane()),
                  VerticalDivider(width: 1, color: _autonomyDividerColor()),
                  SizedBox(
                    width: rightWidth,
                    child: _buildAutonomyTrackingSidebar(),
                  ),
                ],
              ),
            ),
          ],
        );
      },
    );
  }

  Widget _buildAutonomyStackedCockpit() {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        if (_autonomyError != null) _buildAutonomyErrorBanner(compact: true),
        if (_autonomyLiveSyncNotice != null)
          _buildAutonomyLiveSyncNotice(compact: true),
        SizedBox(height: 360, child: _buildAutonomyConversationPane()),
        const SizedBox(height: 12),
        SizedBox(height: 260, child: _buildAutonomyThreadSidebar()),
        const SizedBox(height: 12),
        if (_activeAutonomyRun != null)
          _buildAutonomyActiveRun(_activeAutonomyRun!)
        else
          _emptyAutonomyState('No Project Autopilot run selected'),
      ],
    );
  }

  Widget _buildAutonomyErrorBanner({bool compact = false}) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      width: double.infinity,
      padding: EdgeInsets.all(compact ? 10 : 12),
      color: scheme.errorContainer,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.error_outline, color: scheme.onErrorContainer, size: 20),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              _autonomyError!,
              style: TextStyle(color: scheme.onErrorContainer, fontSize: 13),
            ),
          ),
          const SizedBox(width: 8),
          TextButton.icon(
            onPressed: _autonomyBusy ? null : _retryAutonomyLiveSync,
            icon: const Icon(Icons.refresh, size: 16),
            label: Text(compact ? 'Retry' : 'Refresh now'),
            style: TextButton.styleFrom(
              foregroundColor: scheme.onErrorContainer,
              visualDensity: VisualDensity.compact,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyLiveSyncNotice({bool compact = false}) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      width: double.infinity,
      padding: EdgeInsets.all(compact ? 10 : 12),
      color: scheme.secondaryContainer.withValues(alpha: 0.72),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            Icons.sync_problem_outlined,
            color: scheme.onSecondaryContainer,
            size: 20,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              _autonomyLiveSyncNotice!,
              style: TextStyle(
                color: scheme.onSecondaryContainer,
                fontSize: 13,
              ),
            ),
          ),
          const SizedBox(width: 8),
          TextButton.icon(
            onPressed: _autonomyBusy ? null : _retryAutonomyLiveSync,
            icon: const Icon(Icons.refresh, size: 16),
            label: Text(compact ? 'Retry' : 'Refresh now'),
            style: TextButton.styleFrom(
              foregroundColor: scheme.onSecondaryContainer,
              visualDensity: VisualDensity.compact,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyThreadSidebar() {
    return Container(
      color: _autonomySidebarColor(),
      child: ListView(
        padding: const EdgeInsets.only(bottom: 12),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 14, 10, 10),
            child: Row(
              children: [
                Icon(Icons.forum_outlined,
                    color: Theme.of(context).colorScheme.primary, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text('Autopilot',
                      style: Theme.of(context).textTheme.titleMedium),
                ),
                IconButton(
                  tooltip: 'Refresh runs',
                  onPressed: _autonomyBusy
                      ? null
                      : () async {
                          await _loadCodeRepos();
                          await _loadAutonomyAgentProfiles();
                          await _loadAutonomyAgentSchedulerStatus();
                          await _loadAutonomyRuns();
                        },
                  icon: const Icon(Icons.refresh, size: 20),
                ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 0, 14, 12),
            child: FilledButton.icon(
              onPressed: _autonomyBusy ? null : _startNewAutonomyDraft,
              icon: const Icon(Icons.add, size: 18),
              label: const Text('New run'),
            ),
          ),
          _buildAutonomyRepoTabs(),
          const SizedBox(height: 8),
          _buildAutonomyAgentBench(),
          const SizedBox(height: 8),
          _buildAutonomyHistoryControls(),
          const SizedBox(height: 8),
          if (_autonomyLoading)
            const SizedBox(
              height: 160,
              child: Center(child: CircularProgressIndicator()),
            )
          else if (_autonomyRuns.isEmpty)
            SizedBox(
              height: 160,
              child: _emptyAutonomyState(_autonomyEmptyHistoryLabel()),
            )
          else
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: Column(
                children: [
                  for (final run in _autonomyRuns)
                    _buildAutonomyThreadTile(run),
                ],
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildAutonomyRepoTabs() {
    final scheme = Theme.of(context).colorScheme;
    return SizedBox(
      height: 38,
      child: ListView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 14),
        children: [
          _buildAutonomyRepoTab(
            label: 'All',
            selected: _autonomyHistoryRepoId == null,
            onSelected: () => _selectAutonomyHistoryRepo(null),
            selectedColor: scheme.primary,
          ),
          for (final repo in _codeRepos)
            if (_asInt(repo['id']) != null) ...[
              const SizedBox(width: 8),
              _buildAutonomyRepoTab(
                label: _repoDisplayName(_asInt(repo['id'])),
                selected: _autonomyHistoryRepoId == _asInt(repo['id']),
                onSelected: () =>
                    _selectAutonomyHistoryRepo(_asInt(repo['id'])),
                selectedColor: scheme.primary,
              ),
            ],
        ],
      ),
    );
  }

  Widget _buildAutonomyRepoTab({
    required String label,
    required bool selected,
    required VoidCallback onSelected,
    required Color selectedColor,
  }) {
    return ChoiceChip(
      label: Text(label, overflow: TextOverflow.ellipsis),
      selected: selected,
      onSelected: _autonomyBusy ? null : (_) => onSelected(),
      labelStyle: TextStyle(
        color: selected
            ? Theme.of(context).colorScheme.onPrimary
            : Theme.of(context).colorScheme.onSurfaceVariant,
        fontWeight: selected ? FontWeight.w700 : FontWeight.w600,
      ),
      selectedColor: selectedColor,
      backgroundColor: _autonomyBubbleBackground(Colors.blueGrey, alpha: 0.08),
      side: BorderSide(
        color: selected
            ? selectedColor
            : Theme.of(context).colorScheme.outlineVariant,
      ),
      visualDensity: VisualDensity.compact,
    );
  }

  Widget _buildAutonomyAgentBench() {
    final repoId = _autonomyAgentRepoId;
    final scheme = Theme.of(context).colorScheme;
    final filteredAgents = _filteredAutonomyBenchAgents();
    final viewportHeight = MediaQuery.sizeOf(context).height;
    final benchListHeight = (viewportHeight * _autopilotAgentBenchViewportRatio)
        .clamp(
          _autopilotAgentBenchListMinHeight,
          _autopilotAgentBenchListMaxHeight,
        )
        .toDouble();
    final codexCount = _agentBenchFilterCount(_autopilotAgentBenchFilterCodex);
    final sourceActiveCount =
        _autonomyAgents.where(_agentIsSourceActive).length;
    final enabledCount = _autonomyAgents
        .where((agent) => agent['schedule_enabled'] == true)
        .length;
    final needsInputCount =
        _agentBenchFilterCount(_autopilotAgentBenchFilterNeedsInput);
    if (repoId == null) {
      return Padding(
        padding: const EdgeInsets.symmetric(horizontal: 14),
        child: Text(
          'Pick a repo to see generated agents.',
          style: TextStyle(color: _mutedTextColor(), fontSize: 12),
        ),
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 14),
      child: Container(
        padding: const EdgeInsets.all(8),
        decoration: BoxDecoration(
          color: _autonomyBubbleBackground(scheme.primary, alpha: 0.05),
          border: Border.all(color: _autonomyDividerColor()),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    'Agent bench',
                    style: Theme.of(context).textTheme.labelLarge,
                  ),
                ),
                if (_autonomyAgentsLoading)
                  const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                else
                  Text(
                    _repoDisplayName(repoId),
                    style: TextStyle(color: _mutedTextColor(), fontSize: 11),
                    overflow: TextOverflow.ellipsis,
                  ),
              ],
            ),
            const SizedBox(height: 8),
            if (_autonomyAgents.isEmpty)
              OutlinedButton.icon(
                onPressed: _autonomyBusy ? null : _bootstrapAutonomyAgents,
                icon: const Icon(Icons.auto_awesome, size: 16),
                label: const Text('Generate paused agents'),
              )
            else ...[
              Text(
                '$codexCount Codex-seeded, $sourceActiveCount source-active, $enabledCount enabled, $needsInputCount need input.',
                style: TextStyle(color: _mutedTextColor(), fontSize: 12),
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: [
                  for (final filter in _autopilotAgentBenchFilters)
                    _buildAutonomyAgentBenchFilterChip(filter),
                ],
              ),
              const SizedBox(height: 8),
              SizedBox(
                height: benchListHeight,
                child: Scrollbar(
                  controller: _autonomyAgentBenchScroll,
                  thumbVisibility: true,
                  child: ListView(
                    controller: _autonomyAgentBenchScroll,
                    padding: const EdgeInsets.only(right: 8),
                    primary: false,
                    children: [
                      _buildAutonomyAgentFilterTile(null),
                      if (filteredAgents.isEmpty)
                        Padding(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 8,
                            vertical: 10,
                          ),
                          child: Text(
                            'No ${_agentBenchFilterLabel(_autonomyAgentBenchFilter).toLowerCase()} agents in this repo.',
                            style: TextStyle(
                              color: _mutedTextColor(),
                              fontSize: 12,
                            ),
                          ),
                        ),
                      for (final agent in filteredAgents)
                        _buildAutonomyAgentFilterTile(agent),
                    ],
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildAutonomyAgentBenchFilterChip(String filter) {
    final selected = _autonomyAgentBenchFilter == filter;
    final count = _agentBenchFilterCount(filter);
    final color = _agentBenchFilterColor(filter);
    return ChoiceChip(
      label: Text('${_agentBenchFilterLabel(filter)} $count'),
      selected: selected,
      onSelected: _autonomyBusy
          ? null
          : (_) => setState(() => _autonomyAgentBenchFilter = filter),
      labelStyle: TextStyle(
        color: selected
            ? Theme.of(context).colorScheme.onPrimary
            : Theme.of(context).colorScheme.onSurfaceVariant,
        fontWeight: selected ? FontWeight.w700 : FontWeight.w600,
      ),
      selectedColor: color,
      backgroundColor: _autonomyBubbleBackground(color, alpha: 0.06),
      side: BorderSide(
        color: selected ? color : Theme.of(context).colorScheme.outlineVariant,
      ),
      visualDensity: VisualDensity.compact,
    );
  }

  Widget _buildAutonomyAgentFilterTile(Map<String, dynamic>? agent) {
    final selected = agent == null
        ? _autonomyAgentProfileId == null
        : _autonomyAgentProfileId == _asInt(agent['id']);
    final status = agent?['status']?.toString() ?? '';
    final color = agent == null
        ? Theme.of(context).colorScheme.primary
        : _agentStatusColor(status);
    final label = agent == null ? 'All agents' : _agentDisplayName(agent);
    final pendingQuestionCount =
        agent == null ? 0 : _asInt(agent['pending_question_count']) ?? 0;
    final activeRunCount =
        agent == null ? 0 : _asInt(agent['active_run_count']) ?? 0;
    final operatingState = agent == null
        ? const <String, dynamic>{}
        : _asMap(agent[_autopilotAgentOperatingState]);
    final operatingTitle = operatingState['title']?.toString().trim() ?? '';
    final schedule = agent == null ? '' : _agentScheduleSummary(agent);
    final subtitle = agent == null
        ? 'show every chat in this repo'
        : [
            _agentTierLabel(agent['tier']?.toString() ?? ''),
            _agentStatusLabel(status),
            if (_agentIsCodexAutomation(agent)) 'Codex seeded',
            if (_agentIsSourceActive(agent)) 'source active',
            if (schedule.isNotEmpty) schedule,
            if (operatingTitle.isNotEmpty) operatingTitle,
            if (activeRunCount > 0)
              '$activeRunCount active chat${activeRunCount == 1 ? '' : 's'}',
            if (pendingQuestionCount > 0)
              '$pendingQuestionCount question${pendingQuestionCount == 1 ? '' : 's'}',
          ].where((part) => part.trim().isNotEmpty).join(' - ');
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: InkWell(
        borderRadius: BorderRadius.circular(8),
        onTap: _autonomyBusy
            ? null
            : () => _selectAutonomyAgent(
                agent == null ? null : _asInt(agent['id'])),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 7),
          decoration: BoxDecoration(
            color: selected
                ? _autonomyBubbleBackground(color, alpha: 0.14)
                : Colors.transparent,
            borderRadius: BorderRadius.circular(8),
            border: Border.all(
              color:
                  selected ? color.withValues(alpha: 0.36) : Colors.transparent,
            ),
          ),
          child: Row(
            children: [
              Icon(
                agent == null
                    ? Icons.groups_outlined
                    : _agentIcon(agent['tier']?.toString() ?? ''),
                size: 17,
                color: color,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      label,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        fontSize: 12,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    Text(
                      subtitle,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(color: _mutedTextColor(), fontSize: 11),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  IconData _agentIcon(String tier) {
    switch (tier) {
      case _autopilotAgentTierMacro:
        return Icons.account_tree_outlined;
      case _autopilotAgentTierMicro:
        return Icons.engineering_outlined;
      case _autopilotAgentTierSpecialist:
        return Icons.psychology_alt_outlined;
      default:
        return Icons.smart_toy_outlined;
    }
  }

  Widget _buildAutonomyHistoryControls() {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 14),
      child: Wrap(
        spacing: 8,
        runSpacing: 6,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          FilterChip(
            label: const Text('Archived'),
            selected: _autonomyShowArchived,
            onSelected: _autonomyBusy
                ? null
                : (value) {
                    setState(() => _autonomyShowArchived = value);
                    unawaited(_loadAutonomyRuns());
                  },
            selectedColor: _autonomyBubbleBackground(scheme.primary),
            visualDensity: VisualDensity.compact,
          ),
          TextButton.icon(
            onPressed: _autonomyBusy || _autonomyRuns.isEmpty
                ? null
                : _archiveAutonomyVisibleRuns,
            icon: const Icon(Icons.archive_outlined, size: 16),
            label: const Text('Clear view'),
            style: TextButton.styleFrom(visualDensity: VisualDensity.compact),
          ),
        ],
      ),
    );
  }

  String _autonomyEmptyHistoryLabel() {
    final agent = _agentProfileById(_autonomyAgentProfileId);
    if (agent.isNotEmpty) {
      return _autonomyShowArchived
          ? 'No archived chats for ${_agentDisplayName(agent)}'
          : 'No active chats for ${_agentDisplayName(agent)}';
    }
    if (_autonomyHistoryRepoId != null) {
      return _autonomyShowArchived
          ? 'No archived chats in ${_repoDisplayName(_autonomyHistoryRepoId)}'
          : 'No chats in ${_repoDisplayName(_autonomyHistoryRepoId)} yet';
    }
    return _autonomyShowArchived
        ? 'No archived run history'
        : 'No run history yet';
  }

  void _selectAutonomyHistoryRepo(int? repoId) {
    if (_autonomyHistoryRepoId == repoId) return;
    _cancelAutonomyEventStream();
    setState(() {
      _autonomyHistoryRepoId = repoId;
      _autonomyAgentProfileId = null;
      if (repoId != null) _autonomyRepoId = repoId;
      if (repoId != null &&
          !_runMatchesAutonomyHistoryRepo(_activeAutonomyRun)) {
        _activeAutonomyRun = null;
        _autonomyDraftOpen = false;
      }
      _autonomyRuns = [];
      _autonomyLiveSyncNotice = null;
      _autonomyError = null;
    });
    unawaited(() async {
      await _loadAutonomyAgentProfiles();
      await _loadAutonomyRuns();
    }());
  }

  void _selectAutonomyAgent(int? profileId) {
    if (_autonomyAgentProfileId == profileId) return;
    _cancelAutonomyEventStream();
    setState(() {
      _autonomyAgentProfileId = profileId;
      if (!_runMatchesAutonomyAgent(_activeAutonomyRun)) {
        _activeAutonomyRun = null;
        _autonomyDraftOpen = false;
      }
      _autonomyRuns = [];
      _autonomyLiveSyncNotice = null;
      _autonomyError = null;
    });
    unawaited(_loadAutonomyRuns());
  }

  Future<void> _bootstrapAutonomyAgents() async {
    final repoId = _autonomyAgentRepoId;
    if (repoId == null) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final agents =
          await _api.bootstrapProjectAutonomyAgentProfiles(repoId: repoId);
      if (!mounted) return;
      setState(() => _autonomyAgents = agents);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content:
              Text('Generated paused agents for ${_repoDisplayName(repoId)}.'),
        ),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _syncAutonomyCodexProfiles() async {
    final repoId = _autonomyAgentRepoId;
    if (repoId == null) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
            content: Text('Select a repo before syncing Codex prompts.')),
      );
      return;
    }
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final payload =
          await _api.syncProjectAutonomyCodexProfiles(repoId: repoId);
      final agents = _asMapList(payload['agents']);
      if (!mounted) return;
      if (agents.isNotEmpty) {
        setState(() => _autonomyAgents = agents);
      }
      await _loadAutonomyAgentProfiles(bootstrapIfEmpty: false);
      await _loadAutonomyAgentSchedulerStatus();
      await _loadAutonomyAgentReadiness();
      await _loadAutonomyRuns(silent: true);
      if (!mounted) return;
      final message = payload['message']?.toString().trim();
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(message?.isNotEmpty == true
              ? message!
              : 'Synced Codex prompt snapshots for ${_repoDisplayName(repoId)}.'),
        ),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _archiveAutonomyVisibleRuns() async {
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      await _api.archiveProjectAutonomyRuns(
        repoId: _autonomyHistoryRepoId,
        agentProfileId: _autonomyAgentProfileId,
        reason: _autopilotArchiveReasonOperatorClear,
      );
      if (!mounted) return;
      setState(() {
        _activeAutonomyRun = null;
        _autonomyDraftOpen = false;
        _autonomyRuns = [];
      });
      await _loadAutonomyRuns(silent: true);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Autopilot instances archived from this view.'),
        ),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _setAutonomyAgentPaused(
    Map<String, dynamic> profile, {
    required bool paused,
  }) async {
    final id = _asInt(profile['id']);
    if (id == null) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final updated = paused
          ? await _api.pauseProjectAutonomyAgentProfile(id)
          : await _api.resumeProjectAutonomyAgentProfile(id);
      if (!mounted) return;
      setState(() => _replaceAutonomyAgent(updated));
      await _loadAutonomyAgentSchedulerStatus();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            '${_agentDisplayName(updated)} ${paused ? 'paused' : 'resumed'}.',
          ),
        ),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _updateAutonomyAgentSettings(
    Map<String, dynamic> profile, {
    String? modelPolicy,
    Map<String, dynamic>? promptSetting,
    Map<String, dynamic>? permissions,
    bool? scheduleEnabled,
    Map<String, dynamic>? schedule,
    required String successMessage,
  }) async {
    final id = _asInt(profile['id']);
    if (id == null) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final updated = await _api.updateProjectAutonomyAgentProfile(
        id,
        modelPolicy: modelPolicy,
        promptSetting: promptSetting,
        permissions: permissions,
        scheduleEnabled: scheduleEnabled,
        schedule: schedule,
      );
      if (!mounted) return;
      setState(() => _replaceAutonomyAgent(updated));
      await _loadAutonomyAgentSchedulerStatus();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(successMessage)),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _setAutonomyAgentSchedule(
    Map<String, dynamic> profile,
    String cadence,
  ) {
    final enabled = cadence != _autopilotScheduleCadenceManual;
    return _updateAutonomyAgentSettings(
      profile,
      scheduleEnabled: enabled,
      schedule: _agentSchedulePatch(profile, cadence),
      successMessage: enabled
          ? '${_agentDisplayName(profile)} schedule set to ${_agentScheduleCadenceLabel(cadence)}.'
          : '${_agentDisplayName(profile)} schedule disabled.',
    );
  }

  Future<void> _setAutonomyCodexScheduleMirror({
    required bool enableSourceActive,
    bool alwaysOn = false,
  }) async {
    final repoId = _autonomyAgentRepoId;
    if (repoId == null) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
            content: Text('Select a repo before changing schedules.')),
      );
      return;
    }
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final payload = await _api.setProjectAutonomyCodexSchedules(
        repoId: repoId,
        enableSourceActive: enableSourceActive,
        alwaysOn: alwaysOn,
      );
      final agents = _asMapList(payload['agents']);
      final changed = _asInt(payload['changed']) ?? 0;
      if (!mounted) return;
      if (agents.isNotEmpty) {
        setState(() => _autonomyAgents = agents);
      }
      await _loadAutonomyAgentProfiles(bootstrapIfEmpty: false);
      await _loadAutonomyAgentSchedulerStatus();
      await _loadAutonomyAgentReadiness();
      await _loadAutonomyRuns(silent: true);
      if (!mounted) return;
      final message = payload['message']?.toString().trim();
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(message?.isNotEmpty == true
              ? message!
              : enableSourceActive
                  ? alwaysOn
                      ? 'Enabled $changed source-active Codex agents as always-on queues. They remain plan-only.'
                      : 'Enabled $changed source-active Codex schedules in CHILI. They remain plan-only.'
                  : 'Paused $changed Codex schedules in CHILI.'),
        ),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _adoptAutonomyCodexAgentLoop() async {
    final repoId = _autonomyAgentRepoId;
    if (repoId == null) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
            content: Text('Select a repo before adopting Codex agents.')),
      );
      return;
    }
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final payload = await _api.adoptProjectAutonomyCodexAgentLoop(
        repoId: repoId,
        wakeNow: true,
      );
      final agents = _asMapList(payload['agents']);
      if (!mounted) return;
      if (agents.isNotEmpty) {
        setState(() => _autonomyAgents = agents);
      }
      await _loadAutonomyAgentProfiles(bootstrapIfEmpty: false);
      await _loadAutonomyAgentSchedulerStatus();
      await _loadAutonomyAgentReadiness();
      await _loadAutonomyRuns(silent: true);
      if (!mounted) return;
      final message = payload['message']?.toString().trim();
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            message?.isNotEmpty == true
                ? message!
                : 'Adopted source-active Codex agents as always-on plan-only queues.',
          ),
        ),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _runAutonomyAgentSchedulesNow({bool codexOnly = true}) async {
    final repoId = _autonomyAgentRepoId;
    if (repoId == null) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Select a repo before running agents.')),
      );
      return;
    }
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final payload = await _api.runProjectAutonomyAgentSchedulesNow(
        repoId: repoId,
        codexOnly: codexOnly,
      );
      await _loadAutonomyAgentProfiles(bootstrapIfEmpty: false);
      await _loadAutonomyAgentSchedulerStatus();
      await _loadAutonomyAgentReadiness();
      await _loadAutonomyRuns(silent: true);
      if (!mounted) return;
      final message = payload['message']?.toString().trim();
      final started = _asInt(payload['started']) ?? 0;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(message?.isNotEmpty == true
              ? message!
              : codexOnly
                  ? 'Queued $started active Codex agent cycle(s) now.'
                  : 'Queued $started active agent cycle(s) now.'),
        ),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _runAutonomyCodexSchedulesNow() {
    return _runAutonomyAgentSchedulesNow(codexOnly: true);
  }

  Future<void> _setAutonomyAgentWorktreePermission(
    Map<String, dynamic> profile, {
    required bool enabled,
  }) {
    return _updateAutonomyAgentSettings(
      profile,
      permissions: {_autopilotPermissionWorktree: enabled},
      successMessage: enabled
          ? '${_agentDisplayName(profile)} can create worktree patches on future cycles.'
          : '${_agentDisplayName(profile)} returned to plan-only mode.',
    );
  }

  Future<void> _showEditAutonomyAgentPromptDialog(
    Map<String, dynamic> profile,
  ) async {
    final promptSetting = _asMap(profile['prompt_setting']);
    final promptController = TextEditingController(
      text: promptSetting['system_prompt']?.toString() ?? '',
    );
    var selectedPolicy = profile['model_policy']?.toString().trim() ??
        _autopilotModelPolicyLocalFirst;
    if (!_autopilotModelPolicyValues.contains(selectedPolicy)) {
      selectedPolicy = _autopilotModelPolicyLocalFirst;
    }
    final submitted = await showDialog<bool>(
      context: context,
      builder: (dialogContext) {
        return StatefulBuilder(
          builder: (context, setDialogState) {
            return AlertDialog(
              title: Text('Edit ${_agentDisplayName(profile)}'),
              content: SizedBox(
                width: 620,
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    DropdownButtonFormField<String>(
                      initialValue: selectedPolicy,
                      decoration: const InputDecoration(
                        labelText: 'Model policy',
                        border: OutlineInputBorder(),
                        isDense: true,
                      ),
                      items: const [
                        DropdownMenuItem(
                            value: _autopilotModelPolicyLocalFirst,
                            child: Text('Local first')),
                        DropdownMenuItem(
                            value: _autopilotModelPolicyLocalOnly,
                            child: Text('Local only')),
                        DropdownMenuItem(
                            value: _autopilotModelPolicyCurrent,
                            child: Text('chili-coder:current')),
                      ],
                      onChanged: (value) {
                        if (value == null) return;
                        setDialogState(() => selectedPolicy = value);
                      },
                    ),
                    const SizedBox(height: 12),
                    TextField(
                      controller: promptController,
                      minLines: 8,
                      maxLines: 14,
                      decoration: const InputDecoration(
                        labelText: 'Agent operating prompt',
                        border: OutlineInputBorder(),
                        alignLabelWithHint: true,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      'Future chats and scheduled cycles snapshot this prompt. Existing runs keep their old snapshot.',
                      style: TextStyle(color: _mutedTextColor(), fontSize: 12),
                    ),
                  ],
                ),
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(dialogContext).pop(false),
                  child: const Text('Cancel'),
                ),
                FilledButton.icon(
                  onPressed: () => Navigator.of(dialogContext).pop(true),
                  icon: const Icon(Icons.save_outlined, size: 18),
                  label: const Text('Save'),
                ),
              ],
            );
          },
        );
      },
    );
    final prompt = promptController.text.trim();
    promptController.dispose();
    if (submitted != true || prompt.isEmpty) return;
    await _updateAutonomyAgentSettings(
      profile,
      modelPolicy: selectedPolicy,
      promptSetting: {
        'source': _autopilotAgentSourceDesktopCustom,
        'system_prompt': prompt,
      },
      successMessage: '${_agentDisplayName(profile)} prompt setting updated.',
    );
  }

  Future<void> _showAddCodeRepoDialog() async {
    final pathController =
        TextEditingController(text: _autopilotDefaultRepoPath);
    final nameController = TextEditingController();
    final submitted = await showDialog<bool>(
      context: context,
      builder: (dialogContext) {
        return AlertDialog(
          title: const Text('Add local repo'),
          content: SizedBox(
            width: 460,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: pathController,
                  decoration: const InputDecoration(
                    labelText: 'Repository path',
                    hintText: _autopilotDefaultRepoPath,
                  ),
                  autofocus: true,
                  onSubmitted: (_) => Navigator.of(dialogContext).pop(true),
                ),
                const SizedBox(height: 8),
                Align(
                  alignment: Alignment.centerLeft,
                  child: OutlinedButton.icon(
                    onPressed: () async {
                      final current = pathController.text.trim();
                      final picked = await FilePicker.platform.getDirectoryPath(
                        dialogTitle: 'Select local repository',
                        initialDirectory: current.isNotEmpty
                            ? current
                            : _autopilotDefaultRepoPath,
                      );
                      if (picked != null && picked.trim().isNotEmpty) {
                        pathController.text = picked.trim();
                      }
                    },
                    icon: const Icon(Icons.folder_open_outlined, size: 18),
                    label: const Text('Browse'),
                  ),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: nameController,
                  decoration: const InputDecoration(
                    labelText: 'Display name',
                    hintText: 'Optional',
                  ),
                  onSubmitted: (_) => Navigator.of(dialogContext).pop(true),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(dialogContext).pop(false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.of(dialogContext).pop(true),
              child: const Text('Add repo'),
            ),
          ],
        );
      },
    );
    final path = pathController.text.trim();
    final name = nameController.text.trim();
    pathController.dispose();
    nameController.dispose();
    if (submitted != true || path.isEmpty) return;
    if (!mounted) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final result = await _api.addCodeBrainRepo(
        path: path,
        name: name.isEmpty ? null : name,
      );
      await _loadCodeRepos();
      final repoId = _asInt(result['id']);
      if (!mounted) return;
      setState(() {
        if (repoId != null) {
          _autonomyRepoId = repoId;
          if (_autonomyHistoryRepoId == null) {
            _autonomyAgentProfileId = null;
          }
        }
      });
      await _loadAutonomyAgentProfiles();
      await _loadAutonomyAgentSchedulerStatus();
      await _loadAutonomyRuns();
      if (!mounted) return;
      final message = result['already_registered'] == true
          ? 'Repo already configured; selected it.'
          : 'Repo added and selected.';
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(message)),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _confirmRemoveSelectedCodeRepo() async {
    final repo = _selectedAutonomyRepo();
    final repoId = _asInt(repo?['id']);
    if (repo == null || repoId == null) return;
    if (_isProtectedAutonomyRepo(repo)) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
              'The active CHILI checkout is protected and stays on D drive.'),
        ),
      );
      return;
    }
    final repoName = repo['name']?.toString().trim().isNotEmpty == true
        ? repo['name'].toString()
        : 'repo $repoId';
    final ok = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text('Remove $repoName?'),
        content: Text(
          'This hides ${_repoPathLabel(repo)} from Project Autopilot. '
          'Existing run history stays archived in the backend.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton.tonalIcon(
            onPressed: () => Navigator.of(dialogContext).pop(true),
            icon: const Icon(Icons.folder_off_outlined, size: 18),
            label: const Text('Remove repo'),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      await _api.removeCodeBrainRepo(repoId);
      if (!mounted) return;
      setState(() {
        if (_autonomyRepoId == repoId) _autonomyRepoId = null;
        if (_autonomyHistoryRepoId == repoId) _autonomyHistoryRepoId = null;
        _autonomyAgentProfileId = null;
      });
      await _loadCodeRepos();
      await _loadAutonomyAgentProfiles();
      await _loadAutonomyRuns();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('$repoName removed from Autopilot.')),
      );
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _startAutonomyAgentCycle(Map<String, dynamic> profile) async {
    final id = _asInt(profile['id']);
    if (id == null) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.startProjectAutonomyAgentCycle(id);
      if (!mounted) return;
      setState(() {
        _activeAutonomyRun = run;
        _autonomyDraftOpen = false;
        _syncAutonomyRunListState(run);
      });
      await _loadAutonomyRuns(silent: true);
      await _refreshActiveAutonomyRun(silent: true, force: true);
      _syncAutonomyEventStream();
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  void _replaceAutonomyAgent(Map<String, dynamic> updated) {
    final id = _asInt(updated['id']);
    if (id == null) return;
    final next = List<Map<String, dynamic>>.from(_autonomyAgents);
    final index = next.indexWhere((agent) => _asInt(agent['id']) == id);
    if (index >= 0) {
      next[index] = {...next[index], ...updated};
    } else {
      next.add(updated);
    }
    _autonomyAgents = next;
  }

  Widget _buildAutonomyThreadTile(Map<String, dynamic> run) {
    final runId = run['run_id']?.toString() ?? '';
    final status = run['status']?.toString() ?? 'unknown';
    final stage = run['current_stage']?.toString() ?? '';
    final prompt = run['prompt']?.toString() ?? '';
    final agent = _agentProfileForRun(run);
    final selected = _activeAutonomyRun?['run_id']?.toString() == runId;
    final color = _autonomyStatusColor(status);
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Material(
        color: selected
            ? _autonomyBubbleBackground(
                Theme.of(context).colorScheme.primary,
                alpha: 0.12,
              )
            : Colors.transparent,
        borderRadius: BorderRadius.circular(8),
        child: InkWell(
          borderRadius: BorderRadius.circular(8),
          onTap: () {
            setState(() {
              _activeAutonomyRun = run;
              _autonomyDraftOpen = false;
            });
            _syncAutonomyEventStream();
            unawaited(_refreshActiveAutonomyRun(silent: false, force: true));
          },
          child: Padding(
            padding: const EdgeInsets.all(10),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(_autonomyStatusIcon(status), color: color, size: 18),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        runId.isEmpty ? 'Autopilot run' : runId,
                        style: const TextStyle(
                          fontWeight: FontWeight.w700,
                          fontSize: 12,
                        ),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  prompt.isEmpty ? '(no prompt recorded)' : prompt,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: _mutedTextColor(), fontSize: 12),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  children: [
                    if (agent.isNotEmpty)
                      _miniChip(
                        _agentDisplayName(agent),
                        _autonomyBubbleBackground(Colors.cyan),
                        Colors.cyan.shade900,
                      ),
                    _miniChip(_autonomyStatusLabel(status),
                        color.withValues(alpha: 0.12), color),
                    if (stage.isNotEmpty)
                      _miniChip(
                        _autonomyStageLabel(stage),
                        _autonomyBubbleBackground(Colors.blueGrey),
                        Colors.blueGrey.shade800,
                      ),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  _shortStamp(run['updated_at'] ?? run['created_at']),
                  style: TextStyle(color: _mutedTextColor(), fontSize: 11),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildAutonomyConversationPane() {
    final run = _activeAutonomyRun;
    return Container(
      color: _autonomyPanelColor(),
      child: Column(
        children: [
          _buildAutonomyConversationHeader(run),
          Expanded(
            child: run == null
                ? _emptyAutonomyState('Start or select an Autopilot run')
                : _buildAutonomyChatTimeline(run),
          ),
          _buildAutonomyComposer(),
        ],
      ),
    );
  }

  Widget _buildAutonomyConversationHeader(Map<String, dynamic>? run) {
    final status = run?['status']?.toString() ?? 'ready';
    final stage = run?['current_stage']?.toString() ?? '';
    final merge = run?['merge_status']?.toString() ?? '';
    final planStatus = run?['plan_status']?.toString() ?? '';
    final agent = _agentProfileForRun(run);
    final color = _autonomyStatusColor(status);
    return Container(
      padding: const EdgeInsets.fromLTRB(18, 12, 14, 12),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: _autonomyDividerColor())),
      ),
      child: Row(
        children: [
          Icon(_autonomyStatusIcon(status), color: color, size: 22),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  run?['run_id']?.toString().isNotEmpty == true
                      ? run!['run_id'].toString()
                      : 'Project Autopilot',
                  style: Theme.of(context).textTheme.titleMedium,
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 4),
                Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  children: [
                    if (agent.isNotEmpty)
                      _miniChip(
                        _agentDisplayName(agent),
                        _autonomyBubbleBackground(Colors.cyan),
                        Colors.cyan.shade900,
                      ),
                    _miniChip(_autonomyStatusLabel(status),
                        color.withValues(alpha: 0.12), color),
                    if (stage.isNotEmpty)
                      _miniChip(
                        _autonomyStageLabel(stage),
                        _autonomyBubbleBackground(Colors.indigo),
                        Colors.indigo.shade800,
                      ),
                    if (merge.isNotEmpty)
                      _miniChip(
                        _autonomyMergeStatusLabel(merge),
                        _autonomyBubbleBackground(Colors.blueGrey),
                        Colors.blueGrey.shade800,
                      ),
                    if (planStatus.isNotEmpty)
                      _miniChip(
                        _autonomyPlanStatusLabel(planStatus),
                        _autonomyBubbleBackground(Colors.teal),
                        Colors.teal.shade800,
                      ),
                  ],
                ),
              ],
            ),
          ),
          IconButton(
            tooltip: 'Refresh',
            onPressed: _autonomyBusy || run == null
                ? null
                : () => _refreshActiveAutonomyRun(
                      silent: false,
                      force: true,
                    ),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyChatTimeline(Map<String, dynamic> run) {
    final messages = _asMapList(run['messages']);
    final visibleMessages =
        messages.isNotEmpty ? messages : _fallbackAutonomyMessages(run);
    final widgets = visibleMessages.map(_buildAutonomyMessageBubble).toList();
    final progress = _buildAutonomyProgressBubble(run);
    if (progress != null) widgets.add(progress);
    return ListView(
      controller: _autopilotChatScroll,
      padding: const EdgeInsets.fromLTRB(18, 16, 18, 24),
      children: widgets.isEmpty
          ? [_emptyAutonomyState('Start the conversation with CHILI')]
          : widgets,
    );
  }

  Widget? _buildAutonomyProgressBubble(Map<String, dynamic> run) {
    final status = run['status']?.toString() ?? '';
    final planStatus = run['plan_status']?.toString() ?? '';
    final active =
        const {'queued', 'running', 'validating', 'merging'}.contains(status) ||
            const {'drafting', 'revising'}.contains(planStatus);
    if (!active || _autonomyTerminal(run)) return null;

    final steps = _asMapList(run['steps']);
    final latestStep = steps.isEmpty ? <String, dynamic>{} : steps.last;
    final stage = latestStep['stage']?.toString().trim().isNotEmpty == true
        ? latestStep['stage'].toString()
        : run['current_stage']?.toString() ?? '';
    final stepStatus = latestStep['status']?.toString() ?? status;
    final stepBody = latestStep.isEmpty
        ? ''
        : AutonomyRunPresenter.stepBody(latestStep).trim();
    final body = stepBody.isNotEmpty
        ? 'Current progress: $stepBody'
        : switch (stage) {
            'queued' =>
              'Current progress: waiting for the local Autopilot worker.',
            'plan' =>
              'Current progress: drafting and reviewing the architect plan.',
            'implement' =>
              'Current progress: implementing in an isolated worktree.',
            'validate' => 'Current progress: running validation gates.',
            'merge' => 'Current progress: checking merge safety.',
            _ => 'Current progress: CHILI is working on this run.',
          };
    final color = _autonomyStatusColor(status);
    return _buildChatBubble(
      icon: Icons.sync,
      title: 'CHILI is working',
      body: body,
      meta: _shortStamp(latestStep['created_at'] ?? run['updated_at']),
      color: color,
      background: _autonomyBubbleBackground(color, alpha: 0.08),
      chips: [
        if (stage.isNotEmpty)
          _miniChip(
            _autonomyStageLabel(stage),
            _autonomyBubbleBackground(Colors.indigo),
            Colors.indigo.shade800,
          ),
        if (stepStatus.isNotEmpty)
          _miniChip(
            _autonomyStatusLabel(stepStatus),
            _autonomyBubbleBackground(Colors.green),
            Colors.green.shade800,
          ),
      ],
    );
  }

  List<Map<String, dynamic>> _fallbackAutonomyMessages(
    Map<String, dynamic> run,
  ) {
    final prompt = run['prompt']?.toString() ?? '';
    final out = <Map<String, dynamic>>[
      if (prompt.isNotEmpty)
        {
          'role': 'user',
          'message_type': 'prompt',
          'content': prompt,
          'created_at': run['created_at'],
        },
    ];
    final plan = _asMap(run['plan']);
    final planBody = AutonomyRunPresenter.planBody(plan);
    if (planBody.isNotEmpty) {
      out.add({
        'role': 'assistant',
        'message_type': 'plan',
        'content': planBody,
        'created_at': run['updated_at'],
      });
    }
    final error = run['error_message']?.toString() ?? '';
    final merge = run['merge_message']?.toString() ?? '';
    if (error.isNotEmpty || merge.isNotEmpty) {
      out.add({
        'role': 'assistant',
        'message_type': 'result',
        'content': error.isNotEmpty
            ? AutonomyRunPresenter.blockedRunMessage(run)
            : merge,
        'created_at': run['finished_at'] ?? run['updated_at'],
      });
    }
    return out;
  }

  Widget _buildAutonomyMessageBubble(Map<String, dynamic> message) {
    final role = message['role']?.toString() ?? 'assistant';
    final type = message['message_type']?.toString() ?? 'chat';
    final body = message['content']?.toString() ?? '';
    final imagePaths = _messageImagePaths(message);
    final isUser = role == 'user';
    final scheme = Theme.of(context).colorScheme;
    final color = isUser
        ? scheme.primary
        : switch (type) {
            'plan' => _autonomyStatusColor('completed'),
            'validation' => Colors.cyan.shade700,
            'result' => _autonomyStatusColor('completed'),
            'error' => _autonomyStatusColor('blocked'),
            _ => scheme.secondary,
          };
    final title = isUser
        ? 'You'
        : switch (type) {
            'plan' => 'CHILI Architect',
            'validation' => 'UI/UX validation',
            'result' => 'CHILI result',
            'status' => 'CHILI',
            _ => 'CHILI',
          };
    final icon = isUser
        ? Icons.person_outline
        : switch (type) {
            'plan' => Icons.account_tree_outlined,
            'validation' => Icons.image_search_outlined,
            'result' => Icons.check_circle_outline,
            _ => Icons.auto_awesome,
          };
    return _buildChatBubble(
      icon: icon,
      title: title,
      body: body,
      meta: _shortStamp(message['created_at']),
      alignRight: isUser,
      color: color,
      background: _autonomyBubbleBackground(color, alpha: isUser ? 0.12 : 0.08),
      imagePaths: imagePaths,
    );
  }

  List<String> _messageImagePaths(Map<String, dynamic> message) {
    final metadata = _asMap(message['metadata']);
    final attachments = _asMapList(metadata['attachments']);
    return attachments
        .map((item) => item['path']?.toString() ?? '')
        .where((path) => path.trim().isNotEmpty)
        .toList();
  }

  Widget _buildChatBubble({
    required IconData icon,
    required String title,
    required Color color,
    required Color background,
    String body = '',
    String meta = '',
    List<Widget> chips = const [],
    List<String> imagePaths = const [],
    bool alignRight = false,
  }) {
    final content = ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: _autopilotBubbleMaxWidth),
      child: Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: background,
          border: Border.all(color: color.withValues(alpha: 0.18)),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, color: color, size: 18),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(title,
                      style: const TextStyle(fontWeight: FontWeight.w700),
                      overflow: TextOverflow.ellipsis),
                ),
                if (meta.isNotEmpty)
                  Text(meta,
                      style: TextStyle(color: _mutedTextColor(), fontSize: 11)),
              ],
            ),
            if (body.trim().isNotEmpty) ...[
              const SizedBox(height: 8),
              SelectableText(
                body.length > _autopilotMessagePreviewLimit
                    ? '${body.substring(0, _autopilotMessagePreviewLimit)}...'
                    : body,
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onSurface,
                  fontSize: 13,
                  height: 1.35,
                ),
              ),
            ],
            if (imagePaths.isNotEmpty) ...[
              const SizedBox(height: 10),
              _buildAutopilotImagePreviewStrip(imagePaths, removable: false),
            ],
            if (chips.isNotEmpty) ...[
              const SizedBox(height: 8),
              Wrap(spacing: 6, runSpacing: 6, children: chips),
            ],
          ],
        ),
      ),
    );
    return Align(
      alignment: alignRight ? Alignment.centerRight : Alignment.centerLeft,
      child: content,
    );
  }

  Widget _buildAutopilotImagePreviewStrip(
    List<String> paths, {
    required bool removable,
  }) {
    final border = _autonomyDividerColor();
    return SizedBox(
      height: _autopilotImagePreviewSize,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: paths.length,
        separatorBuilder: (_, __) => const SizedBox(width: 8),
        itemBuilder: (context, index) {
          final path = paths[index];
          return Stack(
            clipBehavior: Clip.none,
            children: [
              Tooltip(
                message: _autopilotFileName(path),
                child: Container(
                  width: _autopilotImagePreviewSize,
                  height: _autopilotImagePreviewSize,
                  decoration: BoxDecoration(
                    border: Border.all(color: border),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  clipBehavior: Clip.antiAlias,
                  child: Image.file(
                    File(path),
                    fit: BoxFit.cover,
                    errorBuilder: (_, __, ___) => Center(
                      child: Icon(
                        Icons.broken_image_outlined,
                        color: _mutedTextColor(),
                      ),
                    ),
                  ),
                ),
              ),
              if (removable)
                Positioned(
                  top: -8,
                  right: -8,
                  child: IconButton.filledTonal(
                    tooltip: 'Remove image',
                    visualDensity: VisualDensity.compact,
                    onPressed: () => _removeAutopilotPendingImage(index),
                    icon: const Icon(Icons.close, size: 16),
                  ),
                ),
            ],
          );
        },
      ),
    );
  }

  Widget _buildAutonomyComposer() {
    final dropColor = Theme.of(context).colorScheme.primary;
    final composer = Container(
      padding: const EdgeInsets.fromLTRB(18, 12, 18, 14),
      decoration: BoxDecoration(
        color: _autonomySidebarColor(),
        border: Border(top: BorderSide(color: _autonomyDividerColor())),
      ),
      child: Column(
        children: [
          if (_activeAutonomyRun?['status']?.toString() ==
                  _autopilotStatusAwaitingApproval &&
              _activeAutonomyRun?['plan_status']?.toString() ==
                  _autopilotStatusAwaitingApproval) ...[
            Builder(builder: (context) {
              final run = _asMap(_activeAutonomyRun);
              final approvalReady =
                  AutonomyRunPresenter.architectApprovalReady(run);
              final blocker =
                  AutonomyRunPresenter.architectApprovalBlocker(run);
              return Container(
                width: double.infinity,
                margin: const EdgeInsets.only(bottom: 10),
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: _autonomyBubbleBackground(
                    approvalReady ? Colors.teal : Colors.orange,
                    alpha: 0.10,
                  ),
                  border: Border.all(
                    color: (approvalReady ? Colors.teal : Colors.orange)
                        .withValues(alpha: 0.22),
                  ),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Wrap(
                  spacing: 10,
                  runSpacing: 8,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    Text(
                      approvalReady
                          ? 'Plan Mode is waiting for approval.'
                          : 'Approval is not available yet.',
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.onSurface,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    if (approvalReady)
                      FilledButton.icon(
                        onPressed: _autonomyBusy ? null : _approveAutopilotPlan,
                        icon: const Icon(Icons.play_arrow, size: 18),
                        label: const Text('Approve and implement'),
                      ),
                    Text(
                      approvalReady
                          ? 'Or send feedback below to revise it.'
                          : blocker,
                      style: TextStyle(color: _mutedTextColor(), fontSize: 12),
                    ),
                  ],
                ),
              );
            }),
          ],
          if (_canSendAutopilotCommand) ...[
            _buildAutopilotCommandStrip(),
            const SizedBox(height: 10),
          ],
          if (_autopilotPendingImages.isNotEmpty) ...[
            _buildAutopilotImagePreviewStrip(
              _autopilotPendingImages,
              removable: true,
            ),
            const SizedBox(height: 10),
          ],
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              IconButton.outlined(
                tooltip: 'Attach images',
                onPressed: _autonomyBusy ? null : _pickAutopilotImages,
                icon: Icon(_autopilotPendingImages.isEmpty
                    ? Icons.attach_file
                    : Icons.collections_outlined),
              ),
              const SizedBox(width: 8),
              IconButton.outlined(
                tooltip: 'Paste image from clipboard',
                onPressed: _autonomyBusy ? null : _pasteAutopilotImage,
                icon: const Icon(Icons.content_paste),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: CallbackShortcuts(
                  bindings: <ShortcutActivator, VoidCallback>{
                    const SingleActivator(LogicalKeyboardKey.enter):
                        _submitAutopilotComposer,
                    const SingleActivator(LogicalKeyboardKey.numpadEnter):
                        _submitAutopilotComposer,
                  },
                  child: TextField(
                    controller: _autopilotPromptCtrl,
                    focusNode: _autopilotPromptFocusNode,
                    minLines: 2,
                    maxLines: 5,
                    inputFormatters: [
                      TextInputFormatter.withFunction(
                        _formatAutopilotComposerInput,
                      ),
                    ],
                    onSubmitted: (_) => _submitAutopilotComposer(),
                    decoration: InputDecoration(
                      labelText: _activeAutonomyRun == null ||
                              _autonomyTerminal(_activeAutonomyRun)
                          ? 'Start an Autopilot chat'
                          : _autopilotPendingImages.isNotEmpty
                              ? _autopilotAttachedImagePromptLabel
                              : 'Message CHILI about this run',
                      alignLabelWithHint: true,
                      border: const OutlineInputBorder(),
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              SizedBox(
                height: 48,
                child: FilledButton.icon(
                  onPressed: _autonomyBusy || _codeRepos.isEmpty
                      ? null
                      : () => unawaited(_startAutopilot()),
                  icon: _autonomyBusy
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.send),
                  label: Text(_activeAutonomyRun == null ||
                          _autonomyTerminal(_activeAutonomyRun)
                      ? 'Start'
                      : 'Send'),
                ),
              ),
            ],
          ),
          if (_codeRepos.isEmpty) ...[
            const SizedBox(height: 8),
            Text(
              'No registered local repos are visible to this desktop backend.',
              style: TextStyle(
                  color: _autonomyStatusColor('blocked'), fontSize: 13),
            ),
          ],
        ],
      ),
    );
    return DropTarget(
      enable: !_autonomyBusy,
      onDragEntered: (_) {
        if (!_autopilotDropActive) {
          setState(() => _autopilotDropActive = true);
        }
      },
      onDragExited: (_) {
        if (_autopilotDropActive) {
          setState(() => _autopilotDropActive = false);
        }
      },
      onDragDone: _handleAutopilotImageDrop,
      child: Stack(
        children: [
          composer,
          if (_autopilotDropActive)
            Positioned.fill(
              child: IgnorePointer(
                child: Container(
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: _autonomyBubbleBackground(dropColor, alpha: 0.18),
                    border: Border.all(
                      color: dropColor.withValues(alpha: 0.42),
                      width: 2,
                    ),
                  ),
                  child: Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
                    decoration: BoxDecoration(
                      color: _autonomySidebarColor(),
                      border: Border.all(color: _autonomyDividerColor()),
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.add_photo_alternate_outlined,
                            color: dropColor),
                        const SizedBox(width: 8),
                        Text(
                          'Drop images to attach',
                          style: TextStyle(
                            color: Theme.of(context).colorScheme.onSurface,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildAutopilotCommandStrip() {
    return SizedBox(
      width: double.infinity,
      child: Wrap(
        spacing: 8,
        runSpacing: 8,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          Text(
            'Commands',
            style: TextStyle(
              color: _mutedTextColor(),
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
          for (final action in _autopilotCommandQuickActions)
            ActionChip(
              tooltip: action['tooltip'],
              visualDensity: VisualDensity.compact,
              avatar: Icon(
                _autopilotCommandIcon(action['command'] ?? ''),
                size: 16,
              ),
              label: Text(action['label'] ?? ''),
              onPressed: _autonomyBusy
                  ? null
                  : () => _submitAutopilotCommand(action['command'] ?? ''),
            ),
        ],
      ),
    );
  }

  IconData _autopilotCommandIcon(String command) {
    switch (command) {
      case _autopilotSlashHelp:
        return Icons.help_outline;
      case _autopilotSlashStatus:
        return Icons.monitor_heart_outlined;
      case _autopilotSlashAgents:
        return Icons.groups_2_outlined;
      case _autopilotSlashPlan:
        return Icons.account_tree_outlined;
      case _autopilotSlashModel:
        return Icons.memory_outlined;
      case _autopilotSlashDoctor:
        return Icons.health_and_safety_outlined;
      case _autopilotSlashQuality:
        return Icons.verified_outlined;
      case _autopilotSlashScheduleCodex:
      case _autopilotSlashScheduleCodexActive:
      case _autopilotSlashScheduleCodexAlwaysOn:
      case _autopilotSlashScheduleCodexPause:
        return Icons.schedule_send_outlined;
      case _autopilotSlashQuestions:
        return Icons.contact_support_outlined;
      default:
        return Icons.terminal_outlined;
    }
  }

  Widget _buildAutonomyRepoPicker() {
    final selectedRepo = _selectedAutonomyRepo();
    final canRemoveRepo =
        selectedRepo != null && !_isProtectedAutonomyRepo(selectedRepo);
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: DropdownButtonFormField<int>(
            key: ValueKey<int?>(_autonomyRepoId),
            initialValue: _autonomyRepoId,
            decoration: const InputDecoration(
              labelText: 'Local repo',
              border: OutlineInputBorder(),
              isDense: true,
            ),
            items: [
              for (final repo in _codeRepos)
                if (_asInt(repo['id']) != null)
                  DropdownMenuItem<int>(
                    value: _asInt(repo['id']),
                    child: Text(
                      '${repo['name']?.toString() ?? 'repo ${repo['id']}'}'
                      ' - ${_repoPathLabel(repo)}'
                      '${repo['reachable_in_current_runtime'] == true ? '' : ' (not reachable here)'}',
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
            ],
            onChanged: _autonomyBusy
                ? null
                : (value) {
                    setState(() {
                      _autonomyRepoId = value;
                      if (_autonomyHistoryRepoId == null) {
                        _autonomyAgentProfileId = null;
                      }
                    });
                    if (_autonomyHistoryRepoId == null) {
                      unawaited(() async {
                        await _loadAutonomyAgentProfiles();
                        await _loadAutonomyAgentReadiness();
                        await _loadAutonomyRuns();
                      }());
                    }
                  },
          ),
        ),
        const SizedBox(width: 8),
        Tooltip(
          message: 'Add local repo',
          child: IconButton.filledTonal(
            onPressed: _autonomyBusy ? null : _showAddCodeRepoDialog,
            icon: const Icon(Icons.create_new_folder_outlined, size: 20),
          ),
        ),
        const SizedBox(width: 6),
        Tooltip(
          message: canRemoveRepo
              ? 'Remove selected repo'
              : 'The CHILI D-drive repo is protected',
          child: IconButton.outlined(
            onPressed: _autonomyBusy || !canRemoveRepo
                ? null
                : _confirmRemoveSelectedCodeRepo,
            icon: const Icon(Icons.folder_off_outlined, size: 20),
          ),
        ),
      ],
    );
  }

  Widget _buildSelectedRepoPathHint() {
    final repoId = _autonomyRepoId;
    if (repoId == null) return const SizedBox.shrink();
    final repo = _codeRepos.cast<Map<String, dynamic>?>().firstWhere(
          (item) => _asInt(item?['id']) == repoId,
          orElse: () => null,
        );
    if (repo == null) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            repo['preferred_for_autopilot'] == true
                ? Icons.verified_outlined
                : Icons.folder_outlined,
            size: 14,
            color: _mutedTextColor(),
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              _repoPathLabel(repo),
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: _mutedTextColor(),
                fontSize: 11,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyAgentInspector(
    Map<String, dynamic> profile, {
    required Map<String, dynamic>? run,
  }) {
    if (profile.isEmpty) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Agent profile', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          Text(
            'Generate repo agents to assign chats to paused macro, micro, and specialist operators.',
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
          const SizedBox(height: 10),
          OutlinedButton.icon(
            onPressed: _autonomyBusy || _autonomyAgentRepoId == null
                ? null
                : _bootstrapAutonomyAgents,
            icon: const Icon(Icons.auto_awesome, size: 18),
            label: const Text('Generate agents'),
          ),
        ],
      );
    }
    final status = profile['status']?.toString() ?? '';
    final tier = profile['tier']?.toString() ?? '';
    final statusColor = _agentStatusColor(status);
    final permissions = _agentEnabledPermissionLabels(profile);
    final promptSetting = _asMap(profile['prompt_setting']);
    final systemPrompt = promptSetting['system_prompt']?.toString() ?? '';
    final modelPolicy = profile['model_policy']?.toString().trim() ??
        _autopilotModelPolicyLocalFirst;
    final codexAutomation = _asMap(promptSetting['codex_automation']);
    final isCodexSeed =
        _isCodexAutomationAgent(promptSetting) || codexAutomation.isNotEmpty;
    final promptFreshnessLabel = _agentPromptFreshnessLabel(profile);
    final promptFreshnessColor = _agentPromptFreshnessColor(profile);
    final operatingState = _asMap(profile[_autopilotAgentOperatingState]);
    final questions = _asMapList(run?['operator_questions']);
    final pendingQuestions = questions
        .where((question) => question['status']?.toString() == 'pending')
        .toList();
    final pendingQuestionCount = pendingQuestions.isNotEmpty
        ? pendingQuestions.length
        : _asInt(profile['pending_question_count']) ?? 0;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Agent profile', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(_agentIcon(tier), color: statusColor, size: 20),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    _agentDisplayName(profile),
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  Text(
                    _agentRoleLabel(profile),
                    style: TextStyle(color: _mutedTextColor(), fontSize: 12),
                  ),
                ],
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            _miniChip(
              _agentStatusLabel(status),
              _autonomyBubbleBackground(statusColor),
              statusColor,
            ),
            if (tier.isNotEmpty)
              _miniChip(
                _agentTierLabel(tier),
                _autonomyBubbleBackground(Colors.indigo),
                Colors.indigo.shade800,
              ),
            _miniChip(
              _agentModelPolicyLabel(modelPolicy),
              _autonomyBubbleBackground(Colors.deepPurple),
              Colors.deepPurple.shade700,
            ),
            _miniChip(
              _agentPromptSourceLabel(promptSetting),
              _autonomyBubbleBackground(Colors.cyan),
              Colors.cyan.shade900,
            ),
            if (promptFreshnessLabel.isNotEmpty)
              _miniChip(
                promptFreshnessLabel,
                _autonomyBubbleBackground(promptFreshnessColor),
                promptFreshnessColor,
              ),
            _miniChip(
              _agentScheduleSummary(profile),
              _autonomyBubbleBackground(Colors.blueGrey),
              Colors.blueGrey.shade800,
            ),
            if (pendingQuestionCount > 0)
              _miniChip(
                '$pendingQuestionCount question${pendingQuestionCount == 1 ? '' : 's'}',
                _autonomyBubbleBackground(Colors.orange),
                Colors.orange.shade900,
              ),
          ],
        ),
        if (operatingState.isNotEmpty) ...[
          const SizedBox(height: 10),
          _buildAutonomyAgentOperatingState(operatingState),
        ],
        if (permissions.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text('Permissions', style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 6),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: permissions
                .map(
                  (permission) => _miniChip(
                    permission,
                    _autonomyBubbleBackground(Colors.teal),
                    Colors.teal.shade900,
                  ),
                )
                .toList(),
          ),
        ],
        if (isCodexSeed) ...[
          const SizedBox(height: 12),
          _buildAutonomyCodexAutomationSeed(profile, codexAutomation),
        ],
        const SizedBox(height: 12),
        _buildAutonomyAgentReadiness(),
        const SizedBox(height: 12),
        _buildAutonomyAgentScheduleControls(profile),
        const SizedBox(height: 12),
        _buildAutonomyAgentSchedulerRuntime(),
        const SizedBox(height: 12),
        _buildAutonomyAgentPermissionControls(profile),
        if (systemPrompt.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text('Operating prompt',
              style: Theme.of(context).textTheme.labelLarge),
          const SizedBox(height: 4),
          Text(
            _agentSystemPromptPreview(systemPrompt),
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
        ],
        if (pendingQuestions.isNotEmpty) ...[
          const SizedBox(height: 12),
          _buildAutonomyOperatorQuestions(pendingQuestions),
        ],
        const SizedBox(height: 12),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            OutlinedButton.icon(
              onPressed: _autonomyBusy
                  ? null
                  : () => _setAutonomyAgentPaused(
                        profile,
                        paused: status != _autopilotAgentStatusPaused,
                      ),
              icon: Icon(
                status == _autopilotAgentStatusPaused
                    ? Icons.play_circle_outline
                    : Icons.pause_circle_outline,
                size: 18,
              ),
              label: Text(
                status == _autopilotAgentStatusPaused ? 'Resume' : 'Pause',
              ),
            ),
            OutlinedButton.icon(
              onPressed: _autonomyBusy || status == _autopilotAgentStatusPaused
                  ? null
                  : () => _startAutonomyAgentCycle(profile),
              icon: const Icon(Icons.schedule_send_outlined, size: 18),
              label: const Text('Run cycle'),
            ),
            OutlinedButton.icon(
              onPressed: _autonomyBusy
                  ? null
                  : () => _showEditAutonomyAgentPromptDialog(profile),
              icon: const Icon(Icons.tune_outlined, size: 18),
              label: const Text('Prompt/model'),
            ),
          ],
        ),
        if (status == _autopilotAgentStatusPaused) ...[
          const SizedBox(height: 8),
          Text(
            'Paused by default. Scheduled cycles can observe, research, summarize, and draft plans only until you explicitly enable more.',
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
        ],
      ],
    );
  }

  Widget _buildAutonomyAgentOperatingState(Map<String, dynamic> state) {
    final stateValue = state['state']?.toString() ?? '';
    final color = _agentOperatingStateColor(stateValue);
    final title = state['title']?.toString().trim() ?? 'Agent state';
    final detail = state['detail']?.toString().trim() ?? '';
    final actionLabel = state['next_action_label']?.toString().trim() ?? '';
    final safety = state['safety']?.toString().trim() ?? '';
    final safetyDetail = state['safety_detail']?.toString().trim() ?? '';
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.08),
        border: Border.all(color: color.withValues(alpha: 0.26)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.route_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  title,
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              if (actionLabel.isNotEmpty)
                _miniChip(
                  actionLabel,
                  _autonomyBubbleBackground(color),
                  color,
                ),
            ],
          ),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              detail,
              style: TextStyle(color: _mutedTextColor(), fontSize: 12),
            ),
          ],
          const SizedBox(height: 7),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              if (stateValue.isNotEmpty)
                _miniChip(
                  stateValue.replaceAll('_', ' '),
                  _autonomyBubbleBackground(color),
                  color,
                ),
              if (safety.isNotEmpty)
                _miniChip(
                  _agentOperatingSafetyLabel(safety),
                  _autonomyBubbleBackground(_agentOperatingSafetyColor(safety)),
                  _agentOperatingSafetyColor(safety),
                ),
            ],
          ),
          if (safetyDetail.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              safetyDetail,
              style: TextStyle(color: _mutedTextColor(), fontSize: 11),
            ),
          ],
        ],
      ),
    );
  }

  Color _agentOperatingStateColor(String state) {
    switch (state) {
      case _autopilotAgentOperatingStateNeedsInput:
      case _autopilotAgentOperatingStateNeedsSync:
      case _autopilotAgentOperatingStatePausedSourceActive:
        return Colors.orange;
      case _autopilotAgentOperatingStateCustomPrompt:
        return Colors.indigo;
      case _autopilotAgentOperatingStateRunning:
      case _autopilotAgentOperatingStateScheduled:
        return _autonomyStatusColor('running');
      case _autopilotAgentOperatingStateReady:
      case _autopilotAgentOperatingStateManualReady:
        return _autonomyStatusColor('completed');
      case _autopilotAgentOperatingStatePaused:
        return Colors.blueGrey;
      default:
        return Colors.blueGrey;
    }
  }

  Color _agentOperatingSafetyColor(String safety) {
    switch (safety) {
      case _autopilotAgentOperatingSafetyMergeCapable:
      case _autopilotAgentOperatingSafetyPatchCapable:
        return Colors.orange;
      case _autopilotAgentOperatingSafetyPlanOnly:
        return Colors.teal;
      default:
        return Colors.blueGrey;
    }
  }

  String _agentOperatingSafetyLabel(String safety) {
    switch (safety) {
      case _autopilotAgentOperatingSafetyMergeCapable:
        return 'merge capable';
      case _autopilotAgentOperatingSafetyPatchCapable:
        return 'patch capable';
      case _autopilotAgentOperatingSafetyPlanOnly:
        return 'plan only';
      default:
        return safety.replaceAll('_', ' ');
    }
  }

  Widget _buildAutonomyAgentSchedulerRuntime() {
    final scheduler = _autonomyAgentScheduler;
    if (scheduler.isEmpty) return const SizedBox.shrink();
    final enabled = scheduler['enabled'] == true;
    final running = scheduler['running'] == true;
    final mode = scheduler['mode']?.toString().trim() ?? '';
    final role = scheduler['scheduler_role']?.toString().trim() ?? '';
    final interval = _asInt(scheduler['interval_seconds']);
    final lastPoll =
        scheduler[_autopilotSchedulerLastPollAt]?.toString().trim() ?? '';
    final nextPoll =
        scheduler[_autopilotSchedulerNextPollAt]?.toString().trim() ?? '';
    final lastError =
        scheduler[_autopilotSchedulerLastError]?.toString().trim() ?? '';
    final lastResult = _asMap(scheduler[_autopilotSchedulerLastResult]);
    final lastStarted = _asInt(lastResult['started']);
    final lastWorkerStarted =
        _asInt(lastResult[_autopilotSchedulerWorkerStarted]);
    final lastWorkerDeferred =
        _asInt(lastResult[_autopilotSchedulerWorkerDeferredCount]);
    final lastChecked = _asInt(lastResult['checked']);
    final lastSkipped = _asInt(lastResult['skipped_count']);
    final lastSource =
        lastResult[_autopilotSchedulerResultSource]?.toString().trim() ?? '';
    final activeWorkers = _asInt(scheduler[_autopilotSchedulerActiveWorkers]);
    final maxWorkers = _asInt(scheduler[_autopilotSchedulerMaxWorkers]);
    final canWake = !_autonomyBusy && _autonomyAgentRepoId != null;
    final color = !enabled
        ? Colors.blueGrey
        : running
            ? Colors.green
            : Colors.orange;
    final title = !enabled
        ? 'Agent runtime disabled'
        : running
            ? 'Agent runtime running'
            : 'Agent runtime waiting';
    final modeLabel = mode == 'standalone'
        ? 'local loop'
        : mode == 'apscheduler'
            ? 'APScheduler'
            : 'scheduler';
    final detail = mode == 'standalone'
        ? 'Desktop/API-only mode keeps agent queues warm, starts due work, and honors rest windows without starting the heavy trading scheduler.'
        : 'Agent runtimes are delegated to the backend scheduler role${role.isEmpty ? '' : ' $role'}.';
    final queue = _asMap(_autonomyAgentReadiness[_autopilotRuntimeQueue]);
    final queued = _asInt(queue['queued_count']);
    final active = _asInt(queue['active_count']);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.08),
        border: Border.all(color: color.withValues(alpha: 0.28)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(
                running ? Icons.sync : Icons.sync_disabled_outlined,
                size: 17,
                color: color,
              ),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  title,
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            detail,
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                modeLabel,
                _autonomyBubbleBackground(Colors.indigo),
                Colors.indigo.shade800,
              ),
              if (role.isNotEmpty)
                _miniChip(
                  role,
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
              if (interval != null)
                _miniChip(
                  '${interval}s',
                  _autonomyBubbleBackground(color),
                  color,
                ),
              if (queued != null)
                _miniChip(
                  '$queued queued',
                  _autonomyBubbleBackground(
                    queued == 0 ? Colors.blueGrey : Colors.orange,
                  ),
                  queued == 0
                      ? Colors.blueGrey.shade800
                      : Colors.orange.shade900,
                ),
              if (active != null)
                _miniChip(
                  '$active active',
                  _autonomyBubbleBackground(
                    active == 0 ? Colors.blueGrey : Colors.green,
                  ),
                  active == 0
                      ? Colors.blueGrey.shade800
                      : Colors.green.shade800,
                ),
              if (activeWorkers != null && maxWorkers != null)
                _miniChip(
                  '$activeWorkers/$maxWorkers workers',
                  _autonomyBubbleBackground(
                    activeWorkers >= maxWorkers ? Colors.orange : Colors.teal,
                  ),
                  activeWorkers >= maxWorkers
                      ? Colors.orange.shade900
                      : Colors.teal.shade900,
                ),
              if (lastPoll.isNotEmpty)
                _miniChip(
                  'last ${_shortStamp(lastPoll)}',
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
              if (nextPoll.isNotEmpty)
                _miniChip(
                  'next ${_shortStamp(nextPoll)}',
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              if (lastStarted != null)
                _miniChip(
                  '$lastStarted started',
                  _autonomyBubbleBackground(
                    lastStarted == 0 ? Colors.blueGrey : Colors.green,
                  ),
                  lastStarted == 0
                      ? Colors.blueGrey.shade800
                      : Colors.green.shade800,
                ),
              if (lastWorkerStarted != null)
                _miniChip(
                  '$lastWorkerStarted workers launched',
                  _autonomyBubbleBackground(
                    lastWorkerStarted == 0 ? Colors.blueGrey : Colors.green,
                  ),
                  lastWorkerStarted == 0
                      ? Colors.blueGrey.shade800
                      : Colors.green.shade800,
                ),
              if (lastWorkerDeferred != null && lastWorkerDeferred > 0)
                _miniChip(
                  '$lastWorkerDeferred deferred',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
              if (lastSource == _autopilotSchedulerResultSourceManual)
                _miniChip(
                  'manual wake',
                  _autonomyBubbleBackground(Colors.deepPurple),
                  Colors.deepPurple.shade700,
                ),
              if (lastChecked != null)
                _miniChip(
                  '$lastChecked checked',
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
              if (lastSkipped != null && lastSkipped > 0)
                _miniChip(
                  '$lastSkipped skipped',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
            ],
          ),
          if (lastError.isNotEmpty) ...[
            const SizedBox(height: 8),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(Icons.error_outline,
                    color: _autonomyStatusColor('failed'), size: 14),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    lastError,
                    style: TextStyle(
                      color: _mutedTextColor(),
                      fontSize: 12,
                    ),
                  ),
                ),
              ],
            ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              FilledButton.tonalIcon(
                onPressed:
                    canWake ? () => _runAutonomyAgentSchedulesNow() : null,
                icon: const Icon(Icons.flash_on_outlined, size: 16),
                label: const Text('Wake Codex'),
              ),
              OutlinedButton.icon(
                onPressed: canWake
                    ? () => _runAutonomyAgentSchedulesNow(codexOnly: false)
                    : null,
                icon: const Icon(Icons.groups_outlined, size: 16),
                label: const Text('Wake all'),
              ),
              OutlinedButton.icon(
                onPressed: _autonomyBusy
                    ? null
                    : () async {
                        await _loadAutonomyAgentSchedulerStatus();
                        await _loadAutonomyAgentReadiness();
                      },
                icon: const Icon(Icons.refresh, size: 16),
                label: const Text('Refresh'),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyAgentReadiness() {
    final readiness = _autonomyAgentReadiness;
    if (readiness.isEmpty) return const SizedBox.shrink();
    final status = readiness['status']?.toString() ?? '';
    final score = _asInt(readiness['score']);
    final checks = _asMapList(readiness['checks']);
    final agents = _asMap(readiness['agents']);
    final codex = _asMap(readiness['codex_automations']);
    final contractCoverage = _asMap(codex['contract_coverage']);
    final codexSchedule = _asMap(codex['schedule_mirror']);
    final codexProfiles = _asMapList(codex['profiles']);
    final teams = _asMapList(readiness['teams']);
    final localModel = _asMap(readiness['local_model']);
    final qualityScorecard = _asMap(readiness[_autopilotQualityScorecard]);
    final codexBench = _asMap(readiness[_autopilotCodexBench]);
    final qualityMonitor = _asMap(readiness[_autopilotAgentQualityMonitor]);
    final capabilityAudit = _asMap(readiness[_autopilotAgentCapabilityAudit]);
    final runtimeQueue = _asMap(readiness[_autopilotRuntimeQueue]);
    final operatorInbox = _asMap(readiness[_autopilotOperatorInbox]);
    final codexAlignment = _asMap(readiness[_autopilotCodexAlignment]);
    final qualityReviews =
        _asMap(qualityScorecard[_autopilotQualityArchitectReviews]);
    final scheduledQuality =
        _asMap(qualityScorecard[_autopilotQualityScheduled]);
    final qualityValidation =
        _asMap(qualityScorecard[_autopilotQualityValidation]);
    final staleCodex = _asStringList(codex['stale_profile_keys']);
    final color =
        status == 'ready' ? _autonomyStatusColor('completed') : Colors.orange;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.08),
        border: Border.all(color: color.withValues(alpha: 0.28)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.fact_check_outlined, color: color, size: 17),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Agent OS readiness',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              if (score != null)
                _miniChip(
                  '$score%',
                  _autonomyBubbleBackground(color),
                  color,
                ),
            ],
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                '${_asInt(agents['total']) ?? 0} agents',
                _autonomyBubbleBackground(Colors.indigo),
                Colors.indigo.shade800,
              ),
              _miniChip(
                '${_asInt(codex['current_imported']) ?? _asInt(codex['imported']) ?? 0}/${_asInt(codex['matching']) ?? 0} codex',
                _autonomyBubbleBackground(Colors.cyan),
                Colors.cyan.shade900,
              ),
              if ((_asInt(codex['historical_imported']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(codex['historical_imported'])} historical',
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
              if (staleCodex.isNotEmpty)
                _miniChip(
                  '${staleCodex.length} stale prompts',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
              if ((_asInt(contractCoverage['total']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(contractCoverage['workspace_count']) ?? 0}/${_asInt(contractCoverage['total']) ?? 0} contracts',
                  _autonomyBubbleBackground(
                    (_asInt(contractCoverage['missing_workspace_count']) ??
                                0) ==
                            0
                        ? Colors.teal
                        : Colors.orange,
                  ),
                  (_asInt(contractCoverage['missing_workspace_count']) ?? 0) ==
                          0
                      ? Colors.teal.shade900
                      : Colors.orange.shade900,
                ),
              if ((_asInt(contractCoverage['d_drive_aligned_count']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(contractCoverage['d_drive_aligned_count'])} D-drive',
                  _autonomyBubbleBackground(Colors.teal),
                  Colors.teal.shade900,
                ),
              if ((_asInt(contractCoverage['key_command_profile_count']) ?? 0) >
                  0)
                _miniChip(
                  '${_asInt(contractCoverage['key_command_profile_count'])} command agents',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
              if ((_asInt(codexSchedule['source_active']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(codexSchedule['source_active_enabled']) ?? 0}/${_asInt(codexSchedule['source_active']) ?? 0} source-active',
                  _autonomyBubbleBackground(Colors.deepPurple),
                  Colors.deepPurple.shade700,
                ),
              if ((localModel['model']?.toString().trim() ?? '').isNotEmpty)
                _miniChip(
                  localModel['coding_ready'] == true
                      ? localModel['model'].toString()
                      : '${localModel['model']} needs coder model',
                  _autonomyBubbleBackground(
                    localModel['coding_ready'] == true
                        ? Colors.green
                        : Colors.orange,
                  ),
                  localModel['coding_ready'] == true
                      ? Colors.green.shade800
                      : Colors.orange.shade900,
                ),
              if (qualityScorecard.isNotEmpty)
                _miniChip(
                  '${_asInt(qualityScorecard['recent_run_count']) ?? 0} recent runs',
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
              if (qualityMonitor.isNotEmpty)
                _miniChip(
                  '${_asInt(qualityMonitor['score']) ?? 0}% quality monitor',
                  _autonomyBubbleBackground(
                    (qualityMonitor['status']?.toString() ?? '') ==
                            _autopilotReadinessPassed
                        ? Colors.teal
                        : Colors.orange,
                  ),
                  (qualityMonitor['status']?.toString() ?? '') ==
                          _autopilotReadinessPassed
                      ? Colors.teal.shade900
                      : Colors.orange.shade900,
                ),
              if (capabilityAudit.isNotEmpty)
                _miniChip(
                  '${_asInt(capabilityAudit['score']) ?? 0}% capability audit',
                  _autonomyBubbleBackground(
                    (capabilityAudit['status']?.toString() ?? '') ==
                            _autopilotReadinessPassed
                        ? Colors.teal
                        : Colors.orange,
                  ),
                  (capabilityAudit['status']?.toString() ?? '') ==
                          _autopilotReadinessPassed
                      ? Colors.teal.shade900
                      : Colors.orange.shade900,
                ),
              if ((_asInt(qualityReviews['total']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(qualityReviews['passed']) ?? 0}/${_asInt(qualityReviews['total']) ?? 0} plan reviews',
                  _autonomyBubbleBackground(
                    (_asInt(qualityReviews['missing_for_approval']) ?? 0) == 0
                        ? Colors.teal
                        : Colors.orange,
                  ),
                  (_asInt(qualityReviews['missing_for_approval']) ?? 0) == 0
                      ? Colors.teal.shade900
                      : Colors.orange.shade900,
                ),
              if ((_asInt(scheduledQuality['repaired']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(scheduledQuality['repaired'])} repaired',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
              if ((_asInt(scheduledQuality['low_quality']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(scheduledQuality['low_quality'])} rejected',
                  _autonomyBubbleBackground(_autonomyStatusColor('failed')),
                  _autonomyStatusColor('failed'),
                ),
              if ((_asInt(qualityValidation['total']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(qualityValidation['passed']) ?? 0}/${_asInt(qualityValidation['total']) ?? 0} validations',
                  _autonomyBubbleBackground(Colors.teal),
                  Colors.teal.shade900,
                ),
              _miniChip(
                '${_asInt(agents['active_schedules']) ?? 0} active schedules',
                _autonomyBubbleBackground(Colors.blueGrey),
                Colors.blueGrey.shade800,
              ),
              if ((_asInt(agents['always_on_schedules']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(agents['always_on_schedules'])} always-on',
                  _autonomyBubbleBackground(Colors.green),
                  Colors.green.shade800,
                ),
              if (runtimeQueue.isNotEmpty)
                _miniChip(
                  '${_asInt(runtimeQueue['queued_count']) ?? 0} queued',
                  _autonomyBubbleBackground(
                    (_asInt(runtimeQueue['queued_count']) ?? 0) == 0
                        ? Colors.blueGrey
                        : Colors.orange,
                  ),
                  (_asInt(runtimeQueue['queued_count']) ?? 0) == 0
                      ? Colors.blueGrey.shade800
                      : Colors.orange.shade900,
                ),
              if (operatorInbox.isNotEmpty)
                _miniChip(
                  '${_asInt(operatorInbox['total_action_count']) ?? 0} inbox',
                  _autonomyBubbleBackground(
                    (_asInt(operatorInbox['total_action_count']) ?? 0) == 0
                        ? Colors.blueGrey
                        : Colors.orange,
                  ),
                  (_asInt(operatorInbox['total_action_count']) ?? 0) == 0
                      ? Colors.blueGrey.shade800
                      : Colors.orange.shade900,
                ),
              if (codexAlignment.isNotEmpty)
                _miniChip(
                  '${_asInt(codexAlignment['score']) ?? 0}% codex match',
                  _autonomyBubbleBackground(
                    (codexAlignment['status']?.toString() ?? '') == 'passed'
                        ? Colors.teal
                        : Colors.orange,
                  ),
                  (codexAlignment['status']?.toString() ?? '') == 'passed'
                      ? Colors.teal.shade900
                      : Colors.orange.shade900,
                ),
              if (codexBench.isNotEmpty)
                _miniChip(
                  '${_asInt(codexBench['current_imported_count']) ?? 0}/${_asInt(codexBench['matching_count']) ?? 0} mirrored',
                  _autonomyBubbleBackground(
                    (codexBench['status']?.toString() ?? '') ==
                            _autopilotReadinessPassed
                        ? Colors.teal
                        : Colors.orange,
                  ),
                  (codexBench['status']?.toString() ?? '') ==
                          _autopilotReadinessPassed
                      ? Colors.teal.shade900
                      : Colors.orange.shade900,
                ),
              if ((_asInt(agents['pending_questions']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(agents['pending_questions'])} questions',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
            ],
          ),
          if ((_asInt(codexSchedule['total']) ?? 0) > 0) ...[
            const SizedBox(height: 10),
            _buildAutonomyCodexScheduleMirror(codexSchedule),
          ],
          if (codexBench.isNotEmpty) ...[
            const SizedBox(height: 10),
            _buildAutonomyCodexBench(codexBench),
          ],
          if (capabilityAudit.isNotEmpty) ...[
            const SizedBox(height: 10),
            _buildAutonomyAgentCapabilityAudit(capabilityAudit),
          ],
          if (qualityScorecard.isNotEmpty) ...[
            const SizedBox(height: 10),
            _buildAutonomyQualityScorecard(qualityScorecard),
          ],
          if (qualityMonitor.isNotEmpty) ...[
            const SizedBox(height: 10),
            _buildAutonomyAgentQualityMonitor(qualityMonitor),
          ],
          if (runtimeQueue.isNotEmpty) ...[
            const SizedBox(height: 10),
            _buildAutonomyRuntimeQueue(runtimeQueue),
          ],
          if (operatorInbox.isNotEmpty) ...[
            const SizedBox(height: 10),
            _buildAutonomyOperatorInbox(operatorInbox),
          ],
          if (codexAlignment.isNotEmpty) ...[
            const SizedBox(height: 10),
            _buildAutonomyCodexAlignment(codexAlignment),
          ],
          if (teams.isNotEmpty) ...[
            const SizedBox(height: 10),
            _buildAutonomyAgentTeams(teams),
          ],
          if (codexProfiles.isNotEmpty) ...[
            const SizedBox(height: 10),
            _buildAutonomyCodexProfileMatrix(codexProfiles),
          ],
          if (checks.isNotEmpty) ...[
            const SizedBox(height: 8),
            ...checks.take(4).map((check) {
              final checkStatus = check['status']?.toString() ?? '';
              final checkColor = checkStatus == 'passed'
                  ? _autonomyStatusColor('completed')
                  : checkStatus == 'failed'
                      ? _autonomyStatusColor('failed')
                      : Colors.orange;
              return Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(
                      checkStatus == 'passed'
                          ? Icons.check_circle_outline
                          : Icons.info_outline,
                      color: checkColor,
                      size: 14,
                    ),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        '${check['title'] ?? 'Check'}: ${check['detail'] ?? ''}',
                        style:
                            TextStyle(color: _mutedTextColor(), fontSize: 12),
                      ),
                    ),
                  ],
                ),
              );
            }),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyAgentCapabilityAudit(Map<String, dynamic> audit) {
    final status = audit['status']?.toString() ?? '';
    final score = _asInt(audit['score']);
    final color = status == _autopilotReadinessPassed
        ? _autonomyStatusColor('completed')
        : status == _autopilotReadinessFailed
            ? _autonomyStatusColor('failed')
            : Colors.orange;
    final detail = audit['detail']?.toString().trim() ?? '';
    final nextAction =
        audit[_autopilotAgentCapabilityNextActionLabel]?.toString().trim() ??
            '';
    final nextActionDetail =
        audit[_autopilotAgentCapabilityNextActionDetail]?.toString().trim() ??
            '';
    final gaps = _asMapList(audit[_autopilotAgentCapabilityGaps]);
    final capabilities = _asMapList(audit[_autopilotAgentCapabilityItems]);
    final visibleCapabilities =
        gaps.isNotEmpty ? gaps : capabilities.take(5).toList();
    bool hasCapabilityAction(String action) {
      if ((audit[_autopilotAgentCapabilityNextAction]?.toString() ?? '') ==
          action) {
        return true;
      }
      for (final item in [...gaps, ...capabilities]) {
        if ((item[_autopilotAgentCapabilityNextAction]?.toString() ?? '') ==
            action) {
          return true;
        }
      }
      return false;
    }

    final canAdoptCodexLoop =
        hasCapabilityAction(_autopilotAgentCapabilityActionEnableAlwaysOn) &&
            !_autonomyBusy &&
            _autonomyAgentRepoId != null;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.06),
        border: Border.all(color: color.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.account_tree_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Agent OS capability audit',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              if (score != null)
                _miniChip('$score%', _autonomyBubbleBackground(color), color),
              const SizedBox(width: 6),
              _miniChip(
                status.isEmpty ? 'checking' : status.replaceAll('_', ' '),
                _autonomyBubbleBackground(color),
                color,
              ),
            ],
          ),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              detail,
              style: TextStyle(color: _mutedTextColor(), fontSize: 12),
            ),
          ],
          if (nextAction.isNotEmpty) ...[
            const SizedBox(height: 8),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(8),
              decoration: BoxDecoration(
                color: _autonomyBubbleBackground(color, alpha: 0.08),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.route_outlined, color: color, size: 15),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          nextAction,
                          style: const TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        if (nextActionDetail.isNotEmpty)
                          Padding(
                            padding: const EdgeInsets.only(top: 2),
                            child: Text(
                              nextActionDetail,
                              style: TextStyle(
                                color: _mutedTextColor(),
                                fontSize: 12,
                              ),
                            ),
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ],
          if (hasCapabilityAction(
              _autopilotAgentCapabilityActionEnableAlwaysOn)) ...[
            const SizedBox(height: 8),
            FilledButton.tonalIcon(
              onPressed:
                  canAdoptCodexLoop ? _adoptAutonomyCodexAgentLoop : null,
              icon: const Icon(Icons.all_inclusive_outlined, size: 16),
              label: const Text('Adopt Codex loop'),
            ),
          ],
          if (visibleCapabilities.isNotEmpty) ...[
            const SizedBox(height: 8),
            for (final capability in visibleCapabilities)
              _buildAutonomyAgentQualityDimension(capability),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyAgentQualityMonitor(Map<String, dynamic> monitor) {
    final status = monitor['status']?.toString() ?? '';
    final score = _asInt(monitor['score']);
    final color = status == _autopilotReadinessPassed
        ? _autonomyStatusColor('completed')
        : status == _autopilotReadinessFailed
            ? _autonomyStatusColor('failed')
            : Colors.orange;
    final detail = monitor['detail']?.toString().trim() ?? '';
    final nextAction =
        monitor[_autopilotAgentQualityNextActionLabel]?.toString().trim() ?? '';
    final nextActionDetail =
        monitor[_autopilotAgentQualityNextActionDetail]?.toString().trim() ??
            '';
    final dimensions = _asMapList(monitor[_autopilotAgentQualityDimensions]);
    final problems = _asStringList(monitor[_autopilotQualityProblems]);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.06),
        border: Border.all(color: color.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.monitor_heart_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Local quality monitor',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              if (score != null)
                _miniChip('$score%', _autonomyBubbleBackground(color), color),
              const SizedBox(width: 6),
              _miniChip(
                status.isEmpty ? 'checking' : status.replaceAll('_', ' '),
                _autonomyBubbleBackground(color),
                color,
              ),
            ],
          ),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              detail,
              style: TextStyle(color: _mutedTextColor(), fontSize: 12),
            ),
          ],
          if (nextAction.isNotEmpty) ...[
            const SizedBox(height: 8),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(8),
              decoration: BoxDecoration(
                color: _autonomyBubbleBackground(color, alpha: 0.08),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.arrow_forward_outlined, color: color, size: 15),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          nextAction,
                          style: const TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        if (nextActionDetail.isNotEmpty)
                          Padding(
                            padding: const EdgeInsets.only(top: 2),
                            child: Text(
                              nextActionDetail,
                              style: TextStyle(
                                color: _mutedTextColor(),
                                fontSize: 12,
                              ),
                            ),
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ],
          if (dimensions.isNotEmpty) ...[
            const SizedBox(height: 8),
            for (final dimension in dimensions.take(5))
              _buildAutonomyAgentQualityDimension(dimension),
          ],
          if (problems.isNotEmpty) ...[
            const SizedBox(height: 8),
            for (final problem in problems.take(3))
              Padding(
                padding: const EdgeInsets.only(top: 3),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.warning_amber_outlined,
                        color: Colors.orange.shade600, size: 14),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        problem,
                        style:
                            TextStyle(color: _mutedTextColor(), fontSize: 12),
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyAgentQualityDimension(Map<String, dynamic> dimension) {
    final status = dimension['status']?.toString() ?? '';
    final color = status == _autopilotReadinessPassed
        ? _autonomyStatusColor('completed')
        : status == _autopilotReadinessFailed
            ? _autonomyStatusColor('failed')
            : Colors.orange;
    final label = dimension['label']?.toString().trim() ?? 'Quality signal';
    final detail = dimension['detail']?.toString().trim() ?? '';
    final count = _asInt(dimension['count']);
    final score = _asInt(dimension['score']);
    return Padding(
      padding: const EdgeInsets.only(top: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            status == _autopilotReadinessPassed
                ? Icons.check_circle_outline
                : status == _autopilotReadinessFailed
                    ? Icons.error_outline
                    : Icons.warning_amber_outlined,
            color: color,
            size: 14,
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        label,
                        style: const TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    if (score != null)
                      _miniChip(
                        '$score',
                        _autonomyBubbleBackground(Colors.indigo),
                        Colors.indigo.shade800,
                      )
                    else if (count != null)
                      _miniChip(
                        '$count',
                        _autonomyBubbleBackground(Colors.blueGrey),
                        Colors.blueGrey.shade800,
                      ),
                  ],
                ),
                if (detail.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      detail,
                      style: TextStyle(
                        color: _mutedTextColor(),
                        fontSize: 12,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyQualityScorecard(Map<String, dynamic> scorecard) {
    final status = scorecard['status']?.toString() ?? '';
    final color = status == _autopilotReadinessPassed
        ? _autonomyStatusColor('completed')
        : Colors.orange;
    final detail = scorecard['detail']?.toString().trim() ?? '';
    final reviews = _asMap(scorecard[_autopilotQualityArchitectReviews]);
    final scheduled = _asMap(scorecard[_autopilotQualityScheduled]);
    final validation = _asMap(scorecard[_autopilotQualityValidation]);
    final problems = _asStringList(scorecard[_autopilotQualityProblems]);
    final reviewAverage = _asInt(reviews['average_score']);
    final scheduledAverage = _asInt(scheduled['average_score']);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.06),
        border: Border.all(color: color.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.verified_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Quality governance',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              _miniChip(
                status.isEmpty ? 'checking' : status.replaceAll('_', ' '),
                _autonomyBubbleBackground(color),
                color,
              ),
            ],
          ),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              detail,
              style: TextStyle(color: _mutedTextColor(), fontSize: 12),
            ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                '${_asInt(scorecard['recent_run_count']) ?? 0} runs / ${_asInt(scorecard['window_days']) ?? _autopilotQualityDefaultWindowDays}d',
                _autonomyBubbleBackground(Colors.blueGrey),
                Colors.blueGrey.shade800,
              ),
              _miniChip(
                '${_asInt(reviews['passed']) ?? 0} pass / ${_asInt(reviews['blocked']) ?? 0} blocked',
                _autonomyBubbleBackground(Colors.teal),
                Colors.teal.shade900,
              ),
              if (reviewAverage != null)
                _miniChip(
                  'review avg $reviewAverage',
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              if ((_asInt(reviews['stale']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(reviews['stale'])} stale',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
              _miniChip(
                '${_asInt(scheduled['passed']) ?? 0} pass / ${_asInt(scheduled['repaired']) ?? 0} repair',
                _autonomyBubbleBackground(Colors.deepPurple),
                Colors.deepPurple.shade700,
              ),
              if (scheduledAverage != null)
                _miniChip(
                  'cycle avg $scheduledAverage',
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              if ((_asInt(scheduled['low_quality']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(scheduled['low_quality'])} rejected',
                  _autonomyBubbleBackground(_autonomyStatusColor('failed')),
                  _autonomyStatusColor('failed'),
                ),
              _miniChip(
                '${_asInt(validation['passed']) ?? 0}/${_asInt(validation['total']) ?? 0} validation',
                _autonomyBubbleBackground(Colors.teal),
                Colors.teal.shade900,
              ),
            ],
          ),
          if (problems.isNotEmpty) ...[
            const SizedBox(height: 8),
            for (final problem in problems.take(3))
              Padding(
                padding: const EdgeInsets.only(top: 3),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.warning_amber_outlined,
                        color: Colors.orange.shade600, size: 14),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        problem,
                        style:
                            TextStyle(color: _mutedTextColor(), fontSize: 12),
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyRuntimeQueue(Map<String, dynamic> queue) {
    final status = queue['status']?.toString() ?? '';
    final color =
        status == 'passed' ? _autonomyStatusColor('completed') : Colors.orange;
    final detail = queue['detail']?.toString().trim() ?? '';
    final problems = _asStringList(queue[_autopilotRuntimeQueueProblems]);
    final queued = _asInt(queue['queued_count']) ?? 0;
    final active = _asInt(queue['active_count']) ?? 0;
    final waiting = _asInt(queue['waiting_count']) ?? 0;
    final questions = _asInt(queue['pending_question_count']) ?? 0;
    final alwaysOn = _asInt(queue['always_on_profile_count']) ?? 0;
    final alwaysOnOpen = _asInt(queue['always_on_open_count']) ?? 0;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.06),
        border: Border.all(color: color.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.dynamic_feed_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Runtime queue',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              _miniChip(
                status.isEmpty ? 'checking' : status.replaceAll('_', ' '),
                _autonomyBubbleBackground(color),
                color,
              ),
            ],
          ),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              detail,
              style: TextStyle(color: _mutedTextColor(), fontSize: 12),
            ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                '$queued queued',
                _autonomyBubbleBackground(
                    queued == 0 ? Colors.blueGrey : Colors.orange),
                queued == 0 ? Colors.blueGrey.shade800 : Colors.orange.shade900,
              ),
              _miniChip(
                '$active active',
                _autonomyBubbleBackground(
                    active == 0 ? Colors.blueGrey : Colors.green),
                active == 0 ? Colors.blueGrey.shade800 : Colors.green.shade800,
              ),
              _miniChip(
                '$waiting waiting',
                _autonomyBubbleBackground(
                    waiting == 0 ? Colors.blueGrey : Colors.deepPurple),
                waiting == 0
                    ? Colors.blueGrey.shade800
                    : Colors.deepPurple.shade700,
              ),
              if (alwaysOn > 0)
                _miniChip(
                  '$alwaysOn always-on',
                  _autonomyBubbleBackground(Colors.green),
                  Colors.green.shade800,
                ),
              if (alwaysOnOpen > 0)
                _miniChip(
                  '$alwaysOnOpen always-on open',
                  _autonomyBubbleBackground(Colors.teal),
                  Colors.teal.shade900,
                ),
              if (questions > 0)
                _miniChip(
                  '$questions questions',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
            ],
          ),
          if (problems.isNotEmpty) ...[
            const SizedBox(height: 8),
            for (final problem in problems.take(3))
              Padding(
                padding: const EdgeInsets.only(top: 3),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.warning_amber_outlined,
                        color: Colors.orange.shade600, size: 14),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        problem,
                        style:
                            TextStyle(color: _mutedTextColor(), fontSize: 12),
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ],
      ),
    );
  }

  Color _operatorInboxItemColor(String kind) {
    switch (kind) {
      case _autopilotOperatorInboxKindApproval:
        return Colors.teal.shade700;
      case _autopilotOperatorInboxKindClarification:
      case _autopilotOperatorInboxKindQuestion:
        return Colors.orange.shade700;
      case _autopilotOperatorInboxKindBlocker:
        return _autonomyStatusColor('failed');
      case _autopilotOperatorInboxKindReply:
        return Colors.indigo.shade700;
      default:
        return Colors.blueGrey.shade700;
    }
  }

  IconData _operatorInboxItemIcon(String kind) {
    switch (kind) {
      case _autopilotOperatorInboxKindApproval:
        return Icons.playlist_add_check_circle_outlined;
      case _autopilotOperatorInboxKindClarification:
        return Icons.help_outline;
      case _autopilotOperatorInboxKindQuestion:
        return Icons.record_voice_over_outlined;
      case _autopilotOperatorInboxKindBlocker:
        return Icons.report_problem_outlined;
      case _autopilotOperatorInboxKindReply:
        return Icons.mark_chat_unread_outlined;
      default:
        return Icons.inbox_outlined;
    }
  }

  String _operatorInboxItemActionLabel(String kind) {
    switch (kind) {
      case _autopilotOperatorInboxKindClarification:
      case _autopilotOperatorInboxKindQuestion:
      case _autopilotOperatorInboxKindReply:
        return _autopilotOperatorInboxActionAnswer;
      case _autopilotOperatorInboxKindApproval:
        return _autopilotOperatorInboxActionReview;
      default:
        return _autopilotOperatorInboxActionOpen;
    }
  }

  IconData _operatorInboxItemActionIcon(String kind) {
    switch (kind) {
      case _autopilotOperatorInboxKindClarification:
      case _autopilotOperatorInboxKindQuestion:
      case _autopilotOperatorInboxKindReply:
        return Icons.reply_outlined;
      case _autopilotOperatorInboxKindApproval:
        return Icons.rate_review_outlined;
      default:
        return Icons.open_in_new;
    }
  }

  Widget _buildAutonomyOperatorInbox(Map<String, dynamic> inbox) {
    final status = inbox['status']?.toString() ?? '';
    final total = _asInt(inbox['total_action_count']) ?? 0;
    final color =
        total == 0 ? _autonomyStatusColor('completed') : Colors.orange.shade700;
    final detail = inbox['detail']?.toString().trim() ?? '';
    final items = _asMapList(inbox[_autopilotOperatorInboxItems]);
    final nextAction =
        inbox[_autopilotOperatorInboxNextAction]?.toString().trim() ?? '';
    final nextActionLabel =
        inbox[_autopilotOperatorInboxNextActionLabel]?.toString().trim() ?? '';
    final nextActionDetail =
        inbox[_autopilotOperatorInboxNextActionDetail]?.toString().trim() ?? '';
    final nextActionKind =
        inbox[_autopilotOperatorInboxNextActionKind]?.toString().trim() ?? '';
    final nextActionRunId =
        inbox[_autopilotOperatorInboxNextActionRunId]?.toString().trim() ?? '';
    final nextActionAgent =
        inbox[_autopilotOperatorInboxNextActionAgent]?.toString().trim() ?? '';
    final showNextAction = nextActionLabel.isNotEmpty &&
        nextAction != _autopilotOperatorInboxActionKeepMonitoring;
    final nextActionColor = showNextAction
        ? _operatorInboxItemColor(nextActionKind)
        : _autonomyStatusColor('completed');
    final nextActionItem = <String, dynamic>{
      'kind': nextActionKind,
      'run_id': nextActionRunId,
    };
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.06),
        border: Border.all(color: color.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.inbox_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Operator inbox',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              _miniChip(
                status.isEmpty ? 'checking' : status.replaceAll('_', ' '),
                _autonomyBubbleBackground(color),
                color,
              ),
            ],
          ),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              detail,
              style: TextStyle(color: _mutedTextColor(), fontSize: 12),
            ),
          ],
          if (showNextAction) ...[
            const SizedBox(height: 8),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(
                  _operatorInboxItemIcon(nextActionKind),
                  color: nextActionColor,
                  size: 16,
                ),
                const SizedBox(width: 7),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        nextActionAgent.isEmpty
                            ? nextActionLabel
                            : '$nextActionLabel - $nextActionAgent',
                        style: const TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                      if (nextActionDetail.isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.only(top: 2),
                          child: Text(
                            nextActionDetail,
                            style: TextStyle(
                              color: _mutedTextColor(),
                              fontSize: 12,
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
                if (nextActionRunId.isNotEmpty) ...[
                  const SizedBox(width: 6),
                  OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(
                      visualDensity: VisualDensity.compact,
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                    ),
                    onPressed: _autonomyBusy
                        ? null
                        : () => _openAutonomyInboxItem(nextActionItem),
                    icon: Icon(
                      _operatorInboxItemActionIcon(nextActionKind),
                      size: 15,
                    ),
                    label: Text(_operatorInboxItemActionLabel(nextActionKind)),
                  ),
                ],
              ],
            ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                '$total actions',
                _autonomyBubbleBackground(color),
                color,
              ),
              _miniChip(
                '${_asInt(inbox['approval_count']) ?? 0} approvals',
                _autonomyBubbleBackground(Colors.teal),
                Colors.teal.shade900,
              ),
              _miniChip(
                '${_asInt(inbox['clarification_count']) ?? 0} clarifications',
                _autonomyBubbleBackground(Colors.orange),
                Colors.orange.shade900,
              ),
              _miniChip(
                '${_asInt(inbox['pending_question_count']) ?? 0} questions',
                _autonomyBubbleBackground(Colors.orange),
                Colors.orange.shade900,
              ),
              if ((_asInt(inbox['reply_waiting_count']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(inbox['reply_waiting_count'])} replies',
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              if ((_asInt(inbox['blocked_count']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(inbox['blocked_count'])} blocked',
                  _autonomyBubbleBackground(_autonomyStatusColor('failed')),
                  _autonomyStatusColor('failed'),
                ),
            ],
          ),
          if (items.isNotEmpty) ...[
            const SizedBox(height: 8),
            for (final item in items.take(4))
              _buildAutonomyOperatorInboxItem(item),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyOperatorInboxItem(Map<String, dynamic> item) {
    final kind = item['kind']?.toString() ?? '';
    final color = _operatorInboxItemColor(kind);
    final label = item['label']?.toString().trim() ?? 'Inbox item';
    final agent = item['agent']?.toString().trim() ?? '';
    final reason = item['reason']?.toString().trim() ?? '';
    final runId = item['run_id']?.toString().trim() ?? '';
    final actionLabel = _operatorInboxItemActionLabel(kind);
    final actionIcon = _operatorInboxItemActionIcon(kind);
    return Padding(
      padding: const EdgeInsets.only(top: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(_operatorInboxItemIcon(kind), color: color, size: 14),
          const SizedBox(width: 6),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  agent.isEmpty ? label : '$label - $agent',
                  style: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                if (reason.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      reason,
                      style: TextStyle(
                        color: _mutedTextColor(),
                        fontSize: 12,
                      ),
                    ),
                  ),
                if (runId.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      runId,
                      style: TextStyle(
                        color: _mutedTextColor(),
                        fontSize: 11,
                      ),
                    ),
                  ),
              ],
            ),
          ),
          if (runId.isNotEmpty) ...[
            const SizedBox(width: 6),
            OutlinedButton.icon(
              style: OutlinedButton.styleFrom(
                visualDensity: VisualDensity.compact,
                padding: const EdgeInsets.symmetric(horizontal: 8),
              ),
              onPressed:
                  _autonomyBusy ? null : () => _openAutonomyInboxItem(item),
              icon: Icon(actionIcon, size: 15),
              label: Text(actionLabel),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyCodexAlignment(Map<String, dynamic> alignment) {
    final status = alignment['status']?.toString() ?? '';
    final score = _asInt(alignment['score']) ?? 0;
    final color =
        status == 'passed' ? _autonomyStatusColor('completed') : Colors.orange;
    final detail = alignment['detail']?.toString().trim() ?? '';
    final dimensions =
        _asMapList(alignment[_autopilotCodexAlignmentDimensions]);
    final gaps = _asMapList(alignment[_autopilotCodexAlignmentGaps]);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.06),
        border: Border.all(color: color.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.compare_arrows_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Local-vs-Codex alignment',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              _miniChip('$score%', _autonomyBubbleBackground(color), color),
            ],
          ),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(detail,
                style: TextStyle(color: _mutedTextColor(), fontSize: 12)),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                '${_asInt(alignment['imported_count']) ?? 0}/${_asInt(alignment['reference_count']) ?? 0} references',
                _autonomyBubbleBackground(Colors.cyan),
                Colors.cyan.shade900,
              ),
              _miniChip(
                '${_asInt(alignment['dimension_count']) ?? dimensions.length} checks',
                _autonomyBubbleBackground(Colors.indigo),
                Colors.indigo.shade800,
              ),
              if (gaps.isNotEmpty)
                _miniChip(
                  '${gaps.length} watch',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
              if ((_asInt(alignment['extra_imported_count']) ?? 0) > 0)
                _miniChip(
                  '${_asInt(alignment['extra_imported_count'])} historical',
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
            ],
          ),
          if (gaps.isNotEmpty) ...[
            const SizedBox(height: 8),
            for (final gap in gaps.take(3))
              _buildAutonomyCodexAlignmentRow(gap, warning: true),
          ] else if (dimensions.isNotEmpty) ...[
            const SizedBox(height: 8),
            for (final dimension in dimensions.take(3))
              _buildAutonomyCodexAlignmentRow(dimension),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyCodexAlignmentRow(
    Map<String, dynamic> dimension, {
    bool warning = false,
  }) {
    final status = dimension['status']?.toString() ?? '';
    final color = status == 'passed' && !warning
        ? _autonomyStatusColor('completed')
        : Colors.orange.shade700;
    final label = dimension['label']?.toString().trim() ??
        dimension['key']?.toString().replaceAll('_', ' ') ??
        'Alignment check';
    final detail = dimension['detail']?.toString().trim() ?? '';
    return Padding(
      padding: const EdgeInsets.only(top: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            status == 'passed' && !warning
                ? Icons.check_circle_outline
                : Icons.warning_amber_outlined,
            color: color,
            size: 14,
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label,
                  style: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                if (detail.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      detail,
                      style: TextStyle(color: _mutedTextColor(), fontSize: 12),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyAgentTeams(List<Map<String, dynamic>> teams) {
    final sorted = [...teams]..sort((a, b) {
        final aRank = _agentTeamRank(a);
        final bRank = _agentTeamRank(b);
        if (aRank != bRank) return aRank.compareTo(bRank);
        final aSupervisor = _asMap(a['supervisor']);
        final bSupervisor = _asMap(b['supervisor']);
        return (aSupervisor['name']?.toString() ?? '')
            .toLowerCase()
            .compareTo((bSupervisor['name']?.toString() ?? '').toLowerCase());
      });
    final visible = sorted.take(_autopilotTeamPreviewLimit).toList();
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(Colors.indigo, alpha: 0.06),
        border: Border.all(color: Colors.indigo.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.account_tree_outlined,
                  color: Colors.indigo.shade400, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Agent teams',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              _miniChip(
                '${teams.length} leads',
                _autonomyBubbleBackground(Colors.indigo),
                Colors.indigo.shade800,
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            'Macro and specialist leads supervise smaller agents. Schedules can run observation and planning cycles, while patch and merge permissions remain separate.',
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
          const SizedBox(height: 8),
          for (final team in visible) _buildAutonomyAgentTeamRow(team),
          if (teams.length > visible.length) ...[
            const SizedBox(height: 4),
            Text(
              'Showing ${visible.length} of ${teams.length} macro teams.',
              style: TextStyle(color: _mutedTextColor(), fontSize: 11),
            ),
          ],
        ],
      ),
    );
  }

  int _agentTeamRank(Map<String, dynamic> team) {
    final status = team['status']?.toString() ?? '';
    switch (status) {
      case 'needs_input':
        return 0;
      case 'running':
        return 1;
      case 'scheduled':
        return 2;
      default:
        return 3;
    }
  }

  String _agentTeamStatusLabel(String status) {
    switch (status) {
      case 'needs_input':
        return 'needs input';
      case 'running':
        return 'running';
      case 'scheduled':
        return 'scheduled';
      case 'paused':
        return 'paused';
      default:
        return status.replaceAll('_', ' ');
    }
  }

  Color _agentTeamStatusColor(String status) {
    switch (status) {
      case 'needs_input':
        return Colors.orange;
      case 'running':
        return _autonomyStatusColor('running');
      case 'scheduled':
        return Colors.deepPurple;
      default:
        return Colors.blueGrey;
    }
  }

  Widget _buildAutonomyAgentTeamRow(Map<String, dynamic> team) {
    final supervisor = _asMap(team['supervisor']);
    final children = _asMapList(team['children']);
    final visibleChildren =
        children.take(_autopilotTeamChildPreviewLimit).toList();
    final status = team['status']?.toString() ?? 'paused';
    final color = _agentTeamStatusColor(status);
    final childCount = _asInt(team['child_count']) ?? children.length;
    final activeRuns = _asInt(team['active_run_count']) ?? 0;
    final questions = _asInt(team['pending_question_count']) ?? 0;
    final scheduledChildren = _asInt(team['scheduled_child_count']) ?? 0;
    final codexChildren = _asInt(team['codex_child_count']) ?? 0;
    final canPatch = team['can_patch'] == true;
    final canMerge = team['can_merge'] == true;
    return Padding(
      padding: const EdgeInsets.only(bottom: 7),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(8),
        decoration: BoxDecoration(
          color: _autonomyBubbleBackground(color, alpha: 0.05),
          border: Border.all(color: color.withValues(alpha: 0.20)),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.account_tree_outlined, color: color, size: 16),
                const SizedBox(width: 7),
                Expanded(
                  child: Text(
                    supervisor['name']?.toString() ??
                        supervisor['profile_key']?.toString() ??
                        'Macro agent',
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                _miniChip(
                  _agentTeamStatusLabel(status),
                  _autonomyBubbleBackground(color),
                  color,
                ),
              ],
            ),
            const SizedBox(height: 6),
            Wrap(
              spacing: 5,
              runSpacing: 5,
              children: [
                _miniChip(
                  '$childCount agents',
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
                if (scheduledChildren > 0)
                  _miniChip(
                    '$scheduledChildren scheduled',
                    _autonomyBubbleBackground(Colors.deepPurple),
                    Colors.deepPurple.shade700,
                  ),
                if (activeRuns > 0)
                  _miniChip(
                    '$activeRuns active chat${activeRuns == 1 ? '' : 's'}',
                    _autonomyBubbleBackground(_autonomyStatusColor('running')),
                    _autonomyStatusColor('running'),
                  ),
                if (questions > 0)
                  _miniChip(
                    '$questions question${questions == 1 ? '' : 's'}',
                    _autonomyBubbleBackground(Colors.orange),
                    Colors.orange.shade900,
                  ),
                if (codexChildren > 0)
                  _miniChip(
                    '$codexChildren codex',
                    _autonomyBubbleBackground(Colors.cyan),
                    Colors.cyan.shade900,
                  ),
                _miniChip(
                  canPatch ? 'patch allowed' : 'patch locked',
                  _autonomyBubbleBackground(
                    canPatch ? Colors.orange : Colors.teal,
                  ),
                  canPatch ? Colors.orange.shade900 : Colors.teal.shade900,
                ),
                _miniChip(
                  canMerge ? 'merge allowed' : 'merge locked',
                  _autonomyBubbleBackground(
                    canMerge ? Colors.orange : Colors.teal,
                  ),
                  canMerge ? Colors.orange.shade900 : Colors.teal.shade900,
                ),
              ],
            ),
            if (visibleChildren.isNotEmpty) ...[
              const SizedBox(height: 7),
              Wrap(
                spacing: 5,
                runSpacing: 5,
                children: [
                  for (final child in visibleChildren)
                    _miniChip(
                      child['name']?.toString() ??
                          child['profile_key']?.toString() ??
                          'agent',
                      _autonomyBubbleBackground(
                        _agentStatusColor(child['status']?.toString() ?? ''),
                      ),
                      _agentStatusColor(child['status']?.toString() ?? ''),
                    ),
                  if (children.length > visibleChildren.length)
                    _miniChip(
                      '+${children.length - visibleChildren.length} more',
                      _autonomyBubbleBackground(Colors.blueGrey),
                      Colors.blueGrey.shade800,
                    ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildAutonomyCodexProfileMatrix(
    List<Map<String, dynamic>> profiles,
  ) {
    final sorted = [...profiles]..sort((a, b) {
        final aRank = _codexProfileMatrixRank(a);
        final bRank = _codexProfileMatrixRank(b);
        if (aRank != bRank) return aRank.compareTo(bRank);
        return (a['name']?.toString() ?? '')
            .toLowerCase()
            .compareTo((b['name']?.toString() ?? '').toLowerCase());
      });
    final visible = sorted.take(_autopilotCodexParityPreviewLimit).toList();
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(Colors.cyan, alpha: 0.06),
        border: Border.all(color: Colors.cyan.withValues(alpha: 0.20)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.compare_arrows_outlined,
                  color: Colors.cyan.shade700, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Codex parity matrix',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              _miniChip(
                '${profiles.length} seeds',
                _autonomyBubbleBackground(Colors.cyan),
                Colors.cyan.shade900,
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            'Each local automation is mirrored as a CHILI agent profile. Source-active agents can be enabled here, but patch and merge permissions stay locked separately.',
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              OutlinedButton.icon(
                onPressed: _autonomyBusy ? null : _syncAutonomyCodexProfiles,
                icon: const Icon(Icons.sync_outlined, size: 16),
                label: const Text('Sync Codex prompts'),
              ),
            ],
          ),
          const SizedBox(height: 8),
          for (final profile in visible)
            _buildAutonomyCodexProfileMatrixRow(profile),
          if (profiles.length > visible.length) ...[
            const SizedBox(height: 4),
            Text(
              'Showing ${visible.length} of ${profiles.length}; use /schedule codex for the full list in chat.',
              style: TextStyle(color: _mutedTextColor(), fontSize: 11),
            ),
          ],
        ],
      ),
    );
  }

  int _codexProfileMatrixRank(Map<String, dynamic> profile) {
    final freshness = profile['prompt_freshness_status']?.toString() ?? '';
    final sourceStatus =
        profile['source_status']?.toString().trim().toUpperCase() ?? '';
    final scheduleEnabled = profile['chili_schedule_enabled'] == true;
    if (freshness == _autopilotPromptFreshnessStale ||
        freshness == _autopilotPromptFreshnessMissingSource ||
        freshness == _autopilotPromptFreshnessMissingProfile) {
      return 0;
    }
    if (sourceStatus == 'ACTIVE' && !scheduleEnabled) return 1;
    if (sourceStatus == 'ACTIVE') return 2;
    return 3;
  }

  Widget _buildAutonomyCodexProfileMatrixRow(Map<String, dynamic> profile) {
    final sourceStatus =
        profile['source_status']?.toString().trim().toUpperCase() ?? 'UNKNOWN';
    final scheduleEnabled = profile['chili_schedule_enabled'] == true;
    final canPatch = profile['can_patch'] == true;
    final canMerge = profile['can_merge'] == true;
    final freshness = profile['prompt_freshness_status']?.toString() ?? '';
    final freshnessColor = _codexFreshnessColor(freshness);
    final contract = _asMap(profile['operating_contract']);
    final operatingState = _asMap(profile[_autopilotAgentOperatingState]);
    final operatingStateValue = operatingState['state']?.toString() ?? '';
    final operatingStateColor = _agentOperatingStateColor(operatingStateValue);
    final nextActionLabel =
        operatingState['next_action_label']?.toString().trim() ?? '';
    final sourceActive = sourceStatus == 'ACTIVE';
    final scheduleColor = scheduleEnabled
        ? _autonomyStatusColor('completed')
        : sourceActive
            ? Colors.orange
            : Colors.blueGrey;
    return Padding(
      padding: const EdgeInsets.only(bottom: 7),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(8),
        decoration: BoxDecoration(
          color: _autonomyBubbleBackground(scheduleColor, alpha: 0.04),
          border: Border.all(color: scheduleColor.withValues(alpha: 0.18)),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(
                  _agentIcon(profile['tier']?.toString() ?? ''),
                  color: scheduleColor,
                  size: 16,
                ),
                const SizedBox(width: 7),
                Expanded(
                  child: Text(
                    profile['name']?.toString() ??
                        profile['profile_key']?.toString() ??
                        'Codex automation',
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 6),
            Wrap(
              spacing: 5,
              runSpacing: 5,
              children: [
                _miniChip(
                  'source ${sourceStatus.toLowerCase()}',
                  _autonomyBubbleBackground(
                    sourceActive ? Colors.deepPurple : Colors.blueGrey,
                  ),
                  sourceActive ? Colors.deepPurple.shade700 : Colors.blueGrey,
                ),
                _miniChip(
                  scheduleEnabled ? 'CHILI scheduled' : 'CHILI paused',
                  _autonomyBubbleBackground(scheduleColor),
                  scheduleColor,
                ),
                if (freshness.isNotEmpty)
                  _miniChip(
                    _codexFreshnessLabel(freshness),
                    _autonomyBubbleBackground(freshnessColor),
                    freshnessColor,
                  ),
                if (nextActionLabel.isNotEmpty)
                  _miniChip(
                    nextActionLabel,
                    _autonomyBubbleBackground(operatingStateColor),
                    operatingStateColor,
                  ),
                _miniChip(
                  canPatch ? 'patch allowed' : 'patch locked',
                  _autonomyBubbleBackground(
                    canPatch ? Colors.orange : Colors.teal,
                  ),
                  canPatch ? Colors.orange.shade900 : Colors.teal.shade900,
                ),
                _miniChip(
                  canMerge ? 'merge allowed' : 'merge locked',
                  _autonomyBubbleBackground(
                    canMerge ? Colors.orange : Colors.teal,
                  ),
                  canMerge ? Colors.orange.shade900 : Colors.teal.shade900,
                ),
              ],
            ),
            if (contract.isNotEmpty) ...[
              const SizedBox(height: 7),
              _buildAutonomyOperatingContractChips(contract, compact: true),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildAutonomyOperatingContractChips(
    Map<String, dynamic> contract, {
    bool compact = false,
  }) {
    final workspace =
        contract[_autopilotContractWorkspace]?.toString().trim() ?? '';
    final inbox = contract[_autopilotContractInbox]?.toString().trim() ?? '';
    final output = contract[_autopilotContractOutput]?.toString().trim() ?? '';
    final state = contract[_autopilotContractState]?.toString().trim() ?? '';
    final commands = _asStringList(contract[_autopilotContractKeyCommands]);
    final safety = _asStringList(contract[_autopilotContractSafetyBoundaries]);
    final chips = <Widget>[
      if (workspace.isNotEmpty)
        _miniChip(
          'ws ${_contractPathTail(workspace)}',
          _autonomyBubbleBackground(Colors.indigo),
          Colors.indigo.shade800,
        ),
      if (inbox.isNotEmpty)
        _miniChip(
          'in ${_contractPathTail(inbox)}',
          _autonomyBubbleBackground(Colors.cyan),
          Colors.cyan.shade900,
        ),
      if (!compact && output.isNotEmpty)
        _miniChip(
          'out ${_contractPathTail(output)}',
          _autonomyBubbleBackground(Colors.cyan),
          Colors.cyan.shade900,
        ),
      if (!compact && state.isNotEmpty)
        _miniChip(
          'state ${_contractPathTail(state)}',
          _autonomyBubbleBackground(Colors.blueGrey),
          Colors.blueGrey.shade800,
        ),
      if (contract[_autopilotContractDDriveAligned] == true)
        _miniChip(
          'D-drive',
          _autonomyBubbleBackground(Colors.teal),
          Colors.teal.shade900,
        ),
      if (contract[_autopilotContractUsesMailboxProtocol] == true)
        _miniChip(
          'mailbox protocol',
          _autonomyBubbleBackground(Colors.teal),
          Colors.teal.shade900,
        ),
      if (contract[_autopilotContractUsesRunLock] == true)
        _miniChip(
          'run.lock',
          _autonomyBubbleBackground(Colors.blueGrey),
          Colors.blueGrey.shade800,
        ),
      if (contract[_autopilotContractRequiresOutReport] == true)
        _miniChip(
          'OUT report',
          _autonomyBubbleBackground(Colors.blueGrey),
          Colors.blueGrey.shade800,
        ),
      if (contract[_autopilotContractUsesPrReviewFlow] == true)
        _miniChip(
          'PR review',
          _autonomyBubbleBackground(Colors.deepPurple),
          Colors.deepPurple.shade700,
        ),
      if (commands.isNotEmpty)
        _miniChip(
          '${commands.length} command${commands.length == 1 ? '' : 's'}',
          _autonomyBubbleBackground(Colors.orange),
          Colors.orange.shade900,
        ),
      if (safety.isNotEmpty)
        _miniChip(
          '${safety.length} safety boundar${safety.length == 1 ? 'y' : 'ies'}',
          _autonomyBubbleBackground(Colors.orange),
          Colors.orange.shade900,
        ),
    ];
    if (chips.isEmpty) return const SizedBox.shrink();
    return Wrap(spacing: 5, runSpacing: 5, children: chips);
  }

  String _codexFreshnessLabel(String status) {
    switch (status) {
      case _autopilotPromptFreshnessCurrent:
        return 'prompt current';
      case _autopilotPromptFreshnessStale:
        return 'prompt stale';
      case _autopilotPromptFreshnessCustom:
        return 'custom prompt';
      case _autopilotPromptFreshnessMissingSource:
        return 'source missing';
      case _autopilotPromptFreshnessMissingProfile:
        return 'profile missing';
      default:
        return status.replaceAll('_', ' ');
    }
  }

  Color _codexFreshnessColor(String status) {
    switch (status) {
      case _autopilotPromptFreshnessCurrent:
        return _autonomyStatusColor('completed');
      case _autopilotPromptFreshnessCustom:
        return Colors.indigo;
      case _autopilotPromptFreshnessStale:
      case _autopilotPromptFreshnessMissingSource:
      case _autopilotPromptFreshnessMissingProfile:
        return Colors.orange;
      default:
        return Colors.blueGrey;
    }
  }

  Widget _buildAutonomyCodexBench(Map<String, dynamic> bench) {
    final status = bench['status']?.toString() ?? '';
    final color = status == _autopilotReadinessPassed
        ? _autonomyStatusColor('completed')
        : status == _autopilotReadinessFailed
            ? _autonomyStatusColor('failed')
            : Colors.orange;
    final detail = bench['detail']?.toString().trim() ?? '';
    final nextAction = bench['next_action_label']?.toString().trim() ?? '';
    final nextActionDetail =
        bench['next_action_detail']?.toString().trim() ?? '';
    final matching = _asInt(bench['matching_count']) ?? 0;
    final mirrored = _asInt(bench['current_imported_count']) ?? 0;
    final sourceActive = _asInt(bench['source_active_count']) ?? 0;
    final enabled = _asInt(bench['source_active_enabled_count']) ?? 0;
    final alwaysOn = _asInt(bench['source_active_always_on_count']) ?? 0;
    final scheduled = _asInt(bench['source_active_scheduled_count']) ?? 0;
    final disabled = _asInt(bench['source_active_disabled_count']) ?? 0;
    final stale = _asInt(bench['stale_count']) ?? 0;
    final custom = _asInt(bench['custom_count']) ?? 0;
    final historical = _asInt(bench['historical_count']) ?? 0;
    final contracts = _asInt(bench['contract_workspace_count']) ?? 0;
    final alignmentScore = _asInt(bench['alignment_score']);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.06),
        border: Border.all(color: color.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.hub_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Codex bench',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              _miniChip('$mirrored/$matching', _autonomyBubbleBackground(color),
                  color),
            ],
          ),
          if (detail.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              detail,
              style: TextStyle(color: _mutedTextColor(), fontSize: 12),
            ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                '$mirrored mirrored',
                _autonomyBubbleBackground(Colors.cyan),
                Colors.cyan.shade900,
              ),
              _miniChip(
                '$enabled/$sourceActive source-active',
                _autonomyBubbleBackground(
                  disabled == 0 ? Colors.teal : Colors.orange,
                ),
                disabled == 0 ? Colors.teal.shade900 : Colors.orange.shade900,
              ),
              if (alwaysOn > 0)
                _miniChip(
                  '$alwaysOn always-on',
                  _autonomyBubbleBackground(Colors.green),
                  Colors.green.shade800,
                ),
              if (scheduled > 0)
                _miniChip(
                  '$scheduled scheduled',
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              if (stale > 0)
                _miniChip(
                  '$stale stale',
                  _autonomyBubbleBackground(Colors.orange),
                  Colors.orange.shade900,
                ),
              if (custom > 0)
                _miniChip(
                  '$custom custom',
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              if (historical > 0)
                _miniChip(
                  '$historical historical',
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
              if (contracts > 0)
                _miniChip(
                  '$contracts contracts',
                  _autonomyBubbleBackground(Colors.teal),
                  Colors.teal.shade900,
                ),
              if (alignmentScore != null)
                _miniChip(
                  '$alignmentScore% aligned',
                  _autonomyBubbleBackground(
                    alignmentScore >= _autopilotCodexAlignmentPassingScore
                        ? Colors.teal
                        : Colors.orange,
                  ),
                  alignmentScore >= _autopilotCodexAlignmentPassingScore
                      ? Colors.teal.shade900
                      : Colors.orange.shade900,
                ),
            ],
          ),
          if (nextAction.isNotEmpty) ...[
            const SizedBox(height: 8),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(8),
              decoration: BoxDecoration(
                color: _autonomyBubbleBackground(color, alpha: 0.08),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.arrow_forward_outlined, color: color, size: 15),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          nextAction,
                          style: const TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        if (nextActionDetail.isNotEmpty)
                          Padding(
                            padding: const EdgeInsets.only(top: 2),
                            child: Text(
                              nextActionDetail,
                              style: TextStyle(
                                color: _mutedTextColor(),
                                fontSize: 12,
                              ),
                            ),
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyCodexScheduleMirror(Map<String, dynamic> mirror) {
    final total = _asInt(mirror['total']) ?? 0;
    final sourceActive = _asInt(mirror['source_active']) ?? 0;
    final sourcePaused = _asInt(mirror['source_paused']) ?? 0;
    final enabled = _asInt(mirror['source_active_enabled']) ?? 0;
    final disabled = _asInt(mirror['source_active_disabled']) ?? 0;
    final alwaysOn = _asInt(mirror['source_active_always_on']) ?? 0;
    final scheduled = _asInt(mirror['source_active_scheduled']) ?? 0;
    final color =
        disabled > 0 ? Colors.orange : _autonomyStatusColor('completed');
    final canExplain = !_autonomyBusy && _canSendAutopilotCommand;
    final canEnable =
        !_autonomyBusy && sourceActive > 0 && (disabled > 0 || alwaysOn > 0);
    final canEnableAlwaysOn =
        !_autonomyBusy && sourceActive > 0 && alwaysOn < sourceActive;
    final canPause = !_autonomyBusy && enabled > 0;
    final canRunNow = !_autonomyBusy && enabled > 0;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.07),
        border: Border.all(color: color.withValues(alpha: 0.20)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.schedule_send_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Codex schedule mirror',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            '$enabled of $sourceActive source-active Codex automations are enabled in CHILI: $alwaysOn always-on, $scheduled scheduled. $disabled remain paused here; all imported agents stay plan-only unless patch permission is enabled separately.',
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                '$total imported',
                _autonomyBubbleBackground(Colors.cyan),
                Colors.cyan.shade900,
              ),
              _miniChip(
                '$sourceActive source active',
                _autonomyBubbleBackground(Colors.deepPurple),
                Colors.deepPurple.shade700,
              ),
              _miniChip(
                '$sourcePaused source paused',
                _autonomyBubbleBackground(Colors.blueGrey),
                Colors.blueGrey.shade800,
              ),
              if (alwaysOn > 0)
                _miniChip(
                  '$alwaysOn always-on',
                  _autonomyBubbleBackground(Colors.green),
                  Colors.green.shade800,
                ),
              if (scheduled > 0)
                _miniChip(
                  '$scheduled scheduled',
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
            ],
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              OutlinedButton.icon(
                onPressed: canExplain
                    ? () =>
                        _submitAutopilotCommand(_autopilotSlashScheduleCodex)
                    : null,
                icon: const Icon(Icons.visibility_outlined, size: 16),
                label: const Text('Explain'),
              ),
              OutlinedButton.icon(
                onPressed: canEnable
                    ? () => _setAutonomyCodexScheduleMirror(
                          enableSourceActive: true,
                        )
                    : null,
                icon: const Icon(Icons.play_circle_outline, size: 16),
                label: const Text('Enable active'),
              ),
              OutlinedButton.icon(
                onPressed: canEnableAlwaysOn
                    ? () => _setAutonomyCodexScheduleMirror(
                          enableSourceActive: true,
                          alwaysOn: true,
                        )
                    : null,
                icon: const Icon(Icons.all_inclusive_outlined, size: 16),
                label: const Text('Always-on'),
              ),
              FilledButton.tonalIcon(
                onPressed: canRunNow ? _runAutonomyCodexSchedulesNow : null,
                icon: const Icon(Icons.flash_on_outlined, size: 16),
                label: const Text('Run active now'),
              ),
              OutlinedButton.icon(
                onPressed: canPause
                    ? () => _setAutonomyCodexScheduleMirror(
                          enableSourceActive: false,
                        )
                    : null,
                icon: const Icon(Icons.pause_circle_outline, size: 16),
                label: const Text('Pause Codex'),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyCodexAutomationSeed(
    Map<String, dynamic> profile,
    Map<String, dynamic> automation,
  ) {
    final schedule = _asMap(profile['schedule']);
    final name = automation['name']?.toString().trim() ?? '';
    final identifier = automation['id']?.toString().trim() ?? '';
    final kind = automation['kind']?.toString().trim() ?? '';
    final automationStatus = automation['status']?.toString().trim() ?? '';
    final automationPath = automation['path']?.toString().trim() ?? '';
    final promptLength = _asInt(automation['prompt_length']);
    final sourceStatus = automationStatus.isNotEmpty
        ? automationStatus
        : schedule['source_status']?.toString().trim() ?? '';
    final cadence = schedule['cadence']?.toString().trim() ?? '';
    final rrule = schedule['rrule']?.toString().trim() ?? '';
    final cadenceLabel = cadence.isNotEmpty
        ? _agentScheduleCadenceLabel(cadence)
        : rrule.isNotEmpty
            ? _agentScheduleRruleLabel(rrule)
            : 'manual';
    final title = name.isNotEmpty ? name : identifier;
    final freshness = _asMap(profile['prompt_freshness']);
    final freshnessLabel = _agentPromptFreshnessLabel(profile);
    final freshnessColor = _agentPromptFreshnessColor(profile);
    final reasons = _asStringList(freshness['reasons']);
    final profileContract = _asMap(profile['operating_contract']);
    final contract = profileContract.isNotEmpty
        ? profileContract
        : _asMap(automation['operating_contract']);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(Colors.cyan),
        border: Border.all(color: Colors.cyan.withValues(alpha: 0.35)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Codex automation seed',
            style: Theme.of(context).textTheme.labelLarge,
          ),
          if (title.isNotEmpty) ...[
            const SizedBox(height: 4),
            Text(
              title,
              style: const TextStyle(fontWeight: FontWeight.w700),
            ),
          ],
          if (automationPath.isNotEmpty) ...[
            const SizedBox(height: 4),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(Icons.description_outlined,
                    color: _mutedTextColor(), size: 14),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    _contractPathTail(automationPath),
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: _mutedTextColor(), fontSize: 11),
                  ),
                ),
              ],
            ),
          ],
          const SizedBox(height: 6),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(cadenceLabel, _autonomyBubbleBackground(Colors.cyan),
                  Colors.cyan.shade900),
              if (kind.isNotEmpty)
                _miniChip(kind, _autonomyBubbleBackground(Colors.indigo),
                    Colors.indigo.shade800),
              if (sourceStatus.isNotEmpty)
                _miniChip(
                    sourceStatus,
                    _autonomyBubbleBackground(Colors.blueGrey),
                    Colors.blueGrey.shade800),
              if (freshnessLabel.isNotEmpty)
                _miniChip(
                  freshnessLabel,
                  _autonomyBubbleBackground(freshnessColor),
                  freshnessColor,
                ),
              if (promptLength != null)
                _miniChip(
                  '$promptLength chars',
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            freshness['status'] == _autopilotPromptFreshnessStale
                ? 'Your local automation changed: ${reasons.isEmpty ? 'resync the profile from the agent generator' : reasons.join(', ')}.'
                : 'Imported paused from your local Codex automation config. Enable scheduling when ready; merge remains locked.',
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
          if (contract.isNotEmpty) ...[
            const SizedBox(height: 8),
            Text('Operating contract',
                style: Theme.of(context).textTheme.labelLarge),
            const SizedBox(height: 6),
            _buildAutonomyOperatingContractChips(contract),
            const SizedBox(height: 7),
            _buildAutonomyOperatingContractDetails(contract),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyOperatingContractDetails(Map<String, dynamic> contract) {
    final commands = _asStringList(contract[_autopilotContractKeyCommands]);
    final safety = _asStringList(contract[_autopilotContractSafetyBoundaries]);
    final rows = <Widget>[
      if (commands.isNotEmpty)
        _buildAutonomyContractDetailRow(
          icon: Icons.terminal_outlined,
          label: 'Command evidence',
          items: commands,
          color: Colors.orange,
        ),
      if (safety.isNotEmpty)
        _buildAutonomyContractDetailRow(
          icon: Icons.gpp_maybe_outlined,
          label: 'Safety boundaries',
          items: safety,
          color: Colors.teal,
        ),
    ];
    if (rows.isEmpty) {
      return Text(
        'No command or safety evidence was extracted from this prompt yet.',
        style: TextStyle(color: _mutedTextColor(), fontSize: 12),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: rows,
    );
  }

  Widget _buildAutonomyContractDetailRow({
    required IconData icon,
    required String label,
    required List<String> items,
    required Color color,
  }) {
    final visible = items.take(_autopilotContractDetailPreviewLimit).toList();
    final hiddenCount = items.length - visible.length;
    return Padding(
      padding: const EdgeInsets.only(top: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, color: color, size: 14),
          const SizedBox(width: 6),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label,
                  style: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 2),
                for (final item in visible)
                  Text(
                    item,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: _mutedTextColor(), fontSize: 11),
                  ),
                if (hiddenCount > 0)
                  Text(
                    '+$hiddenCount more',
                    style: TextStyle(color: _mutedTextColor(), fontSize: 11),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyAgentScheduleControls(Map<String, dynamic> profile) {
    final selectedCadence = _agentScheduleSelection(profile);
    final schedule = _asMap(profile['schedule']);
    final restUntil =
        schedule[_autopilotScheduleRestUntilKey]?.toString().trim() ?? '';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Runtime', style: Theme.of(context).textTheme.labelLarge),
        const SizedBox(height: 6),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            for (final cadence in _agentScheduleCadenceValues)
              ChoiceChip(
                label: Text(_agentScheduleCadenceLabel(cadence)),
                selected: selectedCadence == cadence,
                onSelected: _autonomyBusy
                    ? null
                    : (_) => _setAutonomyAgentSchedule(profile, cadence),
                selectedColor: _autonomyBubbleBackground(
                    Theme.of(context).colorScheme.primary),
                visualDensity: VisualDensity.compact,
              ),
          ],
        ),
        if (restUntil.isNotEmpty) ...[
          const SizedBox(height: 6),
          _miniChip(
            'resting until $restUntil',
            _autonomyBubbleBackground(Colors.orange),
            Colors.orange.shade900,
          ),
        ],
        const SizedBox(height: 6),
        Text(
          selectedCadence == _autopilotScheduleCadenceAlwaysOn
              ? 'Always-on mode keeps the agent warm and queue-driven, starts the next safe plan-only cycle when open work clears, and rests after the configured work window.'
              : 'Cycles are bounded and approval-first, matching Codex-style heartbeats without granting merge authority.',
          style: TextStyle(color: _mutedTextColor(), fontSize: 12),
        ),
      ],
    );
  }

  Widget _buildAutonomyAgentPermissionControls(Map<String, dynamic> profile) {
    final permissions = _asMap(profile['permissions']);
    final worktreeEnabled = permissions[_autopilotPermissionWorktree] == true;
    final mergeEnabled = permissions[_autopilotPermissionMerge] == true;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Implementation permission',
            style: Theme.of(context).textTheme.labelLarge),
        const SizedBox(height: 6),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            FilterChip(
              label: const Text('Worktree patches'),
              selected: worktreeEnabled,
              onSelected: _autonomyBusy
                  ? null
                  : (value) => _setAutonomyAgentWorktreePermission(
                        profile,
                        enabled: value,
                      ),
              selectedColor: _autonomyBubbleBackground(Colors.teal),
              visualDensity: VisualDensity.compact,
            ),
            _miniChip(
              mergeEnabled ? 'merge allowed' : 'merge locked',
              _autonomyBubbleBackground(
                mergeEnabled ? Colors.orange : Colors.blueGrey,
              ),
              mergeEnabled ? Colors.orange.shade900 : Colors.blueGrey.shade800,
            ),
          ],
        ),
        const SizedBox(height: 6),
        Text(
          worktreeEnabled
              ? 'Future cycles may draft patches in isolated worktrees; approval, architect review, validation, and merge gates still apply.'
              : 'Plan-only mode is active. This agent can research and draft plans but scheduled cycles cannot edit files.',
          style: TextStyle(color: _mutedTextColor(), fontSize: 12),
        ),
      ],
    );
  }

  Widget _buildAutonomyOperatorQuestions(
    List<Map<String, dynamic>> questions,
  ) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Needs operator input',
            style: Theme.of(context).textTheme.labelLarge),
        const SizedBox(height: 6),
        ...questions.map((question) {
          final body = question['question']?.toString().trim() ?? '';
          if (body.isEmpty) return const SizedBox.shrink();
          return Container(
            width: double.infinity,
            margin: const EdgeInsets.only(bottom: 6),
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(
              color: _autonomyBubbleBackground(Colors.orange, alpha: 0.10),
              border: Border.all(color: Colors.orange.withValues(alpha: 0.25)),
              borderRadius: BorderRadius.circular(8),
            ),
            child: Text(body, style: const TextStyle(fontSize: 12)),
          );
        }),
      ],
    );
  }

  Widget _buildAutonomyActionPanel(Map<String, dynamic> run) {
    final status = run['status']?.toString() ?? '';
    final planStatus = run['plan_status']?.toString() ?? '';
    final executionMode = run['execution_mode']?.toString() ?? '';
    final terminal = _autonomyTerminal(run);
    final branch = run['integration_branch']?.toString() ?? '';
    final merge = run['merge_status']?.toString() ?? '';
    final canApprove = AutonomyRunPresenter.architectApprovalReady(run);
    final showApprovalReadiness =
        executionMode == _autopilotExecutionModePlanApproval ||
            (planStatus.isNotEmpty && planStatus != _autopilotStatusChatting);
    final canStartPlan = status == _autopilotStatusChatting ||
        planStatus == _autopilotStatusChatting;
    final canMerge = terminal &&
        branch.isNotEmpty &&
        status != 'merged' &&
        merge != 'merged';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _buildAutonomyRepoPicker(),
        _buildSelectedRepoPathHint(),
        const SizedBox(height: 10),
        if (canStartPlan)
          FilledButton.icon(
            onPressed: _autonomyBusy ? null : _startAutopilotPlan,
            icon: const Icon(Icons.account_tree_outlined, size: 18),
            label: const Text(_autopilotStartPlanLabel),
          ),
        if (canStartPlan) const SizedBox(height: 8),
        if (showApprovalReadiness) _buildAutonomyApprovalReadiness(run),
        if (showApprovalReadiness) const SizedBox(height: 8),
        if (canApprove)
          FilledButton.icon(
            onPressed: _autonomyBusy ? null : _approveAutopilotPlan,
            icon: const Icon(Icons.play_arrow, size: 18),
            label: const Text('Approve plan and implement'),
          )
        else if (showApprovalReadiness)
          OutlinedButton.icon(
            onPressed: null,
            icon: const Icon(Icons.lock_outline, size: 18),
            label: const Text('Approve unavailable'),
          ),
        if (canApprove) const SizedBox(height: 8),
        if (!canApprove && showApprovalReadiness) const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            OutlinedButton.icon(
              onPressed: _autonomyBusy
                  ? null
                  : () async {
                      await _loadCodeRepos();
                      await _loadAutonomyAgentProfiles();
                      await _loadAutonomyRuns();
                      await _refreshActiveAutonomyRun(
                        silent: false,
                        force: true,
                      );
                    },
              icon: const Icon(Icons.sync, size: 18),
              label: const Text('Refresh'),
            ),
            OutlinedButton.icon(
              onPressed:
                  _autonomyBusy ? null : () => _copyAutopilotRunSummary(run),
              icon: const Icon(Icons.copy_all_outlined, size: 18),
              label: const Text('Copy summary'),
            ),
            OutlinedButton.icon(
              onPressed: _autonomyBusy || terminal ? null : _cancelAutopilot,
              icon: const Icon(Icons.stop_circle_outlined, size: 18),
              label: const Text('Cancel'),
            ),
            OutlinedButton.icon(
              onPressed: _autonomyBusy || !canMerge ? null : _mergeAutopilot,
              icon: const Icon(Icons.merge_type, size: 18),
              label: const Text('Merge'),
            ),
            if (AutonomyRunPresenter.canRerun(run))
              OutlinedButton.icon(
                onPressed:
                    _autonomyBusy ? null : () => _prefillAutopilotRerun(run),
                icon: const Icon(Icons.replay, size: 18),
                label: const Text('Rerun'),
              ),
            OutlinedButton.icon(
              onPressed: _autonomyBusy ? null : _attachAutopilotScreenshot,
              icon: const Icon(Icons.screenshot_monitor, size: 18),
              label: const Text('Screenshot'),
            ),
            OutlinedButton.icon(
              onPressed:
                  _autonomyBusy ? null : _requestAutopilotVideoValidation,
              icon: const Icon(Icons.videocam_outlined, size: 18),
              label: const Text('Video QA'),
            ),
          ],
        ),
        if (status == _autopilotStatusAwaitingApproval) ...[
          const SizedBox(height: 10),
          Text(
            canApprove
                ? 'Plan Mode is waiting. Send feedback in chat to revise, or approve when it looks right.'
                : AutonomyRunPresenter.architectApprovalBlocker(run),
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
        ] else if (status == _autopilotStatusAwaitingClarification) ...[
          const SizedBox(height: 10),
          Text(
            'CHILI needs clarification before it can produce an approval-ready plan.',
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
        ] else if (canStartPlan) ...[
          const SizedBox(height: 10),
          Text(
            'Brainstorming mode is active. I will not scan or edit the repo until you start a plan.',
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
        ],
      ],
    );
  }

  Widget _buildAutonomyTrackingSidebar() {
    final run = _activeAutonomyRun;
    if (run == null) {
      return Container(
        color: _autonomySidebarColor(),
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text('Autopilot actions',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 12),
            _buildAutonomyRepoPicker(),
            _buildSelectedRepoPathHint(),
            const SizedBox(height: 12),
            _buildAutonomyAgentInspector(
              _selectedAutonomyAgentProfile(),
              run: null,
            ),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              onPressed: _autonomyBusy
                  ? null
                  : () async {
                      await _loadCodeRepos();
                      await _loadAutonomyAgentProfiles();
                      await _loadAutonomyRuns();
                    },
              icon: const Icon(Icons.sync, size: 18),
              label: const Text('Refresh repos and chats'),
            ),
            const SizedBox(height: 24),
            _emptyAutonomyState('Start a chat to see plan and run details'),
          ],
        ),
      );
    }
    final plan = _asMap(run['plan']);
    final architectReview = _asMap(run['architect_review']);
    final agents = _asMapList(run['agents']);
    final files = _asStringList(run['files']);
    final validation = _asMapList(run['validation']);
    final learning = _asMap(run['learning']);
    final artifacts = _asMapList(run['artifacts']);
    final steps = _asMapList(run['steps']);
    final delegations = _asMapList(run['delegations']);
    final expertThreads = _asMapList(run['expert_threads']);
    final pmSynthesis = _asMap(run['pm_synthesis']);
    final childRuns = _asStringList(run['child_runs']);
    final parentRun = _asMap(run['parent_run']);
    final branch = run['integration_branch']?.toString() ?? '';
    final worktree = run['worktree_path']?.toString() ?? '';
    final mergeMessage = run['merge_message']?.toString() ?? '';
    final errorMessage = run['error_message']?.toString() ?? '';
    return Container(
      color: _autonomySidebarColor(),
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text('Autopilot actions',
              style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 12),
          _buildAutonomyActionPanel(run),
          const Divider(height: 28),
          _buildAutonomyAgentInspector(
            _selectedAutonomyAgentProfile(run),
            run: run,
          ),
          const Divider(height: 28),
          Text('Tracking', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 10),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                  _autonomyStatusLabel(run['status']?.toString() ?? 'unknown'),
                  _autonomyStatusColor(run['status']?.toString() ?? '')
                      .withValues(alpha: 0.12),
                  _autonomyStatusColor(run['status']?.toString() ?? '')),
              if (run['current_stage'] != null)
                _miniChip(
                  _autonomyStageLabel('${run['current_stage']}'),
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              if (run['merge_status'] != null)
                _miniChip(
                  _autonomyMergeStatusLabel('${run['merge_status']}'),
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
            ],
          ),
          const SizedBox(height: 12),
          if (parentRun.isNotEmpty) ...[
            _buildAutonomyParentRun(parentRun),
            const SizedBox(height: 12),
          ],
          if (branch.isNotEmpty) _kvSelectable('Branch', branch),
          if (worktree.isNotEmpty) _kvSelectable('Worktree', worktree),
          if (mergeMessage.isNotEmpty) _kvSelectable('Merge', mergeMessage),
          if (errorMessage.isNotEmpty)
            _kvSelectable(
                'Blocked', AutonomyRunPresenter.blockedRunMessage(run)),
          const Divider(height: 28),
          _buildAutonomyArchitectQuality(architectReview),
          const Divider(height: 28),
          _buildAutonomyPlan(plan, files),
          const Divider(height: 28),
          _buildAutonomyDelegations(
            pmSynthesis,
            expertThreads,
            delegations,
            childRuns,
          ),
          const Divider(height: 28),
          _buildAutonomyAgents(agents),
          const Divider(height: 28),
          _buildAutonomyValidation(validation),
          const Divider(height: 28),
          _buildAutonomyArtifacts(artifacts),
          const Divider(height: 28),
          _buildAutonomyLearning(learning),
          const Divider(height: 28),
          _buildAutonomySteps(steps),
        ],
      ),
    );
  }

  Widget _emptyAutonomyState(String label) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Text(label, style: TextStyle(color: _mutedTextColor())),
      ),
    );
  }

  IconData _autonomyStatusIcon(String status) {
    switch (status) {
      case 'merged':
      case 'completed':
        return Icons.check_circle_outline;
      case 'blocked':
        return Icons.pause_circle_outline;
      case 'failed':
      case 'cancelled':
        return Icons.error_outline;
      case 'validating':
        return Icons.fact_check_outlined;
      case 'merging':
        return Icons.merge_type;
      case _autopilotStatusAwaitingApproval:
        return Icons.rule_folder_outlined;
      case _autopilotStatusAwaitingClarification:
        return Icons.help_outline;
      case _autopilotStatusChatting:
        return Icons.forum_outlined;
      case 'running':
        return Icons.autorenew;
      default:
        return Icons.radio_button_unchecked;
    }
  }

  IconData _autonomyArtifactIcon(String type) {
    switch (type) {
      case 'model_call':
        return Icons.memory;
      case 'worktree':
        return Icons.account_tree_outlined;
      case 'diff':
        return Icons.difference_outlined;
      case 'diff_rejected':
        return Icons.warning_amber_outlined;
      case 'commit':
        return Icons.commit;
      case 'architect_review':
        return Icons.verified_outlined;
      case 'quality_gate':
        return Icons.verified_user_outlined;
      case _autopilotArtifactPromptImage:
        return Icons.image_outlined;
      case 'visual_screenshot':
        return Icons.screenshot_monitor;
      case 'visual_video':
        return Icons.videocam_outlined;
      case 'ui_review':
      case 'ux_review':
        return Icons.image_search_outlined;
      default:
        return Icons.inventory_2_outlined;
    }
  }

  Widget _buildAutonomyActiveRun(Map<String, dynamic> run) {
    final runId = run['run_id']?.toString() ?? '';
    final status = run['status']?.toString() ?? 'unknown';
    final stage = run['current_stage']?.toString() ?? '';
    final merge = run['merge_status']?.toString() ?? 'pending';
    final statusColor = _autonomyStatusColor(status);
    final plan = _asMap(run['plan']);
    final architectReview = _asMap(run['architect_review']);
    final agents = _asMapList(run['agents']);
    final files = _asStringList(run['files']);
    final validation = _asMapList(run['validation']);
    final steps = _asMapList(run['steps']);
    final learning = _asMap(run['learning']);
    final delegations = _asMapList(run['delegations']);
    final expertThreads = _asMapList(run['expert_threads']);
    final pmSynthesis = _asMap(run['pm_synthesis']);
    final childRuns = _asStringList(run['child_runs']);
    final parentRun = _asMap(run['parent_run']);
    final branch = run['integration_branch']?.toString() ?? '';
    final worktree = run['worktree_path']?.toString() ?? '';
    final mergeMessage = run['merge_message']?.toString() ?? '';
    final errorMessage = run['error_message']?.toString() ?? '';

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.hub_outlined, color: statusColor),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    runId.isEmpty ? 'Active run' : runId,
                    style: Theme.of(context).textTheme.titleMedium,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                _miniChip(_autonomyStatusLabel(status),
                    statusColor.withValues(alpha: 0.12), statusColor),
                const SizedBox(width: 6),
                _miniChip(
                  _autonomyMergeStatusLabel(merge),
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
              ],
            ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 8,
              runSpacing: 6,
              children: [
                if (stage.isNotEmpty)
                  _miniChip(
                    _autonomyStageLabel(stage),
                    _autonomyBubbleBackground(Colors.indigo),
                    Colors.indigo.shade800,
                  ),
                if (run['repo_id'] != null)
                  _miniChip(
                    'repo #${run['repo_id']}',
                    _autonomyBubbleBackground(Colors.grey),
                    Colors.grey.shade800,
                  ),
                if (branch.isNotEmpty)
                  _miniChip(
                    branch,
                    _autonomyBubbleBackground(Colors.purple),
                    Colors.purple.shade800,
                  ),
              ],
            ),
            const SizedBox(height: 12),
            _buildAutonomyActionPanel(run),
            if (parentRun.isNotEmpty) ...[
              const SizedBox(height: 12),
              _buildAutonomyParentRun(parentRun),
            ],
            if (worktree.isNotEmpty) ...[
              const SizedBox(height: 12),
              _kvSelectable('Worktree', worktree),
            ],
            if (mergeMessage.isNotEmpty) _kvSelectable('Merge', mergeMessage),
            if (errorMessage.isNotEmpty)
              _kvSelectable(
                  'Blocked', AutonomyRunPresenter.blockedRunMessage(run)),
            const Divider(height: 28),
            _buildAutonomyArchitectQuality(architectReview),
            const Divider(height: 28),
            _buildAutonomyPlan(plan, files),
            const Divider(height: 28),
            _buildAutonomyDelegations(
              pmSynthesis,
              expertThreads,
              delegations,
              childRuns,
            ),
            const Divider(height: 28),
            _buildAutonomyAgents(agents),
            const Divider(height: 28),
            _buildAutonomyValidation(validation),
            const Divider(height: 28),
            _buildAutonomyLearning(learning),
            const Divider(height: 28),
            _buildAutonomySteps(steps),
          ],
        ),
      ),
    );
  }

  Widget _buildAutonomyArchitectQuality(Map<String, dynamic> review) {
    final body = AutonomyRunPresenter.architectReviewBody(review);
    final passed = AutonomyRunPresenter.architectReviewPassed(review);
    final status = AutonomyRunPresenter.architectReviewStatusLabel(review);
    final color = passed
        ? _autonomyStatusColor('completed')
        : _autonomyStatusColor('blocked');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Architect quality',
            style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (body.isEmpty)
          Text('Waiting for architect review',
              style: TextStyle(color: _mutedTextColor()))
        else ...[
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                passed ? 'passed' : status,
                _autonomyBubbleBackground(color),
                color,
              ),
              if (review['score'] != null)
                _miniChip(
                  '${review['score']}/100',
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
            ],
          ),
          const SizedBox(height: 8),
          SelectableText(body, style: const TextStyle(fontSize: 13)),
        ],
      ],
    );
  }

  Widget _buildAutonomyApprovalReadiness(Map<String, dynamic> run) {
    final ready = AutonomyRunPresenter.architectApprovalReady(run);
    final blocker = AutonomyRunPresenter.architectApprovalBlocker(run);
    final color = ready ? _autonomyStatusColor('completed') : Colors.orange;
    return Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.08),
        border: Border.all(color: color.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 6,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Text(
                'Approval readiness',
                style: Theme.of(context).textTheme.titleSmall,
              ),
              _miniChip(
                ready ? 'ready' : 'not ready',
                _autonomyBubbleBackground(color),
                color,
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            ready ? 'Ready to approve and implement.' : blocker,
            style: TextStyle(color: _mutedTextColor(), fontSize: 12),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyPlan(Map<String, dynamic> plan, List<String> files) {
    final planBody = AutonomyRunPresenter.planBody(plan);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Architect plan', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (planBody.isEmpty && files.isEmpty)
          Text('Waiting for plan', style: TextStyle(color: _mutedTextColor()))
        else ...[
          if (planBody.isNotEmpty)
            SelectableText(planBody, style: const TextStyle(fontSize: 13)),
          if (files.isNotEmpty) ...[
            const SizedBox(height: 10),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: files
                  .map((file) => _miniChip(
                        file,
                        _autonomyBubbleBackground(Colors.teal),
                        Colors.teal.shade900,
                      ))
                  .toList(),
            ),
          ],
        ],
      ],
    );
  }

  Widget _buildAutonomyParentRun(Map<String, dynamic> parentRun) {
    final runId = parentRun['run_id']?.toString().trim() ?? '';
    if (runId.isEmpty) return const SizedBox.shrink();
    final missing = parentRun['missing'] == true;
    final status = parentRun['status']?.toString().trim() ?? '';
    final stage = parentRun['current_stage']?.toString().trim() ?? '';
    final planStatus = parentRun['plan_status']?.toString().trim() ?? '';
    final merge = parentRun['merge_status']?.toString().trim() ?? '';
    final promptPreview = parentRun['prompt_preview']?.toString().trim() ?? '';
    final agent = _asMap(parentRun['agent_profile']);
    final color = missing ? Colors.orange : _autonomyStatusColor(status);
    final agentName = agent['name']?.toString().trim() ??
        agent['profile_key']?.toString().trim() ??
        '';
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(color, alpha: 0.08),
        border: Border.all(color: color.withValues(alpha: 0.24)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.account_tree_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  missing ? 'Parent run missing' : 'Parent coordinator',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              ActionChip(
                avatar: const Icon(Icons.open_in_new, size: 15),
                label: Text(runId),
                onPressed: _autonomyBusy || missing
                    ? null
                    : () => _openAutonomyRunById(runId),
                visualDensity: VisualDensity.compact,
              ),
            ],
          ),
          if (agentName.isNotEmpty || promptPreview.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              agentName.isNotEmpty ? agentName : promptPreview,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(color: _mutedTextColor(), fontSize: 12),
            ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              if (status.isNotEmpty)
                _miniChip(
                  _autonomyStatusLabel(status),
                  _autonomyBubbleBackground(color),
                  color,
                ),
              if (stage.isNotEmpty)
                _miniChip(
                  _autonomyStageLabel(stage),
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              if (planStatus.isNotEmpty)
                _miniChip(
                  planStatus.replaceAll('_', ' '),
                  _autonomyBubbleBackground(Colors.teal),
                  Colors.teal.shade900,
                ),
              if (merge.isNotEmpty)
                _miniChip(
                  _autonomyMergeStatusLabel(merge),
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyDelegations(
    Map<String, dynamic> pmSynthesis,
    List<Map<String, dynamic>> expertThreads,
    List<Map<String, dynamic>> delegations,
    List<String> childRuns,
  ) {
    final knownChildIds = <String>{
      for (final thread in expertThreads)
        if ((thread['child_run_id']?.toString() ?? '').trim().isNotEmpty)
          thread['child_run_id'].toString(),
    };
    final orphanChildRuns =
        childRuns.where((runId) => !knownChildIds.contains(runId)).toList();
    final synthesisSummary = pmSynthesis['summary']?.toString().trim() ?? '';
    final synthesisNext = pmSynthesis['next_action']?.toString().trim() ?? '';
    final safetyGates = _asStringList(pmSynthesis['safety_gates']);
    final blockers = _asStringList(pmSynthesis['blockers']);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Child agents', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (expertThreads.isEmpty && delegations.isEmpty && childRuns.isEmpty)
          Text(
            'No child-agent runs spawned yet',
            style: TextStyle(color: _mutedTextColor()),
          )
        else ...[
          if (synthesisSummary.isNotEmpty || synthesisNext.isNotEmpty) ...[
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(10),
              margin: const EdgeInsets.only(bottom: 10),
              decoration: BoxDecoration(
                color: _autonomyBubbleBackground(Colors.teal, alpha: 0.08),
                border: Border.all(color: Colors.teal.withValues(alpha: 0.22)),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('PM synthesis',
                      style: Theme.of(context).textTheme.labelLarge),
                  if (synthesisSummary.isNotEmpty) ...[
                    const SizedBox(height: 6),
                    Text(synthesisSummary,
                        style: const TextStyle(fontSize: 12)),
                  ],
                  if (synthesisNext.isNotEmpty) ...[
                    const SizedBox(height: 6),
                    Text(
                      synthesisNext,
                      style: TextStyle(
                        color: _mutedTextColor(),
                        fontSize: 12,
                      ),
                    ),
                  ],
                  if (blockers.isNotEmpty || safetyGates.isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: [
                        for (final blocker in blockers.take(4))
                          _miniChip(
                            blocker,
                            _autonomyBubbleBackground(Colors.orange),
                            Colors.orange.shade900,
                          ),
                        for (final gate in safetyGates.take(4))
                          _miniChip(
                            gate,
                            _autonomyBubbleBackground(Colors.blueGrey),
                            Colors.blueGrey.shade800,
                          ),
                      ],
                    ),
                  ],
                ],
              ),
            ),
          ],
          if (expertThreads.isNotEmpty)
            ...expertThreads.map(_buildAutonomyExpertThread)
          else if (delegations.isNotEmpty)
            ...delegations.map((delegation) {
              final status = delegation['status']?.toString() ?? 'planned';
              final intent = _jsonTextAsMap(
                delegation['intent']?.toString().trim() ?? '',
              );
              final title = intent['display_name']?.toString().trim() ??
                  intent['profile_key']?.toString().trim() ??
                  'Child agent';
              final deliverable =
                  intent['deliverable_summary']?.toString().trim() ??
                      intent['expected_deliverable']?.toString().trim() ??
                      '';
              final childRun = delegation['child_run_id']?.toString() ?? '';
              return Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Wrap(
                      spacing: 6,
                      runSpacing: 4,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        _miniChip(
                          title,
                          _autonomyBubbleBackground(Colors.cyan),
                          Colors.cyan.shade900,
                        ),
                        _miniChip(
                          status.replaceAll('_', ' '),
                          _autonomyBubbleBackground(Colors.indigo),
                          Colors.indigo.shade800,
                        ),
                        if (childRun.isNotEmpty)
                          ActionChip(
                            avatar: const Icon(Icons.open_in_new, size: 15),
                            label: Text(childRun),
                            onPressed: _autonomyBusy
                                ? null
                                : () => _openAutonomyRunById(childRun),
                            visualDensity: VisualDensity.compact,
                          ),
                      ],
                    ),
                    if (deliverable.isNotEmpty) ...[
                      const SizedBox(height: 4),
                      Text(
                        deliverable,
                        style:
                            TextStyle(color: _mutedTextColor(), fontSize: 12),
                      ),
                    ],
                  ],
                ),
              );
            }),
          if (orphanChildRuns.isNotEmpty)
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: orphanChildRuns
                  .map(
                    (runId) => ActionChip(
                      avatar: const Icon(Icons.open_in_new, size: 15),
                      label: Text(runId),
                      onPressed: _autonomyBusy
                          ? null
                          : () => _openAutonomyRunById(runId),
                      visualDensity: VisualDensity.compact,
                    ),
                  )
                  .toList(),
            ),
        ],
      ],
    );
  }

  Widget _buildAutonomyExpertThread(Map<String, dynamic> thread) {
    final title = thread['display_name']?.toString().trim() ??
        thread['name']?.toString().trim() ??
        thread['profile_key']?.toString().trim() ??
        'Expert agent';
    final role = thread['role']?.toString().trim() ?? '';
    final status = thread['status']?.toString().trim() ?? 'ready';
    final childRun = thread['child_run_id']?.toString().trim() ?? '';
    final childRunSummary = _asMap(thread['child_run']);
    final childStatus = childRunSummary['status']?.toString().trim() ?? '';
    final childStage =
        childRunSummary['current_stage']?.toString().trim() ?? '';
    final childPlan = childRunSummary['plan_status']?.toString().trim() ?? '';
    final childMerge = childRunSummary['merge_status']?.toString().trim() ?? '';
    final childColor = childStatus.isEmpty
        ? Colors.blueGrey
        : _autonomyStatusColor(childStatus);
    final deliverable = thread['deliverable_summary']?.toString().trim() ??
        thread['expected_deliverable']?.toString().trim() ??
        '';
    final dependencies = _asStringList(thread['dependencies']);
    final files = _asStringList(thread['files']);
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: _autonomyBubbleBackground(Colors.indigo, alpha: 0.07),
        border: Border.all(color: Colors.indigo.withValues(alpha: 0.22)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 6,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              _miniChip(
                title,
                _autonomyBubbleBackground(Colors.cyan),
                Colors.cyan.shade900,
              ),
              if (role.isNotEmpty)
                _miniChip(
                  role.replaceAll('_', ' '),
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              _miniChip(
                status.replaceAll('_', ' '),
                _autonomyBubbleBackground(childColor),
                childColor,
              ),
              if (childRun.isNotEmpty)
                ActionChip(
                  avatar: const Icon(Icons.open_in_new, size: 15),
                  label: Text(childRun),
                  onPressed: _autonomyBusy
                      ? null
                      : () => _openAutonomyRunById(childRun),
                  visualDensity: VisualDensity.compact,
                ),
              if (childStage.isNotEmpty)
                _miniChip(
                  _autonomyStageLabel(childStage),
                  _autonomyBubbleBackground(Colors.indigo),
                  Colors.indigo.shade800,
                ),
              if (childPlan.isNotEmpty)
                _miniChip(
                  childPlan.replaceAll('_', ' '),
                  _autonomyBubbleBackground(Colors.teal),
                  Colors.teal.shade900,
                ),
              if (childMerge.isNotEmpty)
                _miniChip(
                  _autonomyMergeStatusLabel(childMerge),
                  _autonomyBubbleBackground(Colors.blueGrey),
                  Colors.blueGrey.shade800,
                ),
            ],
          ),
          if (deliverable.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(deliverable, style: const TextStyle(fontSize: 12)),
          ],
          if (dependencies.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: dependencies
                  .take(5)
                  .map(
                    (dependency) => _miniChip(
                      'after $dependency',
                      _autonomyBubbleBackground(Colors.blueGrey),
                      Colors.blueGrey.shade800,
                    ),
                  )
                  .toList(),
            ),
          ],
          if (files.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: files
                  .take(6)
                  .map(
                    (file) => _miniChip(
                      file,
                      _autonomyBubbleBackground(Colors.teal),
                      Colors.teal.shade900,
                    ),
                  )
                  .toList(),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyAgents(List<Map<String, dynamic>> agents) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Agent lanes', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (agents.isEmpty)
          Text('Waiting for lane assignment',
              style: TextStyle(color: _mutedTextColor()))
        else
          ...agents.map((agent) {
            final name = agent['name']?.toString() ??
                agent['role']?.toString() ??
                'agent';
            final role = agent['role']?.toString() ?? '';
            final status = agent['status']?.toString() ?? '';
            final files = _asStringList(agent['files']);
            return Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Wrap(
                    spacing: 6,
                    runSpacing: 4,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      _miniChip(
                        name,
                        _autonomyBubbleBackground(Colors.indigo),
                        Colors.indigo.shade800,
                      ),
                      if (role.isNotEmpty && role != name)
                        _miniChip(
                          role,
                          _autonomyBubbleBackground(Colors.blueGrey),
                          Colors.blueGrey.shade800,
                        ),
                      if (status.isNotEmpty)
                        _miniChip(
                          status,
                          _autonomyBubbleBackground(Colors.green),
                          Colors.green.shade800,
                        ),
                    ],
                  ),
                  if (files.isNotEmpty) ...[
                    const SizedBox(height: 4),
                    Text(files.join(', '),
                        style:
                            TextStyle(color: _mutedTextColor(), fontSize: 12)),
                  ],
                ],
              ),
            );
          }),
      ],
    );
  }

  Widget _buildAutonomyValidation(List<Map<String, dynamic>> validation) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Validation', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (validation.isEmpty)
          Text('No validation results yet',
              style: TextStyle(color: _mutedTextColor()))
        else
          ...validation.map((item) {
            final key = item['step_key']?.toString() ??
                item['command']?.toString() ??
                'command';
            final code = item['exit_code'];
            final ok = code == 0 || code == '0' || item['passed'] == true;
            final stderr = item['stderr']?.toString() ?? '';
            final stdout = item['stdout']?.toString() ?? '';
            final output = stderr.trim().isNotEmpty ? stderr : stdout;
            return Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Wrap(
                    spacing: 6,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      Icon(ok ? Icons.check_circle : Icons.warning_amber,
                          color: ok
                              ? _autonomyStatusColor('completed')
                              : _autonomyStatusColor('blocked'),
                          size: 18),
                      Text(key,
                          style: const TextStyle(fontWeight: FontWeight.w600)),
                      if (code != null)
                        _miniChip(
                            'exit $code',
                            ok
                                ? _autonomyBubbleBackground(Colors.green)
                                : _autonomyBubbleBackground(Colors.orange),
                            ok
                                ? Colors.green.shade800
                                : Colors.orange.shade900),
                    ],
                  ),
                  if (output.trim().isNotEmpty) ...[
                    const SizedBox(height: 4),
                    SelectableText(
                      output.length > 600
                          ? '${output.substring(0, 600)}...'
                          : output,
                      style: const TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 12,
                      ),
                    ),
                  ],
                ],
              ),
            );
          }),
      ],
    );
  }

  Widget _buildAutonomyArtifacts(List<Map<String, dynamic>> artifacts) {
    final visible = artifacts
        .where((artifact) => {
              'model_call',
              'quality_gate',
              'architect_review',
              'worktree',
              'diff',
              'diff_rejected',
              'commit',
              _autopilotArtifactPromptImage,
              'visual_screenshot',
              'visual_video',
              'ui_review',
              'ux_review',
            }.contains(artifact['artifact_type']?.toString()))
        .toList();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Artifacts', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (visible.isEmpty)
          Text('No artifacts yet', style: TextStyle(color: _mutedTextColor()))
        else
          ...visible.take(18).map((artifact) {
            final type = artifact['artifact_type']?.toString() ?? 'artifact';
            final name = artifact['name']?.toString() ?? type;
            final body = AutonomyRunPresenter.artifactBody(artifact);
            final path = _visualArtifactPath(artifact);
            return Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  border: Border.all(color: _autonomyDividerColor()),
                  borderRadius: BorderRadius.circular(8),
                  color:
                      _autonomyBubbleBackground(Colors.blueGrey, alpha: 0.04),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Icon(_autonomyArtifactIcon(type),
                            size: 18, color: _mutedTextColor()),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            name,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(fontWeight: FontWeight.w600),
                          ),
                        ),
                        _miniChip(
                          type,
                          _autonomyBubbleBackground(Colors.blueGrey),
                          Colors.blueGrey.shade800,
                        ),
                      ],
                    ),
                    if (body.isNotEmpty) ...[
                      const SizedBox(height: 8),
                      SelectableText(
                        body.length > 360
                            ? '${body.substring(0, 360)}...'
                            : body,
                        style: TextStyle(
                          color: _mutedTextColor(),
                          fontSize: 12,
                          height: 1.25,
                        ),
                      ),
                    ],
                    if ((type == 'visual_screenshot' ||
                            type == _autopilotArtifactPromptImage) &&
                        path != null &&
                        File(path).existsSync()) ...[
                      const SizedBox(height: 8),
                      ClipRRect(
                        borderRadius: BorderRadius.circular(6),
                        child: Image.file(
                          File(path),
                          height: 120,
                          width: double.infinity,
                          fit: BoxFit.cover,
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            );
          }),
      ],
    );
  }

  String? _visualArtifactPath(Map<String, dynamic> artifact) {
    final json = _asMap(artifact['content_json']);
    final path = json['path']?.toString() ?? '';
    return path.trim().isEmpty ? null : path.trim();
  }

  Widget _buildAutonomyLearning(Map<String, dynamic> learning) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Learning signals', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (learning.isEmpty)
          Text('No learning sample recorded yet',
              style: TextStyle(color: _mutedTextColor()))
        else
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: learning.entries
                .map((entry) => _miniChip(
                    '${entry.key}: ${entry.value}',
                    _autonomyBubbleBackground(Colors.cyan),
                    Colors.cyan.shade900))
                .toList(),
          ),
      ],
    );
  }

  Widget _buildAutonomySteps(List<Map<String, dynamic>> steps) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Run steps', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (steps.isEmpty)
          Text('Waiting for events', style: TextStyle(color: _mutedTextColor()))
        else
          ...steps.map((step) {
            final stage = step['stage']?.toString() ?? '';
            final title = step['title']?.toString() ?? stage;
            final status = step['status']?.toString() ?? '';
            final agent = step['agent_name']?.toString() ?? '';
            final time = _shortStamp(step['created_at']);
            return ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              leading: Icon(Icons.circle, size: 10, color: _mutedTextColor()),
              title: Text(title, overflow: TextOverflow.ellipsis),
              subtitle: Wrap(
                spacing: 6,
                runSpacing: 4,
                children: [
                  if (stage.isNotEmpty)
                    _miniChip(
                      _autonomyStageLabel(stage),
                      _autonomyBubbleBackground(Colors.blueGrey),
                      Colors.blueGrey.shade800,
                    ),
                  if (status.isNotEmpty)
                    _miniChip(
                      _autonomyStatusLabel(status),
                      _autonomyBubbleBackground(Colors.green),
                      Colors.green.shade800,
                    ),
                  if (agent.isNotEmpty)
                    _miniChip(
                      agent,
                      _autonomyBubbleBackground(Colors.indigo),
                      Colors.indigo.shade800,
                    ),
                ],
              ),
              trailing: time.isEmpty
                  ? null
                  : Text(time,
                      style: TextStyle(color: _mutedTextColor(), fontSize: 11)),
            );
          }),
      ],
    );
  }

  Widget _buildQueueTab() {
    if (_projects.isEmpty) {
      return ListView(
        padding: const EdgeInsets.all(24),
        children: [
          Text(
            'No planner projects available. Create one in the web planner, then refresh.',
            style: TextStyle(color: Colors.grey.shade700),
          ),
          const SizedBox(height: 12),
          FilledButton(
              onPressed: _loadProjectsPicker,
              child: const Text('Reload projects')),
        ],
      );
    }
    return ListView(
      padding: const EdgeInsets.all(24),
      children: [
        if (_queueError != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Text(_queueError!,
                style: TextStyle(color: Colors.red.shade800)),
          ),
        TextField(
          controller: _titleCtrl,
          decoration: const InputDecoration(
            labelText: 'Title',
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _descCtrl,
          minLines: 4,
          maxLines: 8,
          decoration: const InputDecoration(
            labelText: 'Description',
            alignLabelWithHint: true,
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 12),
        // Phase E.2 — optional source. The brain auto-detects what the
        // input is and registers/clones as needed. Examples in helperText.
        TextField(
          controller: _sourceCtrl,
          decoration: const InputDecoration(
            labelText: 'Source (optional)',
            helperText: 'D:\\dev\\some-project · /workspace · '
                'https://github.com/USER/REPO · USER/REPO · repo-name',
            helperMaxLines: 2,
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 16),
        DropdownButtonFormField<int>(
          // ignore: deprecated_member_use
          value: _projectId,
          decoration: const InputDecoration(
            labelText: 'Project',
            border: OutlineInputBorder(),
          ),
          items: _projects
              .map(
                (p) => DropdownMenuItem<int>(
                  value: p['id'] as int,
                  child: Text('${p['name'] ?? p['id']}'),
                ),
              )
              .toList(),
          onChanged: (v) => setState(() => _projectId = v),
        ),
        const SizedBox(height: 20),
        FilledButton.icon(
          onPressed: _queueBusy ? null : _submitQueue,
          icon: _queueBusy
              ? const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : const Icon(Icons.send),
          label: const Text('Queue task'),
        ),
      ],
    );
  }

  Widget _buildContextTab() {
    if (_ctxLoading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_ctxError != null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text('Failed to load Context Brain status',
                style: TextStyle(color: Colors.red.shade800)),
            const SizedBox(height: 8),
            Text(_ctxError!, style: TextStyle(color: Colors.grey.shade700)),
            const SizedBox(height: 16),
            FilledButton(
                onPressed: _loadContextBrain, child: const Text('Retry')),
          ],
        ),
      );
    }
    final state = (_ctxStatus?['runtime_state'] as Map?) ?? {};
    final intentDist = (_ctxStatus?['intent_distribution_24h'] as Map?) ?? {};
    final lastAssembly = _ctxStatus?['last_assembly'] as Map?;

    return RefreshIndicator(
      onRefresh: _loadContextBrain,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Icon(Icons.psychology_outlined,
                          color: Theme.of(context).colorScheme.primary),
                      const SizedBox(width: 8),
                      Text('Runtime',
                          style: Theme.of(context).textTheme.titleMedium),
                      const Spacer(),
                      Chip(
                        label: Text(state['mode']?.toString() ?? 'unknown'),
                        backgroundColor: Colors.green.shade50,
                      ),
                    ],
                  ),
                  const Divider(),
                  _kv('Token budget / request',
                      '${state['token_budget_per_request'] ?? '-'}'),
                  _kv('Distillation threshold',
                      '${state['distillation_threshold_tokens'] ?? '-'} tokens'),
                  _kv(
                    'Distillation spend today',
                    '\$${state['spent_today_distillation_usd'] ?? '0'} / \$${state['daily_distillation_usd_cap'] ?? '0'}',
                  ),
                  _kv('Strategy version',
                      '${state['learned_strategy_version'] ?? '-'}'),
                  _kv('Learning enabled',
                      '${state['learning_enabled'] ?? '-'}'),
                  _kv('Last learning cycle',
                      '${state['last_learning_cycle_at'] ?? 'never'}'),
                ],
              ),
            ),
          ),
          if (intentDist.isNotEmpty) ...[
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Intents (last 24h)',
                        style: Theme.of(context).textTheme.titleMedium),
                    const Divider(),
                    ...intentDist.entries
                        .map((e) => _kv('${e.key}', '${e.value}')),
                  ],
                ),
              ),
            ),
          ],
          if (lastAssembly != null) ...[
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Last assembly',
                        style: Theme.of(context).textTheme.titleMedium),
                    const Divider(),
                    _kv('Intent',
                        '${lastAssembly['intent']} (${lastAssembly['intent_confidence']})'),
                    _kv('Tokens used',
                        '${lastAssembly['total_tokens_input']} / ${lastAssembly['budget_token_cap']} (${lastAssembly['budget_used_pct']}%)'),
                    _kv('Elapsed', '${lastAssembly['elapsed_ms']} ms'),
                    _kv('Distilled', '${lastAssembly['distilled']}'),
                    _kv('Sources used', '${lastAssembly['sources_used']}'),
                  ],
                ),
              ),
            ),
          ],
          if (_ctxSources.isNotEmpty) ...[
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Source contribution (24h)',
                        style: Theme.of(context).textTheme.titleMedium),
                    const Divider(),
                    ..._ctxSources.map((s) {
                      final id = s['source_id']?.toString() ?? '?';
                      final sel = s['total_selected'] ?? 0;
                      final tot = s['total_returned'] ?? 0;
                      final rate =
                          ((s['selection_rate'] ?? 0.0) as num).toDouble();
                      return _kv(id,
                          '$sel/$tot (${(rate * 100).toStringAsFixed(0)}% selected)');
                    }),
                  ],
                ),
              ),
            ),
          ],
          if (_ctxAssemblies.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text('Recent assemblies',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            ..._ctxAssemblies.take(15).map((a) => Card(
                  child: ListTile(
                    dense: true,
                    title: Text('${a['intent']} (${a['intent_confidence']})'),
                    subtitle: Text(
                      '${a['total_tokens_input']}/${a['budget_token_cap']} tok · '
                      '${a['elapsed_ms']}ms · '
                      '${(a['sources_used'] is Map) ? (a['sources_used'] as Map).keys.join(',') : '-'}',
                    ),
                    trailing: Text('#${a['id']}',
                        style: TextStyle(color: Colors.grey.shade600)),
                  ),
                )),
          ],
        ],
      ),
    );
  }

  Widget _kv(String k, String v) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        children: [
          SizedBox(
            width: 200,
            child: Text(k, style: TextStyle(color: Colors.grey.shade700)),
          ),
          Expanded(
            child: Text(v, style: const TextStyle(fontWeight: FontWeight.w500)),
          ),
        ],
      ),
    );
  }

  Widget _buildHistoryTab() {
    if (_runsLoading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_runsError != null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(_runsError!),
            const SizedBox(height: 12),
            FilledButton(onPressed: _loadRuns, child: const Text('Retry')),
          ],
        ),
      );
    }

    // Build a filtered + grouped view.
    final filtered = _runs.where((r) {
      final step = r['cycle_step']?.toString() ?? '';
      final notify = r['notify_user'] == true;
      if (_hideRouterEscalate && step == 'router_escalate' && !notify) {
        return false;
      }
      if (_onlyOperatorReview && !notify) {
        return false;
      }
      return true;
    }).toList();

    // Count how many runs we hid so the filter toggle is contextual.
    final hiddenCount = _runs.length - filtered.length;
    final notifyCount = _runs.where((r) => r['notify_user'] == true).length;

    if (filtered.isEmpty && _runs.isEmpty) {
      return const Center(child: Text('No runs yet'));
    }

    return Column(
      children: [
        // Filter / summary bar
        Container(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
          color: Theme.of(context)
              .colorScheme
              .surfaceContainerHighest
              .withValues(alpha: 0.4),
          child: Row(
            children: [
              if (_watchTaskId != null) ...[
                FilterChip(
                  label: Text('Task #$_watchTaskId'),
                  selected: true,
                  onSelected: (_) {
                    setState(() => _watchTaskId = null);
                    unawaited(_loadRuns());
                  },
                  showCheckmark: false,
                  avatar: const Icon(Icons.close, size: 16),
                ),
                const SizedBox(width: 8),
              ],
              FilterChip(
                label: Text(
                  _hideRouterEscalate
                      ? 'Hide auto-escalates ($hiddenCount)'
                      : 'Hide auto-escalates',
                ),
                selected: _hideRouterEscalate,
                onSelected: (v) => setState(() => _hideRouterEscalate = v),
              ),
              const SizedBox(width: 8),
              FilterChip(
                label: Text('Needs review ($notifyCount)'),
                selected: _onlyOperatorReview,
                onSelected: (v) => setState(() => _onlyOperatorReview = v),
                avatar: notifyCount > 0
                    ? const Icon(Icons.flag, size: 16, color: Colors.red)
                    : null,
              ),
              const Spacer(),
              IconButton(
                icon: const Icon(Icons.refresh, size: 20),
                onPressed: _loadRuns,
                tooltip: 'Refresh',
              ),
            ],
          ),
        ),
        if (filtered.isEmpty)
          Expanded(
            child: Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.check_circle_outline,
                      size: 48, color: Colors.green.shade300),
                  const SizedBox(height: 8),
                  Text(
                    'No runs match the current filter',
                    style: TextStyle(color: Colors.grey.shade700),
                  ),
                  if (hiddenCount > 0)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: TextButton(
                        onPressed: () =>
                            setState(() => _hideRouterEscalate = false),
                        child: Text('Show $hiddenCount auto-escalations'),
                      ),
                    ),
                ],
              ),
            ),
          )
        else
          Expanded(
            child: ListView.separated(
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 16),
              itemCount: filtered.length,
              separatorBuilder: (_, __) => const SizedBox(height: 4),
              itemBuilder: (context, i) => _buildRunCard(filtered[i]),
            ),
          ),
      ],
    );
  }

  Widget _buildRunCard(Map<String, dynamic> r) {
    final id = r['id'];
    final tid = r['task_id'];
    final started = r['started_at']?.toString() ?? '';
    final step = r['cycle_step']?.toString() ?? '';
    final decision = r['decision']?.toString() ?? '';
    final esc = r['escalation_reason']?.toString();
    final vid = r['validation_run_id'];
    final snap = r['llm_snapshot'];
    final notify = r['notify_user'] == true;
    final branchName = r['branch_name']?.toString();
    final commitSha = r['commit_sha']?.toString();
    final diffSummary =
        r['diff_summary'] is Map ? r['diff_summary'] as Map : null;
    final pushed = diffSummary?['pushed'] == true;
    final pushUrl = diffSummary?['push_url']?.toString();
    final filesCount = (diffSummary?['files'] is List)
        ? (diffSummary!['files'] as List).length
        : 0;
    final loc = diffSummary?['loc'];
    final highlight = _watchTaskId != null && tid == _watchTaskId;

    // Choose a color + icon based on decision so the eye scans quickly
    final (Color, IconData, String) decisionVisual = switch (decision) {
      'passed' => (Colors.green.shade600, Icons.check_circle, 'passed'),
      'merged' => (Colors.green.shade700, Icons.merge_type, 'merged'),
      'applied' => (Colors.blue.shade700, Icons.task_alt, 'applied'),
      'validation_failed' => (
          Colors.orange.shade700,
          Icons.warning_amber,
          'validation failed'
        ),
      'failed' => (Colors.red.shade700, Icons.cancel, 'failed'),
      'escalated' => (Colors.purple.shade600, Icons.outlined_flag, 'escalated'),
      _ => (Colors.grey.shade700, Icons.circle, decision),
    };
    final (color, decIcon, decLabel) = decisionVisual;

    final shortTime =
        started.length >= 19 ? started.substring(11, 19) : started;
    final shortDate = started.length >= 10 ? started.substring(0, 10) : '';

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
      color: highlight
          ? Colors.amber.shade50
          : (notify ? Colors.red.shade50 : null),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(8),
        side: BorderSide(
          color: notify ? Colors.red.shade200 : Colors.grey.shade200,
          width: 1,
        ),
      ),
      child: ExpansionTile(
        leading: Icon(decIcon, color: color),
        title: Row(
          children: [
            Text(
              'Task #$tid',
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
            const SizedBox(width: 8),
            Text(
              decLabel,
              style: TextStyle(
                  color: color, fontWeight: FontWeight.w500, fontSize: 13),
            ),
            if (notify) ...[
              const SizedBox(width: 8),
              const Icon(Icons.flag, size: 14, color: Colors.red),
            ],
            const Spacer(),
            Text(
              '$shortDate $shortTime',
              style: TextStyle(color: Colors.grey.shade600, fontSize: 11),
            ),
          ],
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Wrap(
            spacing: 6,
            runSpacing: 4,
            children: [
              if (step.isNotEmpty)
                _miniChip(
                    step, Colors.blueGrey.shade50, Colors.blueGrey.shade800),
              if (filesCount > 0)
                _miniChip('$filesCount files', Colors.indigo.shade50,
                    Colors.indigo.shade800),
              if (loc != null)
                _miniChip(
                    '$loc loc', Colors.indigo.shade50, Colors.indigo.shade800),
              if (pushed)
                _miniChip(
                    'pushed', Colors.green.shade50, Colors.green.shade800),
              if (branchName != null && branchName.isNotEmpty)
                _miniChip(
                    branchName, Colors.purple.shade50, Colors.purple.shade800),
            ],
          ),
        ),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _hr('Run #$id'),
                if (esc != null && esc.isNotEmpty)
                  _kvSelectable('Escalation', esc),
                if (vid != null) _kv('Validation run id', '$vid'),
                if (commitSha != null && commitSha.isNotEmpty) ...[
                  _kvSelectable('Commit SHA', commitSha),
                  if (branchName != null && branchName.isNotEmpty)
                    _kvLink(
                      'GitHub branch',
                      'https://github.com/MiacoRindolf/chili-home-copilot/tree/$branchName',
                      branchName,
                    ),
                ],
                if (pushUrl != null && pushUrl.isNotEmpty)
                  _kvSelectable('Push URL', pushUrl),
                if (snap != null) ...[
                  const SizedBox(height: 8),
                  Text('llm_snapshot',
                      style: Theme.of(context).textTheme.labelLarge),
                  const SizedBox(height: 4),
                  SelectableText(
                    _snapshotPreview(snap),
                    style: const TextStyle(fontSize: 12, color: Colors.black54),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _miniChip(String label, Color bg, Color fg) {
    final dark = Theme.of(context).brightness == Brightness.dark;
    final effectiveBg = dark
        ? Color.alphaBlend(
            fg.withValues(alpha: 0.18),
            Theme.of(context).colorScheme.surface,
          )
        : bg;
    final effectiveFg = dark ? Color.lerp(fg, Colors.white, 0.42)! : fg;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: effectiveBg,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: effectiveFg,
          fontSize: 11,
          fontWeight: FontWeight.w500,
        ),
      ),
    );
  }

  Widget _hr(String label) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(
        label,
        style: TextStyle(
          color: _mutedTextColor(),
          fontSize: 11,
          fontWeight: FontWeight.w500,
        ),
      ),
    );
  }

  Widget _kvSelectable(String k, String v) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 130,
            child: Text(k,
                style: TextStyle(color: _mutedTextColor(), fontSize: 12)),
          ),
          Expanded(
            child: SelectableText(v,
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onSurface,
                  fontFamily: 'monospace',
                  fontSize: 12,
                )),
          ),
        ],
      ),
    );
  }

  Widget _kvLink(String k, String url, String displayLabel) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 130,
            child: Text(k,
                style: TextStyle(color: Colors.grey.shade700, fontSize: 12)),
          ),
          Expanded(
            child: SelectableText.rich(
              TextSpan(
                text: displayLabel,
                style: const TextStyle(
                  color: Colors.blue,
                  decoration: TextDecoration.underline,
                  fontFamily: 'monospace',
                  fontSize: 12,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
