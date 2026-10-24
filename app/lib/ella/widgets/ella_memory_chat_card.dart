import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:omi/backend/http/api/conversations.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/pages/conversation_detail/page.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/utils/display_text.dart';
import 'package:omi/utils/l10n_extensions.dart';

/// A recorded memory is an artifact, rather than a reply attributed to the user.
class EllaMemoryChatCard extends StatefulWidget {
  const EllaMemoryChatCard(
      {super.key, required this.message, this.sourceLoader, this.authorityProvider, this.sourcePresenter});
  final ServerMessage message;
  @visibleForTesting
  final Future<ServerConversation?> Function(
    String id, {
    required String expectedAuthenticatedUid,
    required ExactAccountAuthorityVerifier exactAuthority,
  })? sourceLoader;
  @visibleForTesting
  final ExactAccountAuthorityVerifier? Function()? authorityProvider;
  @visibleForTesting
  final Future<void> Function(BuildContext, ServerConversation)? sourcePresenter;

  @override
  State<EllaMemoryChatCard> createState() => _EllaMemoryChatCardState();
}

class _EllaMemoryChatCardState extends State<EllaMemoryChatCard> {
  bool _opening = false;
  bool _failed = false;
  int _generation = 0;
  late String _boundMessageId;
  String? _boundSourceId;

  @override
  void initState() {
    super.initState();
    _boundMessageId = widget.message.id;
    _boundSourceId = widget.message.canonicalConversationId;
  }

  @override
  void didUpdateWidget(covariant EllaMemoryChatCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.message != widget.message ||
        _boundMessageId != widget.message.id ||
        _boundSourceId != widget.message.canonicalConversationId) {
      _boundMessageId = widget.message.id;
      _boundSourceId = widget.message.canonicalConversationId;
      _generation++;
      _opening = false;
      _failed = false;
    }
  }

  Future<void> _openSource() async {
    final id = widget.message.canonicalConversationId;
    final owner = (widget.authorityProvider ?? WalOwnerAuthority.active)();
    final generation = _generation;
    bool isCurrent() => mounted && generation == _generation && owner?.isExactCurrent() == true;
    if (_opening || id == null || id.isEmpty) return;
    if (owner?.isExactCurrent() != true) {
      setState(() => _failed = true);
      return;
    }
    setState(() {
      _opening = true;
      _failed = false;
    });
    try {
      final memory = await (widget.sourceLoader == null
              ? getConversationById(id, expectedAuthenticatedUid: owner!.uid, exactAuthority: owner)
              : widget.sourceLoader!(id, expectedAuthenticatedUid: owner!.uid, exactAuthority: owner))
          .timeout(const Duration(seconds: 10));
      if (!mounted) return;
      if (!isCurrent() || widget.message.canonicalConversationId != id) return;
      if (memory == null || memory.id != id) {
        setState(() => _failed = true);
        return;
      }
      if (widget.sourcePresenter != null) {
        await widget.sourcePresenter!(context, memory);
      } else {
        await Navigator.of(context)
            .push(MaterialPageRoute<void>(builder: (_) => ConversationDetailPage(conversation: memory)));
      }
    } catch (_) {
      if (isCurrent()) setState(() => _failed = true);
    } finally {
      if (mounted && generation == _generation) setState(() => _opening = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final palette = EllaPalette.of(context);
    final sourceId = widget.message.canonicalConversationId;
    final title = widget.message.memories.isEmpty
        ? context.l10n.irisMemoryArtifactLabel
        : safeMemoryDisplayTitle(widget.message.memories.first.structured.title, context.l10n.irisMemoryArtifactLabel);
    return EllaV7Glass(
        padding: const EdgeInsets.all(12),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(
              '${context.l10n.irisMemoryArtifactLabel} · ${DateFormat.jm(Localizations.localeOf(context).toLanguageTag()).format(widget.message.createdAt.toLocal())}',
              style: EllaTypography.of(context).secondary.copyWith(fontSize: 16)),
          TextButton(
            key: const Key('memory-chat-open-source'),
            onPressed: sourceId == null || sourceId.isEmpty || _opening ? null : _openSource,
            style: TextButton.styleFrom(
                foregroundColor: palette.ink,
                minimumSize: const Size(44, 44),
                alignment: Alignment.centerLeft,
                padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 10)),
            child: Row(children: [
              Icon(_opening ? Icons.hourglass_top : Icons.bookmark_outline, size: 16),
              const SizedBox(width: 8),
              Expanded(
                  child: Text(title,
                      style: EllaTypography.of(context).noteBody.copyWith(fontSize: 27, fontWeight: FontWeight.w400))),
              const Icon(Icons.chevron_right_rounded, size: 16),
            ]),
          ),
          if (_failed) Text(context.l10n.irisMemorySourceUnavailable, style: EllaTypography.of(context).secondary),
        ]));
  }
}
