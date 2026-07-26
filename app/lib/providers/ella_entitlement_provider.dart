import 'package:flutter/foundation.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';

enum EllaEntitlementLoadState { idle, loading, ready, redeeming, unavailable }

class EllaEntitlementProvider extends ChangeNotifier {
  EllaEntitlementProvider({EllaEntitlementTransport? transport})
      : _transport = transport ??
            ((SharedPreferencesUtil().demoMode || isEllaEntitlementStubEnabled)
                ? EllaAccessDemoFixtures.defaultTransport
                : const EllaEntitlementHttpTransport());

  EllaEntitlementProvider.demo({
    required EllaEntitlement initialEntitlement,
    EllaInviteRedemptionError? initialInviteError,
    String initialInviteCode = '',
  })  : _transport = EllaAccessDemoTransport(fetchResult: initialEntitlement),
        entitlement = initialEntitlement,
        inviteError = initialInviteError,
        inviteCode = initialInviteCode,
        state = EllaEntitlementLoadState.ready;

  final EllaEntitlementTransport _transport;

  EllaEntitlementLoadState state = EllaEntitlementLoadState.idle;
  EllaEntitlement? entitlement;
  EllaInviteRedemptionError? inviteError;
  String inviteCode = '';
  int _generation = 0;

  EllaQuota? get quota => entitlement?.quota;
  bool get isActive => entitlement?.isActive == true;

  Future<void> load({String prefilledCode = ''}) async {
    final generation = ++_generation;
    if (prefilledCode.isNotEmpty) inviteCode = normalizeEllaInviteCode(prefilledCode);
    state = EllaEntitlementLoadState.loading;
    inviteError = null;
    notifyListeners();
    try {
      final result = await _transport.fetch();
      if (generation != _generation) return;
      entitlement = result;
      state = EllaEntitlementLoadState.ready;
      notifyListeners();
    } catch (_) {
      if (generation != _generation) return;
      state = EllaEntitlementLoadState.unavailable;
      notifyListeners();
    }
  }

  Future<void> redeem(String code) async {
    final normalized = normalizeEllaInviteCode(code);
    if (normalized.isEmpty || state == EllaEntitlementLoadState.redeeming) return;
    final generation = ++_generation;
    inviteCode = normalized;
    inviteError = null;
    state = EllaEntitlementLoadState.redeeming;
    notifyListeners();
    try {
      final result = await _transport.redeem(normalized);
      if (generation != _generation) return;
      entitlement = result;
      state = EllaEntitlementLoadState.ready;
      notifyListeners();
    } on EllaInviteRedemptionException catch (error) {
      if (generation != _generation) return;
      inviteError = error.reason;
      if (error.reason == EllaInviteRedemptionError.capacity) {
        entitlement = EllaAccessDemoFixtures.none;
      }
      state = EllaEntitlementLoadState.ready;
      notifyListeners();
    } catch (_) {
      if (generation != _generation) return;
      state = EllaEntitlementLoadState.unavailable;
      notifyListeners();
    }
  }

  Future<void> retry() => load(prefilledCode: inviteCode);

  void acceptInviteLink(String code) {
    final normalized = normalizeEllaInviteCode(code);
    if (normalized.isEmpty || normalized == inviteCode) return;
    inviteCode = normalized;
    inviteError = null;
    notifyListeners();
  }

  void reset() {
    _generation++;
    state = EllaEntitlementLoadState.idle;
    entitlement = null;
    inviteError = null;
    inviteCode = '';
    notifyListeners();
  }
}

String normalizeEllaInviteCode(String value) => value.toUpperCase().replaceAll(RegExp(r'[^A-Z0-9]'), '');
