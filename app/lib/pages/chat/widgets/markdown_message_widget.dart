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

Widget getMarkdownWidget(BuildContext context, String message, {Function(String)? onAskOmi, double fontSize = 18}) {
  return MarkdownBody(
    data: formatEllaReplyForDisplay(message),
    selectable: false,
    styleSheet: MarkdownStyleSheet(
      p: EllaTypography.of(context).body.copyWith(fontSize: fontSize),
      a: TextStyle(color: EllaPalette.of(context).tealDeep, fontSize: fontSize, decoration: TextDecoration.underline),
      listBullet: EllaTypography.of(context).body.copyWith(fontSize: fontSize),
      blockquote: TextStyle(
        color: EllaPalette.of(context).textPrimary,
        fontSize: fontSize,
        height: 1.4,
        backgroundColor: Colors.transparent,
      ),
      blockquoteDecoration: BoxDecoration(
        color: EllaPalette.of(context).bgTertiary,
        borderRadius: BorderRadius.circular(4),
      ),
      code: TextStyle(
        color: EllaPalette.of(context).textPrimary,
        backgroundColor: Colors.transparent,
        fontFamily: 'monospace',
      ),
      codeblockDecoration: BoxDecoration(
        color: EllaPalette.of(context).bgSecondary,
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
