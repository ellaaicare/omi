import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:omi/backend/http/http_pool_manager.dart' as main_pool;
import 'package:omi/upstream_capture/backend/http/http_pool_manager.dart'
    as upstream_pool;

typedef _Send = Future<http.Response> Function(String method);

class _Harness {
  _Harness(this.send, this.dispose);
  final _Send send;
  final void Function() dispose;
}

_Harness _main(http.Client client) {
  final pool = main_pool.HttpPoolManager.forTesting(client);
  return _Harness(
    (method) => pool.send(
      () => http.Request(method, Uri.parse('https://pool-test.invalid/item')),
      timeout: const Duration(seconds: 2),
      retries: 0,
    ),
    pool.dispose,
  );
}

_Harness _upstream(http.Client client) {
  final pool = upstream_pool.HttpPoolManager.forTesting(client);
  return _Harness(
    (method) => pool.send(
      () => http.Request(method, Uri.parse('https://pool-test.invalid/item')),
      timeout: const Duration(seconds: 2),
      retries: 0,
    ),
    pool.dispose,
  );
}

// Keep expected request errors handled inside their error zone. An ignored
// cleanup Future's error reaches onError instead of escaping the test runner.
Future<List<Object>> _unhandledDuring(Future<void> Function() body) async {
  final done = Completer<void>();
  final errors = <Object>[];
  runZonedGuarded(() async {
    try {
      await body();
      await Future<void>.delayed(Duration.zero);
      await Future<void>.delayed(Duration.zero);
      done.complete();
    } catch (error, stack) {
      done.completeError(error, stack);
    }
  }, (error, stack) {
    errors.add(error);
  });
  await done.future.timeout(const Duration(seconds: 5));
  return errors;
}

void main() {
  final factories = <String, _Harness Function(http.Client)>{
    'main': _main,
    'upstream': _upstream,
  };
  for (final entry in factories.entries) {
    group(entry.key, () {
      test('caller receives timeout without a secondary unhandled error', () async {
        var sends = 0;
        final pool = entry.value(MockClient((request) async {
          sends++;
          throw TimeoutException('fake transport timeout');
        }));
        addTearDown(pool.dispose);

        final errors = await _unhandledDuring(() async {
          await expectLater(pool.send('GET'), throwsA(isA<TimeoutException>()));
        });

        expect(sends, 1);
        expect(errors, isEmpty);
      });

      test('simultaneous GET callers share one successful send', () async {
        var sends = 0;
        final started = Completer<void>();
        final response = Completer<http.Response>();
        final pool = entry.value(MockClient((request) {
          sends++;
          started.complete();
          return response.future;
        }));
        addTearDown(pool.dispose);

        final first = pool.send('GET');
        final second = pool.send('GET');
        await started.future;
        expect(sends, 1);
        response.complete(http.Response('shared', 200));
        final values = await Future.wait([first, second])
            .timeout(const Duration(seconds: 5));
        expect(values.map((value) => value.body), ['shared', 'shared']);
        expect(sends, 1);
      });

      test('failed shared GET is removed and a later GET succeeds', () async {
        var sends = 0;
        final errors = await _unhandledDuring(() async {
          // Error futures and the client that exposes them must belong to
          // this guarded error zone; Future errors cannot cross error zones.
          final started = Completer<void>();
          final firstResponse = Completer<http.Response>();
          final pool = entry.value(MockClient((request) {
            sends++;
            if (sends == 1) {
              started.complete();
              return firstResponse.future;
            }
            return Future.value(http.Response('recovered', 200));
          }));
          addTearDown(pool.dispose);
          final first = pool.send('GET');
          final second = pool.send('GET');
          final firstCheck = expectLater(first, throwsA(isA<TimeoutException>()));
          final secondCheck = expectLater(second, throwsA(isA<TimeoutException>()));
          await started.future;
          expect(sends, 1);
          firstResponse.completeError(TimeoutException('fake transport timeout'));
          await Future.wait([firstCheck, secondCheck])
              .timeout(const Duration(seconds: 5));
          final recovered = await pool.send('GET')
              .timeout(const Duration(seconds: 5));
          expect(recovered.body, 'recovered');
        });
        expect(errors, isEmpty);
        expect(sends, 2);
      });

      test('POST requests remain independent', () async {
        var sends = 0;
        final pool = entry.value(MockClient((request) async {
          sends++;
          return http.Response('ok', 200);
        }));
        addTearDown(pool.dispose);

        await Future.wait([pool.send('POST'), pool.send('POST')]);
        expect(sends, 2);
      });
    });
  }
}
