import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import 'package:omi/backend/schema/conversation.dart';
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
          return _fallback(context);
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
    final asset = MemoryArtworkTopicLibrary.assetFor(widget.conversation);
    return Semantics(
      image: true,
      label: context.l10n.memoryGeneratedArtworkLabel,
      child: Image.asset(
        asset,
        key: Key('memory-curated-art-${widget.conversation.id}'),
        fit: widget.fit,
        gaplessPlayback: true,
      ),
    );
  }
}

class MemoryArtworkTopicLibrary {
  MemoryArtworkTopicLibrary._();

  static const _assetRoot = 'assets/images/ella-memory-topics';
  static const _fallbackAssets = <String>[
    'meal',
    'family',
    'market',
    'walk',
    'phone',
    'reading',
    'music',
    'quiet',
    'celebration',
    'garden',
    'travel',
    'art',
  ];

  static const _keywords = <String, List<String>>{
    'meal': ['dinner', 'lunch', 'breakfast', 'meal', 'restaurant', 'food', 'cook', 'kitchen', 'paella'],
    'family': ['family', 'friend', 'visit', 'together', 'grandchild', 'daughter', 'son', 'sister', 'brother'],
    'market': ['market', 'shopping', 'shop', 'store', 'grocer', 'farmers'],
    'walk': ['walk', 'park', 'outside', 'trail', 'lake', 'river'],
    'phone': ['phone', 'call', 'spoke', 'conversation'],
    'reading': ['book', 'read', 'reading', 'library', 'story'],
    'music': ['music', 'song', 'concert', 'piano', 'record', 'dance'],
    'celebration': ['holiday', 'birthday', 'celebration', 'party', 'anniversary'],
    'garden': ['garden', 'flower', 'plant', 'yard'],
    'travel': ['travel', 'trip', 'train', 'flight', 'vacation', 'journey'],
    'art': ['art', 'museum', 'gallery', 'paint', 'exhibit'],
  };

  static String assetFor(ServerConversation conversation) {
    final source = '${conversation.structured.title} ${conversation.structured.overview}'.toLowerCase();
    for (final entry in _keywords.entries) {
      if (entry.value.any(source.contains)) return '$_assetRoot/${entry.key}.webp';
    }
    final stableIndex = conversation.id.codeUnits.fold<int>(0, (sum, value) => sum + value) % _fallbackAssets.length;
    return '$_assetRoot/${_fallbackAssets[stableIndex]}.webp';
  }
}
