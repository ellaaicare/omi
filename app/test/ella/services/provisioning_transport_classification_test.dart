import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/http_pool_manager.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/ella/services/ella_provisioning_service.dart';
import 'package:omi/env/env.dart';
import 'package:omi/providers/ella_provisioning_provider.dart';

class _SyntheticEnv implements EnvFields {
  @override
  String get apiBaseUrl => 'https://synthetic.invalid/';
  @override
  dynamic noSuchMethod(Invocation invocation) => null;
}

class _Call {
  _Call(this.url, this.method, this.body, this.timeout, this.retries, this.diagnostic);
  final String url, method, body;
  final Duration? timeout;
  final int? retries;
  final void Function(HttpTransportDiagnostic)? diagnostic;
  void fail(HttpTransportFailure failure) => diagnostic
      ?.call(HttpTransportDiagnostic(stage: HttpTransportStage.authHeaders, failure: failure, elapsedMilliseconds: 1));
}

class _Request {
  _Request(this.effect);
  final Future<http.Response?> Function(_Call) effect;
  final calls = <_Call>[];
  Future<http.Response?> call(
      {required String url,
      required Map<String, String> headers,
      required String body,
      required String method,
      Duration? timeout,
      int? retries,
      void Function(HttpTransportDiagnostic)? onTransportDiagnostic}) {
    final c = _Call(url, method, body, timeout, retries, onTransportDiagnostic);
    calls.add(c);
    return effect(c);
  }
}

class _Poll implements EllaProvisioningPollHandle {
  _Poll(this.callback);
  final void Function() callback;
  bool cancelled = false;
  @override
  void cancel() => cancelled = true;
}

Map<String, dynamic> _ready() => {
      'state': 'ready',
      'stage': 'ready',
      'retryable': false,
      'binding_state': 'active',
      'binding_revision': 1,
      'effective_policy_revision': 'synthetic-model:synthetic-voice',
      'account_ready': true,
      'enrichment_ready': false,
      'background_processing_paused': true,
      'enrichment_error_code': 'account_enrichment_worker_unavailable',
    };
EllaProvisioningRequestContext _context() =>
    EllaProvisioningRequestContext(appVersion: 'synthetic-build', locale: 'en-US', timezone: 'UTC');

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() => Env.init(_SyntheticEnv()));
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    EllaProvisioningAuthorityCoordinator.resetForTesting();
  });
  for (final pair in [
    (HttpTransportFailure.authentication, EllaProvisioningClientFailure.authenticationRefresh),
    (HttpTransportFailure.timeout, EllaProvisioningClientFailure.timeout),
    (HttpTransportFailure.network, EllaProvisioningClientFailure.network),
    (HttpTransportFailure.internal, EllaProvisioningClientFailure.unavailable),
  ]) {
    test('null HTTP retains invocation-local ${pair.$1.name} classification', () async {
      final request = _Request((c) async {
        c.fail(pair.$1);
        return null;
      });
      final response = await EllaProvisioningHttpTransport(request: request.call).ensure(_context());
      expect(response.clientFailure, pair.$2);
      expect(response.isAccepted, isFalse);
      expect(response.receipt, isNull);
      expect(request.calls.single.method, 'POST');
      expect(request.calls.single.timeout, const Duration(seconds: 20));
      expect(request.calls.single.retries, 0);
    });
  }
  test('no diagnostic null does not borrow another request global auth error', () async {
    ApiTransportDiagnostics.lastError = 'authentication';
    ApiTransportDiagnostics.lastStatusCode = 401;
    final request = _Request((_) async => null);
    final response = await EllaProvisioningHttpTransport(request: request.call).status();
    expect(response.clientFailure, EllaProvisioningClientFailure.unavailable);
    expect(request.calls.single.method, 'GET');
    expect(request.calls.single.timeout, const Duration(seconds: 10));
    expect(request.calls.single.url, contains('target_schema_version=hermes-user-v1'));
    expect(request.calls.single.retries, 0);
  });
  for (final body in ['', '<html>upstream error</html>', '[]', 'null']) {
    test('unusable success body is response failure not connection: $body', () async {
      final request = _Request((_) async => http.Response(body, 200));
      final response = await EllaProvisioningHttpTransport(request: request.call).status();
      expect(response.clientFailure, EllaProvisioningClientFailure.invalidResponse);
      expect(response.isAccepted, isFalse);
      expect(response.receipt, isNull);
    });
  }
  for (final pair in [
    (
      const ClientApiFailure(ClientApiFailureKind.authenticationRequired, retryable: true),
      EllaProvisioningClientFailure.authenticationRefresh
    ),
    (TimeoutException('synthetic'), EllaProvisioningClientFailure.timeout),
    (const SocketException('synthetic'), EllaProvisioningClientFailure.network),
    (StateError('synthetic'), EllaProvisioningClientFailure.unavailable),
  ]) {
    test('thrown request ${pair.$1.runtimeType} is classified without raw exception retention', () async {
      final request = _Request((_) async => throw pair.$1);
      final response = await EllaProvisioningHttpTransport(request: request.call).status();
      expect(response.clientFailure, pair.$2);
      expect(response.receipt, isNull);
    });
  }
  test('flat deployed ready with paused enrichment still satisfies unchanged binding gates', () async {
    final request = _Request((_) async => http.Response(jsonEncode(_ready()), 200));
    final response = await EllaProvisioningHttpTransport(request: request.call).ensure(_context());
    expect(response.isAccepted, isTrue);
    expect(response.clientFailure, isNull);
    expect(response.receipt!.isOperational, isTrue);
    expect(response.receipt!.errorCode, isEmpty);
  });
  test('missing policy and credentials in ready contract cannot admit', () async {
    for (final body in [
      _ready()..remove('effective_policy_revision'),
      _ready()..['access_token'] = 'synthetic-forbidden'
    ]) {
      final request = _Request((_) async => http.Response(jsonEncode(body), 200));
      final response = await EllaProvisioningHttpTransport(request: request.call).status();
      expect(response.receipt!.isOperational, isFalse);
    }
  });
  for (final pair in [(429, 'quota_exceeded'), (503, 'provider_unavailable'), (401, 'auth_required')]) {
    test('HTTP${pair.$1} keeps HTTP authority and exact error category', () async {
      final polls = <_Poll>[];
      final request = _Request((c) async {
        c.fail(HttpTransportFailure.network);
        return http.Response('', pair.$1);
      });
      final provider = EllaProvisioningProvider(
          transport: EllaProvisioningHttpTransport(request: request.call),
          scheduler: (delay, callback) {
            final p = _Poll(callback);
            polls.add(p);
            return p;
          });
      try {
        await provider.start(uid: 'synthetic-a', requestContext: _context());
        expect(provider.errorCode, pair.$2);
        expect(provider.isOperational, isFalse);
        expect(provider.state, pair.$1 == 401 ? EllaProvisioningState.blocked : EllaProvisioningState.degraded);
        expect(polls.length, pair.$1 == 401 ? 0 : 1);
      } finally {
        provider.dispose();
      }
    });
  }
  test('reset retires held auth-refresh response', () async {
    final held = Completer<http.Response?>();
    final request = _Request((c) async {
      c.fail(HttpTransportFailure.authentication);
      return held.future;
    });
    final provider = EllaProvisioningProvider(transport: EllaProvisioningHttpTransport(request: request.call));
    try {
      final future = provider.start(uid: 'synthetic-a', requestContext: _context());
      await Future<void>.delayed(Duration.zero);
      provider.reset();
      held.complete(null);
      await future;
      expect(provider.state, EllaProvisioningState.idle);
      expect(provider.errorCode, isEmpty);
      expect(provider.isOperational, isFalse);
    } finally {
      provider.dispose();
    }
  });
  test('concurrent invocation diagnostics cannot cross requests', () async {
    final first = Completer<http.Response?>();
    final request = _Request((c) async {
      if (c.method == 'POST') {
        c.fail(HttpTransportFailure.authentication);
        return first.future;
      }
      return null;
    });
    final transport = EllaProvisioningHttpTransport(request: request.call);
    final ensure = transport.ensure(_context());
    final status = await transport.status();
    first.complete(null);
    expect(status.clientFailure, EllaProvisioningClientFailure.unavailable);
    expect((await ensure).clientFailure, EllaProvisioningClientFailure.authenticationRefresh);
  });
  test('typed auth-refresh retries stay bounded then explicit retry can recover ready', () async {
    final polls = <_Poll>[];
    var ready = false;
    final request = _Request((c) async {
      if (ready) return http.Response(jsonEncode(_ready()), 200);
      c.fail(HttpTransportFailure.authentication);
      return null;
    });
    final provider = EllaProvisioningProvider(
        transport: EllaProvisioningHttpTransport(request: request.call),
        maxPollAttempts: 1,
        scheduler: (d, cb) {
          final p = _Poll(cb);
          polls.add(p);
          return p;
        });
    try {
      await provider.start(uid: 'synthetic-a', requestContext: _context());
      expect(provider.errorCode, 'authentication_refresh_unavailable');
      expect(provider.isOperational, isFalse);
      expect(polls.length, 1);
      polls.single.callback();
      await Future<void>.delayed(Duration.zero);
      await Future<void>.delayed(Duration.zero);
      expect(provider.automaticPollingExhausted, isTrue);
      expect(polls.length, 1);
      expect(request.calls.map((c) => c.method), ['POST', 'GET']);
      ready = true;
      await provider.retry();
      expect(provider.isOperational, isTrue);
      expect(provider.errorCode, isEmpty);
      expect(provider.automaticPollingExhausted, isFalse);
      expect(request.calls.map((c) => c.method), ['POST', 'GET', 'POST']);
      expect(polls.length, 1);
    } finally {
      provider.dispose();
    }
  });
  test('client failure annotation cannot make a ready-shaped response accepted', () {
    final response = EllaProvisioningResponse(
        statusCode: 200,
        receipt: EllaProvisioningReceipt.fromJson(_ready()),
        clientFailure: EllaProvisioningClientFailure.authenticationRefresh);
    expect(response.isAccepted, isFalse);
  });
  for (final pair in [
    (429, 'synthetic_rate_limited', 'quota_exceeded'),
    (503, 'synthetic_service_fault', 'provider_unavailable')
  ]) {
    test('HTTP${pair.$1} structured error retains domain code but presents correct category', () async {
      final request = _Request((c) async => http.Response(
          jsonEncode({
            'detail': {'code': pair.$2}
          }),
          pair.$1));
      final polls = <_Poll>[];
      final provider = EllaProvisioningProvider(
          transport: EllaProvisioningHttpTransport(request: request.call),
          scheduler: (d, cb) {
            final p = _Poll(cb);
            polls.add(p);
            return p;
          });
      try {
        await provider.start(uid: 'synthetic-a', requestContext: _context());
        expect(provider.errorCode, pair.$2);
        expect(provider.failurePresentationCode, pair.$3);
        expect(provider.state, EllaProvisioningState.blocked);
        expect(provider.isOperational, isFalse);
        expect(polls, isEmpty, reason: 'Presentation cannot broaden a blocked receipt recovery policy');
        provider.reset();
        expect(provider.failurePresentationCode, isEmpty);
      } finally {
        provider.dispose();
      }
    });
  }
}
