import 'dart:io';
import 'package:path_provider/path_provider.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';
import 'package:omi/ella/services/fictional_transcript_run.dart';
import 'package:omi/ella/services/fictional_transcript_store.dart';
import 'package:omi/ella/services/fictional_transcript_local_load.dart';

/// Uses the existing app-owned authenticated transport. Never accepts a token,
/// API key, arbitrary origin, UID override or a personal conversation ID.
Future<FictionalTranscriptRun> createFictionalTranscriptRun(
    FictionalTranscriptPlan plan, ExactAccountAuthorityVerifier authority) =>
    FictionalTranscriptLocalLoader(
      apiBaseUrl: () => Env.apiBaseUrl ?? '',
      applicationSupport: getApplicationSupportDirectory,
    ).load(plan, authority);

/// Dependency seams permit inert local failure tests. Every instance retains
/// the fixed feature/owner/consent/expiry/origin gates; no transport is invoked
/// by load. The production entry uses only the existing app dependencies.
class FictionalTranscriptLocalLoader {
  const FictionalTranscriptLocalLoader({required this.apiBaseUrl, required this.applicationSupport});
  final String Function() apiBaseUrl;
  final Future<Directory> Function() applicationSupport;

  Future<FictionalTranscriptRun> load(
      FictionalTranscriptPlan plan, ExactAccountAuthorityVerifier authority) async {
    if (!fictionalProducerEnabled) {
      throw const FictionalLocalLoadException(FictionalLocalLoadCode.featureDisabled);
    }
    if (!authority.isExactCurrent()) {
      throw const FictionalLocalLoadException(FictionalLocalLoadCode.authorityRetired);
    }
    if (fictionalHash(authority.uid) != plan.ownerHash) {
      throw const FictionalLocalLoadException(FictionalLocalLoadCode.ownerMismatch);
    }
    if (!DateTime.now().toUtc().isBefore(plan.expiresAt)) {
      throw const FictionalLocalLoadException(FictionalLocalLoadCode.planExpired);
    }
    final base = Uri.tryParse(apiBaseUrl());
    if (base == null || base.scheme != 'https' || base.host != 'api.ella-ai-care.com' ||
        base.userInfo.isNotEmpty || base.hasQuery || base.hasFragment || base.path != '/') {
      throw const FictionalLocalLoadException(FictionalLocalLoadCode.apiOriginInvalid);
    }
    final Directory support;
    try { support = await applicationSupport(); }
    catch (_) {
      throw const FictionalLocalLoadException(FictionalLocalLoadCode.applicationSupportUnavailable);
    }
    final directory = Directory('${support.path}/fictional-transcript-attempts-v1');
    if (!authority.isExactCurrent()) {
      throw const FictionalLocalLoadException(FictionalLocalLoadCode.authorityRetired);
    }
    try { await directory.create(recursive: true); }
    catch (_) {
      throw const FictionalLocalLoadException(FictionalLocalLoadCode.attemptDirectoryUnavailable);
    }
    return FictionalTranscriptRun(
      plan: plan,
      current: () => authority.isExactCurrent() && fictionalHash(authority.uid) == plan.ownerHash,
      reserveAttempt: FictionalAttemptStore(directory).reserve,
      recordReceipt: (receipt) => FictionalAttemptStore(directory).record(plan, receipt),
      transport: (method, path, body) async {
        if (!authority.isExactCurrent()) throw StateError('Account retired');
        final allowed = (method == 'POST' && path == 'v1/conversations/test-transcripts/prepare') ||
            (method == 'POST' && path == 'v1/conversations/test-transcripts/${plan.requestId}/complete' && body.isEmpty) ||
            (method == 'GET' && path == 'v1/conversations/${plan.sourceId}' && body.isEmpty);
        if (!allowed) throw StateError('Unexpected fictional route');
        final response = await makeApiCall(url: base.resolve(path).toString(), headers: const {'Content-Type':'application/json'},
          method: method, body: body,
          timeout: path.endsWith('/complete') ? const Duration(seconds: 300) : const Duration(seconds: 20),
          retries: 0, retryOnUnauthorized: false, enforceAbsoluteTimeout: true,
          requireAuthCheck: true, expectedAuthenticatedUid: authority.uid, exactAuthority: authority);
        if (!authority.isExactCurrent()) throw StateError('Account retired');
        return response == null ? null : FictionalResponse(response.statusCode, response.body);
      },
    );
  }
}
