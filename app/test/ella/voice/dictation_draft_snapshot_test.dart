import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/ella_account_commit_barrier.dart';
import 'package:omi/ella/voice/dictation_draft_snapshot.dart';
import 'package:omi/services/wals/wal_owner_authority.dart';

class _Authority implements AccountCommitAuthority {
  @override
  String uid = 'account-a';
  bool current = true;
  @override
  bool isCurrent() => current;
  @override
  bool isExactCurrent() => current;
}

void main() {
  late _Authority authority;
  late EllaAccountCommitLease lease;
  setUp(() {
    authority = _Authority();
    lease = EllaAccountCommitBarrier.begin(authorityProvider: () => authority, onInvalidated: () {})!;
  });
  tearDown(() => lease.close());

  TextEditingValue? insert(TextEditingValue initial,
      {TextEditingValue? current, int epoch = 4, String words = 'hello'}) {
    return DictationDraftSnapshot(value: initial, lease: lease, epoch: 4).insert(
      transcript: words,
      currentValue: current ?? initial,
      currentLease: lease,
      currentEpoch: epoch,
    );
  }

  test('inserts at caret and preserves text before and after it', () {
    const value = TextEditingValue(text: 'before after', selection: TextSelection.collapsed(offset: 7));
    final result = insert(value)!;
    expect(result.text, 'before hello after');
    expect(result.selection, const TextSelection.collapsed(offset: 13));
  });
  test('selected text is preserved instead of replaced', () {
    const value = TextEditingValue(text: 'keep this', selection: TextSelection(baseOffset: 0, extentOffset: 4));
    expect(insert(value), isNull);
  });
  test('invalid selection appends and preserves existing draft', () {
    expect(insert(const TextEditingValue(text: 'draft'))!.text, 'draft hello');
  });
  test('empty dictation and active composing leave editor unchanged', () {
    const value = TextEditingValue(text: 'draft', selection: TextSelection.collapsed(offset: 5));
    expect(insert(value, words: '  '), isNull);
    expect(insert(value.copyWith(composing: const TextRange(start: 0, end: 3))), isNull);
  });
  test('any text selection or composing change rejects a late result', () {
    const value = TextEditingValue(text: 'draft', selection: TextSelection.collapsed(offset: 5));
    for (final changed in [
      value.copyWith(text: 'draft typed'),
      value.copyWith(selection: const TextSelection.collapsed(offset: 0)),
      value.copyWith(composing: const TextRange(start: 0, end: 2)),
    ]) {
      expect(insert(value, current: changed), isNull);
    }
    expect(insert(value, epoch: 5), isNull);
  });
  test('closed invalidated or replaced exact account lease rejects insertion', () {
    const value = TextEditingValue(text: 'draft');
    final snapshot = DictationDraftSnapshot(value: value, lease: lease, epoch: 4);
    final replacement = EllaAccountCommitBarrier.begin(authorityProvider: () => authority, onInvalidated: () {})!;
    expect(
        snapshot.insert(transcript: 'hello', currentValue: value, currentLease: replacement, currentEpoch: 4), isNull);
    replacement.close();
    authority.current = false;
    expect(insert(value), isNull);
    authority.current = true;
    EllaAccountCommitBarrier.quiesceForAccountTransition();
    expect(insert(value), isNull);
    lease.close();
    expect(insert(value), isNull);
  });
}
