import 'package:flutter/material.dart';

import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:omi/ella/ella_theme.dart';

String formatEllaReplyForDisplay(String message) {
  final trimmed = message.trimRight();
  if (trimmed.contains('\n\n') || trimmed.contains('```')) return trimmed;

  final matches = RegExp(r'[^.!?]+[.!?]+(?:\s+|$)|[^.!?]+$').allMatches(trimmed);
  final sentences = matches.map((match) => match.group(0)!.trim()).where((sentence) => sentence.isNotEmpty).toList();
  if (sentences.length <= 4) return trimmed;

  final paragraphs = <String>[];
  for (var index = 0; index < sentences.length; index += 3) {
    final end = index + 3 < sentences.length ? index + 3 : sentences.length;
    paragraphs.add(sentences.sublist(index, end).join(' '));
  }
  return paragraphs.join('\n\n');
}

Widget getMarkdownWidget(BuildContext context, String message, {Function(String)? onAskOmi}) {
  return MarkdownBody(
    data: formatEllaReplyForDisplay(message),
    selectable: false,
    styleSheet: MarkdownStyleSheet(
      p: const TextStyle(color: EllaColors.textPrimary, fontSize: 16, height: 1.4),
      a: const TextStyle(color: Colors.blue, decoration: TextDecoration.underline),
      listBullet: const TextStyle(color: EllaColors.textPrimary, fontSize: 16),
      blockquote: const TextStyle(
        color: EllaColors.textPrimary,
        fontSize: 16,
        height: 1.4,
        backgroundColor: Colors.transparent,
      ),
      blockquoteDecoration: BoxDecoration(
        color: EllaColors.bgTertiary,
        borderRadius: BorderRadius.circular(4),
      ),
      code: const TextStyle(
        color: EllaColors.textPrimary,
        backgroundColor: Colors.transparent,
        fontFamily: 'monospace',
      ),
      codeblockDecoration: BoxDecoration(
        color: EllaColors.bgSecondary,
        borderRadius: BorderRadius.circular(8),
      ),
    ),
    onTapLink: (text, href, title) {
      if (href != null) {
        launchUrl(Uri.parse(href));
      }
    },
  );
}
