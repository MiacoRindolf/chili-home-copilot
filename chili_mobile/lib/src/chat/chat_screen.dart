import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:url_launcher/url_launcher.dart';

import '../companion/shared_chat_history.dart';
import '../companion/sound_effects.dart';
import '../config/app_config.dart';
import '../desktop/desktop_actions.dart';
import '../network/chili_api_client.dart';
import '../voice/voice_input.dart';
import '../widgets/chili_avatar.dart';

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, required this.sharedHistory});

  final SharedChatHistory sharedHistory;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _controller = TextEditingController();
  final _scrollController = ScrollController();
  final _chatFocusNode = FocusNode();
  final _client = ChiliApiClient();

  String _streamingReply = '';
  bool _isSending = false;
  bool _chatInputHasFocus = false;
  AvatarState _avatarState = AvatarState.idle;

  @override
  void initState() {
    super.initState();
    widget.sharedHistory.addListener(_onHistoryChanged);
    _chatFocusNode.addListener(_onChatFocusChange);
  }

  @override
  void dispose() {
    widget.sharedHistory.removeListener(_onHistoryChanged);
    _chatFocusNode.removeListener(_onChatFocusChange);
    _chatFocusNode.dispose();
    _controller.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  void _onChatFocusChange() {
    if (mounted) setState(() => _chatInputHasFocus = _chatFocusNode.hasFocus);
  }

  AvatarState get _effectiveAvatarState {
    if (_chatInputHasFocus && _avatarState == AvatarState.idle) return AvatarState.reading;
    return _avatarState;
  }

  void _onHistoryChanged() {
    if (mounted) setState(() {});
  }

  double get _chatFontSize {
    final size = AppConfig.instance.fontSize;
    if (size == 'small') return 12;
    if (size == 'large') return 16;
    return 14;
  }

  Size? get _iconButtonMinSize =>
      AppConfig.instance.largerTargets ? const Size(36, 36) : null;

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
    });

    try {
      final resp = await _client.sendMessageStream(
        text,
        onToken: (token) {
          if (mounted) {
            setState(() => _streamingReply += token);
            _scrollController.animateTo(
              _scrollController.position.maxScrollExtent + 80,
              duration: const Duration(milliseconds: 100),
              curve: Curves.easeOut,
            );
          }
        },
      );
      if (mounted && resp.clientAction != null) {
        setState(() => _avatarState = AvatarState.actionPerforming);
      }
      final actionResult = await DesktopActions.execute(resp.clientAction);
      if (mounted && actionResult != null && actionResult.isNotEmpty) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(actionResult), duration: const Duration(seconds: 2)),
        );
      }
      widget.sharedHistory.addAssistant(resp.reply);
      if (mounted) {
        setState(() {
          _streamingReply = '';
          _avatarState = AvatarState.happy;
        });
        Future.delayed(const Duration(milliseconds: 1500), () {
          if (mounted) setState(() => _avatarState = AvatarState.idle);
        });
      }
    } catch (e) {
      widget.sharedHistory.addSystem('Sorry, I could not reach CHILI. Please try again.');
      if (mounted) {
        setState(() {
          _streamingReply = '';
          _avatarState = AvatarState.error;
        });
        Future.delayed(const Duration(seconds: 2), () {
          if (mounted) setState(() => _avatarState = AvatarState.idle);
        });
      }
    } finally {
      if (mounted) setState(() => _isSending = false);
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent + 80,
          duration: const Duration(milliseconds: 300),
          curve: Curves.easeOut,
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('CHILI'),
      ),
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.only(top: 8.0),
              child: ChiliAvatar(
                state: _effectiveAvatarState,
                reduceMotion: AppConfig.instance.reduceMotion,
              ),
            ),
            const Divider(height: 1),
            Expanded(
              child: ListView.builder(
                controller: _scrollController,
                padding: const EdgeInsets.all(16),
                itemCount: widget.sharedHistory.messages.length + (_streamingReply.isNotEmpty ? 1 : 0),
                itemBuilder: (context, index) {
                  final history = widget.sharedHistory.messages;
                  if (index == history.length) {
                    return Align(
                      alignment: Alignment.centerLeft,
                      child: Container(
                        margin: const EdgeInsets.symmetric(vertical: 4),
                        padding: const EdgeInsets.symmetric(
                          horizontal: 12,
                          vertical: 8,
                        ),
                        decoration: BoxDecoration(
                          color: Colors.white,
                          borderRadius: BorderRadius.circular(16),
                        ),
                        child: Text(
                          '$_streamingReply\u200B',
                          style: TextStyle(
                            fontSize: _chatFontSize,
                            color: Colors.black87,
                          ),
                        ),
                      ),
                    );
                  }
                  final m = history[index];
                  final isUser = m.role == 'user';
                  final isSystem = m.role == 'system';
                  return Align(
                    alignment:
                        isUser ? Alignment.centerRight : Alignment.centerLeft,
                    child: Container(
                      margin: const EdgeInsets.symmetric(vertical: 4),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 8,
                      ),
                      decoration: BoxDecoration(
                        color: isUser
                            ? Theme.of(context).colorScheme.primary
                            : isSystem
                                ? Colors.amber.shade100
                                : Colors.white,
                        borderRadius: BorderRadius.circular(16),
                      ),
                          child: isUser
                              ? Text(
                                  m.content,
                                  style: TextStyle(
                                    fontSize: _chatFontSize,
                                    color: Colors.white,
                                  ),
                                )
                              : isSystem
                                  ? Text(
                                      m.content,
                                      style: TextStyle(
                                        fontSize: _chatFontSize,
                                        color: Colors.black87,
                                      ),
                                    )
                                  : MarkdownBody(
                                      data: m.content,
                                      shrinkWrap: true,
                                      onTapLink: (text, href, title) {
                                        if (href != null) launchUrl(Uri.parse(href));
                                      },
                                      styleSheet: MarkdownStyleSheet(
                                        p: TextStyle(fontSize: _chatFontSize, color: Colors.black87),
                                        strong: const TextStyle(fontWeight: FontWeight.bold, color: Colors.black87),
                                        code: TextStyle(
                                          fontSize: _chatFontSize - 1,
                                          backgroundColor: Colors.grey.shade200,
                                          color: Colors.black87,
                                        ),
                                        listBullet: TextStyle(fontSize: _chatFontSize, color: Colors.black87),
                                      ),
                                    ),
                    ),
                  );
                },
              ),
            ),
            const Divider(height: 1),
            Padding(
              padding: const EdgeInsets.all(8.0),
              child: Row(
                children: [
                  VoiceInputButton(
                    onTranscription: (text) {
                      if (mounted && text != null && text.isNotEmpty) {
                        setState(() => _controller.text = text);
                      }
                    },
                    onRecordingStateChanged: (recording) {
                      setState(() {
                        _avatarState = recording
                            ? AvatarState.listening
                            : AvatarState.idle;
                      });
                    },
                    onTranscribing: (transcribing) {
                      if (mounted) {
                        setState(() {
                          _avatarState = transcribing
                              ? AvatarState.thinking
                              : AvatarState.idle;
                        });
                      }
                    },
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: TextField(
                      controller: _controller,
                      focusNode: _chatFocusNode,
                      onSubmitted: (_) => _sendMessage(),
                      decoration: const InputDecoration(
                        hintText: 'Talk to CHILI…',
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  IconButton(
                    icon: _isSending
                        ? SizedBox(
                            width: _iconButtonMinSize != null ? 24 : 20,
                            height: _iconButtonMinSize != null ? 24 : 20,
                            child: const CircularProgressIndicator(strokeWidth: 2),
                          )
                        : Icon(Icons.send, size: _iconButtonMinSize != null ? 24 : null),
                    onPressed: _isSending ? null : _sendMessage,
                    style: IconButton.styleFrom(minimumSize: _iconButtonMinSize),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

enum ChatRole { user, assistant, system }

class ChatMessage {
  ChatMessage({required this.role, required this.content});

  final ChatRole role;
  final String content;
}
