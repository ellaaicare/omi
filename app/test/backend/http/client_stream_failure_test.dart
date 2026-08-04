import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:omi/backend/http/api/messages.dart';
import 'package:omi/backend/http/client_api_failure.dart';
import 'package:omi/backend/http/http_pool_manager.dart';
import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/utils/platform/platform_manager.dart';

class _StreamingClient extends http.BaseClient {
  _StreamingClient(this.handler);

  final Future<http.StreamedResponse> Function(http.BaseRequest request) handler;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) => handler(request);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  Env.init();
  PlatformManager.initializeForTesting();

  test('Hermes SSE failure is typed before it can become assistant content', () {
    expect(
      () => parseMessageChunk('data: Error: hermes_runtime_required', 'message-a'),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.workspaceRequired),
      ),
    );
  });

  test('unknown JSON SSE error is sanitized instead of becoming assistant content', () {
    expect(
      () => parseMessageChunk('data: {"error":"provider included private diagnostic text"}', 'message-a'),
      throwsA(
        isA<ClientApiFailure>()
            .having((failure) => failure.kind, 'kind', ClientApiFailureKind.unavailable)
            .having((failure) => failure.backendCode, 'raw backend code', isNull),
      ),
    );
  });

  test('streaming non-200 surfaces update-required instead of ending empty', () async {
    HttpPoolManager.instance.replaceClientForTesting(
      _StreamingClient(
        (_) async => http.StreamedResponse(
          Stream.value(utf8.encode('{"detail":"upgrade_required"}')),
          426,
        ),
      ),
    );

    await expectLater(
      makeStreamingApiCall(url: 'https://stream.test/chat').toList(),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.updateRequired),
      ),
    );
  });

  test('multipart voice stream surfaces backend failure instead of ending empty', () async {
    final file = File('${Directory.systemTemp.path}/typed-voice-stream.wav')..writeAsBytesSync([1, 2, 3]);
    addTearDown(() async {
      if (await file.exists()) await file.delete();
    });
    HttpPoolManager.instance.replaceClientForTesting(
      _StreamingClient(
        (_) async => http.StreamedResponse(
          Stream.value(utf8.encode('{"detail":"provider_unavailable"}')),
          503,
        ),
      ),
    );

    await expectLater(
      makeMultipartStreamingApiCall(url: 'https://stream.test/voice', files: [file]).toList(),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.unavailable),
      ),
    );
  });

  test('stream transport exception is typed instead of swallowed', () async {
    HttpPoolManager.instance.replaceClientForTesting(
      _StreamingClient((_) => throw const SocketException('offline')),
    );

    await expectLater(
      makeStreamingApiCall(url: 'https://stream.test/chat').toList(),
      throwsA(
        isA<ClientApiFailure>().having((failure) => failure.kind, 'kind', ClientApiFailureKind.unavailable),
      ),
    );
  });
}
