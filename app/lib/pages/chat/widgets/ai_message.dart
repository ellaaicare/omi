import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:omi/utils/display_text.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';

import 'package:auto_size_text/auto_size_text.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:collection/collection.dart';
import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';
import 'package:omi/widgets/shimmer_with_timeout.dart';

import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/app.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/pages/chat/widgets/files_handler_widget.dart';
import 'package:omi/pages/chat/widgets/typing_indicator.dart';
import 'package:omi/pages/conversation_detail/conversation_detail_provider.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/providers/app_provider.dart';
import 'package:omi/providers/connectivity_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/other/temp.dart';
import 'package:omi/widgets/extensions/string.dart';
import 'package:omi/widgets/text_selection_controls.dart';
import 'chart_message_widget.dart';
import 'markdown_message_widget.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_memory_chat_card.dart';
import 'package:omi/ella/widgets/ella_thinking_indicator.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

/// Parse app_id from thinking text (format: "text|app_id:app_id")
String? parseAppIdFromThinking(String thinkingText) {
  if (thinkingText.contains('|app_id:')) {
    var parts = thinkingText.split('|app_id:');
    if (parts.length == 2) {
      return parts[1];
    }
  }
  return null;
}

/// Get the display text from thinking (removes app_id suffix if present)
String getThinkingDisplayText(String thinkingText) {
  int index = thinkingText.indexOf('|app_id:');
  if (index >= 0) {
    return thinkingText.substring(0, index);
  }
  return thinkingText;
}

/// Build app icon widget from app_id
Widget _buildAppIcon(BuildContext context, String appId, {double size = 15, double opacity = 1.0}) {
  final appProvider = Provider.of<AppProvider>(context, listen: false);
  final messageProvider = Provider.of<MessageProvider>(context, listen: false);
  // Check both public apps and user's installed chat apps (includes private MCP apps)
  final app = appProvider.apps.firstWhereOrNull((a) => a.id == appId) ??
      messageProvider.chatApps.firstWhereOrNull((a) => a.id == appId);

  if (app != null) {
    return Opacity(
      opacity: opacity,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(3),
        child: CachedNetworkImage(
          imageUrl: app.getImageUrl(),
          httpHeaders: const {
            "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
          },
          imageBuilder: (context, imageProvider) => Container(
            width: size,
            height: size,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(3),
              image: DecorationImage(
                image: imageProvider,
                fit: BoxFit.cover,
              ),
            ),
          ),
          placeholder: (context, url) => SizedBox(
            width: size,
            height: size,
            child: Icon(
              Icons.apps,
              size: size * 0.7,
              color: EllaPalette.of(context).textTertiary.withOpacity(opacity),
            ),
          ),
          errorWidget: (context, url, error) => Icon(
            Icons.apps,
            size: size * 0.7,
            color: EllaPalette.of(context).textTertiary.withOpacity(opacity),
          ),
        ),
      ),
    );
  }

  // Fallback to generic icon if app not found
  return Opacity(
    opacity: opacity,
    child: Icon(
      Icons.apps,
      size: size,
      color: EllaPalette.of(context).textTertiary.withOpacity(opacity),
    ),
  );
}

/// Get the integration logo path for a thinking text, if applicable
String? _getIntegrationLogoPath(String thinkingText) {
  final text = thinkingText.toLowerCase();
  if (text.contains('notion')) {
    return 'assets/integration_app_logos/notion-logo.png';
  } else if (text.contains('whoop')) {
    return 'assets/integration_app_logos/whoop.png';
  } else if (text.contains('calendar')) {
    return 'assets/integration_app_logos/google-calendar.png';
  } else if (text.contains('gmail')) {
    return 'assets/integration_app_logos/gmail-logo.jpeg';
  } else if (text.contains('github')) {
    return 'assets/integration_app_logos/github-logo.png';
  } else if (text.contains('twitter') || text.contains('tweet')) {
    return 'assets/integration_app_logos/x-logo.avif';
  }
  return null;
}

/// Get the fallback icon for thinking text (used when no integration logo)
IconData _getThinkingIcon(String thinkingText) {
  final text = thinkingText.toLowerCase();
  if (text.contains('thinking')) {
    return FontAwesomeIcons.brain;
  } else if (text.contains('searching the web') || text.contains('searching web')) {
    return FontAwesomeIcons.magnifyingGlass;
  } else if (text.contains('conversations')) {
    return FontAwesomeIcons.comments;
  } else if (text.contains('memories')) {
    return FontAwesomeIcons.lightbulb;
  } else if (text.contains('action item')) {
    return FontAwesomeIcons.listCheck;
  } else if (text.contains('product info')) {
    return FontAwesomeIcons.circleInfo;
  } else if (text.contains('search')) {
    return FontAwesomeIcons.magnifyingGlass;
  }
  return FontAwesomeIcons.brain; // Default brain icon
}

/// Build the thinking icon widget - either an integration logo or a fallback icon
Widget _buildThinkingIconWidget(String thinkingText, {double size = 15, Color color = EllaColors.textPrimary}) {
  final logoPath = _getIntegrationLogoPath(thinkingText);
  if (logoPath != null) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(3),
      child: Image.asset(
        logoPath,
        width: size,
        height: size,
        fit: BoxFit.cover,
        errorBuilder: (context, error, stackTrace) => FaIcon(
          _getThinkingIcon(thinkingText),
          size: size,
          color: color,
        ),
      ),
    );
  }
  return FaIcon(
    _getThinkingIcon(thinkingText),
    size: size,
    color: color,
  );
}

class AIMessage extends StatefulWidget {
  final bool showTypingIndicator;
  final ServerMessage message;
  final Function(String) sendMessage;
  final Function(String)? onAskOmi;
  final bool displayOptions;
  final App? appSender;
  final Function(ServerConversation) updateConversation;
  final Function(int, {String? reason}) setMessageNps;

  const AIMessage({
    super.key,
    required this.message,
    required this.sendMessage,
    this.onAskOmi,
    required this.displayOptions,
    required this.updateConversation,
    required this.setMessageNps,
    this.appSender,
    this.showTypingIndicator = false,
  });

  @override
  State<AIMessage> createState() => _AIMessageState();
}

class _AIMessageState extends State<AIMessage> {
  late List<bool> conversationDetailLoading;

  @override
  void initState() {
    conversationDetailLoading = List.filled(widget.message.memories.length, false);
    super.initState();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SelectionArea(
          contextMenuBuilder: (context, selectableRegionState) {
            return omiSelectionMenuBuilder(context, selectableRegionState, widget.onAskOmi ?? (text) {});
          },
          child: buildMessageWidget(
            widget.message,
            widget.sendMessage,
            widget.showTypingIndicator,
            widget.displayOptions,
            widget.appSender,
            widget.updateConversation,
            widget.setMessageNps,
            onAskOmi: widget.onAskOmi,
          ),
        ),
      ],
    );
  }
}

Widget buildMessageWidget(
  ServerMessage message,
  Function(String) sendMessage,
  bool showTypingIndicator,
  bool displayOptions,
  App? appSender,
  Function(ServerConversation) updateConversation,
  Function(int, {String? reason}) sendMessageNps, {
  Function(String)? onAskOmi,
}) {
  final displayText = message.sender == MessageSender.ai
      ? stripEllaDisplayPrefix(message.text.decodeString)
      : message.text.decodeString;
  if (message.isCanonicalMemoryArtifact) {
    return EllaMemoryChatCard(message: message);
  } else if (message.memories.isNotEmpty) {
    return MemoriesMessageWidget(
        showTypingIndicator: showTypingIndicator,
        messageMemories: message.memories.length > 3 ? message.memories.sublist(0, 3) : message.memories,
        messageText: message.isEmpty ? '...' : displayText,
        updateConversation: updateConversation,
        message: message,
        setMessageNps: sendMessageNps,
        date: message.createdAt,
        onAskOmi: onAskOmi);
  } else if (message.type == MessageType.daySummary) {
    return DaySummaryWidget(
        showTypingIndicator: showTypingIndicator, messageText: displayText, date: message.createdAt);
  } else if (displayOptions) {
    return InitialMessageWidget(
      showTypingIndicator: showTypingIndicator,
      messageText: displayText,
      sendMessage: sendMessage,
      onAskOmi: onAskOmi,
    );
  } else {
    return NormalMessageWidget(
      showTypingIndicator: showTypingIndicator,
      thinkings: message.thinkings,
      messageText: displayText,
      message: message,
      setMessageNps: sendMessageNps,
      createdAt: message.createdAt,
      onAskOmi: onAskOmi,
    );
  }
}

class InitialMessageWidget extends StatelessWidget {
  final bool showTypingIndicator;
  final String messageText;
  final Function(String) sendMessage;
  final Function(String)? onAskOmi;

  const InitialMessageWidget(
      {super.key,
      required this.showTypingIndicator,
      required this.messageText,
      required this.sendMessage,
      this.onAskOmi});

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        showTypingIndicator
            ? const Row(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisAlignment: MainAxisAlignment.start,
                children: [
                  SizedBox(width: 4),
                  TypingIndicator(),
                  Spacer(),
                ],
              )
            : getMarkdownWidget(context, messageText, onAskOmi: onAskOmi),
        const SizedBox(height: 8),
        const SizedBox(height: 8),
        InitialOptionWidget(optionText: 'What did I do yesterday?', sendMessage: sendMessage),
        const SizedBox(height: 8),
        InitialOptionWidget(optionText: 'What could I do differently today?', sendMessage: sendMessage),
        const SizedBox(height: 8),
        InitialOptionWidget(optionText: 'Can you teach me something new?', sendMessage: sendMessage),
      ],
    );
  }
}

class DaySummaryWidget extends StatelessWidget {
  final bool showTypingIndicator;
  final DateTime date;
  final String messageText;

  const DaySummaryWidget({super.key, required this.showTypingIndicator, required this.messageText, required this.date});

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisAlignment: MainAxisAlignment.start,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '📅  Day Summary ~ ${dateTimeFormat('MMM, dd', date)}',
          style: TextStyle(
            fontSize: 18,
            fontWeight: FontWeight.w500,
            color: EllaPalette.of(context).textSecondary,
            decoration: TextDecoration.underline,
          ),
        ),
        const SizedBox(height: 16),
        showTypingIndicator
            ? const Row(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisAlignment: MainAxisAlignment.start,
                children: [
                  SizedBox(width: 4),
                  TypingIndicator(),
                  Spacer(),
                ],
              )
            : daySummaryMessagesList(messageText),
        if (messageText.isNotEmpty && !showTypingIndicator) MessageActionBar(messageText: messageText),
      ],
    );
  }

  List<String> splitMessage(String message) {
    // Check if the string contains numbered items using regex
    bool hasNumbers = RegExp(r'^\d+\.\s').hasMatch(message);

    if (hasNumbers) {
      // Remove numbers followed by period and space
      String cleanedMessage = message.replaceAll(RegExp(r'\d+\.\s'), '');
      return cleanedMessage.split(RegExp(r'\n|\.\s')).where((msg) => msg.trim().isNotEmpty).toList();
    } else {
      // Split by period followed by space
      List<String> listOfMessages = message.split('. ');
      return listOfMessages
          .map((msg) => msg.endsWith('.') ? msg.substring(0, msg.length - 1) : msg)
          .where((msg) => msg.trim().isNotEmpty)
          .toList();
    }
  }

  Widget daySummaryMessagesList(String text) {
    var sentences = splitMessage(text);

    return ListView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: sentences.length,
      itemBuilder: (context, index) {
        return ListTile(
          visualDensity: const VisualDensity(horizontal: 0, vertical: -4),
          contentPadding: const EdgeInsets.symmetric(vertical: 2),
          horizontalTitleGap: 12,
          minLeadingWidth: 0,
          leading: Text(
            '${index + 1}.',
            style: TextStyle(
              fontSize: 15,
              fontWeight: FontWeight.bold,
              color: EllaPalette.of(context).textTertiary,
            ),
          ),
          title: AutoSizeText(
            sentences[index],
            style: TextStyle(
              fontSize: 16.0,
              fontWeight: FontWeight.w500,
              height: 1.35,
              color: EllaPalette.of(context).textPrimary,
            ),
            softWrap: true,
            maxLines: 3,
            overflow: TextOverflow.ellipsis,
          ),
        );
      },
    );
  }
}

class NormalMessageWidget extends StatefulWidget {
  final bool showTypingIndicator;
  final String messageText;
  final List<String> thinkings;
  final ServerMessage message;
  final Function(int, {String? reason}) setMessageNps;
  final DateTime createdAt;
  final Function(String)? onAskOmi;

  const NormalMessageWidget({
    super.key,
    required this.showTypingIndicator,
    required this.messageText,
    required this.message,
    required this.setMessageNps,
    required this.createdAt,
    this.thinkings = const [],
    this.onAskOmi,
  });

  @override
  State<NormalMessageWidget> createState() => _NormalMessageWidgetState();
}

class _NormalMessageWidgetState extends State<NormalMessageWidget> {
  Widget _buildChartShimmer() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
      child: ShimmerWithTimeout(
        baseColor: EllaPalette.of(context).bgSecondary,
        highlightColor: EllaPalette.of(context).bgTertiary,
        timeoutSeconds: 15,
        child: Container(
          height: 236,
          decoration: BoxDecoration(
            color: EllaPalette.of(context).bgSecondary,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: EllaPalette.of(context).bgTertiary),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisAlignment: MainAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        FilesHandlerWidget(message: widget.message),
        widget.showTypingIndicator && widget.messageText.isEmpty
            ? const EllaThinkingIndicator()
            : const SizedBox.shrink(),
        widget.messageText.isEmpty
            ? const SizedBox.shrink()
            : Align(
                alignment: Alignment.centerLeft,
                child: FractionallySizedBox(
                  widthFactor: 0.90,
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                    decoration: BoxDecoration(
                      color: Colors.transparent,
                      border: Border.all(color: EllaPalette.of(context).cardEdge),
                      borderRadius: const BorderRadius.only(
                        topLeft: Radius.circular(20),
                        topRight: Radius.circular(20),
                        bottomLeft: Radius.circular(6),
                        bottomRight: Radius.circular(20),
                      ),
                    ),
                    child: Builder(
                      builder: (context) {
                        String? selectedText;
                        return SelectionArea(
                          onSelectionChanged: (SelectedContent? selectedContent) {
                            selectedText = selectedContent?.plainText;
                          },
                          contextMenuBuilder: (context, selectableRegionState) {
                            return omiSelectionMenuBuilder(
                              context,
                              selectableRegionState,
                              (text) => widget.onAskOmi?.call(text),
                              selectedText: selectedText,
                            );
                          },
                          child: widget.messageText.trim() == "Good morning — I'm here."
                              ? Text(
                                  "Good morning — I'm here.",
                                  style: TextStyle(
                                    fontFamily: EllaTextStyles.noteFont,
                                    fontSize: 18,
                                    fontWeight: FontWeight.w400,
                                    height: 1.45,
                                    color: EllaPalette.of(context).ink,
                                  ),
                                )
                              : getMarkdownWidget(context, widget.messageText,
                                  onAskOmi: widget.onAskOmi, fontSize: 16.5),
                        );
                      },
                    ),
                  ),
                ),
              ),
        if (widget.message.chartData != null)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 4),
            child: ChartMessageWidget(chartData: widget.message.chartData!),
          )
        else if (widget.showTypingIndicator && widget.message.thinkings.any((t) => t.toLowerCase().contains('chart')))
          _buildChartShimmer(),
      ],
    );
  }
}

class MemoriesMessageWidget extends StatefulWidget {
  final bool showTypingIndicator;
  final List<MessageConversation> messageMemories;
  final String messageText;
  final Function(ServerConversation) updateConversation;
  final ServerMessage message;
  final Function(int, {String? reason}) setMessageNps;
  final DateTime date;
  final Function(String)? onAskOmi;
  @visibleForTesting
  final ExactAccountAuthorityVerifier? Function()? sourceAuthorityProvider;
  @visibleForTesting
  final Future<ServerConversation?> Function(String id,
      {required String expectedAuthenticatedUid, required ExactAccountAuthorityVerifier exactAuthority})? sourceLoader;
  @visibleForTesting
  final Future<void> Function(BuildContext, ServerConversation)? sourcePresenter;

  const MemoriesMessageWidget({
    super.key,
    required this.showTypingIndicator,
    required this.messageMemories,
    required this.messageText,
    required this.updateConversation,
    required this.message,
    required this.setMessageNps,
    required this.date,
    this.onAskOmi,
    this.sourceAuthorityProvider,
    this.sourceLoader,
    this.sourcePresenter,
  });

  @override
  State<MemoriesMessageWidget> createState() => _MemoriesMessageWidgetState();
}

class _MemoriesMessageWidgetState extends State<MemoriesMessageWidget> {
  late List<bool> conversationDetailLoading;
  late final ExactAccountAuthorityVerifier? _sourceAuthority;
  List<String> _boundReferenceIds = [];
  int _referenceGeneration = 0;
  bool _showDots = true;
  Timer? _dotsTimer;

  @override
  void initState() {
    _sourceAuthority = (widget.sourceAuthorityProvider ?? WalOwnerAuthority.active)();
    _boundReferenceIds = widget.messageMemories.map((m) => m.id).toList();
    conversationDetailLoading = List.filled(widget.messageMemories.length, false);
    if (widget.showTypingIndicator && widget.messageText == '...' && widget.message.thinkings.isEmpty) {
      _dotsTimer = Timer(const Duration(milliseconds: 500), () {
        if (mounted) {
          setState(() {
            _showDots = false;
          });
        }
      });
    }
    super.initState();
  }

  @override
  void didUpdateWidget(covariant MemoriesMessageWidget oldWidget) {
    super.didUpdateWidget(oldWidget);
    final ids = widget.messageMemories.map((m) => m.id).toList();
    if (!identical(oldWidget.message, widget.message) ||
        !const ListEquality<String>().equals(ids, _boundReferenceIds)) {
      _referenceGeneration++;
      _boundReferenceIds = ids;
      conversationDetailLoading = List.filled(ids.length, false);
    }
  }

  @override
  void dispose() {
    _dotsTimer?.cancel();
    super.dispose();
  }

  Widget _buildChartShimmer() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
      child: ShimmerWithTimeout(
        baseColor: EllaPalette.of(context).bgSecondary,
        highlightColor: EllaPalette.of(context).bgTertiary,
        timeoutSeconds: 15,
        child: Container(
          height: 236,
          decoration: BoxDecoration(
            color: EllaPalette.of(context).bgSecondary,
            borderRadius: BorderRadius.circular(16),
            border: Border.all(color: EllaPalette.of(context).bgTertiary),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    var thinkingTextRaw = widget.message.thinkings.isNotEmpty ? widget.message.thinkings.last.decodeString : null;

    // Parse app_id and display text from thinking messages
    String? currentAppId = thinkingTextRaw != null ? parseAppIdFromThinking(thinkingTextRaw) : null;
    var thinkingText = thinkingTextRaw != null ? getThinkingDisplayText(thinkingTextRaw) : null;

    // Show "thinking" text if we have thinking text, or if dots timer expired and no thinking text yet
    bool shouldShowThinking =
        thinkingText != null || (!_showDots && widget.showTypingIndicator && widget.messageText == '...');
    String displayThinkingText = thinkingText ?? 'Thinking';

    final renderedMessage = widget.message;
    final renderedReferences = List<MessageConversation>.of(widget.messageMemories);
    final renderedGeneration = _referenceGeneration;
    return Container(
      key: const Key('iris-assistant-kept-bubble'),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: Colors.transparent,
        border: Border.all(color: EllaPalette.of(context).cardEdge),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Padding(
          //   padding: const EdgeInsets.only(bottom: 4.0),
          //   child: Text(
          //     formatChatTimestamp(widget.date),
          //     style: TextStyle(
          //       color: Colors.grey.shade500,
          //       fontSize: 12,
          //     ),
          //   ),
          // ),
          widget.showTypingIndicator && widget.messageText == '...'
              ? Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      shouldShowThinking
                          ? Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                mainAxisAlignment: MainAxisAlignment.start,
                                children: [
                                  Row(
                                    mainAxisSize: MainAxisSize.min,
                                    children: [
                                      // Icon stays outside shimmer to preserve colors (app icon or integration logo)
                                      if (currentAppId != null) ...[
                                        _buildAppIcon(context, currentAppId, size: 15),
                                        const SizedBox(width: 6),
                                      ] else ...[
                                        _buildThinkingIconWidget(displayThinkingText, size: 15),
                                        const SizedBox(width: 6),
                                      ],
                                      // Shimmer only applies to text
                                      Flexible(
                                        child: ShimmerWithTimeout(
                                          baseColor: EllaPalette.of(context).textPrimary,
                                          highlightColor: EllaPalette.of(context).textTertiary,
                                          child: Text(
                                            overflow: TextOverflow.fade,
                                            maxLines: 1,
                                            softWrap: false,
                                            displayThinkingText,
                                            style: TextStyle(color: EllaPalette.of(context).textPrimary, fontSize: 15),
                                          ),
                                        ),
                                      ),
                                    ],
                                  )
                                ],
                              ),
                            )
                          : const TypingIndicator(),
                    ],
                  ))
              : widget.showTypingIndicator
                  ? const Row(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      mainAxisAlignment: MainAxisAlignment.start,
                      children: [
                        SizedBox(width: 4),
                        TypingIndicator(),
                        Spacer(),
                      ],
                    )
                  : Builder(
                      builder: (context) {
                        String? selectedText;
                        return SelectionArea(
                          onSelectionChanged: (SelectedContent? selectedContent) {
                            selectedText = selectedContent?.plainText;
                          },
                          contextMenuBuilder: (context, selectableRegionState) {
                            return omiSelectionMenuBuilder(
                              context,
                              selectableRegionState,
                              (text) {
                                widget.onAskOmi?.call(text);
                              },
                              selectedText: selectedText,
                            );
                          },
                          child:
                              getMarkdownWidget(context, widget.messageText, onAskOmi: widget.onAskOmi, fontSize: 16.5),
                        );
                      },
                    ),
          if (widget.messageText.isNotEmpty && widget.messageText != '...' && !widget.showTypingIndicator)
            MessageActionBar(
              messageText: widget.messageText,
              setMessageNps: widget.setMessageNps,
              currentNps: widget.message.rating,
            ),
          if (widget.message.chartData != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 4),
              child: ChartMessageWidget(chartData: widget.message.chartData!),
            )
          else if (widget.showTypingIndicator && widget.message.thinkings.any((t) => t.toLowerCase().contains('chart')))
            _buildChartShimmer(),
          const SizedBox(height: 16),
          for (var data in renderedReferences.indexed) ...[
            Padding(
              padding: const EdgeInsetsDirectional.fromSTEB(0.0, 4.0, 0.0, 4.0),
              child: GestureDetector(
                onTap: () async {
                  final originMessage = renderedMessage;
                  final sourceId = data.$2.id;
                  final owner = _sourceAuthority;
                  bool current() =>
                      mounted &&
                      renderedGeneration == _referenceGeneration &&
                      owner?.isExactCurrent() == true &&
                      identical(widget.message, originMessage) &&
                      data.$1 < widget.messageMemories.length &&
                      widget.messageMemories[data.$1].id == sourceId;
                  if (!current()) return;
                  if (widget.sourceLoader != null) {
                    if (conversationDetailLoading[data.$1]) return;
                    setState(() => conversationDetailLoading[data.$1] = true);
                    try {
                      final memory = await widget.sourceLoader!(sourceId,
                          expectedAuthenticatedUid: owner!.uid, exactAuthority: owner);
                      if (!mounted || !context.mounted || !current() || memory == null || memory.id != sourceId) return;
                      await widget.sourcePresenter?.call(context, memory);
                    } catch (_) {
                      return;
                    } finally {
                      if (current()) setState(() => conversationDetailLoading[data.$1] = false);
                    }
                    return;
                  }
                  final connectivityProvider = Provider.of<ConnectivityProvider>(context, listen: false);
                  if (connectivityProvider.isConnected) {
                    var memProvider = Provider.of<ConversationProvider>(context, listen: false);
                    var idx = -1;
                    var date = DateTime(data.$2.createdAt.year, data.$2.createdAt.month, data.$2.createdAt.day);
                    idx =
                        memProvider.groupedConversations[date]?.indexWhere((element) => element.id == data.$2.id) ?? -1;

                    if (idx != -1) {
                      context.read<ConversationDetailProvider>().updateConversation(data.$2.id, date);
                      var m = memProvider.groupedConversations[date]![idx];
                      MixpanelManager().chatMessageConversationClicked(m);
                      await Navigator.of(context).push(
                        MaterialPageRoute(
                          builder: (c) => ConversationDetailPage(
                            conversation: m,
                          ),
                        ),
                      );
                    } else {
                      if (conversationDetailLoading[data.$1]) return;
                      setState(() => conversationDetailLoading[data.$1] = true);
                      ServerConversation? loaded;
                      try {
                        loaded = await getConversationById(sourceId,
                            expectedAuthenticatedUid: owner!.uid, exactAuthority: owner);
                      } catch (_) {
                        return;
                      } finally {
                        if (current()) setState(() => conversationDetailLoading[data.$1] = false);
                      }
                      final m = loaded;
                      if (!mounted || !context.mounted || !current() || m == null || m.id != sourceId) return;
                      (idx, date) = memProvider.addConversationWithDateGrouped(m);
                      MixpanelManager().chatMessageConversationClicked(m);
                      setState(() => conversationDetailLoading[data.$1] = false);
                      context.read<ConversationDetailProvider>().updateConversation(m.id, date);
                      await Navigator.of(context).push(
                        MaterialPageRoute(
                          builder: (c) => ConversationDetailPage(
                            conversation: m,
                          ),
                        ),
                      );
                      if (current() && SharedPreferencesUtil().modifiedConversationDetails?.id == m.id) {
                        ServerConversation modifiedDetails = SharedPreferencesUtil().modifiedConversationDetails!;
                        widget.updateConversation(SharedPreferencesUtil().modifiedConversationDetails!);
                        var copy = List<MessageConversation>.from(widget.messageMemories);
                        copy[data.$1] = MessageConversation(
                            modifiedDetails.id,
                            modifiedDetails.createdAt,
                            MessageConversationStructured(
                              modifiedDetails.structured.title,
                              modifiedDetails.structured.emoji,
                            ));
                        widget.messageMemories.clear();
                        widget.messageMemories.addAll(copy);
                        SharedPreferencesUtil().modifiedConversationDetails = null;
                        setState(() {});
                      }
                    }
                  } else {
                    ScaffoldMessenger.of(context).showSnackBar(
                      SnackBar(
                        content: Text(context.l10n.pleaseCheckInternetConnection),
                        duration: const Duration(seconds: 2),
                      ),
                    );
                  }
                },
                child: Container(
                  constraints: const BoxConstraints(minHeight: 44),
                  padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 12),
                  width: double.maxFinite,
                  decoration: BoxDecoration(
                    border: Border(top: BorderSide(color: EllaPalette.of(context).cardEdge)),
                  ),
                  child: Row(
                    children: [
                      const Icon(Icons.bookmark_outline, size: 16),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          safeMemoryDisplayTitle(data.$2.structured.title, context.l10n.irisMemoryArtifactLabel),
                          style: EllaTypography.of(context).body.copyWith(fontSize: 16, fontWeight: FontWeight.w700),
                        ),
                      ),
                      const SizedBox(width: 8),
                      conversationDetailLoading[data.$1]
                          ? SizedBox(
                              height: 16,
                              width: 16,
                              child: CircularProgressIndicator(
                                valueColor: AlwaysStoppedAnimation<Color>(EllaPalette.of(context).textDisabled),
                                strokeWidth: 2,
                              ))
                          : FaIcon(FontAwesomeIcons.chevronRight, size: 16, color: EllaPalette.of(context).textDisabled)
                    ],
                  ),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }

  String tryDecodeText(String text) {
    try {
      return utf8.decode(text.codeUnits);
    } catch (e) {
      return text;
    }
  }
}

/// Feedback reason options for thumbs down
enum FeedbackReason {
  tooVerbose('too_verbose', 'Too verbose'),
  incorrectOrHallucination('incorrect_or_hallucination', 'Incorrect / hallucination'),
  notHelpfulOrIrrelevant('not_helpful_or_irrelevant', 'Not helpful / irrelevant'),
  didntFollowInstructions('didnt_follow_instructions', "Didn't follow instructions"),
  other('other', 'Other');

  final String key;
  final String label;

  const FeedbackReason(this.key, this.label);
}

/// Bottom sheet for collecting feedback on chat messages
class FeedbackBottomSheet extends StatefulWidget {
  final Function(String reason, String? comment) onSubmit;

  const FeedbackBottomSheet({
    super.key,
    required this.onSubmit,
  });

  @override
  State<FeedbackBottomSheet> createState() => _FeedbackBottomSheetState();
}

class _FeedbackBottomSheetState extends State<FeedbackBottomSheet> {
  FeedbackReason? _selectedReason;
  final TextEditingController _commentController = TextEditingController();
  final FocusNode _commentFocusNode = FocusNode();

  @override
  void dispose() {
    _commentController.dispose();
    _commentFocusNode.dispose();
    super.dispose();
  }

  void _handleSubmit() {
    if (_selectedReason == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(context.l10n.pleaseSelectReason),
          backgroundColor: Colors.red,
          duration: const Duration(seconds: 2),
        ),
      );
      return;
    }

    HapticFeedback.mediumImpact();
    final comment = _commentController.text.trim();
    widget.onSubmit(_selectedReason!.key, comment.isEmpty ? null : comment);
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        bottom: MediaQuery.of(context).viewInsets.bottom,
      ),
      child: Container(
        decoration: const BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
        ),
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Handle bar
            Center(
              child: Container(
                width: 36,
                height: 4,
                margin: const EdgeInsets.only(bottom: 16),
                decoration: BoxDecoration(
                  color: EllaPalette.of(context).textDisabled,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            ),

            // Header with title and submit button
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  'What went wrong?',
                  style: TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.w600,
                    color: EllaPalette.of(context).textPrimary,
                  ),
                ),
                TextButton(
                  onPressed: _selectedReason != null ? _handleSubmit : null,
                  child: Text(
                    'Submit',
                    style: TextStyle(
                      color: _selectedReason != null ? Colors.blue : EllaPalette.of(context).textDisabled,
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 16),

            // Reason options
            Text(
              'Select a reason',
              style: TextStyle(
                fontSize: 14,
                fontWeight: FontWeight.w500,
                color: EllaPalette.of(context).textTertiary,
              ),
            ),
            const SizedBox(height: 10),

            // Reason chips
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: FeedbackReason.values.map((reason) {
                final isSelected = _selectedReason == reason;
                return GestureDetector(
                  onTap: () {
                    HapticFeedback.selectionClick();
                    setState(() {
                      _selectedReason = reason;
                    });
                  },
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                    decoration: BoxDecoration(
                      color: isSelected ? Colors.blue.withOpacity(0.2) : EllaPalette.of(context).bgSecondary,
                      borderRadius: BorderRadius.circular(20),
                      border: isSelected ? Border.all(color: Colors.blue, width: 1.5) : null,
                    ),
                    child: Text(
                      reason.label,
                      style: TextStyle(
                        color: isSelected ? Colors.blue : EllaPalette.of(context).textPrimary,
                        fontSize: 14,
                        fontWeight: isSelected ? FontWeight.w600 : FontWeight.w400,
                      ),
                    ),
                  ),
                );
              }).toList(),
            ),
            const SizedBox(height: 20),

            // Comment input
            Text(
              'Additional feedback (optional)',
              style: TextStyle(
                fontSize: 14,
                fontWeight: FontWeight.w500,
                color: EllaPalette.of(context).textTertiary,
              ),
            ),
            const SizedBox(height: 10),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 4),
              decoration: BoxDecoration(
                color: EllaPalette.of(context).bgSecondary,
                borderRadius: BorderRadius.circular(12),
              ),
              child: TextField(
                controller: _commentController,
                focusNode: _commentFocusNode,
                style: TextStyle(
                  color: EllaPalette.of(context).textPrimary,
                  fontSize: 15,
                  height: 1.4,
                ),
                decoration: InputDecoration(
                  border: InputBorder.none,
                  contentPadding: const EdgeInsets.symmetric(vertical: 10),
                  hintText: context.l10n.tellUsMoreWhatWentWrong,
                  hintStyle: TextStyle(
                    color: EllaPalette.of(context).textDisabled,
                    fontSize: 15,
                  ),
                ),
                maxLines: 3,
                minLines: 2,
                maxLength: 500,
                buildCounter: (context, {required currentLength, required isFocused, maxLength}) => null,
                textCapitalization: TextCapitalization.sentences,
              ),
            ),
            const SizedBox(height: 16),
          ],
        ),
      ),
    );
  }
}

/// Show the feedback bottom sheet
Future<void> showFeedbackBottomSheet(
  BuildContext context, {
  required Function(String reason, String? comment) onSubmit,
}) async {
  await showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    builder: (context) => FeedbackBottomSheet(onSubmit: onSubmit),
  );
}

class MessageActionBar extends StatelessWidget {
  final String messageText;
  final Function(int, {String? reason})? setMessageNps;
  final int? currentNps;

  const MessageActionBar({
    super.key,
    required this.messageText,
    this.setMessageNps,
    this.currentNps,
  });

  @override
  Widget build(BuildContext context) {
    // Actions are exposed through the selection/long-press context menu in v2.
    return const SizedBox.shrink();
  }
}

class CopyButton extends StatelessWidget {
  final String messageText;
  final bool isUserMessage;

  const CopyButton({
    super.key,
    required this.messageText,
    this.isUserMessage = false,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsetsDirectional.fromSTEB(0.0, 8, 0.0, 0.0),
      child: InkWell(
        splashColor: Colors.transparent,
        focusColor: Colors.transparent,
        hoverColor: Colors.transparent,
        highlightColor: Colors.transparent,
        onTap: () async {
          await Clipboard.setData(ClipboardData(text: messageText));
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text(
                'Message copied to clipboard.',
                style: TextStyle(
                  color: Color.fromARGB(255, 255, 255, 255),
                  fontSize: 12.0,
                ),
              ),
              duration: Duration(milliseconds: 2000),
            ),
          );
        },
        child: Row(
          mainAxisSize: MainAxisSize.max,
          mainAxisAlignment: isUserMessage ? MainAxisAlignment.end : MainAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsetsDirectional.fromSTEB(0.0, 0.0, 4.0, 0.0),
              child: Icon(
                Icons.content_copy,
                color: Theme.of(context).textTheme.bodySmall!.color,
                size: 10.0,
              ),
            ),
            Text(
              'Copy message',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(
              width: 8,
            ),
          ],
        ),
      ),
    );
  }
}

class InitialOptionWidget extends StatelessWidget {
  final String optionText;
  final Function(String) sendMessage;

  const InitialOptionWidget({super.key, required this.optionText, required this.sendMessage});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12.0, vertical: 10),
        width: double.maxFinite,
        decoration: BoxDecoration(
          color: EllaPalette.of(context).bgSecondary,
          borderRadius: BorderRadius.circular(12.0),
        ),
        child: Text(optionText, style: Theme.of(context).textTheme.bodyMedium),
      ),
      onTap: () {
        sendMessage(optionText);
      },
    );
  }
}
