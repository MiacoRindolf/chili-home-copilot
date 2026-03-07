import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:pasteboard/pasteboard.dart';

import '../companion/shared_chat_history.dart';
import '../companion/sound_effects.dart';
import '../companion/chat_send_controller.dart';
import '../config/app_config.dart';
import '../network/chili_api_client.dart';
import '../voice/voice_input.dart';
import '../widgets/chili_avatar.dart';
import '../widgets/chat_message_bubble.dart';

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
  late final ChatSendController _chatSender;

  String _streamingReply = '';
  bool _isSending = false;
  bool _chatInputHasFocus = false;
  AvatarState _avatarState = AvatarState.idle;

  final List<String> _pendingImages = [];
  static const _allowedImageExts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'};

  @override
  void initState() {
    super.initState();
    widget.sharedHistory.addListener(_onHistoryChanged);
    _chatFocusNode.addListener(_onChatFocusChange);
    _chatSender = ChatSendController(_client);
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

  // ── Image helpers ──

  bool _isImageFile(String path) {
    final ext = path.toLowerCase();
    return _allowedImageExts.any((e) => ext.endsWith(e));
  }

  void _addPendingImage(String path) {
    if (_pendingImages.length >= 10) return;
    if (!_isImageFile(path)) return;
    setState(() => _pendingImages.add(path));
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

  // ── Send ──

  Future<void> _sendMessage() async {
    final text = _controller.text.trim();
    final images = List<String>.from(_pendingImages);
    if (text.isEmpty && images.isEmpty) return;
    if (_isSending) return;
    SoundEffects.playButtonClick();

    final displayText = text.isNotEmpty ? text : (images.isNotEmpty ? '(image)' : '');
    widget.sharedHistory.addUser(displayText, imagePaths: images.isNotEmpty ? images : null);
    setState(() {
      _isSending = true;
      _streamingReply = '';
      _avatarState = AvatarState.thinking;
      _controller.clear();
      _pendingImages.clear();
    });

    try {
      final resp = await _chatSender.send(
        displayText,
        imagePaths: images.isNotEmpty ? images : null,
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
      if (!mounted) return;
      await _chatSender.handleClientAction(context, resp.clientAction);
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
                    child: ChatMessageBubble(
                      content: m.content,
                      isUser: isUser,
                      isSystem: isSystem,
                      fontSize: _chatFontSize,
                      margin: const EdgeInsets.symmetric(vertical: 4),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 8,
                      ),
                      borderRadius: BorderRadius.circular(16),
                      maxWidth: null,
                      userColor: Theme.of(context).colorScheme.primary,
                      assistantColor: Colors.white,
                      systemColor: Colors.amber.shade100,
                      imagePaths: m.imagePaths,
                    ),
                  );
                },
              ),
            ),
            const Divider(height: 1),

            // Pending image preview strip
            if (_pendingImages.isNotEmpty)
              Container(
                height: 72,
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: _pendingImages.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 6),
                  itemBuilder: (_, i) => Stack(
                    children: [
                      ClipRRect(
                        borderRadius: BorderRadius.circular(8),
                        child: Image.file(
                          File(_pendingImages[i]),
                          width: 64,
                          height: 64,
                          fit: BoxFit.cover,
                        ),
                      ),
                      Positioned(
                        top: -2,
                        right: -2,
                        child: GestureDetector(
                          onTap: () => _removePendingImage(i),
                          child: Container(
                            decoration: const BoxDecoration(
                              color: Colors.black54,
                              shape: BoxShape.circle,
                            ),
                            padding: const EdgeInsets.all(2),
                            child: const Icon(Icons.close, size: 14, color: Colors.white),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),

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
                  // Attach images button
                  IconButton(
                    icon: Icon(
                      _pendingImages.isNotEmpty ? Icons.collections : Icons.attach_file,
                      color: _pendingImages.isNotEmpty ? Theme.of(context).colorScheme.primary : null,
                    ),
                    onPressed: _pickImages,
                    tooltip: 'Attach images',
                    style: IconButton.styleFrom(minimumSize: _iconButtonMinSize),
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
                        decoration: InputDecoration(
                          hintText: _pendingImages.isNotEmpty
                              ? 'Describe the image(s)…'
                              : 'Talk to CHILI…',
                        ),
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
