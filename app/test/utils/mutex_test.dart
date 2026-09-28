// ellaaicare/ella-ai#1280 RUN-010 / #1287: DeviceService.disconnectDevice() runs on the
// app-termination path with a ~5s OS watchdog budget. A plain Mutex.acquire() would wait
// indefinitely for a stuck holder; Mutex.tryAcquire(timeout) bounds that wait instead, and
// must never leave the lock in a corrupted state when it gives up.
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/utils/mutex.dart';

void main() {
  group('Mutex.tryAcquire', () {
    test('acquires immediately when free', () async {
      final mutex = Mutex();
      final acquired = await mutex.tryAcquire(const Duration(seconds: 2));
      expect(acquired, isTrue);
      mutex.release();
    });

    test('times out well under the requested budget when already held, without corrupting the lock', () async {
      final mutex = Mutex();
      await mutex.acquire(); // held for the whole test until released below

      final stopwatch = Stopwatch()..start();
      final acquired = await mutex.tryAcquire(const Duration(milliseconds: 200));
      stopwatch.stop();

      expect(acquired, isFalse);
      expect(stopwatch.elapsed, lessThan(const Duration(milliseconds: 900)));

      // The timed-out attempt must not have touched the lock: the original
      // holder can still release it, and a fresh acquire then succeeds.
      mutex.release();
      final reacquired = await mutex.tryAcquire(const Duration(seconds: 1));
      expect(reacquired, isTrue);
      mutex.release();
    });

    test('acquires as soon as the holder releases, before the timeout elapses', () async {
      final mutex = Mutex();
      await mutex.acquire();

      final acquireFuture = mutex.tryAcquire(const Duration(seconds: 5));
      await Future<void>.delayed(const Duration(milliseconds: 50));
      mutex.release();

      final acquired = await acquireFuture;
      expect(acquired, isTrue);
      mutex.release();
    });
  });
}
