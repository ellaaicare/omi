import 'dart:async';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';

enum EllaEntitlementLoadState { idle, loading, ready, redeeming, unavailable }

class EllaEntitlementProvider extends ChangeNotifier {
  EllaEntitlementProvider({
    EllaEntitlementTransport? transport,
    Stream<String?>? authenticatedUidChanges,
    String? initialAuthenticatedUid,
  })  : _transport = transport ??
            ((SharedPreferencesUtil().demoMode || isEllaEntitlementStubEnabled)
                ? EllaAccessDemoFixtures.defaultTransport
                : const EllaEntitlementHttpTransport()),
        _boundUid = initialAuthenticatedUid ?? FirebaseAuth.instance.currentUser?.uid {
    final changes =
        authenticatedUidChanges ?? FirebaseAuth.instance.authStateChanges().map((user) => user?.uid).distinct();
    _authSubscription = changes.listen(bindAuthenticatedUid);
  }

  EllaEntitlementProvider.demo({
    required EllaEntitlement initialEntitlement,
    EllaInviteRedemptionError? initialInviteError,
    String initialInviteCode = '',
    int? initialRetryAfterSeconds,
  })  : _transport = SharedPreferencesUtil.isPublicBuild
            ? const EllaEntitlementHttpTransport()
            : EllaAccessDemoTransport(fetchResult: initialEntitlement),
        entitlement = SharedPreferencesUtil.isPublicBuild ? null : initialEntitlement,
        inviteError = SharedPreferencesUtil.isPublicBuild ? null : initialInviteError,
        inviteCode = SharedPreferencesUtil.isPublicBuild ? '' : initialInviteCode,
        retryAfterSeconds = SharedPreferencesUtil.isPublicBuild ? null : initialRetryAfterSeconds,
        state = SharedPreferencesUtil.isPublicBuild ? EllaEntitlementLoadState.idle : EllaEntitlementLoadState.ready,
        supportCode = SharedPreferencesUtil.isPublicBuild ? '' : initialEntitlement.supportCode,
        correlationId = SharedPreferencesUtil.isPublicBuild ? '' : initialEntitlement.correlationId,
        _boundUid = SharedPreferencesUtil.isPublicBuild ? null : 'demo-user',
        _verifiedUid = SharedPreferencesUtil.isPublicBuild ? null : 'demo-user';

  final EllaEntitlementTransport _transport;
  StreamSubscription<String?>? _authSubscription;

  EllaEntitlementLoadState state = EllaEntitlementLoadState.idle;
  EllaEntitlement? entitlement;
  EllaInviteRedemptionError? inviteError;
  String inviteCode = '';
  int? retryAfterSeconds;
  String supportCode = '';
  String correlationId = '';
  String? _boundUid;
  String? _verifiedUid;
  int _generation = 0;

  EllaQuota? get quota => entitlement?.quota;
  bool get isIdentityVerified => _boundUid != null && _boundUid == _verifiedUid;
  bool get canProvision => isIdentityVerified && entitlement?.canProvision == true;
  bool get isActive => isIdentityVerified && entitlement?.isActive == true;
  String? get boundUid => _boundUid;

  @visibleForTesting
  bool get usesDemoTransport => _transport is EllaAccessDemoTransport;

  Future<void> load({String prefilledCode = ''}) async {
    final uid = _boundUid;
    final generation = ++_generation;
    if (prefilledCode.isNotEmpty) inviteCode = normalizeEllaInviteCode(prefilledCode);
    entitlement = null;
    _verifiedUid = null;
    state = EllaEntitlementLoadState.loading;
    _clearFailure();
    notifyListeners();
    if (uid == null || uid.isEmpty) {
      if (generation != _generation) return;
      state = EllaEntitlementLoadState.unavailable;
      notifyListeners();
      return;
    }
    try {
      final result = await _transport.fetch();
      if (generation != _generation || uid != _boundUid) return;
      entitlement = result;
      _verifiedUid = uid;
      _setDiagnostics(result.supportCode, result.correlationId);
      state = EllaEntitlementLoadState.ready;
      notifyListeners();
    } catch (_) {
      if (generation != _generation || uid != _boundUid) return;
      state = EllaEntitlementLoadState.unavailable;
      notifyListeners();
    }
  }

  Future<void> redeem(String code) async {
    final normalized = normalizeEllaInviteCode(code);
    if (normalized.isEmpty || state == EllaEntitlementLoadState.redeeming) return;
    final uid = _boundUid;
    final generation = ++_generation;
    inviteCode = normalized;
    _clearFailure();
    state = EllaEntitlementLoadState.redeeming;
    notifyListeners();
    if (uid == null || uid.isEmpty) {
      if (generation != _generation) return;
      state = EllaEntitlementLoadState.unavailable;
      notifyListeners();
      return;
    }
    try {
      final result = await _transport.redeem(normalized);
      if (generation != _generation || uid != _boundUid) return;
      entitlement = result;
      _verifiedUid = uid;
      _setDiagnostics(result.supportCode, result.correlationId);
      state = EllaEntitlementLoadState.ready;
      notifyListeners();
    } on EllaInviteRedemptionException catch (error) {
      if (generation != _generation || uid != _boundUid) return;
      inviteError = error.reason;
      retryAfterSeconds = error.retryAfterSeconds;
      _setDiagnostics(error.supportCode, error.correlationId);
      if (error.reason == EllaInviteRedemptionError.capacity) {
        entitlement = EllaAccessDemoFixtures.none;
        _verifiedUid = uid;
      }
      state = EllaEntitlementLoadState.ready;
      notifyListeners();
    } catch (_) {
      if (generation != _generation || uid != _boundUid) return;
      state = EllaEntitlementLoadState.unavailable;
      notifyListeners();
    }
  }

  Future<void> retry() => load(prefilledCode: inviteCode);

  void acceptInviteLink(String code) {
    final normalized = normalizeEllaInviteCode(code);
    if (normalized.isEmpty || normalized == inviteCode) return;
    inviteCode = normalized;
    _clearFailure();
    notifyListeners();
  }

  void clearInviteCode() {
    inviteCode = '';
    _clearFailure();
    notifyListeners();
  }

  void bindAuthenticatedUid(String? uid) {
    final normalized = uid?.trim();
    final nextUid = normalized == null || normalized.isEmpty ? null : normalized;
    if (nextUid == _boundUid) return;
    _boundUid = nextUid;
    _clearIdentityState();
    notifyListeners();
  }

  void reset() {
    _boundUid = null;
    _clearIdentityState();
    notifyListeners();
  }

  void _clearIdentityState() {
    _generation++;
    state = EllaEntitlementLoadState.idle;
    entitlement = null;
    _verifiedUid = null;
    inviteError = null;
    inviteCode = '';
    retryAfterSeconds = null;
    supportCode = '';
    correlationId = '';
  }

  void _clearFailure() {
    inviteError = null;
    retryAfterSeconds = null;
    supportCode = '';
    correlationId = '';
  }

  void _setDiagnostics(String support, String correlation) {
    supportCode = support;
    correlationId = correlation;
  }

  @override
  void dispose() {
    _authSubscription?.cancel();
    super.dispose();
  }
}

String normalizeEllaInviteCode(String value) => value.toUpperCase().replaceAll(RegExp(r'[^A-Z0-9]'), '');
