import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/memory.dart';
import 'package:omi/backend/schema/person.dart';
import 'package:omi/ella/services/ella_account_isolation_service.dart';
import 'package:omi/ella/services/ella_workspace_status.dart';
import 'package:omi/providers/memories_provider.dart';
import 'package:omi/providers/people_provider.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
  });

  test('account-scoped caches never appear under the next account', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    final memory = _memory('memory-a', 'uid-a');
    final person = _person('person-a');
    prefs.pendingMemories = [memory];
    prefs.cachedPeople = [person];
    prefs.emergencyContactName = 'Account A contact';
    prefs.pendingEmergency = 'account-a-state';

    prefs.uid = 'uid-b';
    expect(prefs.pendingMemories, isEmpty);
    expect(prefs.cachedPeople, isEmpty);
    expect(prefs.emergencyContactName, isEmpty);
    expect(prefs.pendingEmergency, isEmpty);

    prefs.uid = 'uid-a';
    expect(prefs.pendingMemories.single.id, memory.id);
    expect(prefs.cachedPeople.single.id, person.id);
    expect(prefs.emergencyContactName, 'Account A contact');
  });

  test('legacy unowned caches are quarantined instead of assigned to the signed-in account', () async {
    await SharedPreferencesUtil().saveStringList('pendingMemories', [_memory('legacy', 'unknown').toJsonString()]);
    await SharedPreferencesUtil().saveString('emergencyContactName', 'Unknown owner');
    final prefs = SharedPreferencesUtil()..uid = 'uid-new';

    await prefs.quarantineLegacyAccountCaches();

    expect(prefs.pendingMemories, isEmpty);
    expect(prefs.emergencyContactName, isEmpty);
    expect(prefs.getStringList('ellaLegacyUnownedCache:pendingMemories'), isNotEmpty);
    expect(prefs.getString('ellaLegacyUnownedCache:emergencyContactName'), 'Unknown owner');
  });

  test('pending memory response after account switch does not clear or merge another owner cache', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    await _grantAuthority(prefs, 'uid-a');
    final pending = _memory('pending-a', 'uid-a');
    prefs.pendingMemories = [pending];
    final response = Completer<Memory?>();
    final provider = MemoriesProvider(
      preferences: prefs,
      createMemory: (content, visibility, category) => response.future,
    );

    final sync = provider.syncPendingMemories();
    await Future<void>.delayed(Duration.zero);
    prefs.uid = 'uid-b';
    response.complete(_memory('server-a', 'uid-a'));
    await sync;

    expect(prefs.pendingMemories, isEmpty);
    prefs.uid = 'uid-a';
    expect(prefs.pendingMemories.single.id, 'pending-a');
  });

  test('people response after account switch is not cached under the new account', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-a';
    final response = Completer<List<Person>>();
    final provider = PeopleProvider(preferences: prefs, fetchPeople: () => response.future);

    final pending = provider.setPeople();
    prefs.uid = 'uid-b';
    response.complete([_person('person-a')]);
    await pending;

    expect(prefs.cachedPeople, isEmpty);
    expect(provider.people, isEmpty);
  });

  test('capture, V2V reconnect, Guardian poll, WAL and legacy quarantine stop in order', () async {
    final calls = <String>[];
    final service = EllaAccountIsolationService(
      stopCapture: () => calls.add('capture'),
      stopV2v: () => calls.add('v2v'),
      stopGuardian: () => calls.add('guardian'),
      stopServices: () => calls.add('wal-services'),
      quarantineLegacy: () => calls.add('quarantine'),
    );

    await service.stopForAccountTransition();

    expect(calls, ['capture', 'v2v', 'guardian', 'wal-services', 'quarantine']);
  });

  test('workspace proof is opaque and route states remain honest without receipts', () async {
    final prefs = SharedPreferencesUtil()..uid = 'uid-private-value';
    await prefs.saveString('aiConsentProfileBindingId', 'profile-private-value');
    await prefs.saveEllaProvisioningReceipt('uid-private-value', {
      'state': 'ready',
      'binding_state': 'active',
      'binding_revision': 8,
      'effective_policy_revision': 'policy-8',
      'runtime_provider': 'self_hosted_hermes',
      'runtime_status': 'ready',
    });
    await prefs.markEllaProvisioningVerified('uid-private-value', at: DateTime.utc(2026, 8, 2));

    final status = EllaWorkspaceStatus.current(
      preferences: prefs,
      uid: 'uid-private-value',
      email: 'new@example.test',
    );

    expect(status.workspaceVerified, isTrue);
    expect(status.workspaceFingerprint, matches(RegExp(r'^[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}$')));
    expect(status.workspaceFingerprint, isNot(contains('uid-private-value')));
    expect(status.chat, EllaRouteVerification.notVerified);
    expect(status.voice, EllaRouteVerification.notVerified);
    expect(status.whispers, EllaRouteVerification.notVerified);
    expect(status.quarantinedAudioCount, 0);
  });
}

Future<void> _grantAuthority(SharedPreferencesUtil prefs, String uid) async {
  prefs.acceptAiConsent(
    receiptId: 'aicr_$uid',
    uid: uid,
    profileBindingId: 'profile-$uid',
    serverDecidedAt: '2026-08-02T00:00:00Z',
  );
  prefs.markAiConsentServerVerified(
    uid: uid,
    receiptId: 'aicr_$uid',
    policyVersion: SharedPreferencesUtil.currentAiConsentContractVersion,
    processorSetHash: SharedPreferencesUtil.currentAiConsentProcessorSetHash,
    profileBindingId: 'profile-$uid',
    scopeVersion: SharedPreferencesUtil.currentAiConsentScopeVersion,
    scopeHash: SharedPreferencesUtil.currentAiConsentScopeHash,
  );
}

Memory _memory(String id, String uid) => Memory(
      id: id,
      uid: uid,
      content: 'Memory $id',
      category: MemoryCategory.manual,
      createdAt: DateTime.utc(2026, 8, 2),
      updatedAt: DateTime.utc(2026, 8, 2),
      visibility: MemoryVisibility.private,
    );

Person _person(String id) => Person(
      id: id,
      name: id,
      createdAt: DateTime.utc(2026, 8, 2),
      updatedAt: DateTime.utc(2026, 8, 2),
    );

extension on Memory {
  String toJsonString() => '{"id":"$id","uid":"$uid","content":"$content","category":"manual",'
      '"created_at":"${createdAt.toIso8601String()}","updated_at":"${updatedAt.toIso8601String()}",'
      '"visibility":"private"}';
}
