import 'dart:convert';
import 'dart:typed_data';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/services/memory_artwork_cache.dart';
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
    final artwork = widget.conversation.artwork;
    _request = (widget.api ?? MemoryArtworkApi()).loadForDisplay(
      widget.conversation.id,
      enqueueIfMissing: artwork == null || artwork.status == MemoryArtworkStatus.unavailable,
    );
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
          return _fallback(context);
        }
        return Semantics(
          image: true,
          label: context.l10n.memoryGeneratedArtworkLabel,
          child: CachedNetworkImage(
            imageUrl: result!.url.toString(),
            key: Key('memory-generated-artwork-${widget.conversation.id}'),
            cacheKey: result.cacheKey,
            cacheManager: MemoryArtworkCache.manager,
            fit: widget.fit,
            useOldImageOnUrlChange: true,
            placeholder: (_, __) => _fallback(context),
            errorWidget: (_, __, ___) => _fallback(context),
          ),
        );
      },
    );
  }

  Widget _fallback(BuildContext context) {
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
    return _placeholder();
  }

  Widget _placeholder() {
    return Semantics(
      label: context.l10n.memoryArtworkPreparingLabel,
      child: DecoratedBox(
        key: Key('memory-artwork-placeholder-${widget.conversation.id}'),
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [Color(0xFFE9E3D8), Color(0xFFDCE9E3)],
          ),
        ),
        child: const Center(
          child: Icon(Icons.brush_outlined, color: Color(0xFF57736A), size: 30),
        ),
      ),
    );
  }
}
