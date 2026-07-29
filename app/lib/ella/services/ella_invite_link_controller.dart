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

  void clear() {
    if (_pendingCode.isEmpty) return;
    _pendingCode = '';
    notifyListeners();
  }
}

@visibleForTesting
String extractEllaInviteCode(Uri uri) {
  final scheme = uri.scheme.toLowerCase();
  if (scheme == 'https') {
    if (uri.host.toLowerCase() != 'ella-ai-care.com' || (uri.path != '/invite' && uri.path != '/invite/')) {
      return '';
    }
    return normalizeEllaInviteCode(_fragmentParameters(uri.fragment)['c'] ?? '');
  }

  if (scheme != 'omi' || uri.host.toLowerCase() != 'invite') return '';
  final fragmentCode = _fragmentParameters(uri.fragment)['c'];
  final queryCode = uri.queryParameters['c'] ?? uri.queryParameters['code'];
  final pathCode = uri.pathSegments.isEmpty ? '' : uri.pathSegments.first;
  return normalizeEllaInviteCode(fragmentCode ?? queryCode ?? pathCode);
}

Map<String, String> _fragmentParameters(String fragment) {
  if (fragment.isEmpty) return const {};
  try {
    return Uri.splitQueryString(fragment);
  } on FormatException {
    return const {};
  }
}
