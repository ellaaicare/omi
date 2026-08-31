import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:http/http.dart' as http;

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

const memoryArtworkSchemaVersion = 'ella.memory_artwork.v1';
const memoryArtworkDefaultStyle = 'ella.memory_artwork.style.soft-gouache.v1';
const memoryArtworkPaperCollageStyle = 'ella.memory_artwork.style.paper-collage.v1';
const memoryArtworkGraphicLandscapeStyle = 'ella.memory_artwork.style.graphic-landscape.v1';
const memoryArtworkWatercolorJournalStyle = 'ella.memory_artwork.style.watercolor-journal.v1';
const memoryArtworkAnimeStorybookStyle = 'ella.memory_artwork.style.anime-storybook.v1';
const memoryArtworkCinematicStillStyle = 'ella.memory_artwork.style.cinematic-still.v1';

typedef MemoryArtworkHttpCall = Future<http.Response?> Function({
  required String url,
  required Map<String, String> headers,
  required String body,
  required String method,
  Duration? timeout,
  int? retries,
  bool? requireAuthCheck,
  String? expectedAuthenticatedUid,
  ExactAccountAuthorityVerifier? exactAuthority,
});
typedef MemoryArtworkAuthorityProvider = ExactAccountAuthorityVerifier? Function();

enum MemoryArtworkResultStatus { generating, ready, unavailable, declined }

enum MemoryArtworkQueueState { running, paused, cancelled, completed, needsAttention }

enum MemoryArtworkQueueAction { pause, resume, cancel }

/// Full historical regeneration is opt-in because it can consume a meaningful
/// image allowance. A preview always targets one bounded recent page.
enum MemoryArtworkBackfillMode { preview, all }

class MemoryArtworkResult {
  const MemoryArtworkResult({
    required this.status,
    this.url,
    this.cacheKey = '',
    this.styleVersion = '',
    this.enrichmentRevision = '',
    this.failureCode = '',
    this.refreshPending = false,
    this.refreshFailureCode = '',
    this.requestedStyleVersion = '',
  });

  final MemoryArtworkResultStatus status;
  final Uri? url;
  final String cacheKey;
  final String styleVersion;
  final String enrichmentRevision;
  final String failureCode;
  final bool refreshPending;
  final String refreshFailureCode;
  final String requestedStyleVersion;

  bool get isReady => status == MemoryArtworkResultStatus.ready && url != null;
}

class MemoryArtworkPreferences {
  const MemoryArtworkPreferences({
    required this.consent,
    required this.consentVersion,
    required this.styleVersion,
    required this.releaseEnabled,
  });

  final String consent;
  final String consentVersion;
  final String styleVersion;
  final bool releaseEnabled;
}

class MemoryArtworkPreferenceUpdate {
  const MemoryArtworkPreferenceUpdate({required this.saved, this.failureCode = ''});

  final bool saved;
  final String failureCode;
}

class MemoryArtworkBackfillPage {
  const MemoryArtworkBackfillPage({
    required this.queued,
    required this.existing,
    required this.skipped,
    required this.hasMore,
    this.nextCursor,
    this.mode = MemoryArtworkBackfillMode.preview,
  });

  final int queued;
  final int existing;
  final int skipped;
  final bool hasMore;
  final String? nextCursor;
  final MemoryArtworkBackfillMode mode;
}

class MemoryArtworkStyleProgress {
  const MemoryArtworkStyleProgress({
    required this.styleVersion,
    required this.state,
    required this.ready,
    required this.active,
    required this.queued,
    required this.retrying,
    required this.failed,
    required this.total,
    required this.remaining,
  });

  final String styleVersion;
  final MemoryArtworkQueueState state;
  final int ready;
  final int active;
  final int queued;
  final int retrying;
  final int failed;
  final int total;
  final int remaining;

  double get progress => total == 0 ? 0 : (ready / total).clamp(0, 1);
}

class MemoryArtworkQueueStatus extends MemoryArtworkStyleProgress {
  const MemoryArtworkQueueStatus({
    required this.generationId,
    required super.styleVersion,
    required super.state,
    required this.controlState,
    required this.scanStatus,
    required this.scanned,
    required this.pagesProcessed,
    required this.autoContinue,
    required this.batchSize,
    required this.batchRemaining,
    required this.pauseReason,
    required super.ready,
    required super.active,
    required super.queued,
    required super.retrying,
    required super.failed,
    required super.total,
    required super.remaining,
    required this.styles,
    this.updatedAt,
  });

  final String generationId;
  final MemoryArtworkQueueState controlState;
  final String scanStatus;
  final int scanned;
  final int pagesProcessed;
  final bool autoContinue;
  final int batchSize;
  final int batchRemaining;
  final String pauseReason;
  final List<MemoryArtworkStyleProgress> styles;
  final DateTime? updatedAt;

  bool get canPause => controlState == MemoryArtworkQueueState.running && (remaining > 0 || scanStatus != 'completed');
  bool get canResume =>
      controlState == MemoryArtworkQueueState.paused || controlState == MemoryArtworkQueueState.cancelled;
  bool get canCancel =>
      controlState != MemoryArtworkQueueState.cancelled && (remaining > 0 || scanStatus != 'completed');
}

class MemoryArtworkApi {
  static const int _maxHistoricalBatchSize = 10;
  MemoryArtworkApi({
    MemoryArtworkHttpCall request = makeApiCall,
    MemoryArtworkAuthorityProvider authorityProvider = WalOwnerAuthority.active,
    String? baseUrl,
  })  : _request = request,
        _authorityProvider = authorityProvider,
        _baseUrl = baseUrl?.trim() ?? '';

  final MemoryArtworkHttpCall _request;
  final MemoryArtworkAuthorityProvider _authorityProvider;
  final String _baseUrl;

  String cacheKeyForDisplay({
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) {
    final authority = _authorityProvider();
    if (authority == null || memoryId.trim().isEmpty || !authority.isExactCurrent()) return '';
    return _cacheKey(
      authority: authority,
      memoryId: memoryId,
      styleVersion: styleVersion,
      enrichmentRevision: enrichmentRevision,
    );
  }

  Future<MemoryArtworkResult> fetch(String memoryId) async {
    final authority = _authorityProvider();
    if (authority == null || memoryId.trim().isEmpty) return _unavailable('memory_artwork_authority_unavailable');
    return _fetchWithAuthority(authority, memoryId);
  }

  Future<MemoryArtworkResult> loadForDisplay(
    String memoryId, {
    bool enqueueIfMissing = false,
    int pollAttempts = 10,
    Duration pollInterval = const Duration(seconds: 3),
  }) async {
    final authority = _authorityProvider();
    if (authority == null || memoryId.trim().isEmpty) return _unavailable('memory_artwork_authority_unavailable');

    var result = await _fetchWithAuthority(authority, memoryId);
    if (result.isReady || result.status == MemoryArtworkResultStatus.declined || !authority.isExactCurrent()) {
      return result;
    }
    if (enqueueIfMissing && result.status == MemoryArtworkResultStatus.unavailable) {
      result = await _enqueueWithAuthority(authority, memoryId);
      if (result.status != MemoryArtworkResultStatus.generating || !authority.isExactCurrent()) {
        return result;
      }
    }
    if (result.status != MemoryArtworkResultStatus.generating) return result;

    for (var attempt = 0; attempt < pollAttempts && authority.isExactCurrent(); attempt++) {
      await Future<void>.delayed(pollInterval);
      if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
      result = await _fetchWithAuthority(authority, memoryId);
      if (result.status != MemoryArtworkResultStatus.generating) return result;
    }
    return result;
  }

  Future<MemoryArtworkResult> _fetchWithAuthority(ExactAccountAuthorityVerifier authority, String memoryId) async {
    if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
    final response = await _call(
      authority,
      method: 'GET',
      path: 'v1/ella/memories/${Uri.encodeComponent(memoryId)}/artwork',
    );
    if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
    if (response == null || response.statusCode != 200) return _unavailable(_safeFailureCode(response?.body));
    final payload = _jsonObject(response.body);
    if (payload == null || payload['schema_version'] != memoryArtworkSchemaVersion) {
      return _unavailable('memory_artwork_response_invalid');
    }
    final status = MemoryArtworkResultStatus.values.asNameMap()[payload['status']?.toString() ?? ''] ??
        MemoryArtworkResultStatus.unavailable;
    final rawUrl = payload['url']?.toString().trim() ?? '';
    final url = Uri.tryParse(rawUrl);
    if (status == MemoryArtworkResultStatus.ready && (url == null || url.scheme != 'https' || url.host.isEmpty)) {
      return _unavailable('memory_artwork_url_invalid');
    }
    return MemoryArtworkResult(
      status: status,
      url: status == MemoryArtworkResultStatus.ready ? url : null,
      cacheKey: status == MemoryArtworkResultStatus.ready
          ? _cacheKey(
              authority: authority,
              memoryId: memoryId,
              styleVersion: payload['style_version']?.toString().trim() ?? '',
              enrichmentRevision: payload['enrichment_revision']?.toString().trim() ?? '',
            )
          : '',
      styleVersion: payload['style_version']?.toString().trim() ?? '',
      enrichmentRevision: payload['enrichment_revision']?.toString().trim() ?? '',
      failureCode: payload['failure_code']?.toString().trim() ?? '',
      refreshPending: payload['refresh_pending'] == true,
      refreshFailureCode: payload['refresh_failure_code']?.toString().trim() ?? '',
      requestedStyleVersion: payload['requested_style_version']?.toString().trim() ?? '',
    );
  }

  Future<MemoryArtworkResult> _enqueueWithAuthority(ExactAccountAuthorityVerifier authority, String memoryId) async {
    if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
    final response = await _call(
      authority,
      method: 'POST',
      path: 'v1/ella/memories/${Uri.encodeComponent(memoryId)}/artwork',
    );
    if (!authority.isExactCurrent()) return _unavailable('memory_artwork_authority_changed');
    if (response == null || response.statusCode < 200 || response.statusCode >= 300) {
      return _unavailable(_safeFailureCode(response?.body));
    }
    final payload = _jsonObject(response.body);
    final status = MemoryArtworkResultStatus.values.asNameMap()[payload?['status']?.toString() ?? ''] ??
        MemoryArtworkResultStatus.unavailable;
    return MemoryArtworkResult(
      status: status,
      failureCode: status == MemoryArtworkResultStatus.unavailable
          ? (payload?['outcome']?.toString().trim() ?? 'memory_artwork_unavailable')
          : '',
    );
  }

  Future<MemoryArtworkPreferences?> preferences() async {
    final authority = _authorityProvider();
    if (authority == null) return null;
    final response = await _call(authority, method: 'GET', path: 'v1/ella/memory-artwork/preferences');
    if (response == null || response.statusCode != 200) return null;
    final payload = _jsonObject(response.body);
    if (payload == null || payload['schema_version'] != memoryArtworkSchemaVersion) return null;
    return MemoryArtworkPreferences(
      consent: payload['consent']?.toString().trim() ?? 'not_set',
      consentVersion: payload['consent_version']?.toString().trim() ?? '',
      styleVersion: payload['style_version']?.toString().trim() ?? memoryArtworkDefaultStyle,
      releaseEnabled: payload['release_enabled'] == true,
    );
  }

  Future<MemoryArtworkPreferenceUpdate> setStyle({required String consentVersion, required String styleVersion}) async {
    final authority = _authorityProvider();
    if (authority == null) {
      return const MemoryArtworkPreferenceUpdate(saved: false, failureCode: 'memory_artwork_authority_unavailable');
    }
    if (consentVersion.isEmpty || !_supportedStyles.contains(styleVersion)) {
      return const MemoryArtworkPreferenceUpdate(saved: false, failureCode: 'memory_artwork_preference_invalid');
    }
    final response = await _call(
      authority,
      method: 'PUT',
      path: 'v1/ella/memory-artwork/preferences',
      body: jsonEncode({'consent': 'accepted', 'consent_version': consentVersion, 'style_version': styleVersion}),
    );
    if (!authority.isExactCurrent()) {
      return const MemoryArtworkPreferenceUpdate(saved: false, failureCode: 'memory_artwork_authority_changed');
    }
    if (response?.statusCode != 200) {
      return MemoryArtworkPreferenceUpdate(saved: false, failureCode: _safeFailureCode(response?.body));
    }
    return const MemoryArtworkPreferenceUpdate(saved: true);
  }

  Future<MemoryArtworkBackfillPage?> backfillNext({
    String? cursor,
    MemoryArtworkBackfillMode mode = MemoryArtworkBackfillMode.preview,
  }) async {
    final authority = _authorityProvider();
    final normalizedCursor = cursor?.trim() ?? '';
    if (authority == null || normalizedCursor.contains('/')) return null;
    final response = await _call(
      authority,
      method: 'POST',
      path: 'v1/ella/memory-artwork/backfill',
      body: jsonEncode({'mode': mode.name, if (normalizedCursor.isNotEmpty) 'cursor': normalizedCursor}),
    );
    if (response?.statusCode != 200 || !authority.isExactCurrent()) return null;
    final payload = _jsonObject(response!.body);
    if (payload == null ||
        payload['schema_version'] != memoryArtworkSchemaVersion ||
        payload['mode']?.toString() != mode.name) {
      return null;
    }
    final hasMore = payload['has_more'];
    final nextCursor = payload['next_cursor']?.toString().trim() ?? '';
    if (hasMore is! bool || (hasMore && (nextCursor.isEmpty || nextCursor.contains('/')))) return null;
    return MemoryArtworkBackfillPage(
      queued: _nonNegativeInt(payload['queued']),
      existing: _nonNegativeInt(payload['existing']),
      skipped: _nonNegativeInt(payload['skipped']),
      hasMore: hasMore,
      nextCursor: hasMore ? nextCursor : null,
      mode: mode,
    );
  }

  Future<bool> backfillRecent() async => await backfillNext() != null;

  Future<MemoryArtworkQueueStatus?> queueStatus() async {
    final authority = _authorityProvider();
    if (authority == null) return null;
    final response = await _call(
      authority,
      method: 'GET',
      path: 'v1/ella/memory-artwork/queue',
      timeout: const Duration(seconds: 30),
    );
    if (response?.statusCode != 200 || !authority.isExactCurrent()) return null;
    return _queueStatusFromPayload(_jsonObject(response!.body));
  }

  Future<MemoryArtworkQueueStatus?> controlQueue({
    required MemoryArtworkQueueAction action,
    required String generationId,
    bool autoContinue = false,
  }) async {
    final authority = _authorityProvider();
    if (authority == null || !_generationId.hasMatch(generationId)) return null;
    final response = await _call(
      authority,
      method: 'POST',
      path: 'v1/ella/memory-artwork/queue/control',
      timeout: const Duration(seconds: 30),
      body: jsonEncode({
        'action': action.name,
        'generation_id': generationId,
        if (action == MemoryArtworkQueueAction.resume) 'auto_continue': autoContinue,
      }),
    );
    if (response?.statusCode != 200 || !authority.isExactCurrent()) return null;
    return _queueStatusFromPayload(_jsonObject(response!.body));
  }

  Future<http.Response?> _call(
    ExactAccountAuthorityVerifier authority, {
    required String method,
    required String path,
    String body = '',
    Duration timeout = const Duration(seconds: 15),
  }) {
    final normalizedBase = _resolvedBaseUrl();
    if (normalizedBase.isEmpty) return Future<http.Response?>.value(null);
    final base = normalizedBase.endsWith('/') ? normalizedBase : '$normalizedBase/';
    return _request(
      url: '$base$path',
      headers: const {'Accept': 'application/json'},
      body: body,
      method: method,
      timeout: timeout,
      retries: 0,
      requireAuthCheck: true,
      expectedAuthenticatedUid: authority.uid,
      exactAuthority: authority,
    );
  }

  String _resolvedBaseUrl() {
    if (_baseUrl.isNotEmpty) return _baseUrl;
    try {
      return Env.apiBaseUrl?.trim() ?? '';
    } catch (_) {
      // Widget tests and signed-out startup can run before Env is initialized.
      return '';
    }
  }

  static Map<String, dynamic>? _jsonObject(String body) {
    try {
      final decoded = jsonDecode(body);
      return decoded is Map ? Map<String, dynamic>.from(decoded) : null;
    } catch (_) {
      return null;
    }
  }

  static String _safeFailureCode(String? body) {
    final payload = body == null ? null : _jsonObject(body);
    final detail = payload?['detail'];
    final raw = detail is Map ? detail['code']?.toString().trim() ?? '' : '';
    return RegExp(r'^[a-z0-9_]{1,80}$').hasMatch(raw) ? raw : 'memory_artwork_unavailable';
  }

  static int _nonNegativeInt(Object? value) => value is int && value >= 0 ? value : 0;

  static MemoryArtworkQueueStatus? _queueStatusFromPayload(Map<String, dynamic>? payload) {
    if (payload == null || payload['schema_version'] != 'ella.memory_artwork.queue.v1') return null;
    final generationId = payload['generation_id']?.toString().trim() ?? '';
    final styleVersion = payload['style_version']?.toString().trim() ?? '';
    final state = _queueState(payload['state']);
    final controlState = _queueState(payload['control_state']);
    if (!_generationId.hasMatch(generationId) || !_supportedStyles.contains(styleVersion)) return null;
    if (state == null || controlState == null) return null;
    final ready = _strictNonNegativeInt(payload['ready']);
    final active = _strictNonNegativeInt(payload['active']);
    final queued = _strictNonNegativeInt(payload['queued']);
    final retrying = _strictNonNegativeInt(payload['retrying']);
    final failed = _strictNonNegativeInt(payload['failed']);
    final total = _strictNonNegativeInt(payload['total']);
    final remaining = _strictNonNegativeInt(payload['remaining']);
    final scanned = _strictNonNegativeInt(payload['scanned']);
    final pagesProcessed = _strictNonNegativeInt(payload['pages_processed']);
    final autoContinue = payload['auto_continue'];
    final batchSize = _strictNonNegativeInt(payload['batch_size']);
    final batchRemaining = _strictNonNegativeInt(payload['batch_remaining']);
    final pauseReason = payload['pause_reason']?.toString().trim() ?? '';
    if (ready == null ||
        active == null ||
        queued == null ||
        retrying == null ||
        failed == null ||
        total == null ||
        remaining == null ||
        scanned == null ||
        pagesProcessed == null ||
        autoContinue is! bool ||
        batchSize == null ||
        batchSize < 1 ||
        batchSize > _maxHistoricalBatchSize ||
        batchRemaining == null ||
        (!autoContinue && batchRemaining > batchSize) ||
        total != ready + active + queued + retrying + failed ||
        remaining != active + queued + retrying + failed) {
      return null;
    }
    final stylePayloads = payload['styles'];
    if (stylePayloads is! List) return null;
    final styles = <MemoryArtworkStyleProgress>[];
    for (final raw in stylePayloads) {
      if (raw is! Map) return null;
      final item = Map<String, dynamic>.from(raw);
      final itemStyle = item['style_version']?.toString().trim() ?? '';
      final itemState = _queueState(item['state']);
      final itemReady = _strictNonNegativeInt(item['ready']);
      final itemActive = _strictNonNegativeInt(item['active']);
      final itemQueued = _strictNonNegativeInt(item['queued']);
      final itemRetrying = _strictNonNegativeInt(item['retrying']);
      final itemFailed = _strictNonNegativeInt(item['failed']);
      final itemTotal = _strictNonNegativeInt(item['total']);
      final itemRemaining = _strictNonNegativeInt(item['remaining']);
      if (!_supportedStyles.contains(itemStyle) ||
          itemState == null ||
          itemReady == null ||
          itemActive == null ||
          itemQueued == null ||
          itemRetrying == null ||
          itemFailed == null ||
          itemTotal == null ||
          itemRemaining == null ||
          itemTotal != itemReady + itemActive + itemQueued + itemRetrying + itemFailed ||
          itemRemaining != itemActive + itemQueued + itemRetrying + itemFailed) {
        return null;
      }
      styles.add(
        MemoryArtworkStyleProgress(
          styleVersion: itemStyle,
          state: itemState,
          ready: itemReady,
          active: itemActive,
          queued: itemQueued,
          retrying: itemRetrying,
          failed: itemFailed,
          total: itemTotal,
          remaining: itemRemaining,
        ),
      );
    }
    final updatedAt = DateTime.tryParse(payload['updated_at']?.toString() ?? '')?.toUtc();
    return MemoryArtworkQueueStatus(
      generationId: generationId,
      styleVersion: styleVersion,
      state: state,
      controlState: controlState,
      scanStatus: payload['scan_status']?.toString().trim() ?? 'idle',
      scanned: scanned,
      pagesProcessed: pagesProcessed,
      autoContinue: autoContinue,
      batchSize: batchSize,
      batchRemaining: batchRemaining,
      pauseReason: pauseReason,
      ready: ready,
      active: active,
      queued: queued,
      retrying: retrying,
      failed: failed,
      total: total,
      remaining: remaining,
      styles: List.unmodifiable(styles),
      updatedAt: updatedAt,
    );
  }

  static int? _strictNonNegativeInt(Object? value) => value is int && value >= 0 ? value : null;

  static MemoryArtworkQueueState? _queueState(Object? value) => switch (value?.toString()) {
        'running' => MemoryArtworkQueueState.running,
        'paused' => MemoryArtworkQueueState.paused,
        'cancelled' => MemoryArtworkQueueState.cancelled,
        'completed' => MemoryArtworkQueueState.completed,
        'needs_attention' => MemoryArtworkQueueState.needsAttention,
        _ => null,
      };

  static MemoryArtworkResult _unavailable(String code) =>
      MemoryArtworkResult(status: MemoryArtworkResultStatus.unavailable, failureCode: code);

  static String _cacheKey({
    required ExactAccountAuthorityVerifier authority,
    required String memoryId,
    required String styleVersion,
    required String enrichmentRevision,
  }) {
    final ownerNamespace = authority is ActiveWalAuthority
        ? authority.owner.storageNamespace
        : sha256.convert(utf8.encode(authority.uid)).toString().substring(0, 24);
    return sha256
        .convert(
          utf8.encode('ella-memory-artwork-cache-v1\n$ownerNamespace\n$memoryId\n$styleVersion\n$enrichmentRevision'),
        )
        .toString();
  }
}

final _generationId = RegExp(r'^[0-9a-f]{64}$');

const _supportedStyles = {
  memoryArtworkDefaultStyle,
  memoryArtworkPaperCollageStyle,
  memoryArtworkGraphicLandscapeStyle,
  memoryArtworkWatercolorJournalStyle,
  memoryArtworkAnimeStorybookStyle,
  memoryArtworkCinematicStillStyle,
};
