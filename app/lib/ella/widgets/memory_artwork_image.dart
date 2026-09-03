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

bool _artworkReadinessChanged(ServerConversation previous, ServerConversation current) {
  String? enrichmentValue(ServerConversation conversation, String key) =>
      conversation.enrichmentState?[key]?.toString();

  return previous.status != current.status ||
      previous.activeSummaryVersionId != current.activeSummaryVersionId ||
      enrichmentValue(previous, 'status') != enrichmentValue(current, 'status') ||
      enrichmentValue(previous, 'canonical_status') != enrichmentValue(current, 'canonical_status') ||
      enrichmentValue(previous, 'pending') != enrichmentValue(current, 'pending');
}

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
    this.maxTransientRetries = 3,
    this.maxImageDownloadRetries = 2,
    this.refreshEpoch = 0,
    this.authorityEpoch = 0,
    this.enqueueIfMissing = false,
    this.allowManualGeneration = false,
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

  /// Display reads never create artwork. A visible in-progress result or a
  /// transport failure gets only this many follow-up reads before the parent
  /// queue revision or an explicit refresh must ask again.
  final int maxTransientRetries;

  /// A failed signed URL or corrupted cached image gets a small number of
  /// recovery reads. Further failures wait for a parent queue refresh or a new
  /// account authority instead of continuously fetching artwork the user
  /// cannot display.
  final int maxImageDownloadRetries;

  /// A parent-owned queue completion revision. It refreshes visible cards after
  /// the server finishes a batch without letting scrolling create new jobs.
  final int refreshEpoch;

  /// An account/profile change invalidates signed URLs and disk bytes even if
  /// a memory id happens to collide across authorities.
  final int authorityEpoch;
  final bool enqueueIfMissing;

  /// Offers an explicit, single-memory generation action after an
  /// authenticated display read confirms that artwork is unavailable.
  /// Passive list rendering remains read-only.
  final bool allowManualGeneration;

  @override
  State<MemoryArtworkImage> createState() => _MemoryArtworkImageState();
}

class _MemoryArtworkImageState extends State<MemoryArtworkImage> {
  MemoryArtworkResult? _remoteResult;
  File? _cachedFile;
  String _displayCacheKey = '';
  String _cacheKey = '';
  int _requestGeneration = 0;
  Timer? _retryTimer;
  bool _imageRetryScheduled = false;
  int _authorityUnavailableRetries = 0;
  int? _authorityRetryBudgetEpoch;
  String? _authorityRetryBudgetMemoryId;
  bool _authorityRetryBudgetExhausted = false;
  int _transientRetries = 0;
  int? _transientRetryBudgetEpoch;
  int? _transientRetryBudgetRefreshEpoch;
  String? _transientRetryBudgetMemoryId;
  int _imageDownloadRetries = 0;
  int? _imageRetryBudgetAuthorityEpoch;
  int? _imageRetryBudgetRefreshEpoch;
  String? _imageRetryBudgetMemoryId;
  bool _manualGenerationInFlight = false;

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
        _artworkReadinessChanged(oldWidget.conversation, widget.conversation) ||
        oldWidget.refreshEpoch != widget.refreshEpoch ||
        oldWidget.authorityEpoch != widget.authorityEpoch ||
        oldWidget.enqueueIfMissing != widget.enqueueIfMissing) {
      _refreshRequest(invalidateCachedArtwork: oldWidget.authorityEpoch != widget.authorityEpoch);
    }
  }

  @override
  void dispose() {
    _retryTimer?.cancel();
    super.dispose();
  }

  void _refreshRequest({bool invalidateCachedArtwork = false}) {
    _manualGenerationInFlight = false;
    _resetAuthorityRetryBudgetIfNeeded();
    _resetTransientRetryBudgetIfNeeded();
    _resetImageRetryBudgetIfNeeded();
    // A persistent authority failure remains quiet until its authority changes.
    // Once the final bounded retry succeeds, however, later queue revisions
    // are valid and must be allowed to refresh the card.
    if (_authorityRetryBudgetExhausted && _isAuthorityUnavailable(_remoteResult)) return;
    _retryTimer?.cancel();
    _retryTimer = null;
    _imageRetryScheduled = false;
    final generation = ++_requestGeneration;
    final api = widget.api ?? MemoryArtworkApi();
    final artwork = widget.conversation.artwork;
    final cacheKey = _cacheKeyForDisplay(api, artwork);
    final previousDisplayCacheKey = _displayCacheKey;
    if (invalidateCachedArtwork) {
      MemoryArtworkCache.forgetDisplayCacheKey(previousDisplayCacheKey);
      MemoryArtworkCache.forgetDisplayCacheKey(cacheKey);
    }
    _displayCacheKey = cacheKey;
    final resolvedCacheKey = invalidateCachedArtwork ? cacheKey : MemoryArtworkCache.resolveDisplayCacheKey(cacheKey);
    _remoteResult = null;
    if (_cacheKey != resolvedCacheKey || invalidateCachedArtwork) {
      _cacheKey = resolvedCacheKey;
      _cachedFile = null;
    }
    if (invalidateCachedArtwork && resolvedCacheKey.isNotEmpty) {
      unawaited(_evictThenLoadRemote(api, artwork, generation, resolvedCacheKey));
      return;
    }
    if (resolvedCacheKey.isNotEmpty) {
      unawaited(_loadCachedFile(resolvedCacheKey, generation));
    }
    unawaited(_loadRemoteResult(api, artwork, generation));
  }

  String _cacheKeyForDisplay(MemoryArtworkApi api, MemoryArtworkState? artwork) {
    return api.cacheKeyForDisplay(
      memoryId: widget.conversation.id,
      styleVersion: artwork?.styleVersion ?? '',
      enrichmentRevision: artwork?.enrichmentRevision ?? '',
    );
  }

  void _resetAuthorityRetryBudgetIfNeeded() {
    final memoryId = widget.conversation.id;
    if (_authorityRetryBudgetEpoch == widget.authorityEpoch && _authorityRetryBudgetMemoryId == memoryId) return;
    _authorityRetryBudgetEpoch = widget.authorityEpoch;
    _authorityRetryBudgetMemoryId = memoryId;
    _authorityUnavailableRetries = 0;
    _authorityRetryBudgetExhausted = false;
  }

  void _resetTransientRetryBudgetIfNeeded() {
    final memoryId = widget.conversation.id;
    if (_transientRetryBudgetEpoch == widget.authorityEpoch &&
        _transientRetryBudgetRefreshEpoch == widget.refreshEpoch &&
        _transientRetryBudgetMemoryId == memoryId) {
      return;
    }
    _transientRetryBudgetEpoch = widget.authorityEpoch;
    _transientRetryBudgetRefreshEpoch = widget.refreshEpoch;
    _transientRetryBudgetMemoryId = memoryId;
    _transientRetries = 0;
  }

  void _resetImageRetryBudgetIfNeeded() {
    final memoryId = widget.conversation.id;
    if (_imageRetryBudgetAuthorityEpoch == widget.authorityEpoch &&
        _imageRetryBudgetRefreshEpoch == widget.refreshEpoch &&
        _imageRetryBudgetMemoryId == memoryId) {
      return;
    }
    _imageRetryBudgetAuthorityEpoch = widget.authorityEpoch;
    _imageRetryBudgetRefreshEpoch = widget.refreshEpoch;
    _imageRetryBudgetMemoryId = memoryId;
    _imageDownloadRetries = 0;
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

  Future<void> _evictCachedFile(String cacheKey) {
    final cacheEvictor = widget.cacheEvictor;
    if (cacheEvictor != null) return cacheEvictor(cacheKey);
    return MemoryArtworkCache.manager.removeFile(cacheKey);
  }

  Future<void> _evictThenLoadRemote(
    MemoryArtworkApi api,
    MemoryArtworkState? artwork,
    int generation,
    String cacheKey,
  ) async {
    try {
      await _evictCachedFile(cacheKey);
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
    bool enqueueIfMissing = false,
  }) async {
    MemoryArtworkResult result;
    try {
      // Queue ownership is the source of truth for generation. A card only
      // reads its current state once; it must not multiply GET traffic by
      // doing a private 30-second poll for every visible memory.
      result = await api.loadForDisplay(
        widget.conversation.id,
        enqueueIfMissing: widget.enqueueIfMissing || enqueueIfMissing,
        pollAttempts: 0,
      );
    } catch (_) {
      if (!mounted || generation != _requestGeneration) return;
      setState(() {
        _remoteResult = const MemoryArtworkResult(
          status: MemoryArtworkResultStatus.unavailable,
          failureCode: 'memory_artwork_transport_unavailable',
        );
      });
      _scheduleRetry(api, artwork, generation, transientTransportFailure: true);
      return;
    }
    if (!mounted || generation != _requestGeneration) return;
    if (!result.isAuthorityCurrent) {
      setState(() {
        _remoteResult = const MemoryArtworkResult(
          status: MemoryArtworkResultStatus.unavailable,
          failureCode: 'memory_artwork_authority_changed',
        );
        _cachedFile = null;
      });
      return;
    }
    if (_mustSuppressCachedArtwork(result)) {
      final suppressedCacheKeys = {_displayCacheKey, _cacheKey}..removeWhere((cacheKey) => cacheKey.isEmpty);
      MemoryArtworkCache.suppressDisplayCacheKeys(suppressedCacheKeys);
      setState(() {
        _remoteResult = result;
        _cachedFile = null;
        _cacheKey = _displayCacheKey;
      });
      unawaited(_evictSuppressedCachedArtwork(suppressedCacheKeys));
      return;
    }
    final readyCacheKey = result.isReady ? result.cacheKey : '';
    var publishedReadyCacheKey = readyCacheKey;
    if (readyCacheKey.isNotEmpty) {
      final recoveredDisplayCacheKey = _cacheKeyForDisplay(api, artwork);
      final provisionalCacheKey = recoveredDisplayCacheKey.isNotEmpty ? recoveredDisplayCacheKey : _displayCacheKey;
      final readyCacheKeys = {provisionalCacheKey, readyCacheKey}..removeWhere((cacheKey) => cacheKey.isEmpty);
      await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(
        readyCacheKeys,
        _evictCachedFile,
      );
      if (!mounted || generation != _requestGeneration || !result.isAuthorityCurrent) return;
      final rememberedCacheKey = await MemoryArtworkCache.rememberDisplayCacheKey(
        provisionalCacheKey: provisionalCacheKey,
        authoritativeCacheKey: readyCacheKey,
        isAuthorityCurrent: () => result.isAuthorityCurrent,
      );
      if (!mounted || generation != _requestGeneration || !result.isAuthorityCurrent) return;
      if (rememberedCacheKey == null) {
        setState(() {
          _remoteResult = const MemoryArtworkResult(
            status: MemoryArtworkResultStatus.unavailable,
            failureCode: 'memory_artwork_cache_cleanup_unavailable',
          );
          _cachedFile = null;
        });
        _scheduleRetry(api, artwork, generation, transientTransportFailure: true);
        return;
      }
      publishedReadyCacheKey = rememberedCacheKey;
      _displayCacheKey = provisionalCacheKey;
    }
    setState(() {
      _remoteResult = result;
      if (publishedReadyCacheKey.isNotEmpty && publishedReadyCacheKey != _cacheKey) {
        _cacheKey = publishedReadyCacheKey;
        _cachedFile = null;
      }
    });
    if (loadCachedFile &&
        publishedReadyCacheKey.isNotEmpty &&
        !MemoryArtworkCache.isNetworkOnlyDisplayCacheKey(publishedReadyCacheKey)) {
      unawaited(_loadCachedFile(publishedReadyCacheKey, generation));
    }
    if (_shouldRetry(result)) _scheduleRetry(api, artwork, generation, result: result);
  }

  Future<void> _generateArtwork() async {
    if (_manualGenerationInFlight || !_canManuallyGenerate(_remoteResult)) return;
    _retryTimer?.cancel();
    _retryTimer = null;
    _transientRetries = 0;
    _imageDownloadRetries = 0;
    final generation = ++_requestGeneration;
    final api = widget.api ?? MemoryArtworkApi();
    setState(() {
      _manualGenerationInFlight = true;
      _remoteResult = const MemoryArtworkResult(status: MemoryArtworkResultStatus.generating);
    });
    try {
      await _loadRemoteResult(
        api,
        widget.conversation.artwork,
        generation,
        enqueueIfMissing: true,
      );
    } finally {
      if (mounted && generation == _requestGeneration) {
        setState(() => _manualGenerationInFlight = false);
      }
    }
  }

  Future<void> _evictSuppressedCachedArtwork(Set<String> cacheKeys) async {
    await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(cacheKeys, _evictCachedFile);
  }

  void _handleImageLoadFailure(MemoryArtworkApi api, MemoryArtworkState? artwork, int generation, String cacheKey) {
    if (!mounted || generation != _requestGeneration || _imageRetryScheduled) return;
    if (_imageDownloadRetries >= widget.maxImageDownloadRetries) {
      setState(() {
        _cachedFile = null;
        _remoteResult = const MemoryArtworkResult(
          status: MemoryArtworkResultStatus.unavailable,
          failureCode: 'memory_artwork_download_unavailable',
        );
      });
      return;
    }
    _imageDownloadRetries++;
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
      if (cacheKey.isNotEmpty && !MemoryArtworkCache.isNetworkOnlyDisplayCacheKey(cacheKey)) {
        await _evictCachedFile(cacheKey);
      }
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
    return result.refreshPending ||
        result.status == MemoryArtworkResultStatus.generating ||
        _isAuthorityUnavailable(result);
  }

  bool _isAuthorityUnavailable(MemoryArtworkResult? result) {
    return const {
      'memory_artwork_authority_unavailable',
      'memory_artwork_runtime_authority_unavailable',
    }.contains(result?.failureCode);
  }

  void _scheduleRetry(
    MemoryArtworkApi api,
    MemoryArtworkState? artwork,
    int generation, {
    MemoryArtworkResult? result,
    bool transientTransportFailure = false,
  }) {
    if (!mounted || generation != _requestGeneration || _retryTimer?.isActive == true) return;
    if (_isAuthorityUnavailable(result)) {
      if (_authorityUnavailableRetries >= widget.maxAuthorityUnavailableRetries) {
        _authorityRetryBudgetExhausted = true;
        return;
      }
      _authorityUnavailableRetries++;
    } else if (transientTransportFailure ||
        result?.refreshPending == true ||
        result?.status == MemoryArtworkResultStatus.generating) {
      if (_transientRetries >= widget.maxTransientRetries) return;
      _transientRetries++;
    } else {
      return;
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
          child: _readyNetworkArtwork(context, result!),
        ),
      );
    }
    final fallbackKind =
        result == null || result.status == MemoryArtworkResultStatus.generating || result.refreshPending
            ? _MemoryArtworkFallbackKind.preparing
            : _MemoryArtworkFallbackKind.unavailable;
    return _cachedArtworkOrFallback(context, kind: fallbackKind);
  }

  Widget _readyNetworkArtwork(BuildContext context, MemoryArtworkResult result) {
    final imageKey = Key('memory-generated-artwork-network-${widget.conversation.id}-${widget.authorityEpoch}');
    if (MemoryArtworkCache.isNetworkOnlyDisplayCacheKey(_cacheKey)) {
      final generation = _requestGeneration;
      return Image.network(
        result.url.toString(),
        key: imageKey,
        fit: widget.fit,
        gaplessPlayback: true,
        frameBuilder: (_, child, frame, __) =>
            frame == null ? _cachedArtworkOrFallback(context, kind: _MemoryArtworkFallbackKind.preparing) : child,
        errorBuilder: (_, __, ___) {
          WidgetsBinding.instance.addPostFrameCallback((_) {
            _handleImageLoadFailure(
              widget.api ?? MemoryArtworkApi(),
              widget.conversation.artwork,
              generation,
              _cacheKey,
            );
          });
          return _cachedArtworkOrFallback(context, kind: _MemoryArtworkFallbackKind.preparing);
        },
      );
    }
    return CachedNetworkImage(
      imageUrl: result.url.toString(),
      key: imageKey,
      cacheKey: _cacheKey,
      cacheManager: MemoryArtworkCache.manager,
      fit: widget.fit,
      useOldImageOnUrlChange: true,
      placeholder: (_, __) => _cachedArtworkOrFallback(context, kind: _MemoryArtworkFallbackKind.preparing),
      errorListener: (_) => _handleImageLoadFailure(
        widget.api ?? MemoryArtworkApi(),
        widget.conversation.artwork,
        _requestGeneration,
        _cacheKey,
      ),
      errorWidget: (_, __, ___) => _cachedArtworkOrFallback(context, kind: _MemoryArtworkFallbackKind.preparing),
    );
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
      'memory_artwork_enrichment_not_terminal',
      'memory_artwork_memory_not_found',
      'memory_artwork_preference_authority_stale',
      'memory_artwork_release_disabled',
      'memory_artwork_sensitive_source_excluded',
      'memory_artwork_source_stale',
    }.contains(result.failureCode);
  }

  bool _canManuallyGenerate(MemoryArtworkResult? result) {
    return widget.allowManualGeneration && result?.canRequestGeneration == true;
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
      return _sourcePhotoFallback(context, bytes, kind: kind);
    }
    return _placeholder(kind);
  }

  Widget _sourcePhotoFallback(
    BuildContext context,
    Uint8List bytes, {
    required _MemoryArtworkFallbackKind kind,
  }) {
    final isPreparing = kind == _MemoryArtworkFallbackKind.preparing;
    final canGenerate = !isPreparing && !_manualGenerationInFlight && _canManuallyGenerate(_remoteResult);
    final photo = Semantics(
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
    if (!isPreparing && !canGenerate) return photo;
    return Stack(
      fit: StackFit.expand,
      children: [
        photo,
        if (isPreparing)
          Center(
            child: Semantics(
              label: context.l10n.memoryArtworkPreparingLabel,
              child: Material(
                color: const Color(0xE6F8F2E8),
                shape: const CircleBorder(),
                child: Padding(
                  padding: const EdgeInsets.all(10),
                  child: SizedBox(
                    key: Key('memory-artwork-generation-progress-${widget.conversation.id}'),
                    width: 38,
                    height: 38,
                    child: const Stack(
                      alignment: Alignment.center,
                      children: [
                        CircularProgressIndicator(strokeWidth: 2.5, color: Color(0xFF3A776A)),
                        Icon(Icons.auto_awesome_rounded, color: Color(0xFF57736A), size: 17),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          )
        else if (canGenerate)
          Positioned(
            right: 12,
            bottom: 12,
            child: Semantics(
              label: context.l10n.memoryArtworkRetry,
              button: true,
              child: Material(
                color: const Color(0xF2F8F2E8),
                borderRadius: BorderRadius.circular(24),
                elevation: 1,
                child: InkWell(
                  key: Key('memory-artwork-photo-retry-${widget.conversation.id}'),
                  borderRadius: BorderRadius.circular(24),
                  onTap: _generateArtwork,
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        const Icon(Icons.auto_awesome_outlined, color: Color(0xFF3A776A), size: 18),
                        const SizedBox(width: 6),
                        Text(
                          context.l10n.memoryArtworkRetry,
                          style: const TextStyle(
                            color: Color(0xFF315F55),
                            fontSize: 12,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
      ],
    );
  }

  Widget _placeholder(_MemoryArtworkFallbackKind kind) {
    final isPreparing = kind == _MemoryArtworkFallbackKind.preparing;
    final canGenerate = !isPreparing && !_manualGenerationInFlight && _canManuallyGenerate(_remoteResult);
    final useCompactLayout = MediaQuery.textScalerOf(context).scale(12) > 18;
    final semanticsLabel = isPreparing
        ? context.l10n.memoryArtworkPreparingLabel
        : canGenerate
            ? context.l10n.memoryArtworkRetry
            : context.l10n.memoryArtworkUnavailableLabel;
    final visibleLabel = isPreparing
        ? context.l10n.memoryArtworkPreparingShort
        : canGenerate
            ? context.l10n.memoryArtworkRetry
            : context.l10n.memoryArtworkUnavailableLabel;
    const decoration = BoxDecoration(
      gradient: LinearGradient(
        begin: Alignment.topLeft,
        end: Alignment.bottomRight,
        colors: [Color(0xFFE9E3D8), Color(0xFFDCE9E3)],
      ),
    );
    final content = Center(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (!useCompactLayout) ...[
              if (isPreparing)
                SizedBox(
                  key: Key('memory-artwork-generation-progress-${widget.conversation.id}'),
                  width: 38,
                  height: 38,
                  child: const Stack(
                    alignment: Alignment.center,
                    children: [
                      CircularProgressIndicator(strokeWidth: 2.5, color: Color(0xFF3A776A)),
                      Icon(Icons.auto_awesome_rounded, color: Color(0xFF57736A), size: 17),
                    ],
                  ),
                )
              else
                Icon(
                  canGenerate ? Icons.auto_awesome_outlined : Icons.brush_outlined,
                  color: const Color(0xFF57736A),
                  size: 30,
                ),
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
    );
    if (!canGenerate) {
      return Semantics(
        label: semanticsLabel,
        child: DecoratedBox(
          key: Key('memory-artwork-placeholder-${widget.conversation.id}'),
          decoration: decoration,
          child: content,
        ),
      );
    }
    return Semantics(
      label: semanticsLabel,
      button: true,
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          key: Key('memory-artwork-placeholder-${widget.conversation.id}'),
          onTap: _generateArtwork,
          child: Ink(
            decoration: decoration,
            child: content,
          ),
        ),
      ),
    );
  }
}
