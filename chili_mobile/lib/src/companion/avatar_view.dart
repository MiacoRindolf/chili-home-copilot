import 'dart:async';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:pasteboard/pasteboard.dart';
import 'package:window_manager/window_manager.dart';
import 'package:desktop_drop/desktop_drop.dart';

import '../config/app_config.dart';
import '../config/layout_constants.dart';
import '../network/chili_api_client.dart';
import '../screen/focus_controller.dart';
import '../screen/focus_target.dart';
import '../screen/region_selector.dart';
import '../voice/tts_controller.dart';
import '../voice/voice_input.dart';
import '../widgets/chili_avatar.dart';
import '../widgets/chat_message_bubble.dart';
import 'chat_send_controller.dart';
import 'onboarding_overlay.dart';
import 'sound_effects.dart';
import 'shared_chat_history.dart';

/// The small floating avatar with an expandable chat bubble.
///
/// - Drag the avatar to move the window.
/// - Single tap to toggle the chat bubble.
/// - Double-tap to open the full app.
/// - Long-press the mic button to record voice.
/// - Say the wake word (e.g. "Chili") then your question for hands-free.
/// - Drop files onto the avatar to send them to CHILI.
class AvatarView extends StatefulWidget {
  const AvatarView({
    super.key,
    required this.sharedHistory,
    required this.onOpenFullApp,
    required this.focusController,
    this.pauseWakeWord,
    this.ttsPlaying,
    this.ttsInterruptRequested,
    this.lastTtsText,
    this.wakeWordCommand,
    this.wakeWordReply,
    this.wakeWordStatus,
    this.wakeWordPartial,
    this.wakeWordFollowUpActive,
  });

  final SharedChatHistory sharedHistory;
  final VoidCallback onOpenFullApp;
  final FocusController focusController;
  final ValueNotifier<bool>? pauseWakeWord;
  final ValueNotifier<bool>? ttsPlaying;
  final ValueNotifier<bool>? ttsInterruptRequested;
  final ValueNotifier<String?>? lastTtsText;
  final ValueNotifier<String?>? wakeWordCommand;
  final ValueNotifier<String?>? wakeWordReply;
  final ValueNotifier<String?>? wakeWordStatus;
  final ValueNotifier<String?>? wakeWordPartial;
  final ValueNotifier<bool>? wakeWordFollowUpActive;

  @override
  State<AvatarView> createState() => _AvatarViewState();
}

class _AvatarViewState extends State<AvatarView> {
  final _client = ChiliApiClient();
  final _controller = TextEditingController();
  final _scrollController = ScrollController();
  final _chatFocusNode = FocusNode();
  late final TtsController _ttsController;
  late final ChatSendController _chatSender;
  FocusController get _focus => widget.focusController;
  String _streamingReply = '';
  String? _lastFailedMessage;
  bool _isSending = false;
  bool _showChat = false;
  bool _isDraggingFile = false;
  AvatarState _avatarState = AvatarState.idle;
  bool _showOnboarding = false;
  int _onboardingStep = 0;
  bool _userMuted = false;
  bool _recording = false;
  bool _chatInputHasFocus = false;
  CancelToken? _activeCancelToken;
  final List<String> _pendingImages = [];
  static const _allowedImageExts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'};
  bool _showHappyBriefly = false;
  bool _showWakeDetectedBriefly = false;
  Size _lastChatWindowSize = LayoutConstants.avatarWindowLarge;
  String? _prevWakeWordStatus;
  bool _actionPerforming = false;
  int _thinkingIndex = 0;
  Timer? _thinkingTimer;
  static const _thinkingMessages = [
    'Hmm, let me think…',
    'Cooking up an answer…',
    'Consulting my spicy brain…',
    'One moment…',
    'Almost there…',
  ];

  @override
  void initState() {
    super.initState();
    _ttsController = TtsController(
      ttsPlaying: widget.ttsPlaying,
      onFinish: _finishSpeaking,
      lastTtsText: widget.lastTtsText,
    );
    _chatSender = ChatSendController(_client);
    _loadOnboardingState();
    widget.sharedHistory.addListener(_onHistoryChanged);
    widget.wakeWordReply?.addListener(_onWakeWordReply);
    widget.wakeWordStatus?.addListener(_onStatusChanged);
    widget.wakeWordPartial?.addListener(_onStatusChanged);
    widget.wakeWordFollowUpActive?.addListener(_onStatusChanged);
    widget.ttsInterruptRequested?.addListener(_onTtsInterrupt);
    _chatFocusNode.addListener(_onChatFocusChange);
  }

  @override
  void dispose() {
    _thinkingTimer?.cancel();
    widget.sharedHistory.removeListener(_onHistoryChanged);
    widget.wakeWordReply?.removeListener(_onWakeWordReply);
    widget.wakeWordStatus?.removeListener(_onStatusChanged);
    widget.wakeWordPartial?.removeListener(_onStatusChanged);
    widget.wakeWordFollowUpActive?.removeListener(_onStatusChanged);
    widget.ttsInterruptRequested?.removeListener(_onTtsInterrupt);
    _chatFocusNode.removeListener(_onChatFocusChange);
    _chatFocusNode.dispose();
    _ttsController.dispose();
    _controller.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  void _onChatFocusChange() {
    if (mounted) {
      setState(() => _chatInputHasFocus = _chatFocusNode.hasFocus);
    }
  }

  void _onHistoryChanged() {
    if (mounted) setState(() {});
  }

  Future<void> _loadOnboardingState() async {
    await AppConfig.instance.load();
    if (mounted) {
      setState(() => _showOnboarding = !AppConfig.instance.onboardingDone);
    }
  }

  Future<void> _onOnboardingNext() async {
    if (_onboardingStep >= 2) {
      await AppConfig.instance.setOnboardingDone();
      if (mounted) {
        setState(() {
          _showOnboarding = false;
          _onboardingStep = 0;
        });
      }
    } else {
      if (mounted) {
        setState(() => _onboardingStep++);
      }
    }
  }

  void _onStatusChanged() {
    final status = widget.wakeWordStatus?.value ?? '';
    final wasTranscribing = _prevWakeWordStatus?.startsWith('Transcribing') ?? false;
    final nowListening = status.contains('say your command') || status.contains('follow-up');
    if (wasTranscribing && nowListening && status.isNotEmpty) {
      SoundEffects.playWakeDetected();
      if (mounted) {
        setState(() => _showWakeDetectedBriefly = true);
        Future.delayed(const Duration(seconds: 1), () {
          if (mounted) {
            setState(() => _showWakeDetectedBriefly = false);
          }
        });
      }
    }
    _prevWakeWordStatus = status;
    if (mounted) setState(() {});
  }

  /// Resolves the avatar state to display from current signals.
  AvatarState _effectiveAvatarState(bool showListeningState) {
    if (_showWakeDetectedBriefly) return AvatarState.wakeDetected;
    if (_lastFailedMessage != null && !_isSending) return AvatarState.error;
    if (_showHappyBriefly) return AvatarState.happy;
    if (_actionPerforming) return AvatarState.actionPerforming;
    if (showListeningState) return AvatarState.listening;
    if (_userMuted && _avatarState == AvatarState.idle) return AvatarState.muted;
    if (_chatInputHasFocus && _avatarState == AvatarState.idle && _showChat) return AvatarState.reading;
    if (_focus.isFocused.value && _avatarState == AvatarState.idle) {
      return AvatarState.focused;
    }
    return _avatarState;
  }

  Future<void> _onWakeWordReply() async {
    final reply = widget.wakeWordReply?.value;
    if (reply == null || !mounted) return;
    final command = widget.wakeWordCommand?.value;
    widget.wakeWordCommand?.value = null;
    widget.wakeWordReply!.value = null;
    _controller.clear();
    if (command != null && command.isNotEmpty) {
      widget.sharedHistory.addUser(command);
    }
    widget.sharedHistory.addAssistant(reply);
    if (mounted) {
      setState(() => _avatarState = AvatarState.speaking);
    }
    await _openChatWindow();
    _scrollToBottom();
    SoundEffects.playReplyArrived();
    _ttsController.speak(reply);
  }

  void _onTtsInterrupt() {
    if (widget.ttsInterruptRequested?.value != true) return;
    widget.ttsInterruptRequested?.value = false;
    _ttsController.stop();
  }

  void _finishSpeaking() {
    if (mounted) {
      setState(() {
        _avatarState = AvatarState.idle;
        _showHappyBriefly = true;
      });
      Future.delayed(const Duration(milliseconds: 1500), () {
        if (mounted) setState(() => _showHappyBriefly = false);
      });
    }
  }

  Future<void> _toggleChat() async {
    if (_showChat) {
      await _closeChatWindow();
    } else {
      await _openChatWindow();
    }
  }

  Future<void> _openChatWindow() async {
    if (_showChat) return;
    final pos = await windowManager.getPosition();
    final oldSize = await windowManager.getSize();
    final newSize = _lastChatWindowSize;
    await windowManager.setSize(newSize);
    final dx = (newSize.width - oldSize.width) / 2;
    await windowManager.setPosition(Offset(pos.dx - dx, pos.dy));
    if (mounted) setState(() => _showChat = true);
  }

  Future<void> _closeChatWindow() async {
    if (!_showChat) return;
    _lastChatWindowSize = await windowManager.getSize();
    final pos = await windowManager.getPosition();
    final dx = (_lastChatWindowSize.width - LayoutConstants.avatarWindowSmall.width) / 2;
    await windowManager.setSize(LayoutConstants.avatarWindowSmall);
    await windowManager.setPosition(Offset(pos.dx + dx, pos.dy));
    if (mounted) setState(() => _showChat = false);
  }

  bool _isImageFile(String path) {
    final ext = path.toLowerCase();
    return _allowedImageExts.any((e) => ext.endsWith(e));
  }

  void _addPendingImage(String path) {
    if (_pendingImages.length >= 5) return;
    if (!_isImageFile(path)) return;
    setState(() => _pendingImages.add(path));
    if (!_showChat) _openChatWindow();
  }

  void _removePendingImage(int index) {
    setState(() => _pendingImages.removeAt(index));
  }

  Future<void> _pickImages() async {
    final result = await FilePicker.platform.pickFiles(
      type: FileType.image,
      allowMultiple: true,
    );
    if (result == null) return;
    for (final file in result.files) {
      if (file.path != null) _addPendingImage(file.path!);
    }
  }

  Future<void> _pasteImage() async {
    try {
      final imageBytes = await Pasteboard.image;
      if (imageBytes == null || imageBytes.isEmpty) return;
      final dir = Directory.systemTemp;
      final ts = DateTime.now().millisecondsSinceEpoch;
      final tempFile = File('${dir.path}/chili_paste_$ts.png');
      await tempFile.writeAsBytes(imageBytes);
      _addPendingImage(tempFile.path);
    } catch (_) {}
  }

  Future<void> _takeScreenshot() async {
    try {
      final imageBytes = await Pasteboard.image;
      if (imageBytes != null && imageBytes.isNotEmpty) {
        final dir = Directory.systemTemp;
        final ts = DateTime.now().millisecondsSinceEpoch;
        final tempFile = File('${dir.path}/chili_screenshot_$ts.png');
        await tempFile.writeAsBytes(imageBytes);
        _addPendingImage(tempFile.path);
      }
    } catch (_) {}
  }

  void _stopGenerating() {
    _activeCancelToken?.cancel();
  }

  Future<void> _toggleFocus() async {
    if (_focus.isFocused.value) {
      _focus.stop();
      setState(() => _avatarState = AvatarState.idle);
      return;
    }

    final target = await Navigator.push<FocusTarget>(
      context,
      MaterialPageRoute(builder: (_) => const FocusSelectorScreen()),
    );
    if (target == null || !mounted) return;

    _focus.start(target);
    if (mounted) setState(() => _avatarState = AvatarState.focused);
  }

  Future<void> _sendMessage() async {
    final text = _controller.text.trim();
    final images = List<String>.from(_pendingImages);

    if (text.isEmpty && images.isEmpty && !_focus.isFocused.value) return;
    if (_isSending) return;
    SoundEffects.playButtonClick();

    // Capture a fresh screenshot on-demand when Focus Mode is active.
    if (_focus.isFocused.value) {
      final shotPath = await _focus.captureNow();
      if (shotPath != null && !images.contains(shotPath)) {
        images.add(shotPath);
      }
    }

    if (text.isEmpty && images.isEmpty) return;

    final cancelToken = CancelToken();
    _activeCancelToken = cancelToken;

    final displayText = text.isNotEmpty ? text : (images.isNotEmpty ? '(image)' : '');
    final messageToSend = _focus.isFocused.value
        ? '[User has Focus Mode active on ${_focus.target?.label ?? 'screen'}. The attached image shows their current view.] $displayText'
        : displayText;
    widget.sharedHistory.addUser(displayText, imagePaths: images.isNotEmpty ? images : null);
    setState(() {
      _isSending = true;
      _streamingReply = '';
      _avatarState = AvatarState.thinking;
      _controller.clear();
      _pendingImages.clear();
      _thinkingIndex = 0;
    });
    _thinkingTimer?.cancel();
    _thinkingTimer = Timer.periodic(const Duration(milliseconds: 2500), (_) {
      if (!mounted || !_isSending) return;
      setState(() => _thinkingIndex = (_thinkingIndex + 1) % _thinkingMessages.length);
    });
    _scrollToBottom();

    try {
      final resp = await _chatSender.send(
        messageToSend,
        imagePaths: images.isNotEmpty ? images : null,
        cancelToken: cancelToken,
        onToken: (token) {
          if (mounted) {
            setState(() {
              _streamingReply += token;
              _avatarState = AvatarState.speaking;
            });
            _scrollToBottom();
          }
        },
      );
      if (!mounted) return;
      if (!resp.wasCancelled && resp.clientAction != null) {
        setState(() => _actionPerforming = true);
        await _chatSender.handleClientAction(context, resp.clientAction);
        if (mounted) setState(() => _actionPerforming = false);
      }
      if (!mounted) return;
      SoundEffects.playReplyArrived();
      widget.sharedHistory.addAssistant(resp.reply);
      setState(() {
        _streamingReply = '';
        _lastFailedMessage = null;
        _avatarState = AvatarState.idle;
        _showHappyBriefly = !resp.wasCancelled;
      });
      _scrollToBottom();
      if (!resp.wasCancelled) {
        Future.delayed(const Duration(milliseconds: 1500), () {
          if (mounted) setState(() => _showHappyBriefly = false);
        });
      }
    } catch (e) {
      if (!mounted) return;
      widget.sharedHistory.addAssistant('Could not reach CHILI. Is the server running?');
      setState(() {
        _streamingReply = '';
        _lastFailedMessage = displayText;
        _avatarState = AvatarState.idle;
      });
      _scrollToBottom();
    } finally {
      _activeCancelToken = null;
      _thinkingTimer?.cancel();
      _thinkingTimer = null;
      if (mounted) {
        setState(() => _isSending = false);
      }
    }
  }

  void _onFilesDropped(DropDoneDetails details) {
    if (details.files.isEmpty) return;
    setState(() => _isDraggingFile = false);
    int added = 0;
    for (final xFile in details.files) {
      if (_isImageFile(xFile.path)) {
        _addPendingImage(xFile.path);
        added++;
      }
    }
    if (added == 0) {
      final names = details.files.map((f) => f.name).join(', ');
      widget.sharedHistory.addAssistant(
        'Received files: $names\nOnly images (jpg, png, gif, webp) are supported right now.',
      );
      _openChatWindow();
      _scrollToBottom();
    }
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent + 60,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    });
  }

  double get _chatFontSize {
    final size = AppConfig.instance.fontSize;
    if (size == 'small') return 11;
    if (size == 'large') return 14;
    return 12;
  }

  Size? get _iconButtonMinSize =>
      AppConfig.instance.largerTargets ? const Size(36, 36) : null;

  List<Widget> _contextAwareSuggestionChips() {
    final h = DateTime.now().hour;
    final isWeekend = DateTime.now().weekday >= DateTime.saturday;
    final labels = <String>[];
    if (h >= 5 && h < 12) {
      labels.addAll(["What's the weather?", "List my chores", "What's on my schedule?"]);
      if (isWeekend) labels.add('Play music');
    } else if (h >= 12 && h < 17) {
      labels.addAll(['Search the web', 'Add a chore', 'Open Notepad', "What's the weather?"]);
    } else if (h >= 17 && h < 21) {
      labels.addAll(['Play relaxing music', 'List my chores', "What's the weather?", 'Search the web']);
    } else {
      labels.addAll(['Play lofi', 'What time is it?', 'Search the web', 'List my chores']);
    }
    return labels.map((l) => _suggestionChip(l)).toList();
  }

  Widget _suggestionChip(String label) {
    return ActionChip(
      label: Text(label, style: TextStyle(fontSize: _chatFontSize - 1)),
      onPressed: () {
        _controller.text = label;
        _sendMessage();
      },
      backgroundColor: Colors.grey.shade100,
      padding: EdgeInsets.symmetric(
        horizontal: AppConfig.instance.largerTargets ? 12 : 8,
        vertical: AppConfig.instance.largerTargets ? 6 : 4,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final status = widget.wakeWordStatus?.value;
    final followUpActive = widget.wakeWordFollowUpActive?.value ?? false;
    final effectiveStatus = followUpActive && (status == null || status.isEmpty)
        ? 'Listening... (follow-up)'
        : status;
    final showListeningState = followUpActive && _avatarState != AvatarState.speaking && _avatarState != AvatarState.thinking;
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: Stack(
        children: [
          DropTarget(
            onDragEntered: (_) => setState(() => _isDraggingFile = true),
            onDragExited: (_) => setState(() => _isDraggingFile = false),
            onDragDone: _onFilesDropped,
            child: Column(
              children: [
                // ── Avatar + overlaid status chip ──
                SizedBox(
                  height: 200,
                  width: 200,
                  child: Stack(
                    alignment: Alignment.center,
                    children: [
                      // Draggable / tappable background area
                      GestureDetector(
                        onPanStart: (_) => windowManager.startDragging(),
                        onTap: _toggleChat,
                        onDoubleTap: widget.onOpenFullApp,
                        behavior: HitTestBehavior.translucent,
                        child: const SizedBox.expand(),
                      ),
                      // Focus + mic mute toggles (outside GestureDetector)
                      Positioned(
                        top: 4,
                        right: 4,
                        child: Material(
                          color: Colors.transparent,
                          child: Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              ValueListenableBuilder<bool>(
                                valueListenable: _focus.isFocused,
                                builder: (_, focused, __) => IconButton(
                                  icon: Icon(
                                    focused ? Icons.center_focus_strong : Icons.center_focus_weak,
                                    size: 18,
                                    color: focused
                                        ? const Color(0xFF7E57C2)
                                        : Colors.white70,
                                  ),
                                  onPressed: _toggleFocus,
                                  tooltip: focused
                                      ? 'Stop Focus Mode'
                                      : 'Focus Mode',
                                  padding: const EdgeInsets.all(4),
                                  constraints: const BoxConstraints(
                                      minWidth: 28, minHeight: 28),
                                ),
                              ),
                              IconButton(
                                icon: Icon(
                                  _userMuted ? Icons.mic_off : Icons.mic_none,
                                  size: 18,
                                  color: Colors.white70,
                                ),
                                onPressed: () {
                                  setState(() {
                                    _userMuted = !_userMuted;
                                    widget.pauseWakeWord?.value =
                                        _recording || _userMuted;
                                  });
                                },
                                tooltip: _userMuted
                                    ? 'Resume listening'
                                    : 'Pause listening',
                                padding: const EdgeInsets.all(4),
                                constraints: const BoxConstraints(
                                    minWidth: 28, minHeight: 28),
                              ),
                            ],
                          ),
                        ),
                      ),
                      // Avatar & drag indicator (non-interactive — pass through to drag handler)
                      if (_isDraggingFile)
                        IgnorePointer(
                          child: Container(
                            height: 160,
                            width: 160,
                            decoration: BoxDecoration(
                              shape: BoxShape.circle,
                              color: const Color(0xFF42A5F5).withValues(alpha: 0.4),
                              border: Border.all(
                                color: const Color(0xFF42A5F5),
                                width: 3,
                              ),
                            ),
                            child: const Icon(
                              Icons.file_present,
                              size: 48,
                              color: Colors.white70,
                            ),
                          ),
                        ),
                      if (!_isDraggingFile)
                        Positioned(
                          top: 0,
                          child: IgnorePointer(
                            child: AnimatedSwitcher(
                              duration: const Duration(milliseconds: 220),
                              switchInCurve: Curves.easeOut,
                              switchOutCurve: Curves.easeIn,
                              child: ChiliAvatar(
                                key: ValueKey(_effectiveAvatarState(showListeningState).index),
                                state: _effectiveAvatarState(showListeningState),
                                reduceMotion: AppConfig.instance.reduceMotion,
                              ),
                            ),
                          ),
                        ),
                      // Status chip + partial transcription (non-interactive)
                      Positioned(
                        bottom: 2,
                        left: 6,
                        right: 6,
                        child: IgnorePointer(
                          child: Column(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              if ((widget.wakeWordPartial?.value ?? '').isNotEmpty)
                                Padding(
                                  padding: const EdgeInsets.only(bottom: 2),
                                  child: Text(
                                    widget.wakeWordPartial!.value!,
                                    textAlign: TextAlign.center,
                                    style: TextStyle(
                                      fontSize: 9,
                                      color: Colors.white.withValues(alpha: 0.85),
                                      fontStyle: FontStyle.italic,
                                    ),
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                ),
                              if (effectiveStatus != null && effectiveStatus.isNotEmpty)
                                Container(
                                  padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                                  decoration: BoxDecoration(
                                    color: _statusColor(effectiveStatus).withValues(alpha: 0.85),
                                    borderRadius: BorderRadius.circular(12),
                                  ),
                                  child: Text(
                                    effectiveStatus,
                                    textAlign: TextAlign.center,
                                    style: const TextStyle(
                                      fontSize: 10,
                                      color: Colors.white,
                                      fontWeight: FontWeight.w500,
                                    ),
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                ),
                              ValueListenableBuilder<bool>(
                                valueListenable: _focus.isFocused,
                                builder: (_, focused, __) {
                                  if (!focused) return const SizedBox.shrink();
                                  return Container(
                                    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                                    decoration: BoxDecoration(
                                      color: const Color(0xFF7E57C2).withValues(alpha: 0.85),
                                      borderRadius: BorderRadius.circular(12),
                                    ),
                                    child: Row(
                                      mainAxisSize: MainAxisSize.min,
                                      children: [
                                        const Icon(Icons.center_focus_strong, size: 12, color: Colors.white),
                                        const SizedBox(width: 4),
                                        Text(
                                          'Focus: ${_focus.target?.label ?? 'Screen'}',
                                          style: const TextStyle(
                                            fontSize: 10,
                                            color: Colors.white,
                                            fontWeight: FontWeight.w500,
                                          ),
                                        ),
                                      ],
                                    ),
                                  );
                                },
                              ),
                            ],
                          ),
                        ),
                        ),
                      ],
                    ),
                  ),

                // ── Focus status chip (below avatar, outside Stack) ──
                if (_showChat) ...[
                  const SizedBox(height: 6),
                  Expanded(child: _buildChatBubble()),
                  _buildResizeHandle(),
                ],
              ],
            ),
          ),
          if (_showOnboarding) _buildOnboardingOverlay(),
        ],
      ),
    );
  }

  Widget _buildOnboardingOverlay() {
    return OnboardingOverlay(
      step: _onboardingStep,
      onNext: _onOnboardingNext,
    );
  }

  static String _timeAwareGreeting() {
    final h = DateTime.now().hour;
    if (h < 5) return "Late night? I'm here if you need me.";
    if (h < 12) return 'Good morning! What can I help with?';
    if (h < 17) return 'Good afternoon! What can I do for you?';
    if (h < 21) return 'Good evening!';
    return "Late night? I'm here if you need me.";
  }

  Color _statusColor(String status) {
    if (status.startsWith('Heard:')) return const Color(0xFFF57C00);
    if (status.startsWith('Processing') || status.startsWith('Transcribing')) {
      return const Color(0xFF388E3C);
    }
    if (status.contains('follow-up')) return const Color(0xFF1976D2);
    if (status.startsWith('Error') || status.startsWith('No mic')) {
      return const Color(0xFFD32F2F);
    }
    return const Color(0xFF616161);
  }

  Widget _buildResizeHandle() {
    return GestureDetector(
      onPanStart: (_) => windowManager.startResizing(ResizeEdge.bottom),
      child: MouseRegion(
        cursor: SystemMouseCursors.resizeUpDown,
        child: Center(
          child: Container(
            height: 14,
            width: 40,
            margin: const EdgeInsets.only(bottom: 2),
            child: Center(
              child: Container(
                height: 4,
                width: 30,
                decoration: BoxDecoration(
                  color: Colors.grey.shade400,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildChatBubble() {
    return Container(
      width: 275,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.2),
            blurRadius: 12,
            offset: const Offset(0, 4),
          ),
        ],
      ),
      child: Material(
        color: Colors.transparent,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // Pending image previews
            if (_pendingImages.isNotEmpty) ...[
              SizedBox(
                height: 64,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: _pendingImages.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 4),
                  itemBuilder: (_, i) => Stack(
                    children: [
                      ClipRRect(
                        borderRadius: BorderRadius.circular(8),
                        child: Image.file(
                          File(_pendingImages[i]),
                          width: 60,
                          height: 60,
                          fit: BoxFit.cover,
                        ),
                      ),
                      Positioned(
                        top: -4,
                        right: -4,
                        child: GestureDetector(
                          onTap: () => _removePendingImage(i),
                          child: Container(
                            decoration: const BoxDecoration(
                              color: Colors.black54,
                              shape: BoxShape.circle,
                            ),
                            padding: const EdgeInsets.all(2),
                            child: const Icon(Icons.close, size: 12, color: Colors.white),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 6),
            ],

            // Input row
            Row(
              children: [
                VoiceInputButton(
                  onTranscription: (text) {
                    if (mounted && text != null && text.isNotEmpty) {
                      _controller.text = text;
                      if (_focus.isFocused.value) {
                        _sendMessage();
                      } else {
                        setState(() => _avatarState = AvatarState.idle);
                      }
                    }
                  },
                  onRecordingStateChanged: (recording) {
                    _recording = recording;
                    widget.pauseWakeWord?.value = recording || _userMuted;
                    if (mounted) {
                      setState(() {
                        _avatarState = recording
                            ? AvatarState.listening
                            : AvatarState.idle;
                      });
                    }
                  },
                  onTranscribing: (transcribing) {
                    if (mounted) {
                      setState(() {
                        _avatarState =
                            transcribing ? AvatarState.thinking : AvatarState.idle;
                      });
                    }
                  },
                ),
                // Image attach button
                SizedBox(
                  width: 28,
                  height: 28,
                  child: PopupMenuButton<String>(
                    padding: EdgeInsets.zero,
                    iconSize: 18,
                    icon: Icon(
                      _pendingImages.isNotEmpty ? Icons.collections : Icons.attach_file,
                      size: 18,
                      color: _pendingImages.isNotEmpty ? const Color(0xFFEF5350) : null,
                    ),
                    tooltip: 'Attach images',
                    onSelected: (value) async {
                      if (value == 'pick') {
                        await _pickImages();
                      } else if (value == 'paste') {
                        await _pasteImage();
                      } else if (value == 'screenshot') {
                        await _takeScreenshot();
                      }
                    },
                    itemBuilder: (_) => const [
                      PopupMenuItem(value: 'pick', child: Text('Browse files…')),
                      PopupMenuItem(value: 'paste', child: Text('Paste from clipboard')),
                      PopupMenuItem(value: 'screenshot', child: Text('Paste screenshot')),
                    ],
                  ),
                ),
                const SizedBox(width: 4),
                Expanded(
                  child: KeyboardListener(
                    focusNode: FocusNode(),
                    onKeyEvent: (event) async {
                      if (event is KeyDownEvent &&
                          event.logicalKey == LogicalKeyboardKey.keyV &&
                          HardwareKeyboard.instance.isControlPressed) {
                        await _pasteImage();
                      }
                    },
                    child: TextField(
                      controller: _controller,
                      focusNode: _chatFocusNode,
                      onSubmitted: (_) => _sendMessage(),
                      style: const TextStyle(fontSize: 13),
                      decoration: InputDecoration(
                        isDense: true,
                        contentPadding: const EdgeInsets.symmetric(
                          horizontal: 12,
                          vertical: 10,
                        ),
                        hintText: _pendingImages.isNotEmpty
                            ? 'Describe the image(s)…'
                            : 'Ask CHILI…',
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(20),
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 6),
                _isSending
                    ? IconButton(
                        onPressed: _stopGenerating,
                        icon: Icon(Icons.stop_circle,
                            size: _iconButtonMinSize != null ? 24 : 20),
                        tooltip: 'Stop generating',
                        style: IconButton.styleFrom(
                          backgroundColor: const Color(0xFFD32F2F),
                          foregroundColor: Colors.white,
                          minimumSize: _iconButtonMinSize,
                        ),
                      )
                    : IconButton(
                        onPressed: _sendMessage,
                        icon: Icon(Icons.send,
                            size: _iconButtonMinSize != null ? 24 : 20),
                        style: IconButton.styleFrom(
                          backgroundColor: const Color(0xFFEF5350),
                          foregroundColor: Colors.white,
                          minimumSize: _iconButtonMinSize,
                        ),
                      ),
              ],
            ),

            // Thinking message (rotating) while waiting for response
            if (_isSending) ...[
              const SizedBox(height: 6),
              Text(
                _thinkingMessages[_thinkingIndex],
                style: TextStyle(
                  fontSize: 12,
                  color: Colors.grey.shade600,
                  fontStyle: FontStyle.italic,
                ),
              ),
            ],

            // Time-aware greeting when chat is empty
            if (widget.sharedHistory.messages.isEmpty && !_isSending) ...[
              const SizedBox(height: 8),
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(
                  _timeAwareGreeting(),
                  style: TextStyle(
                    fontSize: 13,
                    color: Colors.grey.shade700,
                    fontStyle: FontStyle.italic,
                  ),
                ),
              ),
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: _contextAwareSuggestionChips(),
              ),
            ],

            // TTS progress and Stop button (only when audio is actually playing)
            if (widget.ttsPlaying?.value == true) ...[
              const SizedBox(height: 6),
              const LinearProgressIndicator(),
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: SizedBox(
                  height: 28,
                  child: TextButton.icon(
                    onPressed: _ttsController.stop,
                    icon: const Icon(Icons.stop_circle_outlined, size: 16),
                    label: const Text('Stop speaking',
                        style: TextStyle(fontSize: 11)),
                    style: TextButton.styleFrom(
                      foregroundColor: const Color(0xFFD32F2F),
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                    ),
                  ),
                ),
              ),
            ],

            // Scrollable message history (includes streaming reply when present)
            if (widget.sharedHistory.messages.isNotEmpty || _streamingReply.isNotEmpty) ...[
              const SizedBox(height: 8),
              Expanded(
                child: ListView.builder(
                  controller: _scrollController,
                  itemCount: widget.sharedHistory.messages.length + (_streamingReply.isNotEmpty ? 1 : 0),
                  padding: EdgeInsets.zero,
                  itemBuilder: (context, index) {
                    final history = widget.sharedHistory.messages;
                    if (index == history.length) {
                      return Align(
                        alignment: Alignment.centerLeft,
                        child: Container(
                          margin: const EdgeInsets.symmetric(vertical: 3),
                          padding: const EdgeInsets.symmetric(
                              horizontal: 10, vertical: 7),
                        constraints: const BoxConstraints(maxWidth: 220),
                          decoration: BoxDecoration(
                            color: const Color(0xFFF5F5F5),
                            borderRadius: BorderRadius.circular(12),
                          ),
                          child: Text(
                            '$_streamingReply\u200B',
                            style: TextStyle(
                              fontSize: _chatFontSize,
                              height: 1.4,
                              color: Colors.black87,
                            ),
                          ),
                        ),
                      );
                    }
                    final msg = history[index];
                    final isUser = msg.role == 'user';
                    return Align(
                      alignment:
                          isUser ? Alignment.centerRight : Alignment.centerLeft,
                      child: ChatMessageBubble(
                        content: msg.content,
                        isUser: isUser,
                        isSystem: false,
                        fontSize: _chatFontSize,
                        margin: const EdgeInsets.symmetric(vertical: 3),
                        padding: const EdgeInsets.symmetric(
                          horizontal: 10,
                          vertical: 7,
                        ),
                        borderRadius: BorderRadius.circular(12),
                        maxWidth: 220,
                        userColor: const Color(0xFFEF5350),
                        assistantColor: const Color(0xFFF5F5F5),
                        systemColor: Colors.amber.shade100,
                        imagePaths: msg.imagePaths,
                      ),
                    );
                  },
                ),
              ),
            ],

            // Retry button when last message is the connection error
            if (_lastFailedMessage != null &&
                widget.sharedHistory.messages.length >= 2 &&
                widget.sharedHistory.messages.last.role == 'assistant' &&
                widget.sharedHistory.messages.last.content == 'Could not reach CHILI. Is the server running?') ...[
              const SizedBox(height: 6),
              SizedBox(
                height: 28,
                child: TextButton.icon(
                  onPressed: _isSending
                      ? null
                      : () {
                          final msg = _lastFailedMessage!;
                          widget.sharedHistory.removeLastTwo();
                          setState(() => _lastFailedMessage = null);
                          _controller.text = msg;
                          _sendMessage();
                        },
                  icon: const Icon(Icons.refresh, size: 14),
                  label: const Text('Retry', style: TextStyle(fontSize: 11)),
                  style: TextButton.styleFrom(
                    foregroundColor: const Color(0xFF388E3C),
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                  ),
                ),
              ),
            ],

            // Open full app link
            const SizedBox(height: 6),
            Center(
              child: TextButton.icon(
                onPressed: widget.onOpenFullApp,
                icon: const Icon(Icons.open_in_new, size: 14),
                label: const Text(
                  'Open Full App',
                  style: TextStyle(fontSize: 11),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
