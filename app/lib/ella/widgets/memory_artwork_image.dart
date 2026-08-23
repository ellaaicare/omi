import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/utils/l10n_extensions.dart';

class MemoryArtworkImage extends StatefulWidget {
  const MemoryArtworkImage({super.key, required this.conversation, this.api, this.fit = BoxFit.cover});

  final ServerConversation conversation;
  final MemoryArtworkApi? api;
  final BoxFit fit;

  @override
  State<MemoryArtworkImage> createState() => _MemoryArtworkImageState();
}

class _MemoryArtworkImageState extends State<MemoryArtworkImage> {
  Future<MemoryArtworkResult>? _request;

  @override
  void initState() {
    super.initState();
    _refreshRequest();
  }

  @override
  void didUpdateWidget(covariant MemoryArtworkImage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.conversation.id != widget.conversation.id ||
        oldWidget.conversation.artwork?.enrichmentRevision != widget.conversation.artwork?.enrichmentRevision ||
        oldWidget.conversation.artwork?.status != widget.conversation.artwork?.status) {
      _refreshRequest();
    }
  }

  void _refreshRequest() {
    _request = widget.conversation.artwork?.isReady == true
        ? (widget.api ?? MemoryArtworkApi()).fetch(widget.conversation.id)
        : null;
  }

  Uint8List? _sourcePhoto() {
    for (final photo in widget.conversation.photos) {
      if (photo.discarded || photo.base64.trim().isEmpty) continue;
      try {
        return base64Decode(photo.base64);
      } on FormatException {
        continue;
      }
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final request = _request;
    if (request == null) return _fallback(context);
    return FutureBuilder<MemoryArtworkResult>(
      future: request,
      builder: (context, snapshot) {
        final result = snapshot.data;
        if (result?.isReady != true) {
          return _fallback(context, loading: snapshot.connectionState != ConnectionState.done);
        }
        return Semantics(
          image: true,
          label: context.l10n.memoryGeneratedArtworkLabel,
          child: Image.network(
            result!.url.toString(),
            key: Key('memory-generated-artwork-${widget.conversation.id}'),
            fit: widget.fit,
            gaplessPlayback: true,
            errorBuilder: (_, __, ___) => _fallback(context),
          ),
        );
      },
    );
  }

  Widget _fallback(BuildContext context, {bool loading = false}) {
    final bytes = _sourcePhoto();
    if (bytes != null) {
      return Semantics(
        image: true,
        label: context.l10n.todayMemoryPhotoLabel,
        child: Image.memory(
          bytes,
          key: const Key('memory-source-photo'),
          fit: widget.fit,
          gaplessPlayback: true,
          errorBuilder: (_, __, ___) => _placeholder(),
        ),
      );
    }
    return _placeholder(loading: loading);
  }

  Widget _placeholder({bool loading = false}) {
    final emoji = widget.conversation.structured.emoji.trim();
    return ExcludeSemantics(
      child: KeyedSubtree(
        key: const Key('memory-fallback-art'),
        child: DecoratedBox(
          key: Key('memory-artwork-placeholder-${widget.conversation.id}'),
          decoration: const BoxDecoration(
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [Color(0xFFE6F0EA), Color(0xFFF3E7D8), Color(0xFFE7D9C9)],
            ),
          ),
          child: Stack(
            fit: StackFit.expand,
            children: [
              Center(
                child: Text(
                  emoji.isEmpty ? '🪽' : emoji,
                  style: const TextStyle(fontSize: 34, color: EllaColors.inkSoft),
                ),
              ),
              if (loading)
                const Align(
                  alignment: Alignment.bottomCenter,
                  child: LinearProgressIndicator(
                    minHeight: 2,
                    color: EllaColors.tealDeep,
                    backgroundColor: Colors.transparent,
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
