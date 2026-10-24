import 'package:flutter/material.dart';

import 'package:omi/backend/schema/message.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/pages/chat/widgets/files_handler_widget.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/widgets/extensions/string.dart';
import 'package:omi/widgets/text_selection_controls.dart';

class HumanMessage extends StatelessWidget {
  final ServerMessage message;
  final Function(String)? onAskOmi;
  final VoidCallback? onRetry;

  const HumanMessage({super.key, required this.message, this.onAskOmi, this.onRetry});

  @override
  Widget build(BuildContext context) {
    String text = message.text.decodeString;
    String? contextText;
    String messageText = text;

    final contextRegex = RegExp(r'^Context: "([\s\S]+?)"\n\n');
    final match = contextRegex.firstMatch(text);

    if (match != null) {
      contextText = match.group(1);
      messageText = text.substring(match.end);
    }

    return Padding(
      padding: EdgeInsets.zero,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          FilesHandlerWidget(message: message),
          if (contextText != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 6.0, right: 4.0),
              child: Container(
                constraints: const BoxConstraints(maxWidth: 300),
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                decoration: BoxDecoration(
                  color: Colors.transparent,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.subdirectory_arrow_right, size: 14, color: EllaPalette.of(context).textTertiary),
                    const SizedBox(width: 8),
                    Flexible(
                      child: Text(
                        contextText.length > 50 ? '${contextText.substring(0, 50)}...' : contextText,
                        style: TextStyle(
                          color: EllaPalette.of(context).textTertiary,
                          fontSize: 13,
                        ),
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          Align(
            alignment: Alignment.centerRight,
            child: ConstrainedBox(
              constraints: BoxConstraints(maxWidth: MediaQuery.sizeOf(context).width * 0.86),
              child: Container(
                decoration: BoxDecoration(
                  color: EllaPalette.of(context).userBubbleFill,
                  borderRadius: const BorderRadius.only(
                    topLeft: Radius.circular(20),
                    topRight: Radius.circular(20),
                    bottomLeft: Radius.circular(20),
                    bottomRight: Radius.circular(6),
                  ),
                ),
                padding: const EdgeInsets.symmetric(horizontal: 15, vertical: 11),
                child: SelectableText(
                  messageText.trimRight(),
                  style: EllaTypography.of(context)
                      .body
                      .copyWith(color: EllaPalette.of(context).userBubbleInk, fontSize: 16.5),
                  contextMenuBuilder: (context, editableTextState) {
                    return omiSelectionMenuBuilder(context, editableTextState, (text) {
                      onAskOmi?.call(text);
                    });
                  },
                ),
              ),
            ),
          ),
          if (message.clientDeliveryState == ClientMessageDeliveryState.failed)
            Padding(
              key: Key('chat-send-failed-${message.id}'),
              padding: const EdgeInsets.only(top: 6, right: 4),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  Text(
                    context.l10n.ellaChatCouldntSend,
                    style: EllaTypography.of(context).caption.copyWith(color: EllaPalette.of(context).error),
                  ),
                  const SizedBox(width: 8),
                  TextButton(onPressed: onRetry, child: Text(context.l10n.retry)),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
