import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/services/memory_artwork_cache.dart';
import 'package:omi/utils/l10n_extensions.dart';

typedef MemoryArtworkCachedFileLookup = Future<File?> Function(String cacheKey);

class MemoryArtworkImage extends StatefulWidget {
  const MemoryArtworkImage({
    super.key,
    required this.conversation,
    this.api,
    this.cachedFileLookup,
    this.fit = BoxFit.cover,
  });

  final ServerConversation conversation;
  final MemoryArtworkApi? api;
  final MemoryArtworkCachedFileLookup? cachedFileLookup;
  final BoxFit fit;

  @override
  State<MemoryArtworkImage> createState() => _MemoryArtworkImageState();
}

class _MemoryArtworkImageState extends State<MemoryArtworkImage> {
  MemoryArtworkResult? _remoteResult;
  File? _cachedFile;
  String _cacheKey = '';
  int _requestGeneration = 0;

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
        oldWidget.conversation.artwork?.status != widget.conversation.artwork?.status ||
        oldWidget.conversation.artwork?.styleVersion != widget.conversation.artwork?.styleVersion) {
      _refreshRequest();
    }
  }

  void _refreshRequest() {
    final generation = ++_requestGeneration;
    final api = widget.api ?? MemoryArtworkApi();
    final artwork = widget.conversation.artwork;
    final cacheKey = api.cacheKeyForDisplay(
      memoryId: widget.conversation.id,
      styleVersion: artwork?.styleVersion ?? '',
      enrichmentRevision: artwork?.enrichmentRevision ?? '',
    );
    _remoteResult = null;
    if (_cacheKey != cacheKey) {
      _cacheKey = cacheKey;
      _cachedFile = null;
    }
    if (cacheKey.isNotEmpty) {
      unawaited(_loadCachedFile(cacheKey, generation));
    }
    unawaited(_loadRemoteResult(api, artwork, generation));
  }

  Future<void> _loadCachedFile(String cacheKey, int generation) async {
    try {
      final lookup = widget.cachedFileLookup ?? _defaultCachedFileLookup;
      final file = await lookup(cacheKey);
      if (file == null || !file.existsSync()) return;
      if (!mounted || generation != _requestGeneration || cacheKey != _cacheKey) return;
      setState(() => _cachedFile = file);
    } catch (_) {
      // A cache read failure must not block the authenticated network refresh.
    }
  }

  Future<File?> _defaultCachedFileLookup(String cacheKey) async {
    final info = await MemoryArtworkCache.manager.getFileFromCache(cacheKey);
    return info?.file;
  }

  Future<void> _loadRemoteResult(MemoryArtworkApi api, MemoryArtworkState? artwork, int generation) async {
    MemoryArtworkResult result;
    try {
      result = await api.loadForDisplay(
        widget.conversation.id,
        enqueueIfMissing: artwork == null || artwork.status == MemoryArtworkStatus.unavailable,
      );
    } catch (_) {
      return;
    }
    if (!mounted || generation != _requestGeneration) return;
    setState(() => _remoteResult = result);
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
    final result = _remoteResult;
    if (result?.isReady == true) {
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
          placeholder: (_, __) => _cachedArtworkOrFallback(context),
          errorWidget: (_, __, ___) => _cachedArtworkOrFallback(context),
        ),
      );
    }
    return _cachedArtworkOrFallback(context);
  }

  Widget _cachedArtworkOrFallback(BuildContext context) {
    final cachedFile = _cachedFile;
    if (cachedFile == null) return _fallback(context);
    return Semantics(
      image: true,
      label: context.l10n.memoryGeneratedArtworkLabel,
      child: Image.file(
        cachedFile,
        key: Key('memory-cached-artwork-${widget.conversation.id}'),
        fit: widget.fit,
        gaplessPlayback: true,
        errorBuilder: (_, __, ___) => _fallback(context),
      ),
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
        child: const Center(child: Icon(Icons.brush_outlined, color: Color(0xFF57736A), size: 30)),
      ),
    );
  }
}
