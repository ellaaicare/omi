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
typedef MemoryArtworkCacheEvictor = Future<void> Function(String cacheKey);

enum _MemoryArtworkFallbackKind { preparing, unavailable }

class MemoryArtworkImage extends StatefulWidget {
  const MemoryArtworkImage({
    super.key,
    required this.conversation,
    this.api,
    this.cachedFileLookup,
    this.cacheEvictor,
    this.fit = BoxFit.cover,
    this.retryDelay = const Duration(seconds: 5),
    this.maxAuthorityUnavailableRetries = 3,
    this.refreshEpoch = 0,
    this.authorityEpoch = 0,
    this.enqueueIfMissing = false,
  });

  final ServerConversation conversation;
  final MemoryArtworkApi? api;
  final MemoryArtworkCachedFileLookup? cachedFileLookup;
  final MemoryArtworkCacheEvictor? cacheEvictor;
  final BoxFit fit;
  final Duration retryDelay;

  /// A replacement authority can take a moment to persist after its notifier
  /// fires. Retry that narrow race a few times, then wait for the next parent
  /// refresh instead of polling a failing authenticated endpoint forever.
  final int maxAuthorityUnavailableRetries;

  /// A parent-owned queue completion revision. It refreshes visible cards after
  /// the server finishes a batch without letting scrolling create new jobs.
  final int refreshEpoch;

  /// An account/profile change invalidates signed URLs and disk bytes even if
  /// a memory id happens to collide across authorities.
  final int authorityEpoch;
  final bool enqueueIfMissing;

  @override
  State<MemoryArtworkImage> createState() => _MemoryArtworkImageState();
}

class _MemoryArtworkImageState extends State<MemoryArtworkImage> {
  MemoryArtworkResult? _remoteResult;
  File? _cachedFile;
  String _cacheKey = '';
  int _requestGeneration = 0;
  Timer? _retryTimer;
  bool _imageRetryScheduled = false;
  int _authorityUnavailableRetries = 0;

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
        oldWidget.conversation.artwork?.styleVersion != widget.conversation.artwork?.styleVersion ||
        oldWidget.refreshEpoch != widget.refreshEpoch ||
        oldWidget.authorityEpoch != widget.authorityEpoch) {
      _refreshRequest(invalidateCachedArtwork: oldWidget.authorityEpoch != widget.authorityEpoch);
    }
  }

  @override
  void dispose() {
    _retryTimer?.cancel();
    super.dispose();
  }

  void _refreshRequest({bool invalidateCachedArtwork = false}) {
    _retryTimer?.cancel();
    _retryTimer = null;
    _imageRetryScheduled = false;
    _authorityUnavailableRetries = 0;
    final generation = ++_requestGeneration;
    final api = widget.api ?? MemoryArtworkApi();
    final artwork = widget.conversation.artwork;
    final cacheKey = api.cacheKeyForDisplay(
      memoryId: widget.conversation.id,
      styleVersion: artwork?.styleVersion ?? '',
      enrichmentRevision: artwork?.enrichmentRevision ?? '',
    );
    _remoteResult = null;
    if (_cacheKey != cacheKey || invalidateCachedArtwork) {
      _cacheKey = cacheKey;
      _cachedFile = null;
    }
    if (invalidateCachedArtwork && cacheKey.isNotEmpty) {
      unawaited(_evictThenLoadRemote(api, artwork, generation, cacheKey));
      return;
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

  Future<void> _evictThenLoadRemote(
    MemoryArtworkApi api,
    MemoryArtworkState? artwork,
    int generation,
    String cacheKey,
  ) async {
    try {
      await (widget.cacheEvictor ?? MemoryArtworkCache.manager.removeFile)(cacheKey);
    } catch (_) {
      // A stale local file is never authority for a replacement account.
    }
    if (!mounted || generation != _requestGeneration) return;
    await _loadRemoteResult(api, artwork, generation, loadCachedFile: false);
  }

  Future<void> _loadRemoteResult(
    MemoryArtworkApi api,
    MemoryArtworkState? artwork,
    int generation, {
    bool loadCachedFile = true,
  }) async {
    MemoryArtworkResult result;
    try {
      result = await api.loadForDisplay(widget.conversation.id, enqueueIfMissing: widget.enqueueIfMissing);
    } catch (_) {
      _scheduleRetry(api, artwork, generation);
      return;
    }
    if (!mounted || generation != _requestGeneration) return;
    final readyCacheKey = result.isReady ? result.cacheKey : '';
    setState(() {
      _remoteResult = result;
      if (readyCacheKey.isNotEmpty && readyCacheKey != _cacheKey) {
        _cacheKey = readyCacheKey;
        _cachedFile = null;
      }
    });
    if (loadCachedFile && readyCacheKey.isNotEmpty) {
      unawaited(_loadCachedFile(readyCacheKey, generation));
    }
    if (_shouldRetry(result)) _scheduleRetry(api, artwork, generation, result: result);
  }

  void _handleImageLoadFailure(MemoryArtworkApi api, MemoryArtworkState? artwork, int generation, String cacheKey) {
    if (!mounted || generation != _requestGeneration || _imageRetryScheduled) return;
    _imageRetryScheduled = true;
    unawaited(_recoverImageDownload(api, artwork, generation, cacheKey));
  }

  Future<void> _recoverImageDownload(
    MemoryArtworkApi api,
    MemoryArtworkState? artwork,
    int generation,
    String cacheKey,
  ) async {
    try {
      final evict = widget.cacheEvictor ?? MemoryArtworkCache.manager.removeFile;
      if (cacheKey.isNotEmpty) await evict(cacheKey);
    } catch (_) {
      // A cache eviction failure must not stop signed URL recovery.
    }
    if (!mounted || generation != _requestGeneration) return;
    setState(() {
      _remoteResult = null;
      _cachedFile = null;
    });
    _retryTimer?.cancel();
    _retryTimer = Timer(widget.retryDelay, () {
      _retryTimer = null;
      if (!mounted || generation != _requestGeneration) return;
      _imageRetryScheduled = false;
      unawaited(_loadRemoteResult(api, artwork, generation));
    });
  }

  bool _shouldRetry(MemoryArtworkResult result) {
    if (result.refreshPending) return true;
    if (result.status == MemoryArtworkResultStatus.generating) return true;
    if (result.status != MemoryArtworkResultStatus.unavailable) return false;
    return const {
      'memory_artwork_unavailable',
      'memory_artwork_not_found',
      'memory_artwork_generation_not_queued',
      'memory_artwork_enrichment_not_terminal',
      'memory_artwork_provider_unavailable',
      'memory_artwork_provider_failed',
      // The account/profile notifier can arrive just before the replacement
      // authority is persisted. Retry rather than leaving the visible card
      // blank until the user navigates away and back.
      'memory_artwork_authority_unavailable',
      'memory_artwork_runtime_authority_unavailable',
      'memory_artwork_worker_failed',
      'memory_artwork_finalize_conflict',
      'memory_artwork_object_missing',
      'memory_artwork_storage_failed',
    }.contains(result.failureCode);
  }

  void _scheduleRetry(
    MemoryArtworkApi api,
    MemoryArtworkState? artwork,
    int generation, {
    MemoryArtworkResult? result,
  }) {
    if (!mounted || generation != _requestGeneration || _retryTimer?.isActive == true) return;
    if (result?.failureCode == 'memory_artwork_authority_unavailable') {
      if (_authorityUnavailableRetries >= widget.maxAuthorityUnavailableRetries) return;
      _authorityUnavailableRetries++;
    }
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
        child: KeyedSubtree(
          key: Key('memory-generated-artwork-${widget.conversation.id}'),
          child: CachedNetworkImage(
            imageUrl: result!.url.toString(),
            key: Key('memory-generated-artwork-network-${widget.conversation.id}-${widget.authorityEpoch}'),
            cacheKey: result.cacheKey,
            cacheManager: MemoryArtworkCache.manager,
            fit: widget.fit,
            useOldImageOnUrlChange: true,
            placeholder: (_, __) => _cachedArtworkOrFallback(context, kind: _MemoryArtworkFallbackKind.preparing),
            errorListener: (_) => _handleImageLoadFailure(
              widget.api ?? MemoryArtworkApi(),
              widget.conversation.artwork,
              _requestGeneration,
              result.cacheKey,
            ),
            errorWidget: (_, __, ___) => _cachedArtworkOrFallback(context, kind: _MemoryArtworkFallbackKind.preparing),
          ),
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
