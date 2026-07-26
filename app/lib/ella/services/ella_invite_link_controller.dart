import 'package:flutter/foundation.dart';

import 'package:omi/providers/ella_entitlement_provider.dart';

class EllaInviteLinkController extends ChangeNotifier {
  EllaInviteLinkController._();

  static final EllaInviteLinkController instance = EllaInviteLinkController._();

  String _pendingCode = '';

  String get pendingCode => _pendingCode;

  bool accept(Uri uri) {
    final code = extractEllaInviteCode(uri);
    if (code.isEmpty) return false;
    _pendingCode = code;
    notifyListeners();
    return true;
  }

  String consume() {
    final code = _pendingCode;
    _pendingCode = '';
    return code;
  }

  @visibleForTesting
  void clear() {
    _pendingCode = '';
    notifyListeners();
  }
}

@visibleForTesting
String extractEllaInviteCode(Uri uri) {
  final isInviteHost = uri.host.toLowerCase() == 'invite';
  final inviteIndex = uri.pathSegments.indexWhere((segment) => segment.toLowerCase() == 'invite');
  final isInvitePath = inviteIndex >= 0;
  if (!isInviteHost && !isInvitePath) return '';

  final queryCode = uri.queryParameters['code'] ?? uri.queryParameters['invite'];
  final pathCode = inviteIndex >= 0 && inviteIndex + 1 < uri.pathSegments.length
      ? uri.pathSegments[inviteIndex + 1]
      : isInviteHost && uri.pathSegments.isNotEmpty
          ? uri.pathSegments.first
          : '';
  return normalizeEllaInviteCode(queryCode ?? pathCode);
}
