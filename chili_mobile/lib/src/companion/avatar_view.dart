import 'dart:async';
import 'dart:io';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:window_manager/window_manager.dart';
import 'package:desktop_drop/desktop_drop.dart';

import '../config/app_config.dart';
import '../desktop/desktop_actions.dart';
import '../network/chili_api_client.dart';
import '../voice/voice_input.dart';
import '../widgets/chili_avatar.dart';
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
    this.pauseWakeWord,
    this.ttsPlaying,
    this.ttsInterruptRequested,
    this.wakeWordCommand,
    this.wakeWordReply,
    this.wakeWordStatus,
    this.wakeWordPartial,
    this.wakeWordFollowUpActive,
  });

  final SharedChatHistory sharedHistory;
  final VoidCallback onOpenFullApp;
  final ValueNotifier<bool>? pauseWakeWord;
  final ValueNotifier<bool>? ttsPlaying;
  final ValueNotifier<bool>? ttsInterruptRequested;
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
  final _audioPlayer = AudioPlayer();
  StreamSubscription<void>? _ttsCompleteSub;
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
  bool _showHappyBriefly = false;
  bool _showWakeDetectedBriefly = false;
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
    _ttsCompleteSub?.cancel();
    _controller.dispose();
    _scrollController.dispose();
    _audioPlayer.dispose();
    super.dispose();
  }

  void _onChatFocusChange() {
    if (mounted) setState(() => _chatInputHasFocus = _chatFocusNode.hasFocus);
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
      if (mounted) setState(() {
        _showOnboarding = false;
        _onboardingStep = 0;
      });
    } else {
      if (mounted) setState(() => _onboardingStep++);
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
    return _avatarState;
  }

  void _onWakeWordReply() {
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
      setState(() {
        _avatarState = AvatarState.speaking;
        _showChat = true;
      });
    }
    windowManager.setSize(const Size(300, 520));
    _scrollToBottom();
    SoundEffects.playReplyArrived();
    _speakReply(reply);
  }

  void _onTtsInterrupt() {
    if (widget.ttsInterruptRequested?.value != true) return;
    widget.ttsInterruptRequested?.value = false;
    _stopTts();
  }

  Future<void> _stopTts() async {
    _ttsCompleteSub?.cancel();
    _ttsCompleteSub = null;
    try {
      await _audioPlayer.stop();
    } catch (_) {}
    _finishSpeaking();
  }

  Future<void> _speakReply(String text) async {
    if (text.trim().isEmpty) {
      _finishSpeaking();
      return;
    }
    widget.ttsPlaying?.value = true;
    try {
      final audioBytes = await _client.fetchTts(text);
      if (audioBytes == null || audioBytes.isEmpty || !mounted) {
        debugPrint('[AvatarView] TTS: no audio (fetch failed or empty)');
        _finishSpeaking();
        return;
      }
      final dir = await getTemporaryDirectory();
      final file = File(p.join(dir.path, 'chili_tts_${DateTime.now().millisecondsSinceEpoch}.mp3'));
      await file.writeAsBytes(audioBytes);
      if (!mounted) {
        _finishSpeaking();
        return;
      }
      await _audioPlayer.stop();
      void onDone() {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          _finishSpeaking();
        });
        try { file.deleteSync(); } catch (_) {}
      }
      _ttsCompleteSub?.cancel();
      _ttsCompleteSub = _audioPlayer.onPlayerComplete.listen((_) => onDone());
      await _audioPlayer.play(DeviceFileSource(file.path));
    } catch (e) {
      debugPrint('[AvatarView] TTS error: $e');
      _finishSpeaking();
    }
  }

  void _finishSpeaking() {
    widget.ttsPlaying?.value = false;
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
    final willShow = !_showChat;
    if (willShow) {
      await windowManager.setSize(const Size(300, 520));
    } else {
      await windowManager.setSize(const Size(200, 200));
    }
    if (mounted) setState(() => _showChat = willShow);
  }

  Future<void> _sendMessage() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _isSending) return;
    SoundEffects.playButtonClick();

    widget.sharedHistory.addUser(text);
    setState(() {
      _isSending = true;
      _streamingReply = '';
      _avatarState = AvatarState.thinking;
      _controller.clear();
      _thinkingIndex = 0;
    });
    _thinkingTimer?.cancel();
    _thinkingTimer = Timer.periodic(const Duration(milliseconds: 2500), (_) {
      if (!mounted || !_isSending) return;
      setState(() => _thinkingIndex = (_thinkingIndex + 1) % _thinkingMessages.length);
    });
    _scrollToBottom();

    try {
      final resp = await _client.sendMessageStream(
        text,
        onToken: (token) {
          if (mounted) {
            setState(() => _streamingReply += token);
            _scrollToBottom();
          }
        },
      );
      if (!mounted) return;
      if (resp.clientAction != null) {
        setState(() => _actionPerforming = true);
      }
      final actionResult = await DesktopActions.execute(resp.clientAction);
      if (mounted) setState(() => _actionPerforming = false);
      if (mounted && actionResult != null && actionResult.isNotEmpty) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(actionResult), duration: const Duration(seconds: 2)),
        );
      }
      if (!mounted) return;
      SoundEffects.playReplyArrived();
      widget.sharedHistory.addAssistant(resp.reply);
      setState(() {
        _streamingReply = '';
        _lastFailedMessage = null;
        _avatarState = AvatarState.idle;
        _showHappyBriefly = true;
      });
      _scrollToBottom();
      Future.delayed(const Duration(milliseconds: 1500), () {
        if (mounted) setState(() => _showHappyBriefly = false);
      });
    } catch (e) {
      if (!mounted) return;
      widget.sharedHistory.addAssistant('Could not reach CHILI. Is the server running?');
      setState(() {
        _streamingReply = '';
        _lastFailedMessage = text;
        _avatarState = AvatarState.idle;
      });
      _scrollToBottom();
    } finally {
      _thinkingTimer?.cancel();
      _thinkingTimer = null;
      if (mounted) {
        setState(() => _isSending = false);
      }
    }
  }

  void _onFilesDropped(DropDoneDetails details) {
    if (details.files.isEmpty) return;
    final names = details.files.map((f) => f.name).join(', ');
    widget.sharedHistory.addAssistant('Received files: $names\n(File analysis coming soon!)');
    setState(() {
      _isDraggingFile = false;
      if (!_showChat) _showChat = true;
    });
    windowManager.setSize(const Size(300, 520));
    _scrollToBottom();
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
            child: SingleChildScrollView(
          child: Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                // ── Avatar + overlaid status chip ──
                GestureDetector(
                  onPanStart: (_) => windowManager.startDragging(),
                  onTap: _toggleChat,
                  onDoubleTap: widget.onOpenFullApp,
                  child: SizedBox(
                    height: 200,
                    width: 200,
                    child: Stack(
                      alignment: Alignment.center,
                      children: [
                        // Mic mute toggle (pause/resume wake word)
                        Positioned(
                          top: 4,
                          right: 4,
                          child: Material(
                            color: Colors.transparent,
                            child: IconButton(
                              icon: Icon(
                                _userMuted ? Icons.mic_off : Icons.mic_none,
                                size: 18,
                                color: Colors.white70,
                              ),
                              onPressed: () {
                                setState(() {
                                  _userMuted = !_userMuted;
                                  widget.pauseWakeWord?.value = _recording || _userMuted;
                                });
                              },
                              tooltip: _userMuted ? 'Resume listening' : 'Pause listening',
                              padding: const EdgeInsets.all(4),
                              constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
                            ),
                          ),
                        ),
                        if (_isDraggingFile)
                          Container(
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
                        if (!_isDraggingFile)
                          Positioned(
                            top: 0,
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
                        // Status chip + partial transcription overlaid at bottom
                        Positioned(
                          bottom: 2,
                          left: 6,
                          right: 6,
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
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                ),

                // ── Chat bubble ──
                if (_showChat) ...[
                  const SizedBox(height: 6),
                  _buildChatBubble(),
                ],
              ],
            ),
          ),
        ),
      ),
          if (_showOnboarding) _buildOnboardingOverlay(),
        ],
      ),
    );
  }

  Widget _buildOnboardingOverlay() {
    const steps = [
      "Say 'Chili' to start a conversation",
      "Drag me to move",
      "Tap to chat, double-tap for full app",
    ];
    final text = steps[_onboardingStep.clamp(0, steps.length - 1)];
    return Positioned.fill(
      child: Material(
        color: Colors.black54,
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          text,
                          textAlign: TextAlign.center,
                          style: const TextStyle(fontSize: 13, height: 1.4),
                        ),
                        const SizedBox(height: 12),
                        FilledButton(
                          onPressed: _onOnboardingNext,
                          child: const Text('Got it'),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
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
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // Input row
            Row(
              children: [
                VoiceInputButton(
                  onTranscription: (text) {
                    if (mounted && text != null && text.isNotEmpty) {
                      setState(() {
                        _controller.text = text;
                        _avatarState = AvatarState.idle;
                      });
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
                const SizedBox(width: 6),
                Expanded(
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
                      hintText: 'Ask CHILI…',
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(20),
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 6),
                IconButton(
                  onPressed: _isSending ? null : _sendMessage,
                  icon: _isSending
                      ? SizedBox(
                          width: _iconButtonMinSize != null ? 24 : 18,
                          height: _iconButtonMinSize != null ? 24 : 18,
                          child: const CircularProgressIndicator(strokeWidth: 2),
                        )
                      : Icon(Icons.send, size: _iconButtonMinSize != null ? 24 : 20),
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

            // TTS progress and Stop button
            if (_avatarState == AvatarState.speaking) ...[
              const SizedBox(height: 6),
              const LinearProgressIndicator(),
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: SizedBox(
                  height: 28,
                  child: TextButton.icon(
                    onPressed: _stopTts,
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
              ConstrainedBox(
                constraints: const BoxConstraints(maxHeight: 200),
                child: ListView.builder(
                  controller: _scrollController,
                  shrinkWrap: true,
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
                      child: Container(
                        margin: const EdgeInsets.symmetric(vertical: 3),
                        padding: const EdgeInsets.symmetric(
                            horizontal: 10, vertical: 7),
                        constraints: const BoxConstraints(maxWidth: 220),
                        decoration: BoxDecoration(
                          color: isUser
                              ? const Color(0xFFEF5350)
                              : const Color(0xFFF5F5F5),
                          borderRadius: BorderRadius.circular(12),
                        ),
                        child: isUser
                            ? Text(
                                msg.content,
                                style: TextStyle(
                                  fontSize: _chatFontSize,
                                  height: 1.4,
                                  color: Colors.white,
                                ),
                              )
                            : MarkdownBody(
                                data: msg.content,
                                shrinkWrap: true,
                                onTapLink: (text, href, title) {
                                  if (href != null) launchUrl(Uri.parse(href));
                                },
                                styleSheet: MarkdownStyleSheet(
                                  p: TextStyle(fontSize: _chatFontSize, height: 1.4, color: Colors.black87),
                                  strong: TextStyle(fontSize: _chatFontSize, fontWeight: FontWeight.bold, color: Colors.black87),
                                  code: TextStyle(fontSize: _chatFontSize - 1, backgroundColor: Colors.grey.shade200, color: Colors.black87),
                                  listBullet: TextStyle(fontSize: _chatFontSize, color: Colors.black87),
                                ),
                              ),
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
