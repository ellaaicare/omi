import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/ella/services/memory_artwork_api.dart';
import 'package:omi/ella/services/memory_artwork_cache.dart';
import 'package:omi/utils/display_text.dart';
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
    this.maxVisibleEnrichmentRetries = 12,
    this.maxImageDownloadRetries = 2,
    this.refreshEpoch = 0,
    this.authorityEpoch = 0,
    this.enqueueIfMissing = false,
    this.allowManualGeneration = false,
    this.prefetchedResult,
    this.deferRemoteFetch = false,
    this.prefetchResolved = false,
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

  /// The newest visible day can race enrichment publication. Recheck that
  /// read-only state for at most one minute without reserving artwork work.
  final int maxVisibleEnrichmentRetries;

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

  /// A day-batch response supplied by the Home collage. It avoids one signed
  /// URL request per visible memory while retaining the same cache path.
  final MemoryArtworkResult? prefetchedResult;

  /// Holds network reads until the parent day batch resolves. Owner-scoped
  /// disk bytes still load immediately.
  final bool deferRemoteFetch;

  /// Distinguishes a successful day response that omitted this memory from a
  /// day request that has not completed yet.
  final bool prefetchResolved;

  static const _automaticGenerationBudgetCapacity = 256;
  static const _automaticPreEgressAttemptLimit = 3;
  static final LinkedHashSet<String> _automaticGenerationAttempts = LinkedHashSet<String>();
  static final Set<String> _automaticGenerationInFlight = <String>{};
  static final LinkedHashMap<String, int> _automaticPreEgressAttempts = LinkedHashMap<String, int>();

  static bool beginAutomaticGeneration(String key) {
    if (key.isEmpty ||
        _automaticGenerationAttempts.contains(key) ||
        (_automaticPreEgressAttempts[key] ?? 0) >= _automaticPreEgressAttemptLimit ||
        !_automaticGenerationInFlight.add(key)) {
      return false;
    }
    return true;
  }

  static void commitAutomaticGeneration(String key) {
    _automaticGenerationInFlight.remove(key);
    _automaticPreEgressAttempts.remove(key);
    if (key.isEmpty || !_automaticGenerationAttempts.add(key)) return;
    while (_automaticGenerationAttempts.length > _automaticGenerationBudgetCapacity) {
      _automaticGenerationAttempts.remove(_automaticGenerationAttempts.first);
    }
  }

  static void releaseAutomaticGeneration(String key, {bool countPreEgressFailure = false}) {
    _automaticGenerationInFlight.remove(key);
    if (!countPreEgressFailure || key.isEmpty) return;
    final attempts = (_automaticPreEgressAttempts.remove(key) ?? 0) + 1;
    _automaticPreEgressAttempts[key] = attempts;
    while (_automaticPreEgressAttempts.length > _automaticGenerationBudgetCapacity) {
      _automaticPreEgressAttempts.remove(_automaticPreEgressAttempts.keys.first);
    }
  }

  @visibleForTesting
  static void resetAutomaticGenerationBudgetForTesting() {
    _automaticGenerationAttempts.clear();
    _automaticGenerationInFlight.clear();
    _automaticPreEgressAttempts.clear();
  }

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
  int _visibleEnrichmentRetries = 0;
  int? _transientRetryBudgetEpoch;
  int? _transientRetryBudgetRefreshEpoch;
  String? _transientRetryBudgetMemoryId;
  int _imageDownloadRetries = 0;
  int? _imageRetryBudgetAuthorityEpoch;
  int? _imageRetryBudgetRefreshEpoch;
  String? _imageRetryBudgetMemoryId;
  bool _manualGenerationInFlight = false;
  double _physicalTargetWidth = 1536;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _refreshRequest();
    });
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
        oldWidget.enqueueIfMissing != widget.enqueueIfMissing ||
        oldWidget.prefetchedResult != widget.prefetchedResult ||
        oldWidget.deferRemoteFetch != widget.deferRemoteFetch ||
        oldWidget.prefetchResolved != widget.prefetchResolved) {
      _refreshRequest();
    }
  }

  @override
  void dispose() {
    _retryTimer?.cancel();
    super.dispose();
  }

  void _refreshRequest() {
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
    _displayCacheKey = cacheKey;
    final resolvedCacheKey = MemoryArtworkCache.resolveDisplayCacheKey(cacheKey);
    final legacyCacheKey = api.legacyCacheKeyForDisplay(
      memoryId: widget.conversation.id,
      styleVersion: artwork?.styleVersion ?? '',
      enrichmentRevision: artwork?.enrichmentRevision ?? '',
    );
    _remoteResult = null;
    if (_cacheKey != resolvedCacheKey) {
      _cacheKey = resolvedCacheKey;
      _cachedFile = null;
    }
    if (resolvedCacheKey.isNotEmpty) {
      unawaited(_loadCachedFile(resolvedCacheKey, generation, legacyCacheKey: legacyCacheKey, api: api));
    } else if (cacheKey.isNotEmpty && api.isDisplayAuthorityCurrent()) {
      unawaited(
        _restoreOwnerScopedCachedFile(
          cacheKey,
          generation,
          legacyCacheKey: legacyCacheKey,
          api: api,
        ),
      );
    }
    final prefetchedResult = widget.prefetchedResult;
    if (prefetchedResult != null) {
      unawaited(_loadRemoteResult(api, artwork, generation, suppliedResult: prefetchedResult));
    } else if (widget.prefetchResolved) {
      _remoteResult = const MemoryArtworkResult(status: MemoryArtworkResultStatus.unavailable);
    } else if (!widget.deferRemoteFetch) {
      unawaited(_loadRemoteResult(api, artwork, generation));
    }
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
    _visibleEnrichmentRetries = 0;
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

  Future<void> _loadCachedFile(
    String cacheKey,
    int generation, {
    String legacyCacheKey = '',
    MemoryArtworkApi? api,
  }) async {
    try {
      final lookup = widget.cachedFileLookup ?? _defaultCachedFileLookup;
      var resolvedKey = cacheKey;
      var file = await lookup(cacheKey);
      if ((file == null || !file.existsSync()) && legacyCacheKey.isNotEmpty && legacyCacheKey != cacheKey) {
        final legacyFile = await lookup(legacyCacheKey);
        if (legacyFile != null && legacyFile.existsSync() && (api?.isDisplayAuthorityCurrent() ?? false)) {
          final migratedKey = await MemoryArtworkCache.rememberDisplayCacheKey(
            provisionalCacheKey: _displayCacheKey,
            authoritativeCacheKey: legacyCacheKey,
            isAuthorityCurrent: () => api?.isDisplayAuthorityCurrent() ?? false,
          );
          if (migratedKey != null) {
            resolvedKey = migratedKey;
            file = legacyFile;
          }
        }
      }
      if (file == null || !file.existsSync()) return;
      if (!mounted || generation != _requestGeneration || cacheKey != _cacheKey) return;
      setState(() {
        _cacheKey = resolvedKey;
        _cachedFile = file;
      });
    } catch (_) {
      // A cache read failure must not block the authenticated network refresh.
    }
  }

  Future<File?> _defaultCachedFileLookup(String cacheKey) async {
    final info = await MemoryArtworkCache.manager.getFileFromCache(cacheKey);
    return info?.file;
  }

  Future<void> _restoreOwnerScopedCachedFile(
    String cacheKey,
    int generation, {
    required String legacyCacheKey,
    required MemoryArtworkApi api,
  }) async {
    final trustedKey = await MemoryArtworkCache.rememberDisplayCacheKey(
      provisionalCacheKey: cacheKey,
      authoritativeCacheKey: cacheKey,
      isAuthorityCurrent: api.isDisplayAuthorityCurrent,
    );
    if (trustedKey == null || !mounted || generation != _requestGeneration) return;
    if (_cacheKey != trustedKey) {
      setState(() => _cacheKey = trustedKey);
    }
    await _loadCachedFile(trustedKey, generation, legacyCacheKey: legacyCacheKey, api: api);
  }

  Future<void> _evictCachedFile(String cacheKey) {
    final cacheEvictor = widget.cacheEvictor;
    if (cacheEvictor != null) return cacheEvictor(cacheKey);
    return MemoryArtworkCache.manager.removeFile(cacheKey);
  }

  void _handleCachedFileDecodeFailure(String cacheKey) {
    if (cacheKey.isEmpty) return;
    final generation = _requestGeneration;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || generation != _requestGeneration || cacheKey != _cacheKey) return;
      unawaited(_discardCorruptedCachedFile(cacheKey, generation));
    });
  }

  Future<void> _discardCorruptedCachedFile(String cacheKey, int generation) async {
    try {
      await _evictCachedFile(cacheKey);
    } catch (_) {
      return;
    }
    if (!mounted || generation != _requestGeneration || cacheKey != _cacheKey) return;
    setState(() => _cachedFile = null);
  }

  Future<void> _loadRemoteResult(
    MemoryArtworkApi api,
    MemoryArtworkState? artwork,
    int generation, {
    bool loadCachedFile = true,
    bool enqueueIfMissing = false,
    MemoryArtworkResult? suppliedResult,
  }) async {
    MemoryArtworkResult result;
    var usedDayBatch = false;
    try {
      // Queue ownership is the source of truth for generation. A card only
      // reads its current state once; it must not multiply GET traffic by
      // doing a private 30-second poll for every visible memory.
      if (suppliedResult != null) {
        usedDayBatch = api.supportsDayArtworkBatch;
        result = suppliedResult;
      } else if (enqueueIfMissing) {
        result = await api.loadForDisplay(widget.conversation.id, enqueueIfMissing: true, pollAttempts: 0);
      } else if (!api.supportsDayArtworkBatch) {
        result = await api.loadForDisplay(widget.conversation.id, pollAttempts: 0);
      } else {
        usedDayBatch = true;
        final createdAt = widget.conversation.createdAt.toLocal();
        final day = await api.fetchDay(
          DateTime(createdAt.year, createdAt.month, createdAt.day),
          utcOffsetMinutes: createdAt.timeZoneOffset.inMinutes,
          authorityRevision: widget.authorityEpoch,
          contentRevision: widget.refreshEpoch,
        );
        result = day?.items[widget.conversation.id] ??
            MemoryArtworkResult(
              status: MemoryArtworkResultStatus.unavailable,
              failureCode: day == null ? 'memory_artwork_transport_unavailable' : '',
            );
      }
      if (!api.supportsDayArtworkBatch &&
          suppliedResult == null &&
          !enqueueIfMissing &&
          widget.enqueueIfMissing &&
          result.isAuthorityCurrent &&
          result.canRequestGeneration) {
        final automaticKey = _automaticGenerationKey(api);
        if (MemoryArtworkImage.beginAutomaticGeneration(automaticKey)) {
          var enqueueAttempted = false;
          try {
            if (result.failureCode == 'memory_artwork_object_missing') {
              result = await api.loadRetryForDisplay(
                widget.conversation.id,
                pollAttempts: 0,
                onEnqueueAttempt: () {
                  enqueueAttempted = true;
                  MemoryArtworkImage.commitAutomaticGeneration(automaticKey);
                },
              );
            } else {
              result = await api.loadAutomaticallyForDisplay(
                widget.conversation.id,
                pollAttempts: 0,
                onEnqueueAttempt: () {
                  enqueueAttempted = true;
                  MemoryArtworkImage.commitAutomaticGeneration(automaticKey);
                },
              );
            }
          } finally {
            if (!enqueueAttempted) {
              MemoryArtworkImage.releaseAutomaticGeneration(
                automaticKey,
                countPreEgressFailure: result.failureCode == 'memory_artwork_transport_unavailable',
              );
            }
          }
        }
      }
    } catch (_) {
      if (!mounted || generation != _requestGeneration) return;
      setState(() {
        _remoteResult = const MemoryArtworkResult(
          status: MemoryArtworkResultStatus.unavailable,
          failureCode: 'memory_artwork_transport_unavailable',
        );
      });
      if (!usedDayBatch) {
        _scheduleRetry(api, artwork, generation, transientTransportFailure: true);
      }
      return;
    }
    if (!mounted || generation != _requestGeneration) return;
    result = result.forPhysicalWidth(_physicalTargetWidth);
    if (!result.isAuthorityCurrent) {
      setState(() {
        _remoteResult = const MemoryArtworkResult(
          status: MemoryArtworkResultStatus.unavailable,
          failureCode: 'memory_artwork_authority_changed',
        );
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
      if (_shouldRetry(result)) _scheduleRetry(api, artwork, generation, result: result);
      return;
    }
    final readyCacheKey = result.isReady ? result.cacheKey : '';
    var publishedReadyCacheKey = readyCacheKey;
    if (readyCacheKey.isNotEmpty) {
      final recoveredDisplayCacheKey = _cacheKeyForDisplay(api, artwork);
      final provisionalCacheKey = recoveredDisplayCacheKey.isNotEmpty ? recoveredDisplayCacheKey : _displayCacheKey;
      final readyCacheKeys = {provisionalCacheKey, readyCacheKey}..removeWhere((cacheKey) => cacheKey.isEmpty);
      await MemoryArtworkCache.evictSuppressedDisplayCacheKeys(readyCacheKeys, _evictCachedFile);
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
      }
    });
    if (loadCachedFile &&
        publishedReadyCacheKey.isNotEmpty &&
        !MemoryArtworkCache.isNetworkOnlyDisplayCacheKey(publishedReadyCacheKey)) {
      unawaited(_loadCachedFile(publishedReadyCacheKey, generation));
    }
    if (!usedDayBatch && _shouldRetry(result)) {
      _scheduleRetry(api, artwork, generation, result: result);
    }
  }

  String _automaticGenerationKey(MemoryArtworkApi api) {
    final sourceRevision = widget.conversation.activeSummaryVersionId?.trim() ?? '';
    return api.automaticGenerationKey(memoryId: widget.conversation.id, sourceRevision: sourceRevision);
  }

  Future<void> _generateArtwork() async {
    if (_manualGenerationInFlight || !_canManuallyGenerate(_remoteResult)) return;
    _retryTimer?.cancel();
    _retryTimer = null;
    _transientRetries = 0;
    _visibleEnrichmentRetries = 0;
    _imageDownloadRetries = 0;
    final generation = ++_requestGeneration;
    final api = widget.api ?? MemoryArtworkApi();
    setState(() {
      _manualGenerationInFlight = true;
      _remoteResult = const MemoryArtworkResult(status: MemoryArtworkResultStatus.generating);
    });
    try {
      await _loadRemoteResult(api, widget.conversation.artwork, generation, enqueueIfMissing: true);
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
    if (!mounted || generation != _requestGeneration) return;
    setState(() {
      _remoteResult = null;
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
        _isVisibleEnrichmentPending(result) ||
        _isAuthorityUnavailable(result) ||
        _isTransportUnavailable(result);
  }

  bool _isVisibleEnrichmentPending(MemoryArtworkResult? result) =>
      widget.enqueueIfMissing && result?.failureCode == 'memory_artwork_enrichment_not_terminal';

  bool _isAuthorityUnavailable(MemoryArtworkResult? result) {
    return const {
      'memory_artwork_authority_unavailable',
      'memory_artwork_runtime_authority_unavailable',
    }.contains(result?.failureCode);
  }

  bool _isTransportUnavailable(MemoryArtworkResult? result) =>
      result?.failureCode == 'memory_artwork_transport_unavailable';

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
    } else if (_isVisibleEnrichmentPending(result)) {
      if (_visibleEnrichmentRetries >= widget.maxVisibleEnrichmentRetries) return;
      _visibleEnrichmentRetries++;
    } else if (transientTransportFailure ||
        _isTransportUnavailable(result) ||
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
    return LayoutBuilder(
      builder: (context, constraints) {
        final logicalWidth = constraints.maxWidth.isFinite && constraints.maxWidth > 0 ? constraints.maxWidth : 512.0;
        _physicalTargetWidth = logicalWidth * MediaQuery.devicePixelRatioOf(context);
        return _buildArtwork(context);
      },
    );
  }

  Widget _buildArtwork(BuildContext context) {
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
        cacheWidth: result.selectedVariantWidth ?? result.pixelWidth,
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
      memCacheWidth: result.selectedVariantWidth ?? result.pixelWidth,
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
      'memory_artwork_consent_required',
      'memory_artwork_deletion_pending',
      'memory_artwork_discarded',
      'memory_artwork_memory_not_found',
      'memory_artwork_sensitive_source_excluded',
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
        cacheWidth: _decodeWidth,
        gaplessPlayback: true,
        errorBuilder: (_, __, ___) {
          _handleCachedFileDecodeFailure(_cacheKey);
          return _fallback(context, kind: kind);
        },
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

  Widget _sourcePhotoFallback(BuildContext context, Uint8List bytes, {required _MemoryArtworkFallbackKind kind}) {
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
    return _fallbackImageWithAction(context, photo, kind: kind);
  }

  Widget _fallbackImageWithAction(BuildContext context, Widget image, {required _MemoryArtworkFallbackKind kind}) {
    final isPreparing = kind == _MemoryArtworkFallbackKind.preparing;
    final canGenerate = !isPreparing && !_manualGenerationInFlight && _canManuallyGenerate(_remoteResult);
    if (!isPreparing && !canGenerate) return image;
    return LayoutBuilder(
      builder: (context, constraints) => Stack(
        fit: StackFit.expand,
        children: [
          image,
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
            _photoRetryAction(compact: constraints.maxWidth < 180),
        ],
      ),
    );
  }

  Widget _photoRetryAction({required bool compact}) {
    final key = Key('memory-artwork-photo-retry-${widget.conversation.id}');
    if (compact) {
      return Positioned(
        right: 8,
        bottom: 8,
        child: Semantics(
          label: context.l10n.memoryArtworkRetry,
          button: true,
          child: Material(
            color: const Color(0xF2F8F2E8),
            shape: const CircleBorder(),
            elevation: 1,
            clipBehavior: Clip.antiAlias,
            child: InkWell(
              key: key,
              onTap: _generateArtwork,
              child: const SizedBox(
                width: 44,
                height: 44,
                child: Icon(Icons.auto_awesome_outlined, color: Color(0xFF3A776A), size: 20),
              ),
            ),
          ),
        ),
      );
    }
    return Positioned(
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
            key: key,
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
                    style: const TextStyle(color: Color(0xFF315F55), fontSize: 12, fontWeight: FontWeight.w700),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _placeholder(_MemoryArtworkFallbackKind kind) {
    final isPreparing = kind == _MemoryArtworkFallbackKind.preparing;
    final canGenerate = !isPreparing && !_manualGenerationInFlight && _canManuallyGenerate(_remoteResult);
    final useCompactLayout = MediaQuery.textScalerOf(context).scale(12) > 18;
    final category = widget.conversation.structured.category.trim();
    final emoji = parseEllaDisplayValue(widget.conversation.structured.emoji).text.trim();
    final fallbackTitle = safeMemoryDisplayTitle(widget.conversation.structured.title, category);
    final semanticsLabel = fallbackTitle.isEmpty ? context.l10n.untitledConversation : fallbackTitle;
    final visibleLabel = fallbackTitle.isEmpty ? context.l10n.untitledConversation : fallbackTitle;
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
              else if (emoji.isNotEmpty)
                Text(emoji, style: const TextStyle(fontSize: 28))
              else
                Icon(
                  canGenerate ? Icons.auto_awesome_outlined : Icons.auto_stories_outlined,
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
          child: Ink(decoration: decoration, child: content),
        ),
      ),
    );
  }

  int get _decodeWidth {
    final target = _physicalTargetWidth.isFinite && _physicalTargetWidth > 0 ? _physicalTargetWidth.ceil() : 1536;
    for (final width in const [384, 768, 1536]) {
      if (width >= target) return width;
    }
    return 1536;
  }
}
