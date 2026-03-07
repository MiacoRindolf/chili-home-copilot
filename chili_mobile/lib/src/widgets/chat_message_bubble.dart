import 'dart:io';

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
    this.imagePaths,
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

  /// Local file paths of attached images (typically on user messages).
  final List<String>? imagePaths;

  @override
  Widget build(BuildContext context) {
    final bgColor = isUser
        ? userColor
        : (isSystem ? systemColor : assistantColor);

    final images = imagePaths;
    final hasImages = images != null && images.isNotEmpty;

    Widget textChild;
    if (isUser || isSystem) {
      textChild = Text(
        content,
        style: TextStyle(
          fontSize: fontSize,
          height: isUser ? 1.4 : null,
          color: isUser ? Colors.white : Colors.black87,
        ),
      );
    } else {
      textChild = MarkdownBody(
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

    Widget child;
    if (hasImages) {
      child = Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          _ImageStrip(paths: images, borderRadius: borderRadius),
          if (content.isNotEmpty && content != '(image)') ...[
            const SizedBox(height: 6),
            textChild,
          ],
        ],
      );
    } else {
      child = textChild;
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

class _ImageStrip extends StatelessWidget {
  const _ImageStrip({required this.paths, required this.borderRadius});

  final List<String> paths;
  final BorderRadius borderRadius;

  @override
  Widget build(BuildContext context) {
    if (paths.length == 1) {
      return ClipRRect(
        borderRadius: borderRadius,
        child: Image.file(
          File(paths.first),
          width: double.infinity,
          fit: BoxFit.cover,
          errorBuilder: (_, __, ___) => const _BrokenImagePlaceholder(),
        ),
      );
    }

    return SizedBox(
      height: 100,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: paths.length,
        separatorBuilder: (_, __) => const SizedBox(width: 4),
        itemBuilder: (_, i) => ClipRRect(
          borderRadius: borderRadius,
          child: Image.file(
            File(paths[i]),
            height: 100,
            width: 100,
            fit: BoxFit.cover,
            errorBuilder: (_, __, ___) => const _BrokenImagePlaceholder(),
          ),
        ),
      ),
    );
  }
}

class _BrokenImagePlaceholder extends StatelessWidget {
  const _BrokenImagePlaceholder();

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 80,
      height: 60,
      color: Colors.grey.shade300,
      child: const Icon(Icons.broken_image, color: Colors.grey),
    );
  }
}
