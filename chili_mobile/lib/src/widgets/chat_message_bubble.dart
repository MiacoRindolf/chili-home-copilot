import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:url_launcher/url_launcher.dart';

/// Shared chat message bubble used by both the floating avatar quick chat
/// and the full-screen ChatScreen.
class ChatMessageBubble extends StatelessWidget {
  const ChatMessageBubble({
    super.key,
    required this.content,
    required this.isUser,
    required this.isSystem,
    required this.fontSize,
    required this.margin,
    required this.padding,
    required this.borderRadius,
    this.maxWidth,
    required this.userColor,
    required this.assistantColor,
    required this.systemColor,
  });

  final String content;
  final bool isUser;
  final bool isSystem;
  final double fontSize;
  final EdgeInsetsGeometry margin;
  final EdgeInsetsGeometry padding;
  final BorderRadius borderRadius;
  final double? maxWidth;
  final Color userColor;
  final Color assistantColor;
  final Color systemColor;

  @override
  Widget build(BuildContext context) {
    final bgColor = isUser
        ? userColor
        : (isSystem ? systemColor : assistantColor);

    Widget child;
    if (isUser || isSystem) {
      child = Text(
        content,
        style: TextStyle(
          fontSize: fontSize,
          height: isUser ? 1.4 : null,
          color: isUser ? Colors.white : Colors.black87,
        ),
      );
    } else {
      child = MarkdownBody(
        data: content,
        shrinkWrap: true,
        onTapLink: (text, href, title) {
          if (href != null) {
            launchUrl(Uri.parse(href));
          }
        },
        styleSheet: MarkdownStyleSheet(
          p: TextStyle(
            fontSize: fontSize,
            height: 1.4,
            color: Colors.black87,
          ),
          strong: TextStyle(
            fontSize: fontSize,
            fontWeight: FontWeight.bold,
            color: Colors.black87,
          ),
          code: TextStyle(
            fontSize: fontSize - 1,
            backgroundColor: Colors.grey.shade200,
            color: Colors.black87,
          ),
          listBullet: TextStyle(
            fontSize: fontSize,
            color: Colors.black87,
          ),
        ),
      );
    }

    return Container(
      margin: margin,
      padding: padding,
      constraints: maxWidth != null
          ? BoxConstraints(maxWidth: maxWidth!)
          : const BoxConstraints(),
      decoration: BoxDecoration(
        color: bgColor,
        borderRadius: borderRadius,
      ),
      child: child,
    );
  }
}

