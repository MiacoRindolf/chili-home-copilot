import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/material.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:window_manager/window_manager.dart';
import 'package:desktop_drop/desktop_drop.dart';

import '../network/chili_api_client.dart';
import '../voice/voice_input.dart';
import '../widgets/chili_avatar.dart';

enum _ChatRole { user, assistant }

class _QuickChatMessage {
  _QuickChatMessage({required this.role, required this.content});
  final _ChatRole role;
  final String content;
}

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
    required this.onOpenFullApp,
    this.pauseWakeWord,
    this.wakeWordCommand,
    this.wakeWordReply,
    this.wakeWordStatus,
    this.wakeWordPartial,
    this.wakeWordFollowUpActive,
  });

  final VoidCallback onOpenFullApp;
  final ValueNotifier<bool>? pauseWakeWord;
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
  final _audioPlayer = AudioPlayer();
  StreamSubscription<void>? _ttsCompleteSub;
  final _messages = <_QuickChatMessage>[];
  bool _isSending = false;
  bool _showChat = false;
  bool _isDraggingFile = false;
  AvatarState _avatarState = AvatarState.idle;

  @override
  void initState() {
    super.initState();
    widget.wakeWordReply?.addListener(_onWakeWordReply);
    widget.wakeWordStatus?.addListener(_onStatusChanged);
    widget.wakeWordPartial?.addListener(_onStatusChanged);
    widget.wakeWordFollowUpActive?.addListener(_onStatusChanged);
  }

  @override
  void dispose() {
    widget.wakeWordReply?.removeListener(_onWakeWordReply);
    widget.wakeWordStatus?.removeListener(_onStatusChanged);
    widget.wakeWordPartial?.removeListener(_onStatusChanged);
    widget.wakeWordFollowUpActive?.removeListener(_onStatusChanged);
    _ttsCompleteSub?.cancel();
    _controller.dispose();
    _scrollController.dispose();
    _audioPlayer.dispose();
    super.dispose();
  }

  void _onStatusChanged() {
    if (mounted) setState(() {});
  }

  void _onWakeWordReply() {
    final reply = widget.wakeWordReply?.value;
    if (reply == null || !mounted) return;
    final command = widget.wakeWordCommand?.value;
    widget.wakeWordCommand?.value = null;
    widget.wakeWordReply!.value = null;
    _controller.clear();
    setState(() {
      if (command != null && command.isNotEmpty) {
        _messages.add(_QuickChatMessage(role: _ChatRole.user, content: command));
      }
      _messages.add(_QuickChatMessage(role: _ChatRole.assistant, content: reply));
      _avatarState = AvatarState.speaking;
      _showChat = true;
    });
    windowManager.setSize(const Size(300, 520));
    _scrollToBottom();
    _speakReply(reply);
  }

  Future<void> _speakReply(String text) async {
    if (text.trim().isEmpty) {
      _finishSpeaking();
      return;
    }
    widget.pauseWakeWord?.value = true;
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
        _finishSpeaking();
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
    widget.pauseWakeWord?.value = false;
    if (mounted) setState(() => _avatarState = AvatarState.idle);
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

    setState(() {
      _isSending = true;
      _messages.add(_QuickChatMessage(role: _ChatRole.user, content: text));
      _avatarState = AvatarState.thinking;
      _controller.clear();
    });
    _scrollToBottom();

    try {
      final reply = await _client.sendMessage(text);
      if (!mounted) return;
      setState(() {
        _messages.add(_QuickChatMessage(role: _ChatRole.assistant, content: reply));
        _avatarState = AvatarState.speaking;
      });
      _scrollToBottom();
      await Future.delayed(const Duration(seconds: 2));
      if (mounted) {
        setState(() => _avatarState = AvatarState.idle);
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _messages.add(_QuickChatMessage(
          role: _ChatRole.assistant,
          content: 'Could not reach CHILI. Is the server running?',
        ));
        _avatarState = AvatarState.idle;
      });
      _scrollToBottom();
    } finally {
      if (mounted) {
        setState(() => _isSending = false);
      }
    }
  }

  void _onFilesDropped(DropDoneDetails details) {
    if (details.files.isEmpty) return;
    final names = details.files.map((f) => f.name).join(', ');
    setState(() {
      _isDraggingFile = false;
      _messages.add(_QuickChatMessage(
        role: _ChatRole.assistant,
        content: 'Received files: $names\n(File analysis coming soon!)',
      ));
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
      body: DropTarget(
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
                            child: ChiliAvatar(
                              state: showListeningState
                                  ? AvatarState.listening
                                  : _avatarState,
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
    );
  }

  Color _statusColor(String status) {
    if (status.startsWith('Heard:')) return const Color(0xFFF57C00);
    if (status.startsWith('Processing')) return const Color(0xFF388E3C);
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
                    widget.pauseWakeWord?.value = recording;
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
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.send, size: 20),
                  style: IconButton.styleFrom(
                    backgroundColor: const Color(0xFFEF5350),
                    foregroundColor: Colors.white,
                  ),
                ),
              ],
            ),

            // Scrollable message history
            if (_messages.isNotEmpty) ...[
              const SizedBox(height: 8),
              ConstrainedBox(
                constraints: const BoxConstraints(maxHeight: 200),
                child: ListView.builder(
                  controller: _scrollController,
                  shrinkWrap: true,
                  itemCount: _messages.length,
                  padding: EdgeInsets.zero,
                  itemBuilder: (context, index) {
                    final msg = _messages[index];
                    final isUser = msg.role == _ChatRole.user;
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
                        child: Text(
                          msg.content,
                          style: TextStyle(
                            fontSize: 12,
                            height: 1.4,
                            color: isUser ? Colors.white : Colors.black87,
                          ),
                        ),
                      ),
                    );
                  },
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
