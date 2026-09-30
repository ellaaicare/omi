import 'dart:async';

import 'package:omi/ella/services/ai_consent_active_session_lease.dart';
import 'package:omi/ella/services/ella_audio_emission_gate.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

typedef EllaConsentLeaseFactory = AiConsentActiveSessionLease Function({
  required String uid,
  required FutureOr<void> Function() onAuthorityLost,
});

/// Why a bound capture authority was revoked.
enum EllaCaptureRevocationReason {
  /// The signed-in account no longer matches the account the session was bound to.
  accountSwitch,

  /// The consent lease reported authority loss (server revoke, local decline, ...).
  consentLost,

  /// A per-frame check found the lease no longer current or in another generation.
  authorityNotCurrent,

  /// The owner ended the binding (sign-out, dispose, rebind).
  released,
}

class EllaCaptureRevocation {
  const EllaCaptureRevocation({required this.uid, required this.reason});

  final String? uid;
  final EllaCaptureRevocationReason reason;

  @override
  String toString() => 'EllaCaptureRevocation(${reason.name})';
}

/// Account binding + live consent authority for the upstream capture stack.
///
/// One instance guards one app process. [bind] starts a fresh
/// [AiConsentActiveSessionLease] for the signed-in account and records the
/// lease generation; [admitsFrame] is the per-frame check every gated capture
/// seam calls, and it is exactly [mayEmitAudio] plus the requirement that the
/// authenticated account is still the bound one. The first failing frame
/// revokes the binding (fail closed) and publishes an [EllaCaptureRevocation]
/// so the runtime can stop upstream capture through its public API; nothing is
/// re-admitted until a new [bind] succeeds with a new generation.
class EllaCaptureAuthority {
  EllaCaptureAuthority({
    String Function()? authenticatedUid,
    EllaConsentLeaseFactory? leaseFactory,
    bool Function(String uid)? sessionStartAllowed,
  })  : _authenticatedUid = authenticatedUid ?? _productionAuthenticatedUid,
        _leaseFactory = leaseFactory ?? _productionLease,
        _sessionStartAllowed = sessionStartAllowed ?? _productionSessionStartAllowed;

  static String _productionAuthenticatedUid() => WalOwnerAuthority.authenticatedUid;

  static AiConsentActiveSessionLease _productionLease({
    required String uid,
    required FutureOr<void> Function() onAuthorityLost,
  }) =>
      AiConsentActiveSessionLease(uid: uid, onAuthorityLost: onAuthorityLost);

  static bool _productionSessionStartAllowed(String uid) =>
      AiConsentActiveSessionLease.authorityForSessionStart(expectedUid: uid) != null;

  final String Function() _authenticatedUid;
  final EllaConsentLeaseFactory _leaseFactory;
  final bool Function(String uid) _sessionStartAllowed;
  final StreamController<EllaCaptureRevocation> _revocations = StreamController<EllaCaptureRevocation>.broadcast(
    sync: true,
  );

  AiConsentActiveSessionLease? _lease;
  String? _boundUid;
  int _expectedGeneration = 0;
  int _bindingEpoch = 0;
  int _framesAdmitted = 0;
  int _framesDropped = 0;
  bool _disposed = false;

  String? get boundUid => _boundUid;

  /// The lease generation observed at [bind]; 0 while unbound.
  int get expectedGeneration => _expectedGeneration;

  /// Process-local identity of the current binding, including same-UID rebinds.
  int get bindingEpoch => _bindingEpoch;

  bool get isBound => _lease != null;

  int get framesAdmitted => _framesAdmitted;

  int get framesDropped => _framesDropped;

  Stream<EllaCaptureRevocation> get revocations => _revocations.stream;

  /// Side-effect free: whether a frame would be admitted right now.
  bool get hasCurrentAuthority {
    final lease = _lease;
    return lease != null &&
        _authenticatedUid() == _boundUid &&
        mayEmitAudio(boundUid: _boundUid, lease: lease, expectedGeneration: _expectedGeneration);
  }

  /// Binds capture to [uid] with a freshly started consent lease. Returns true
  /// only when the new session holds current authority. Rebinding the same
  /// account while current keeps the existing session; any other rebind first
  /// revokes the previous session.
  bool bind(String uid) {
    if (_disposed) return false;
    final account = uid.trim();
    if (_lease != null && _boundUid == account && hasCurrentAuthority) return true;
    if (_lease != null) {
      _revoke(_authenticatedUid() != _boundUid
          ? EllaCaptureRevocationReason.accountSwitch
          : EllaCaptureRevocationReason.released);
    }
    if (account.isEmpty || _authenticatedUid() != account || !_sessionStartAllowed(account)) return false;

    late final AiConsentActiveSessionLease lease;
    lease = _leaseFactory(uid: account, onAuthorityLost: () => _onLeaseLost(lease));
    lease.start();
    if (!lease.hasCurrentAuthority) {
      lease.stop();
      return false;
    }
    _lease = lease;
    _boundUid = account;
    _expectedGeneration = lease.generation;
    _bindingEpoch++;
    return true;
  }

  /// Per-frame gate. Returns true only when [mayEmitAudio] admits the bound
  /// account, lease, and generation AND the authenticated account is unchanged.
  /// The first refusal revokes the binding.
  bool admitsFrame() {
    if (hasCurrentAuthority) {
      _framesAdmitted++;
      return true;
    }
    _framesDropped++;
    if (_lease != null) {
      _revoke(_authenticatedUid() != _boundUid
          ? EllaCaptureRevocationReason.accountSwitch
          : EllaCaptureRevocationReason.authorityNotCurrent);
    }
    return false;
  }

  /// Ends the binding (sign-out, flag off, dispose). Idempotent.
  void release() => _revoke(EllaCaptureRevocationReason.released);

  void _onLeaseLost(AiConsentActiveSessionLease lease) {
    if (identical(lease, _lease)) _revoke(EllaCaptureRevocationReason.consentLost);
  }

  void _revoke(EllaCaptureRevocationReason reason) {
    final lease = _lease;
    if (lease == null) return;
    final uid = _boundUid;
    _lease = null;
    _boundUid = null;
    _expectedGeneration = 0;
    _bindingEpoch++;
    lease.stop();
    if (!_revocations.isClosed) _revocations.add(EllaCaptureRevocation(uid: uid, reason: reason));
  }

  void dispose() {
    if (_disposed) return;
    release();
    _disposed = true;
    unawaited(_revocations.close());
  }
}
