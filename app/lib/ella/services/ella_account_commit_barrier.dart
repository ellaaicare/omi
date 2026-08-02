import 'package:omi/services/wals/wal_owner_authority.dart';

typedef ActiveAccountAuthorityProvider = AccountCommitAuthority? Function();

/// Synchronously invalidates account-scoped async commits before an identity
/// transition, while exact owner/consent checks protect every later await.
class EllaAccountCommitBarrier {
  const EllaAccountCommitBarrier._();

  static int _generation = 0;
  static final Map<Object, void Function()> _inFlight = {};

  static EllaAccountCommitLease? begin({
    required ActiveAccountAuthorityProvider authorityProvider,
    required void Function() onInvalidated,
  }) {
    final authority = authorityProvider();
    if (authority == null || !authority.isCurrent()) return null;
    final token = Object();
    final lease = EllaAccountCommitLease._(
      token: token,
      generation: _generation,
      authority: authority,
    );
    _inFlight[token] = onInvalidated;
    return lease;
  }

  static void quiesceForAccountTransition() {
    _generation++;
    final callbacks = List<void Function()>.from(_inFlight.values);
    _inFlight.clear();
    for (final callback in callbacks) {
      callback();
    }
  }

  static bool _isCurrent(EllaAccountCommitLease lease) =>
      !lease._closed && lease.generation == _generation && lease.authority.isCurrent();

  static void _close(EllaAccountCommitLease lease) {
    if (lease._closed) return;
    lease._closed = true;
    _inFlight.remove(lease.token);
  }
}

class EllaAccountCommitLease implements ExactAccountAuthorityVerifier {
  EllaAccountCommitLease._({required this.token, required this.generation, required this.authority});

  final Object token;
  final int generation;
  final AccountCommitAuthority authority;
  bool _closed = false;

  bool get isCurrent => EllaAccountCommitBarrier._isCurrent(this);

  @override
  String get uid => authority.uid;

  @override
  bool isExactCurrent() => isCurrent;

  void close() => EllaAccountCommitBarrier._close(this);
}
