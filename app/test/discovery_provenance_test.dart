import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/utils/discovery_provenance.dart';

void main() {
  test('same-pass advertised Friend row has count-only provenance', () {
    final l = DiscoveryProvenanceLedger(); final p = l.begin();
    l.knownInput(p, 0);
    l.candidate(p, 'private-id', 'scan_callback_advertised_name');
    l.admitted(p, 'private-id', 'scan_callback_advertised_name');
    l.publish(p, [(id: 'private-id', friend: true)]); l.finish(p);
    expect(l.snapshot['friend_row_scan_callback_advertised_name'], 1);
    expect(l.snapshot['known_id_input_count'], 0);
    expect(l.snapshot['phase'], 'completed');
    expect(jsonEncode(l.snapshot), isNot(contains('private-id')));
    expect(DiscoveryProvenanceLedger.safeSnapshot(l.snapshot), l.snapshot);
  });
  test('retained publication is separate from a fresh retrieval', () {
    final l = DiscoveryProvenanceLedger(); final p = l.begin();
    l.retainedPublication(p, ['private-id']);
    l.publish(p, [(id: 'private-id', friend: true)]);
    expect(l.snapshot['friend_row_retained'], 1);
    l.admitted(p, 'private-id', 'retrieved_known');
    l.publish(p, [(id: 'private-id', friend: true)]);
    expect(l.snapshot['friend_row_retained'], 0);
    expect(l.snapshot['friend_row_retrieved_known'], 1);
  });
  test('unknown row is not mislabeled fresh or retained', () {
    final l = DiscoveryProvenanceLedger(); final p = l.begin();
    l.publish(p, [(id: 'unobserved', friend: true)]);
    expect(l.snapshot['friend_row_unknown'], 1);
    expect(l.snapshot['friend_row_retained'], 0);
    expect(l.snapshot['known_id_input_count'], isNull);
  });
  test('retired in-flight token cannot populate a new pass', () {
    final l = DiscoveryProvenanceLedger(); final old = l.begin();
    l.admitted(old, 'old-owner-id', 'retrieved_known'); l.retire();
    final fresh = l.begin();
    l.candidate(old, 'old-owner-id', 'scan_callback_advertised_name');
    l.admitted(old, 'old-owner-id', 'scan_callback_advertised_name');
    l.retainedPublication(old, ['old-owner-id']);
    l.publish(old, [(id: 'old-owner-id', friend: true)]); l.finish(old);
    expect(l.snapshot['phase'], 'scanning');
    expect(l.snapshot['candidate_scan_callback_advertised_name'], 0);
    expect(l.snapshot['friend_row_scan_callback_advertised_name'], 0);
    expect(l.current, fresh);
  });
  test('retirement clears rows and known-input evidence', () {
    final l = DiscoveryProvenanceLedger(); final p = l.begin();
    l.knownInput(p, 1); l.admitted(p, 'private-id', 'retrieved_connected');
    l.publish(p, [(id: 'private-id', friend: true)]); l.retire();
    expect(l.snapshot['known_id_input_count'], isNull);
    expect(l.snapshot['friend_row_retrieved_connected'], 0);
    expect(l.snapshot['phase'], 'retired');
  });
  test('repeat callback count is not a native dedup decision', () {
    final l = DiscoveryProvenanceLedger(); final p = l.begin();
    l.candidate(p, 'same', 'scan_other'); l.candidate(p, 'same', 'scan_callback_cached_name');
    expect(l.snapshot['candidate_repeat_events'], 1);
    expect(l.snapshot['native_dedup_decision'], 'not_observed_by_this_ledger');
  });
  test('unknown raw source strings and extra fields never export', () {
    final l = DiscoveryProvenanceLedger(); final p = l.begin();
    l.candidate(p, 'id', 'untrusted-name-or-source');
    expect(jsonEncode(l.snapshot), isNot(contains('untrusted-name-or-source')));
    expect(l.snapshot['candidate_unknown'], 1);
    expect(DiscoveryProvenanceLedger.safeSnapshot({...l.snapshot,'private_id':'secret'}), isNull);
    expect(DiscoveryProvenanceLedger.safeSnapshot({...l.snapshot,'phase':'secret'}), isNull);
  });
  test('bounded overflow reports uncertainty without retaining unbounded keys', () {
    final l = DiscoveryProvenanceLedger(); final p = l.begin();
    for (var i = 0; i < 1100; i++) { l.admitted(p, 'id-$i', 'restored'); }
    l.publish(p, [(id: 'id-1099', friend: true)]);
    expect(l.snapshot['correlation_overflow'], greaterThan(0));
    expect(l.snapshot['friend_row_unknown'], 1);
    expect(jsonEncode(l.snapshot), isNot(contains('id-1099')));
  });
  test('all real source labels preserve their advertised-vs-cache distinction', () {
    final l = DiscoveryProvenanceLedger();
    expect(l.candidateSource(source:'scan',advertisedName:true,cachedName:true), 'scan_callback_advertised_name');
    expect(l.candidateSource(source:'scan',advertisedName:false,cachedName:true), 'scan_callback_cached_name');
    expect(l.candidateSource(source:'retrievedConnected',advertisedName:false,cachedName:true), 'retrieved_connected');
    expect(l.candidateSource(source:'retrievedKnown',advertisedName:false,cachedName:true), 'retrieved_known');
    expect(l.candidateSource(source:'restored',advertisedName:false,cachedName:true), 'restored');
  });
  test('navigation cancel retains only completed counts for the exact owner', () {
    final l=DiscoveryProvenanceLedger();
    final p=l.begin(owner:(uid:'fictional-a',generation:1));
    l.admitted(p,'id','retrieved_known');l.publish(p,[(id:'id',friend:true)]);l.finish(p);l.cancel();
    expect(l.snapshotFor((uid:'fictional-a',generation:1))['friend_row_retrieved_known'],1);
    expect(l.snapshotFor((uid:'fictional-a',generation:2))['friend_row_retrieved_known'],0);
    expect(l.snapshotFor((uid:'fictional-b',generation:1))['friend_row_retrieved_known'],0);
    l.retire();expect(l.snapshot['friend_row_retrieved_known'],0);
  });

}
