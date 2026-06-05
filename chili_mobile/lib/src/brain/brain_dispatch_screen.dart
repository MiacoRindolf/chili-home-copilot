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
import 'autopilot_ui.dart';
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
  final ScrollController _autopilotChatScroll = ScrollController();
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
  static const _autopilotChatFollowThreshold = 96.0;
  static const _autopilotPastedImagePrefix = 'chili_autopilot_paste';
  static const _autopilotExecutionModePlanApproval =
      ChiliApiClient.projectAutonomyPlanApprovalMode;
  static const _autopilotStatusAwaitingApproval = 'awaiting_approval';
  static const _autopilotStatusAwaitingClarification =
      'awaiting_clarification';
  static const _autopilotStatusChatting = 'chatting';
  static const _autopilotAttachmentKindImage = 'image';
  static const _autopilotArtifactPromptImage = 'prompt_image';
  static const _autopilotStartPlanLabel = 'Start plan';
  static const _autopilotAttachedImagePromptLabel =
      'Describe the attached image(s)';
  static const int _statusPollIntervalSeconds = 30;
  static const int _autopilotPollIntervalSeconds = 5;
  List<Map<String, dynamic>> _codeRepos = [];
  int? _autonomyRepoId;
  List<Map<String, dynamic>> _autonomyRuns = [];
  Map<String, dynamic>? _activeAutonomyRun;
  bool _autonomyLoading = false;
  bool _autonomyBusy = false;
  bool _autonomyListInFlight = false;
  bool _autonomyRefreshInFlight = false;
  bool _statusRefreshInFlight = false;
  bool _autopilotDropActive = false;
  String? _lastAutopilotChatSignature;
  String? _autonomyError;
  Timer? _autonomyTimer;

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
      unawaited(_loadAutonomyRuns());
    } else if (_tabs.index == 3) {
      unawaited(_loadRuns());
    } else if (_tabs.index == 4) {
      unawaited(_loadContextBrain());
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
      if (pick == null) {
        for (final repo in list) {
          if (repo['reachable_in_current_runtime'] == true) {
            pick = _asInt(repo['id']);
            if (pick != null) break;
          }
        }
      }
      if (pick == null && list.isNotEmpty) {
        pick = _asInt(list.first['id']);
      }
      if (mounted) {
        setState(() {
          _codeRepos = list;
          _autonomyRepoId = pick;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() => _autonomyError =
            'Could not load local repos: ${userVisibleNetworkError(e)}');
      }
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
    if (index < 0) {
      _autonomyRuns = [run, ..._autonomyRuns];
      return;
    }
    final updated = List<Map<String, dynamic>>.from(_autonomyRuns);
    updated[index] = {...updated[index], ...run};
    _autonomyRuns = updated;
  }

  Future<void> _loadAutonomyRuns({bool silent = false}) async {
    if (!mounted) return;
    if (_autonomyListInFlight) return;
    _autonomyListInFlight = true;
    if (!silent) {
      setState(() {
        _autonomyLoading = true;
        _autonomyError = null;
      });
    }
    try {
      final list = await _api.getProjectAutonomyRuns(limit: 20);
      Map<String, dynamic>? active = _activeAutonomyRun;
      if (list.isNotEmpty) {
        final activeId = active?['run_id']?.toString();
        Map<String, dynamic>? matchingActive;
        for (final item in list) {
          if (item['run_id']?.toString() == activeId) {
            matchingActive = item;
            break;
          }
        }
        active = matchingActive == null
            ? list.first
            : {
                ...matchingActive,
                ...?active,
                'status': matchingActive['status'],
                'current_stage': matchingActive['current_stage'],
                'plan_status': matchingActive['plan_status'],
                'merge_status': matchingActive['merge_status'],
                'updated_at': matchingActive['updated_at'],
              };
      }
      if (!mounted) return;
      setState(() {
        _autonomyRuns = list;
        _activeAutonomyRun = active;
        _autonomyLoading = false;
        _autonomyError = null;
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
    if (_autonomyTerminal(_activeAutonomyRun) && silent && !force) return;
    if (_autonomyRefreshInFlight) return;
    _autonomyRefreshInFlight = true;
    try {
      final run = await _api.getProjectAutonomyRun(runId);
      if (!mounted) return;
      final oldRunId = _activeAutonomyRun?['run_id']?.toString();
      final newRunId = run['run_id']?.toString();
      final follow = force ||
          oldRunId != newRunId ||
          _lastAutopilotChatSignature == null ||
          _autopilotChatIsNearBottom();
      final signature = _autopilotChatSignature(run);
      setState(() {
        _activeAutonomyRun = run;
        _syncAutonomyRunListState(run);
        _autonomyError = null;
      });
      _scheduleAutopilotChatFollow(
        signature: signature,
        force: force || oldRunId != newRunId,
        wasNearBottom: follow,
      );
    } catch (e) {
      if (!mounted || silent) return;
      setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      _autonomyRefreshInFlight = false;
    }
  }

  Future<void> _startAutopilot() async {
    final prompt = _autopilotPromptCtrl.text.trim();
    final attachments = _autopilotAttachmentPayloads();
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
              executionMode: _autopilotExecutionModePlanApproval,
              startPlanning: false,
              attachments: attachments,
            );
      if (!mounted) return;
      setState(() {
        _activeAutonomyRun = run;
        _syncAutonomyRunListState(run);
        _autopilotPromptCtrl.clear();
        _autopilotPendingImages.clear();
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
          _syncAutonomyRunListState(run);
        });
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
          _syncAutonomyRunListState(run);
        });
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
    setState(() {
      _autopilotPromptCtrl.text = prompt;
      _autopilotPromptCtrl.selection = TextSelection.collapsed(
        offset: _autopilotPromptCtrl.text.length,
      );
      _autopilotPendingImages.clear();
      if (repoId != null) _autonomyRepoId = repoId;
      _autonomyError = null;
    });
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(
        content: Text('Prompt restored. Press Run to start a fresh safe run.'),
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
          _syncAutonomyRunListState(run);
        });
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
    final reviewBody =
        AutonomyRunPresenter.architectReviewBody(_asMap(run['architect_review']));
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
    _tabs.removeListener(_onTabChanged);
    _tabs.dispose();
    _titleCtrl.dispose();
    _descCtrl.dispose();
    _sourceCtrl.dispose();
    _autopilotPromptCtrl.dispose();
    _autopilotChatScroll.dispose();
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
                  Icon(Icons.smart_toy,
                      size: 40, color: Theme.of(context).colorScheme.primary),
                  const SizedBox(height: 12),
                  Text(
                    'Pair Your Device',
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                  const SizedBox(height: 4),
                  Text(
                    'Connect to the Brain autopilot',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.onSurfaceVariant,
                      fontSize: 13,
                    ),
                  ),
                  const SizedBox(height: 12),
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
          padding: const EdgeInsets.fromLTRB(24, 16, 16, 8),
          child: Row(
            children: [
              Icon(Icons.smart_toy, color: Theme.of(context).colorScheme.primary),
              const SizedBox(width: 10),
              Text(
                'Brain',
                style: Theme.of(context)
                    .textTheme
                    .headlineSmall
                    ?.copyWith(fontWeight: FontWeight.w700),
              ),
              const SizedBox(width: 8),
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  'Autopilot',
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                    fontSize: 13,
                  ),
                ),
              ),
              const Spacer(),
              _headerStatusPill(context),
            ],
          ),
        ),
        TabBar(
          controller: _tabs,
          tabs: const [
            Tab(text: 'Status', icon: Icon(Icons.dashboard_outlined, size: 18)),
            Tab(text: 'Autopilot', icon: Icon(Icons.auto_awesome, size: 18)),
            Tab(text: 'Queue', icon: Icon(Icons.playlist_add, size: 18)),
            Tab(text: 'History', icon: Icon(Icons.history, size: 18)),
            Tab(text: 'Context', icon: Icon(Icons.account_tree_outlined, size: 18)),
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

  /// Compact header indicator: kill-switch state, else live/connecting.
  Widget _headerStatusPill(BuildContext context) {
    final s = _status;
    if (s == null) {
      return const ApStatusPill('Connecting…',
          color: Colors.grey, icon: Icons.cloud_off);
    }
    final killRaw = s['kill_switch'];
    final active = killRaw is Map && killRaw['active'] == true;
    if (active) {
      return const ApStatusPill('Kill switch ON',
          color: Colors.red, icon: Icons.block);
    }
    return const ApStatusPill('Live', color: Colors.green, icon: Icons.bolt);
  }

  Widget _buildStatusTab() {
    if (_statusError != null) {
      return ApEmptyState(
        icon: Icons.cloud_off,
        message: 'Couldn’t load status',
        detail: _statusError,
        action: FilledButton.icon(
          onPressed: _refreshStatus,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Retry'),
        ),
      );
    }
    final s = _status;
    if (s == null) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            CircularProgressIndicator(),
            SizedBox(height: 12),
            Text('Loading status…'),
          ],
        ),
      );
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

    final cs = Theme.of(context).colorScheme;
    double spendTotal = 0;
    for (final row in spendToday) {
      if (row is Map && row['spend_usd'] is num) {
        spendTotal += (row['spend_usd'] as num).toDouble();
      }
    }

    return RefreshIndicator(
      onRefresh: _refreshStatus,
      child: ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.all(24),
      children: [
        // Batch 6 — Kill switch panel: color-coded state + single toggle.
        ApPanel(
          color: active
              ? cs.error.withValues(alpha: 0.06)
              : Colors.green.withValues(alpha: 0.05),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(active ? Icons.block : Icons.shield_outlined,
                      color: active ? cs.error : Colors.green.shade600, size: 20),
                  const SizedBox(width: 8),
                  Text('Kill switch',
                      style: Theme.of(context).textTheme.titleMedium),
                  const SizedBox(width: 10),
                  ApStatusPill(
                    active ? 'ON' : 'Off',
                    color: active ? Colors.red : Colors.green,
                  ),
                  const Spacer(),
                  if (!active)
                    FilledButton.icon(
                      onPressed: () => _confirmKillSwitch(true),
                      icon: const Icon(Icons.block, size: 16),
                      label: const Text('Enable'),
                      style: FilledButton.styleFrom(backgroundColor: cs.error),
                    )
                  else
                    OutlinedButton.icon(
                      onPressed: () => _confirmKillSwitch(false),
                      icon: const Icon(Icons.play_arrow, size: 16),
                      label: const Text('Disable'),
                    ),
                ],
              ),
              if (active && reason.isNotEmpty) ...[
                const SizedBox(height: 8),
                Text(reason, style: TextStyle(color: cs.onSurfaceVariant)),
              ],
              const SizedBox(height: 6),
              Text(
                active
                    ? 'Automated dispatch is halted until disabled.'
                    : 'Automated dispatch is allowed.',
                style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant),
              ),
            ],
          ),
        ),
        const SizedBox(height: 10),
        // Batch 9 — freshness line.
        Row(
          children: [
            Icon(Icons.schedule, size: 14, color: cs.onSurfaceVariant),
            const SizedBox(width: 6),
            Text('Last activity ${_agoLabel(lastIso)}',
                style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
          ],
        ),
        const SizedBox(height: 18),
        // Batch 7 — 5-min counters as stat tiles.
        const ApSectionHeader('Counters · last 5 min', icon: Icons.bolt),
        const SizedBox(height: 6),
        if (counters.isEmpty)
          Text('No runs in the last 5 minutes',
              style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant))
        else
          Wrap(
            spacing: 10,
            runSpacing: 10,
            children: counters.entries.map((e) {
              return SizedBox(
                width: 130,
                child: ApStatCard(
                  label: e.key.toString().replaceAll('_', ' '),
                  value: '${e.value}',
                ),
              );
            }).toList(),
          ),
        const SizedBox(height: 20),
        // Batch 8 — spend today panel + total.
        ApSectionHeader(
          'LLM spend · today',
          icon: Icons.attach_money,
          trailing: spendToday.isEmpty
              ? null
              : Text('\$${spendTotal.toStringAsFixed(4)}',
                  style: TextStyle(
                      fontWeight: FontWeight.w700, color: cs.onSurface)),
        ),
        const SizedBox(height: 6),
        if (spendToday.isEmpty)
          Text('No LLM spend recorded today',
              style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant))
        else
          ApPanel(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Column(
              children: [
                for (int i = 0; i < spendToday.length; i++) ...[
                  if (i > 0) Divider(height: 14, color: cs.outlineVariant),
                  Builder(builder: (context) {
                    final m = Map<String, dynamic>.from(
                        spendToday[i] as Map<dynamic, dynamic>);
                    final prov = '${m['provider'] ?? ''}';
                    final calls = m['calls'] ?? 0;
                    final usd = (m['spend_usd'] is num)
                        ? (m['spend_usd'] as num).toDouble()
                        : 0.0;
                    return Row(
                      children: [
                        Icon(Icons.smart_toy_outlined,
                            size: 16, color: cs.secondary),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(prov,
                              style: const TextStyle(
                                  fontWeight: FontWeight.w600)),
                        ),
                        Text('$calls calls',
                            style: TextStyle(
                                fontSize: 12, color: cs.onSurfaceVariant)),
                        const SizedBox(width: 12),
                        Text('\$${usd.toStringAsFixed(4)}',
                            style:
                                const TextStyle(fontWeight: FontWeight.w600)),
                      ],
                    );
                  }),
                ],
              ],
            ),
          ),
      ],
      ),
    );
  }

  Map<String, dynamic> _asMap(dynamic raw) {
    if (raw is Map<String, dynamic>) return raw;
    if (raw is Map) return Map<String, dynamic>.from(raw);
    return <String, dynamic>{};
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
        ],
      ),
    );
  }

  Widget _buildAutonomyThreadSidebar() {
    return Container(
      color: _autonomySidebarColor(),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
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
              onPressed: _autonomyBusy
                  ? null
                  : () {
                      _autopilotPromptCtrl.clear();
                      setState(() {
                        _activeAutonomyRun = null;
                        _autopilotPendingImages.clear();
                      });
                    },
              icon: const Icon(Icons.add, size: 18),
              label: const Text('New run'),
              // Batch 14 — full-width, taller, prominent primary action.
              style: FilledButton.styleFrom(
                  minimumSize: const Size.fromHeight(40)),
            ),
          ),
          Expanded(
            child: _autonomyLoading
                // Batch 15 — labeled loading state.
                ? const Center(
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        CircularProgressIndicator(),
                        SizedBox(height: 12),
                        Text('Loading runs…'),
                      ],
                    ),
                  )
                : _autonomyRuns.isEmpty
                    ? _emptyAutonomyState('No run history yet')
                    : ListView.builder(
                        padding: const EdgeInsets.fromLTRB(8, 0, 8, 12),
                        itemCount: _autonomyRuns.length,
                        itemBuilder: (context, index) =>
                            _buildAutonomyThreadTile(_autonomyRuns[index]),
                      ),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyThreadTile(Map<String, dynamic> run) {
    final runId = run['run_id']?.toString() ?? '';
    final status = run['status']?.toString() ?? 'unknown';
    final stage = run['current_stage']?.toString() ?? '';
    final prompt = run['prompt']?.toString() ?? '';
    final selected = _activeAutonomyRun?['run_id']?.toString() == runId;
    final color = _autonomyStatusColor(status);
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: DecoratedBox(
        // Batch 12 — clearer selection: tinted fill + colored border.
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(8),
          border: selected
              ? Border.all(color: color.withValues(alpha: 0.55))
              : null,
        ),
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
            setState(() => _activeAutonomyRun = run);
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
                // Batch 13 — relative "ago" timestamp with a clock icon.
                Row(
                  children: [
                    Icon(Icons.schedule, size: 11, color: _mutedTextColor()),
                    const SizedBox(width: 4),
                    Text(
                      _agoLabel(
                          (run['updated_at'] ?? run['created_at'])?.toString()),
                      style: TextStyle(color: _mutedTextColor(), fontSize: 11),
                    ),
                  ],
                ),
              ],
            ),
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
    final color = _autonomyStatusColor(status);
    final runId = run?['run_id']?.toString() ?? '';
    return Container(
      padding: const EdgeInsets.fromLTRB(18, 12, 14, 12),
      decoration: BoxDecoration(
        // Batch 31 — subtle tint separates the header from the chat area.
        color: Theme.of(context)
            .colorScheme
            .surfaceContainerHighest
            .withValues(alpha: 0.35),
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
          // Batch 32 — copy the run ID for sharing / lookups.
          if (runId.isNotEmpty)
            IconButton(
              tooltip: 'Copy run ID',
              visualDensity: VisualDensity.compact,
              onPressed: () {
                Clipboard.setData(ClipboardData(text: runId));
                ScaffoldMessenger.of(context).showSnackBar(
                  const SnackBar(
                    content: Text('Run ID copied'),
                    behavior: SnackBarBehavior.floating,
                    duration: Duration(seconds: 2),
                  ),
                );
              },
              icon: const Icon(Icons.copy, size: 18),
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
    final active = const {'queued', 'running', 'validating', 'merging'}
            .contains(status) ||
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
            'queued' => 'Current progress: waiting for the local Autopilot worker.',
            'plan' => 'Current progress: drafting and reviewing the architect plan.',
            'implement' => 'Current progress: implementing in an isolated worktree.',
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
    final bool truncated = body.length > _autopilotMessagePreviewLimit;
    final content = ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: _autopilotBubbleMaxWidth),
      child: Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: background,
          border: Border.all(color: color.withValues(alpha: 0.18)),
          // Batch 21 — asymmetric "tail" corner toward the speaker.
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(12),
            topRight: const Radius.circular(12),
            bottomLeft: Radius.circular(alignRight ? 12 : 4),
            bottomRight: Radius.circular(alignRight ? 4 : 12),
          ),
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
                truncated
                    ? '${body.substring(0, _autopilotMessagePreviewLimit)}…'
                    : body,
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onSurface,
                  fontSize: 13,
                  height: 1.35,
                ),
              ),
              // Batch 22 — explicit truncation hint.
              if (truncated)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    'message truncated',
                    style: TextStyle(
                      color: _mutedTextColor(),
                      fontSize: 11,
                      fontStyle: FontStyle.italic,
                    ),
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
              final review = _asMap(_activeAutonomyRun?['architect_review']);
              final reviewPassed =
                  AutonomyRunPresenter.architectReviewPassed(review);
              return Container(
                width: double.infinity,
                margin: const EdgeInsets.only(bottom: 10),
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: _autonomyBubbleBackground(
                    reviewPassed ? Colors.teal : Colors.orange,
                    alpha: 0.10,
                  ),
                  border: Border.all(
                    color: (reviewPassed ? Colors.teal : Colors.orange)
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
                      reviewPassed
                          ? 'Plan Mode is waiting for approval.'
                          : 'Architect quality gate has not passed.',
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.onSurface,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    if (reviewPassed)
                      FilledButton.icon(
                        onPressed:
                            _autonomyBusy ? null : _approveAutopilotPlan,
                        icon: const Icon(Icons.play_arrow, size: 18),
                        label: const Text('Approve and implement'),
                      ),
                    Text(
                      reviewPassed
                          ? 'Or send feedback below to revise it.'
                          : 'Send feedback below so CHILI can revise it.',
                      style: TextStyle(color: _mutedTextColor(), fontSize: 12),
                    ),
                  ],
                ),
              );
            }),
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
                      // Batch 23 — modern rounded, filled input.
                      filled: true,
                      fillColor: Theme.of(context).colorScheme.surface,
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
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
                      : _startAutopilot,
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
          // Batch 24 — input affordance hint.
          const SizedBox(height: 6),
          Align(
            alignment: Alignment.centerLeft,
            child: Text(
              'Enter to send · attach, paste or drag-drop images',
              style: TextStyle(color: _mutedTextColor(), fontSize: 11),
            ),
          ),
          if (_codeRepos.isEmpty) ...[
            const SizedBox(height: 8),
            Row(
              children: [
                Icon(Icons.warning_amber_rounded,
                    size: 16, color: _autonomyStatusColor('blocked')),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    'No registered local repos are visible to this desktop backend.',
                    style: TextStyle(
                        color: _autonomyStatusColor('blocked'), fontSize: 13),
                  ),
                ),
              ],
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
                    // Batch 25 — rounded drop highlight.
                    borderRadius: BorderRadius.circular(12),
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

  Widget _buildAutonomyRepoPicker() {
    return DropdownButtonFormField<int>(
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
                '${repo['reachable_in_current_runtime'] == true ? '' : ' (not reachable here)'}',
                overflow: TextOverflow.ellipsis,
              ),
            ),
      ],
      onChanged: _autonomyBusy
          ? null
          : (value) => setState(() => _autonomyRepoId = value),
    );
  }

  Widget _buildAutonomyActionPanel(Map<String, dynamic> run) {
    final status = run['status']?.toString() ?? '';
    final planStatus = run['plan_status']?.toString() ?? '';
    final architectReview = _asMap(run['architect_review']);
    final terminal = _autonomyTerminal(run);
    final branch = run['integration_branch']?.toString() ?? '';
    final merge = run['merge_status']?.toString() ?? '';
    final canApprove = status == _autopilotStatusAwaitingApproval &&
        planStatus == _autopilotStatusAwaitingApproval &&
        AutonomyRunPresenter.architectReviewPassed(architectReview) &&
        _asMap(run['plan']).isNotEmpty;
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
        const SizedBox(height: 10),
        if (canStartPlan)
          FilledButton.icon(
            onPressed: _autonomyBusy ? null : _startAutopilotPlan,
            icon: const Icon(Icons.account_tree_outlined, size: 18),
            label: const Text(_autopilotStartPlanLabel),
          ),
        if (canStartPlan) const SizedBox(height: 8),
        if (canApprove)
          FilledButton.icon(
            onPressed: _autonomyBusy ? null : _approveAutopilotPlan,
            icon: const Icon(Icons.play_arrow, size: 18),
            label: const Text('Approve plan and implement'),
          ),
        if (canApprove) const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            OutlinedButton.icon(
              onPressed: _autonomyBusy
                  ? null
                  : () async {
                      await _loadCodeRepos();
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
            AutonomyRunPresenter.architectReviewPassed(architectReview)
                ? 'Plan Mode is waiting. Send feedback in chat to revise, or approve when it looks right.'
                : 'Architect quality gate has not passed. Send feedback in chat to revise this plan.',
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
            const SizedBox(height: 12),
            OutlinedButton.icon(
              onPressed: _autonomyBusy
                  ? null
                  : () async {
                      await _loadCodeRepos();
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
    // Batch 11 — shared empty state upgraded to the AP-UX primitive (icon +
    // typographic hierarchy); improves all six call sites at once.
    return ApEmptyState(icon: Icons.forum_outlined, message: label);
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
                _miniChip(
                    _autonomyStatusLabel(status),
                    statusColor.withValues(alpha: 0.12),
                    statusColor),
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
            if (worktree.isNotEmpty) ...[
              const SizedBox(height: 12),
              _kvSelectable('Worktree', worktree),
            ],
            if (mergeMessage.isNotEmpty) _kvSelectable('Merge', mergeMessage),
            // Batch 33 — blocked/error surfaced as a prominent banner.
            if (errorMessage.isNotEmpty) ...[
              const SizedBox(height: 10),
              ApPanel(
                color: Theme.of(context)
                    .colorScheme
                    .error
                    .withValues(alpha: 0.06),
                padding: const EdgeInsets.all(12),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(Icons.error_outline,
                        size: 18, color: Theme.of(context).colorScheme.error),
                    const SizedBox(width: 8),
                    Expanded(
                      child: SelectableText(
                        AutonomyRunPresenter.blockedRunMessage(run),
                        style: TextStyle(
                            color: Theme.of(context).colorScheme.error),
                      ),
                    ),
                  ],
                ),
              ),
            ],
            const Divider(height: 28),
            _buildAutonomyArchitectQuality(architectReview),
            const Divider(height: 28),
            _buildAutonomyPlan(plan, files),
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
        const ApSectionHeader('Architect quality', icon: Icons.verified_outlined),
        const SizedBox(height: 4),
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

  Widget _buildAutonomyPlan(Map<String, dynamic> plan, List<String> files) {
    final planBody = AutonomyRunPresenter.planBody(plan);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const ApSectionHeader('Architect plan', icon: Icons.architecture),
        const SizedBox(height: 4),
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

  Widget _buildAutonomyAgents(List<Map<String, dynamic>> agents) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const ApSectionHeader('Agent lanes', icon: Icons.groups_outlined),
        const SizedBox(height: 4),
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
        const ApSectionHeader('Validation', icon: Icons.fact_check_outlined),
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
                    const SizedBox(height: 6),
                    // Batch 36 — console-style output block.
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.all(10),
                      decoration: BoxDecoration(
                        color: Theme.of(context)
                            .colorScheme
                            .surfaceContainerHighest
                            .withValues(alpha: 0.6),
                        borderRadius: BorderRadius.circular(8),
                      ),
                      child: SelectableText(
                        output.length > 600
                            ? '${output.substring(0, 600)}…'
                            : output,
                        style: const TextStyle(
                          fontFamily: 'monospace',
                          fontSize: 12,
                          height: 1.3,
                        ),
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
        const ApSectionHeader('Artifacts', icon: Icons.folder_open_outlined),
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
                        // Batch 38 — artifact icon coloured for quick scanning.
                        Icon(_autonomyArtifactIcon(type),
                            size: 18,
                            color: Theme.of(context).colorScheme.secondary),
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
                      // Batch 40 — framed image preview.
                      Container(
                        decoration: BoxDecoration(
                          borderRadius: BorderRadius.circular(6),
                          border: Border.all(color: _autonomyDividerColor()),
                        ),
                        clipBehavior: Clip.antiAlias,
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
        // Batch 39 — surface what the take(18) cap hides (no silent truncation).
        if (visible.length > 18)
          Padding(
            padding: const EdgeInsets.only(top: 2),
            child: Text(
              'Showing 18 of ${visible.length} artifacts',
              style: TextStyle(
                  color: _mutedTextColor(),
                  fontSize: 11,
                  fontStyle: FontStyle.italic),
            ),
          ),
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
        const ApSectionHeader('Learning signals', icon: Icons.school_outlined),
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
        const ApSectionHeader('Run steps', icon: Icons.timeline),
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
            // Batch 37 — timeline dot coloured by step status.
            final dotColor = status.isNotEmpty
                ? _autonomyStatusColor(status)
                : _mutedTextColor();
            return ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              leading: Icon(Icons.circle, size: 10, color: dotColor),
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
    // Batch 18 — empty-projects state via the shared primitive.
    if (_projects.isEmpty) {
      return ApEmptyState(
        icon: Icons.folder_off_outlined,
        message: 'No planner projects',
        detail: 'Create one in the web planner, then reload.',
        action: FilledButton.icon(
          onPressed: _loadProjectsPicker,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Reload projects'),
        ),
      );
    }
    final cs = Theme.of(context).colorScheme;
    return ListView(
      padding: const EdgeInsets.all(24),
      children: [
        const ApSectionHeader('Queue a task', icon: Icons.playlist_add),
        const SizedBox(height: 10),
        if (_queueError != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: ApPanel(
              color: cs.error.withValues(alpha: 0.06),
              padding: const EdgeInsets.all(12),
              child: Row(
                children: [
                  Icon(Icons.error_outline, size: 18, color: cs.error),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(_queueError!,
                        style: TextStyle(color: cs.error)),
                  ),
                ],
              ),
            ),
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
            helperText: 'C:\\dev\\some-project · /workspace · '
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
          label: Text(_queueBusy ? 'Queueing…' : 'Queue task'),
          // Batch 18 — full-width primary action.
          style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(46)),
        ),
      ],
    );
  }

  Widget _buildContextTab() {
    // Batch 19 — consistent loading / error states.
    if (_ctxLoading) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            CircularProgressIndicator(),
            SizedBox(height: 12),
            Text('Loading Context Brain…'),
          ],
        ),
      );
    }
    if (_ctxError != null) {
      return ApEmptyState(
        icon: Icons.cloud_off,
        message: 'Failed to load Context Brain status',
        detail: _ctxError,
        action: FilledButton.icon(
          onPressed: _loadContextBrain,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Retry'),
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
                      // Batch 20 — mode pill color-coded like the header.
                      Builder(builder: (_) {
                        final mode = state['mode']?.toString() ?? 'unknown';
                        final color = mode == 'paused'
                            ? Colors.red
                            : (mode == 'unknown' ? Colors.grey : Colors.green);
                        return ApStatusPill(mode, color: color);
                      }),
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
    // Batch 16 — consistent loading / error states.
    if (_runsLoading) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            CircularProgressIndicator(),
            SizedBox(height: 12),
            Text('Loading run history…'),
          ],
        ),
      );
    }
    if (_runsError != null) {
      return ApEmptyState(
        icon: Icons.cloud_off,
        message: 'Couldn’t load run history',
        detail: _runsError,
        action: FilledButton.icon(
          onPressed: _loadRuns,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Retry'),
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
      return const ApEmptyState(
        icon: Icons.history,
        message: 'No runs yet',
        detail: 'Autopilot runs will appear here as they happen.',
      );
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
            child: ApEmptyState(
              icon: Icons.filter_alt_off_outlined,
              message: 'No runs match the current filter',
              detail: 'Everything is clear — nothing needs review.',
              action: hiddenCount > 0
                  ? TextButton(
                      onPressed: () =>
                          setState(() => _hideRouterEscalate = false),
                      child: Text('Show $hiddenCount auto-escalations'),
                    )
                  : null,
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

    final cs = Theme.of(context).colorScheme;
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
      // Batch 43 — dark-mode-safe highlight/notify tints (were *.shade50).
      color: highlight
          ? Colors.amber.withValues(alpha: 0.12)
          : (notify ? cs.error.withValues(alpha: 0.08) : null),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(8),
        // Batch 44 — theme border (was Colors.grey.shade200).
        side: BorderSide(
          color: notify ? cs.error.withValues(alpha: 0.4) : cs.outlineVariant,
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
              // Batch 45 — theme-muted (was Colors.grey.shade600).
              style: TextStyle(color: _mutedTextColor(), fontSize: 11),
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
                  const ApSectionHeader('LLM snapshot', icon: Icons.memory),
                  const SizedBox(height: 4),
                  // Batch 41/42 — console block; fixes a dark-mode legibility
                  // bug (was hardcoded Colors.black54, invisible on dark).
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: Theme.of(context)
                          .colorScheme
                          .surfaceContainerHighest
                          .withValues(alpha: 0.6),
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: SelectableText(
                      _snapshotPreview(snap),
                      style: const TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 12,
                        height: 1.3,
                      ),
                    ),
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
