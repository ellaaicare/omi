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

enum _MemoryArtworkFallbackKind { preparing, unavailable }

class MemoryArtworkImage extends StatefulWidget {
  const MemoryArtworkImage({
    super.key,
    required this.conversation,
    this.api,
    this.cachedFileLookup,
    this.fit = BoxFit.cover,
    this.retryDelay = const Duration(seconds: 5),
  });

  final ServerConversation conversation;
  final MemoryArtworkApi? api;
  final MemoryArtworkCachedFileLookup? cachedFileLookup;
  final BoxFit fit;
  final Duration retryDelay;

  @override
  State<MemoryArtworkImage> createState() => _MemoryArtworkImageState();
}

class _MemoryArtworkImageState extends State<MemoryArtworkImage> {
  MemoryArtworkResult? _remoteResult;
  File? _cachedFile;
  String _cacheKey = '';
  int _requestGeneration = 0;
  Timer? _retryTimer;

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

  @override
  void dispose() {
    _retryTimer?.cancel();
    super.dispose();
  }

  void _refreshRequest() {
    _retryTimer?.cancel();
    _retryTimer = null;
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
        enqueueIfMissing: artwork == null ||
            artwork.status == MemoryArtworkStatus.generating ||
            artwork.status == MemoryArtworkStatus.unavailable,
      );
    } catch (_) {
      _scheduleRetry(api, artwork, generation);
      return;
    }
    if (!mounted || generation != _requestGeneration) return;
    setState(() => _remoteResult = result);
    if (_shouldRetry(result)) _scheduleRetry(api, artwork, generation);
  }

  bool _shouldRetry(MemoryArtworkResult result) {
    if (result.status == MemoryArtworkResultStatus.generating) return true;
    if (result.status != MemoryArtworkResultStatus.unavailable) return false;
    return const {
      'memory_artwork_unavailable',
      'memory_artwork_not_found',
      'memory_artwork_generation_not_queued',
      'memory_artwork_provider_unavailable',
      'memory_artwork_provider_failed',
      'memory_artwork_runtime_authority_unavailable',
      'memory_artwork_worker_failed',
      'memory_artwork_finalize_conflict',
      'memory_artwork_object_missing',
      'memory_artwork_storage_failed',
    }.contains(result.failureCode);
  }

  void _scheduleRetry(MemoryArtworkApi api, MemoryArtworkState? artwork, int generation) {
    if (!mounted || generation != _requestGeneration || _retryTimer?.isActive == true) return;
    _retryTimer = Timer(widget.retryDelay, () {
      _retryTimer = null;
      if (!mounted || generation != _requestGeneration) return;
      unawaited(_loadRemoteResult(api, artwork, generation));
    });
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
    if (_mustSuppressCachedArtwork(result)) {
      return _fallback(context, kind: _MemoryArtworkFallbackKind.unavailable);
    }
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
          placeholder: (_, __) => _cachedArtworkOrFallback(context, kind: _MemoryArtworkFallbackKind.preparing),
          errorWidget: (_, __, ___) => _cachedArtworkOrFallback(context, kind: _MemoryArtworkFallbackKind.unavailable),
        ),
      );
    }
    final fallbackKind = result == null ||
            result.status == MemoryArtworkResultStatus.generating ||
            result.status == MemoryArtworkResultStatus.unavailable
        ? _MemoryArtworkFallbackKind.preparing
        : _MemoryArtworkFallbackKind.unavailable;
    return _cachedArtworkOrFallback(context, kind: fallbackKind);
  }

  bool _mustSuppressCachedArtwork(MemoryArtworkResult? result) {
    if (result == null) return false;
    if (result.status == MemoryArtworkResultStatus.declined) return true;
    if (result.status != MemoryArtworkResultStatus.unavailable) return false;
    return const {
      'memory_artwork_authority_changed',
      'memory_artwork_consent_required',
      'memory_artwork_deletion_pending',
      'memory_artwork_discarded',
      'memory_artwork_memory_not_found',
      'memory_artwork_preference_authority_stale',
      'memory_artwork_release_disabled',
      'memory_artwork_sensitive_source_excluded',
      'memory_artwork_source_stale',
    }.contains(result.failureCode);
  }

  Widget _cachedArtworkOrFallback(BuildContext context, {required _MemoryArtworkFallbackKind kind}) {
    final cachedFile = _cachedFile;
    if (cachedFile == null) return _fallback(context, kind: kind);
    return Semantics(
      image: true,
      label: context.l10n.memoryGeneratedArtworkLabel,
      child: Image.file(
        cachedFile,
        key: Key('memory-cached-artwork-${widget.conversation.id}'),
        fit: widget.fit,
        gaplessPlayback: true,
        errorBuilder: (_, __, ___) => _fallback(context, kind: kind),
      ),
    );
  }

  Widget _fallback(BuildContext context, {required _MemoryArtworkFallbackKind kind}) {
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
          errorBuilder: (_, __, ___) => _placeholder(kind),
        ),
      );
    }
    return _placeholder(kind);
  }

  Widget _placeholder(_MemoryArtworkFallbackKind kind) {
    final isPreparing = kind == _MemoryArtworkFallbackKind.preparing;
    final useCompactLayout = MediaQuery.textScalerOf(context).scale(12) > 18;
    final semanticsLabel =
        isPreparing ? context.l10n.memoryArtworkPreparingLabel : context.l10n.memoryArtworkUnavailableLabel;
    final visibleLabel =
        isPreparing ? context.l10n.memoryArtworkPreparingShort : context.l10n.memoryArtworkUnavailableLabel;
    return Semantics(
      label: semanticsLabel,
      child: DecoratedBox(
        key: Key('memory-artwork-placeholder-${widget.conversation.id}'),
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [Color(0xFFE9E3D8), Color(0xFFDCE9E3)],
          ),
        ),
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (!useCompactLayout) ...[
                  const Icon(Icons.brush_outlined, color: Color(0xFF57736A), size: 30),
                  const SizedBox(height: 8),
                ],
                Text(
                  visibleLabel,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: Color(0xFF57736A), fontSize: 12, fontWeight: FontWeight.w600),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
