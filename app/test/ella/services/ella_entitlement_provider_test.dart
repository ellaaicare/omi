import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/demo/ella_access_demo_fixtures.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';

class _QueuedEntitlementTransport implements EllaEntitlementTransport {
  final List<Completer<EllaEntitlement>> fetches = [];

  @override
  Future<EllaEntitlement> fetch() {
    final completer = Completer<EllaEntitlement>();
    fetches.add(completer);
    return completer.future;
  }

  @override
  Future<EllaEntitlement> redeem(String code) => throw UnimplementedError();
}

void main() {
  test('only an entitlement verified for the currently bound UID can unlock provisioning', () async {
    final identities = StreamController<String?>.broadcast(sync: true);
    final transport = _QueuedEntitlementTransport();
    final provider = EllaEntitlementProvider(
      transport: transport,
      initialAuthenticatedUid: 'user-a',
      authenticatedUidChanges: identities.stream,
    );
    addTearDown(provider.dispose);
    addTearDown(identities.close);

    final userALoad = provider.load();
    expect(provider.state, EllaEntitlementLoadState.loading);

    identities.add(null);
    identities.add('user-b');
    expect(provider.boundUid, 'user-b');
    expect(provider.entitlement, isNull);
    expect(provider.canProvision, isFalse);

    transport.fetches.single.complete(EllaAccessDemoFixtures.active);
    await userALoad;
    expect(provider.entitlement, isNull);
    expect(provider.canProvision, isFalse);

    final userBLoad = provider.load();
    transport.fetches.last.complete(EllaAccessDemoFixtures.invited);
    await userBLoad;

    expect(provider.isIdentityVerified, isTrue);
    expect(provider.canProvision, isTrue);
    expect(provider.isActive, isFalse);
  });

  test('sign-out immediately clears a previously active entitlement', () async {
    final identities = StreamController<String?>.broadcast(sync: true);
    final transport = _QueuedEntitlementTransport();
    final provider = EllaEntitlementProvider(
      transport: transport,
      initialAuthenticatedUid: 'user-a',
      authenticatedUidChanges: identities.stream,
    );
    addTearDown(provider.dispose);
    addTearDown(identities.close);

    final load = provider.load();
    transport.fetches.single.complete(EllaAccessDemoFixtures.active);
    await load;
    expect(provider.isActive, isTrue);

    provider.reset();
    expect(provider.boundUid, isNull);
    expect(provider.entitlement, isNull);
    expect(provider.isIdentityVerified, isFalse);
    expect(provider.canProvision, isFalse);
  });

  test('preserves a typed access failure rather than reducing it to a generic unavailable state', () async {
    final provider = EllaEntitlementProvider(
      transport: const _FailingEntitlementTransport(
        EllaEntitlementRequestException(EllaEntitlementFailureKind.unavailable, supportCode: 'ELLA-ACCESS-RETRY'),
      ),
      authenticatedUidChanges: const Stream.empty(),
      initialAuthenticatedUid: 'user-a',
    );
    addTearDown(provider.dispose);

    await provider.load();

    expect(provider.state, EllaEntitlementLoadState.unavailable);
    expect(provider.accessFailureKind, EllaEntitlementFailureKind.unavailable);
    expect(provider.supportCode, 'ELLA-ACCESS-RETRY');
  });
}

class _FailingEntitlementTransport implements EllaEntitlementTransport {
  const _FailingEntitlementTransport(this.failure);

  final EllaEntitlementRequestException failure;

  @override
  Future<EllaEntitlement> fetch() => Future.error(failure);

  @override
  Future<EllaEntitlement> redeem(String code) => Future.error(failure);
}
