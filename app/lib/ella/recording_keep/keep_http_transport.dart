import 'dart:convert';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

import 'explicit_recording_keep.dart';

/// Prepared production adapter. No request occurs until the caller invokes it.
/// The same full captured authority must fence both UI and makeApiCall egress.
class KeepHttpTransport {
  KeepHttpTransport({required this.authority, required this.owner});

  final ActiveWalAuthority authority;
  final KeepOwner owner;

  bool isCurrent(KeepOwner candidate) =>
      identical(candidate, owner) &&
      owner.uid == authority.uid &&
      owner.receiptId == authority.consent.receiptId &&
      authority.isExactCurrent();

  Future<KeepResponse> call(KeepRequest request) async {
    if (!isCurrent(request.owner)) {
      throw ExactAccountAuthorityChangedException('Recording authority changed');
    }
    final path = request.path;
    // No arbitrary URL, query, model operation, index, or alternate CID route.
    if (!RegExp(r'^/v1/conversations/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/keep$').hasMatch(path) ||
        (request.method != 'GET' && request.method != 'POST')) {
      throw StateError('Unsupported recording retention request');
    }
    final base = Env.apiBaseUrl;
    if (base == null || !base.endsWith('/')) {
      throw StateError('Recording service unavailable');
    }
    final response = await makeApiCall(
      url: '$base${path.substring(1)}',
      method: request.method,
      headers: const {'Content-Type': 'application/json'},
      body: request.body == null ? '' : jsonEncode(request.body),
      timeout: const Duration(seconds: 10),
      enforceAbsoluteTimeout: true,
      retries: 0,
      retryOnUnauthorized: false,
      requireAuthCheck: true,
      expectedAuthenticatedUid: owner.uid,
      exactAuthority: authority,
    );
    if (!isCurrent(request.owner)) {
      throw ExactAccountAuthorityChangedException('Recording authority changed');
    }
    if (response == null) throw StateError('Recording response unconfirmed');
    if (response.statusCode != 200) {
      return KeepResponse(response.statusCode, const {});
    }
    // Keep receipts are metadata only; bound decoding before allocating JSON.
    if (response.bodyBytes.length > 65536) throw FormatException('Receipt too large');
    final body = jsonDecode(response.body);
    if (body is! Map<String, dynamic>) throw FormatException('Invalid receipt');
    return KeepResponse(200, body);
  }
}
