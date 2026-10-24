import 'dart:async';
import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/ella/services/summary_read_retry.dart';

void main() {
  test('GET waves use10/30/120/300 seconds and capped300 from completion', () {
    fakeAsync((a) {
      var count = 0;
      final r = SummaryReadRetry(
        current: () => true,
        pending: () => true,
        externallyBusy: () => false,
        refresh: () async {
          count++;
        },
        now: a.getClock(DateTime.utc(2026)).now,
      );
      r.reconsider();
      for (final seconds in [10, 30, 120, 300, 300]) {
        a.elapse(Duration(seconds: seconds - 1));
        a.flushMicrotasks();
        final before = count;
        a.elapse(const Duration(seconds: 1));
        a.flushMicrotasks();
        expect(count, before + 1);
      }
      expect(count, 5);
      r.dispose();
    });
  });
  test('repeated foreground bind does not postpone an existing deadline', () {
    fakeAsync((a) {
      var count = 0;
      final r = SummaryReadRetry(
        current: () => true,
        pending: () => true,
        externallyBusy: () => false,
        refresh: () async {
          count++;
        },
        now: a.getClock(DateTime.utc(2026)).now,
      );
      r.reconsider();
      a.elapse(const Duration(seconds: 5));
      r.reconsider();
      a.elapse(const Duration(seconds: 4));
      r.setActive(true);
      a.elapse(const Duration(seconds: 1));
      a.flushMicrotasks();
      expect(count, 1);
      r.dispose();
    });
  });
  test('pause cancels timer and foreground return rearms remaining original deadline', () {
    fakeAsync((a) {
      var count = 0;
      final r = SummaryReadRetry(
        current: () => true,
        pending: () => true,
        externallyBusy: () => false,
        refresh: () async {
          count++;
        },
        now: a.getClock(DateTime.utc(2026)).now,
      );
      r.reconsider();
      a.elapse(const Duration(seconds: 5));
      r.setActive(false);
      a.elapse(const Duration(seconds: 20));
      expect(count, 0);
      r.setActive(true);
      a.elapse(Duration.zero);
      a.flushMicrotasks();
      expect(count, 1);
      r.dispose();
    });
  });
  test('existing provider read and held scheduler read cannot overlap', () {
    fakeAsync((a) {
      var count = 0;
      var busy = true;
      final held = Completer<void>();
      final r = SummaryReadRetry(
        current: () => true,
        pending: () => true,
        externallyBusy: () => busy,
        refresh: () {
          count++;
          return held.future;
        },
        now: a.getClock(DateTime.utc(2026)).now,
      );
      r.reconsider();
      a.elapse(const Duration(seconds: 12));
      expect(count, 0);
      busy = false;
      a.elapse(const Duration(seconds: 1));
      a.flushMicrotasks();
      expect(count, 1);
      r.reconsider();
      a.elapse(const Duration(hours: 1));
      expect(count, 1);
      held.complete();
      a.flushMicrotasks();
      r.dispose();
    });
  });
  test('retired generation completion cannot rearm into replacement account', () {
    fakeAsync((a) {
      var count = 0;
      var current = true;
      final held = Completer<void>();
      final r = SummaryReadRetry(
        current: () => current,
        pending: () => true,
        externallyBusy: () => false,
        refresh: () {
          count++;
          return held.future;
        },
        now: a.getClock(DateTime.utc(2026)).now,
      );
      r.reconsider();
      a.elapse(const Duration(seconds: 10));
      expect(count, 1);
      current = false;
      r.cancel();
      held.complete();
      a.flushMicrotasks();
      a.elapse(const Duration(hours: 1));
      expect(count, 1);
      current = true;
      r.reconsider();
      a.elapse(const Duration(seconds: 10));
      a.flushMicrotasks();
      expect(count, 2);
      r.dispose();
    });
  });
  test('confirmed enrichment stops reads until a new pending fact', () {
    fakeAsync((a) {
      var count = 0;
      var pending = true;
      final r = SummaryReadRetry(
        current: () => true,
        pending: () => pending,
        externallyBusy: () => false,
        refresh: () async {
          count++;
          pending = false;
        },
        now: a.getClock(DateTime.utc(2026)).now,
      );
      r.reconsider();
      a.elapse(const Duration(seconds: 10));
      a.flushMicrotasks();
      a.elapse(const Duration(hours: 1));
      expect(count, 1);
      pending = true;
      r.reconsider();
      a.elapse(const Duration(seconds: 10));
      a.flushMicrotasks();
      expect(count, 2);
      r.dispose();
    });
  });
  test('read failure retries without changing data or widening authority', () {
    fakeAsync((a) {
      var count = 0;
      const retained = 'useful generic summary';
      final r = SummaryReadRetry(
        current: () => true,
        pending: () => true,
        externallyBusy: () => false,
        refresh: () async {
          count++;
          throw StateError('synthetic transport error');
        },
        now: a.getClock(DateTime.utc(2026)).now,
      );
      r.reconsider();
      a.elapse(const Duration(seconds: 10));
      a.flushMicrotasks();
      expect(count, 1);
      a.elapse(const Duration(seconds: 30));
      a.flushMicrotasks();
      expect(count, 2);
      expect(retained, 'useful generic summary');
      r.dispose();
    });
  });
  test('dispose with held read prevents all later scheduling', () {
    fakeAsync((a) {
      var count = 0;
      final held = Completer<void>();
      final r = SummaryReadRetry(
        current: () => true,
        pending: () => true,
        externallyBusy: () => false,
        refresh: () {
          count++;
          return held.future;
        },
        now: a.getClock(DateTime.utc(2026)).now,
      );
      r.reconsider();
      a.elapse(const Duration(seconds: 10));
      expect(count, 1);
      r.dispose();
      held.complete();
      a.flushMicrotasks();
      a.elapse(const Duration(days: 1));
      expect(count, 1);
    });
  });
}
